"""Deterministic ownership events and a separate, opt-in delivery outbox."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from hashlib import sha256
from math import isfinite
from time import perf_counter
from typing import Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from carmind.maintenance import ReminderPolicy, add_months, evaluate_maintenance
from carmind.ownership import SERVICE_TYPES, utc
from carmind.storage import encode


class EventType(str, Enum):
    ODOMETER_UPDATE_REQUESTED = "ODOMETER_UPDATE_REQUESTED"
    MAINTENANCE_DUE_SOON = "MAINTENANCE_DUE_SOON"
    MAINTENANCE_DUE = "MAINTENANCE_DUE"
    MAINTENANCE_OVERDUE = "MAINTENANCE_OVERDUE"
    OWNER_REMINDER_DUE_SOON = "OWNER_REMINDER_DUE_SOON"
    OWNER_REMINDER_DUE = "OWNER_REMINDER_DUE"
    OWNER_REMINDER_OVERDUE = "OWNER_REMINDER_OVERDUE"


class EventStatus(str, Enum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"


class NotificationStatus(str, Enum):
    PENDING = "PENDING"
    DEFERRED = "DEFERRED"
    SENT = "SENT"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class ProactivePolicy:
    due_soon_km: float = 1000
    due_soon_days: int = 30
    odometer_stale_days: int = 30
    followup_days: int = 30

    def __post_init__(self):
        if (type(self.due_soon_km) not in (int, float) or not isfinite(self.due_soon_km)
                or self.due_soon_km < 0 or any(type(value) is not int or value < 1 for value in
                    (self.due_soon_days, self.odometer_stale_days, self.followup_days))):
            raise ValueError("Invalid proactive product policy")


@dataclass(frozen=True)
class NotificationPreferences:
    enabled: bool = False
    preferred_channel: str = "web"
    timezone: str = "UTC"
    language: str = "en"
    quiet_enabled: bool = True
    quiet_start: str = "22:00"
    quiet_end: str = "08:00"
    odometer_followups_enabled: bool = False

    def __post_init__(self):
        if any(type(value) is not bool for value in
               (self.enabled, self.quiet_enabled, self.odometer_followups_enabled)):
            raise ValueError("Notification switches must be explicit booleans")
        if self.preferred_channel not in {"web", "whatsapp", "console"} or self.language not in {"en", "ar"}:
            raise ValueError("Unsupported notification channel or language")
        try:
            ZoneInfo(self.timezone)
            start, end = time.fromisoformat(self.quiet_start), time.fromisoformat(self.quiet_end)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            raise ValueError("Invalid notification timezone or quiet hours") from None
        if (start.second or end.second or start.microsecond or end.microsecond
                or len(self.quiet_start) != 5 or len(self.quiet_end) != 5 or start == end):
            raise ValueError("Quiet hours need distinct HH:MM boundaries")


@dataclass(frozen=True)
class ProactiveEvent:
    event_id: str
    owner_id: str
    vehicle_id: str
    event_type: EventType
    status: EventStatus
    source_type: str
    priority: str
    maintenance_item: str | None
    source_id: str | None
    rule_id: str | None
    source_page: str | None
    due_km: float | None
    due_date: date | None
    current_mileage: float | None
    reason: str
    evidence: dict
    dedupe_key: str
    created_at: datetime
    effective_at: datetime


@dataclass(frozen=True)
class NotificationIntent:
    notification_id: str
    event_id: str
    owner_id: str
    vehicle_id: str
    channel: str
    event_type: EventType
    scheduled_for: datetime
    status: NotificationStatus
    attempt_count: int


@dataclass(frozen=True)
class EvaluationReport:
    vehicles_evaluated: int
    maintenance_rules_evaluated: int
    events_created: int
    events_resolved: int
    duplicate_events_prevented: int
    pending_notifications: int
    elapsed_ms: float


@dataclass(frozen=True)
class DeliveryReport:
    sent: int
    failed: int
    skipped: int


class NotificationSender(Protocol):
    def send(self, notification: NotificationIntent, text: str) -> None: ...


class WebNotificationSink:
    """Compatibility no-op; Web reads events and the runner does not send them."""

    def send(self, notification: NotificationIntent, text: str) -> None:
        return None


class ConsoleNotificationSender:
    def __init__(self, output=print):
        self.output = output

    def send(self, notification: NotificationIntent, text: str) -> None:
        self.output(text)


def _identity(parts):
    return sha256(encode(parts).encode()).hexdigest()[:32]


def _event_from_row(row):
    return ProactiveEvent(row["id"], row["owner_id"], row["vehicle_id"], EventType(row["event_type"]),
                          EventStatus(row["status"]), row["source_type"], row["priority"],
                          row["maintenance_item"], row["source_id"], row["rule_id"], row["source_page"],
                          row["due_km"], date.fromisoformat(row["due_date"]) if row["due_date"] else None,
                          row["current_mileage"], row["reason"], row["evidence"], row["dedupe_key"],
                          datetime.fromisoformat(row["created_at"]), datetime.fromisoformat(row["effective_at"]))


def _intent_from_row(row):
    return NotificationIntent(row["id"], row["event_id"], row["owner_id"], row["vehicle_id"],
                              row["channel"], EventType(row["event_type"]),
                              datetime.fromisoformat(row["scheduled_for"]),
                              NotificationStatus(row["status"]), row["attempt_count"])


def _delivery_time(now, preferences):
    if not preferences.quiet_enabled:
        return now
    local = now.astimezone(ZoneInfo(preferences.timezone))
    start, end = time.fromisoformat(preferences.quiet_start), time.fromisoformat(preferences.quiet_end)
    clock = local.time().replace(tzinfo=None)
    quiet = (start <= clock < end) if start < end else (clock >= start or clock < end)
    if not quiet:
        return now
    next_day = local.date() + timedelta(days=1) if (start > end and clock >= start) else local.date()
    return datetime.combine(next_day, end, ZoneInfo(preferences.timezone)).astimezone(timezone.utc)


def _display_target(event):
    date_first = event.due_date is not None and (event.due_km is None or
        event.evidence.get("attention_basis") == "date")
    if date_first:
        return event.due_date.isoformat(), "date"
    if event.due_km is not None:
        return f"{event.due_km:g}", "km"
    return "unknown", "unknown"


def notification_text(event: ProactiveEvent, language="en"):
    item = (event.maintenance_item or "maintenance").replace("_", " ")
    if event.event_type == EventType.ODOMETER_UPDATE_REQUESTED:
        return ("متى آخر قراءة لعداد سيارتك؟ حدّث الممشى عشان تكون تذكيرات الصيانة أدق."
                if language == "ar" else "Could you update your car's odometer? Your mileage information is getting old.")
    owner = event.source_type == "OWNER"
    target_value, target_unit = _display_target(event)
    if language == "ar":
        prefix = "تذكيرك" if owner else "حسب جدول الصيانة الموثق"
        state = ("قربت" if "SOON" in event.event_type else
                 "تأخرت" if "OVERDUE" in event.event_type else "حان وقتها")
        target = f" بتاريخ {target_value}" if target_unit == "date" else f" عند {target_value} كم" if target_unit == "km" else ""
        return f"{prefix}: صيانة {item} {state}{target}."
    prefix = "Your reminder" if owner else "Based on the verified maintenance schedule"
    state = "is approaching" if "SOON" in event.event_type else "is overdue" if "OVERDUE" in event.event_type else "is due"
    target = f" on {target_value}" if target_unit == "date" else f" at {target_value} km" if target_unit == "km" else ""
    return f"{prefix}: {item} service {state}{target}."


def explain_event(event: ProactiveEvent, language="en"):
    if event.event_type == EventType.ODOMETER_UPDATE_REQUESTED:
        return ("آخر قراءة محفوظة قديمة أو غير موجودة؛ ما خمّنت الممشى الحالي."
                if language == "ar" else "The last saved odometer reading is old or missing. I have not estimated your current mileage.")
    source = ("تذكير أنت طلبته" if event.source_type == "OWNER" else "جدول صيانة موثق"
              ) if language == "ar" else ("your own reminder" if event.source_type == "OWNER" else "a verified maintenance schedule")
    target_value, target_unit = _display_target(event)
    target = f"{target_value} km" if target_unit == "km" else target_value
    page = f" (page {event.source_page})" if event.source_page else ""
    if language == "ar":
        return f"هذا التنبيه من {source}. الحد: {target}. المصدر: {event.source_id or 'طلبك'}{page}."
    return f"This comes from {source}. Due point: {target}. Source: {event.source_id or 'your request'}{page}."


class ProactiveService:
    def __init__(self, app, policy=ProactivePolicy()):
        self.app, self.store, self.policy = app, app.store, policy

    def preferences(self, owner_id):
        row = self.store.notification_preferences(owner_id)
        return NotificationPreferences(**{key: bool(row[key]) if key in
            {"enabled", "quiet_enabled", "odometer_followups_enabled"} else row[key]
            for key in NotificationPreferences.__dataclass_fields__}) if row else NotificationPreferences()

    def set_preferences(self, owner_id, preferences, *, now):
        if type(preferences) is not NotificationPreferences:
            raise ValueError("Invalid notification preferences")
        with self.store.transaction():
            if not self.store.db.execute("SELECT 1 FROM owners WHERE id=?", (owner_id,)).fetchone():
                raise ValueError("Unknown owner")
            self.store.save_notification_preferences(owner_id, preferences.__dict__, now)
        return preferences

    def add_owner_reminder(self, owner_id, vehicle_id, maintenance_item, *, now,
                           after_km=None, after_months=None, within_transaction=False):
        now = utc(now)
        vehicle = self.store.vehicle(owner_id, vehicle_id, now)
        if maintenance_item not in SERVICE_TYPES or (after_km is None) == (after_months is None):
            raise ValueError("Choose one supported maintenance item and due dimension")
        if after_km is not None:
            if type(after_km) not in (int, float) or not isfinite(after_km) or after_km <= 0:
                raise ValueError("Invalid distance reminder")
            reading = self._latest_reading(vehicle_id, now)
            if reading is None or self._stale(reading, now):
                raise ValueError("Update the odometer before creating a distance reminder")
            due_km, due_date = vehicle.profile.mileage_km + after_km, None
            if not isfinite(due_km):
                raise ValueError("Distance reminder exceeds supported range")
        else:
            if type(after_months) is not int or not 1 <= after_months <= 120:
                raise ValueError("Invalid calendar reminder")
            due_km = None
            due_date = add_months(now.astimezone(ZoneInfo(self.preferences(owner_id).timezone)).date(), after_months)
        reminder_id = "owner-reminder-" + uuid4().hex
        if within_transaction:
            self.store.save_owner_reminder(reminder_id, owner_id, vehicle_id, maintenance_item, due_km, due_date, now)
        else:
            with self.store.transaction():
                self.store.save_owner_reminder(reminder_id, owner_id, vehicle_id, maintenance_item, due_km, due_date, now)
                self.evaluate_vehicle(owner_id, vehicle_id, now)
        return reminder_id

    def _latest_reading(self, vehicle_id, now):
        return max((row for row in self.store.odometer_events(vehicle_id)
                    if datetime.fromisoformat(row["occurred_at"]) <= now),
                   key=lambda row: (row["occurred_at"], row["created_at"], row["id"]), default=None)

    def _stale(self, reading, now):
        return reading is None or now - datetime.fromisoformat(reading["occurred_at"]) >= timedelta(days=self.policy.odometer_stale_days)

    def _candidate(self, owner_id, vehicle_id, event_type, source_type, now, *, item=None,
                   source_id=None, rule_id=None, page=None, due_km=None, due_date=None,
                   mileage=None, reason, evidence=None, baseline=None):
        key = encode((vehicle_id, source_type, source_id, rule_id, baseline, due_km,
                      due_date.isoformat() if due_date else None))
        event_id = "proactive-" + _identity(key)
        return {"id": event_id, "owner_id": owner_id, "vehicle_id": vehicle_id,
                "event_type": event_type.value, "source_type": source_type, "status": EventStatus.OPEN.value,
                "priority": "HIGH" if event_type.value.endswith("OVERDUE") else
                            "LOW" if event_type.value.endswith("DUE_SOON") else "NORMAL",
                "maintenance_item": item, "source_id": source_id, "rule_id": rule_id,
                "source_page": page, "due_km": due_km, "due_date": due_date.isoformat() if due_date else None,
                "current_mileage": mileage, "reason": reason, "evidence": evidence or {},
                "dedupe_key": key, "created_at": now.isoformat(), "effective_at": now.isoformat(),
                "updated_at": now.isoformat()}

    def _conditions(self, owner_id, vehicle_id, now):
        vehicle = self.store.vehicle(owner_id, vehicle_id, now)
        reading = self._latest_reading(vehicle_id, now)
        stale = self._stale(reading, now)
        mileage = vehicle.profile.mileage_km if not stale else None
        candidates, rules = [], 0
        if stale:
            candidates.append(self._candidate(owner_id, vehicle_id, EventType.ODOMETER_UPDATE_REQUESTED,
                "SYSTEM", now, source_id="odometer_freshness", reason="Odometer reading is missing or stale.",
                evidence={"last_reading_at": reading["occurred_at"] if reading else None,
                          "stale_after_days": self.policy.odometer_stale_days}))
        request = self.app._maintenance_request(vehicle)
        history = self.store.service_records(vehicle_id, now)
        if request is not None:
            rules = len(request.pack.rules)
            policy = request.policy or ReminderPolicy(self.policy.due_soon_km, self.policy.due_soon_days)
            states = evaluate_maintenance(request.pack, request.profile, vehicle_id, now, mileage,
                history, request.operating_condition, policy, request.in_service_date)
            sources = {source.source_id: source for source in request.pack.sources}
            rule_by_id = {rule.rule_id: rule for rule in request.pack.rules}
            for state in states:
                if state.status not in {"UPCOMING", "DUE", "OVERDUE"}:
                    continue
                kind = EventType("MAINTENANCE_" + ("DUE_SOON" if state.status == "UPCOMING" else state.status))
                source, rule = sources[state.source_id], rule_by_id[state.manufacturer_rule_id]
                km_attention = (state.remaining_km is not None and state.remaining_km <= policy.upcoming_km)
                date_attention = (state.remaining_days is not None and state.remaining_days <= policy.upcoming_days)
                basis = ("date" if date_attention and (not km_attention or
                         (state.remaining_days <= 0 and state.remaining_km > 0)) else
                         "distance" if km_attention else "unknown")
                candidates.append(self._candidate(owner_id, vehicle_id, kind, "MANUFACTURER", now,
                    item=state.maintenance_item, source_id=state.source_id, rule_id=state.manufacturer_rule_id,
                    page=rule.page, due_km=state.due_odometer_km, due_date=state.due_date,
                    mileage=mileage, reason=state.reason,
                    evidence={"source_title": source.document_title, "source_reference": source.official_source_reference,
                              "applicability": "VERIFIED_APPLICABLE", "remaining_km": state.remaining_km,
                              "remaining_days": state.remaining_days, "last_service_id": state.last_service_id,
                              "attention_basis": basis},
                    baseline=state.last_service_id))
        for reminder in self.store.owner_reminders(owner_id, vehicle_id):
            created = datetime.fromisoformat(reminder["created_at"])
            service = next((record for record in history if record.service_type == reminder["maintenance_item"]
                            and record.performed_at > created), None)
            if service:
                if reminder["status"] != "RESOLVED" or reminder["resolved_by_service_id"] != service.record_id:
                    self.store.set_owner_reminder_resolution(reminder["id"], service.record_id)
                continue
            if reminder["status"] == "RESOLVED":
                self.store.set_owner_reminder_resolution(reminder["id"], None)
            due_km = reminder["due_km"]
            due_date = date.fromisoformat(reminder["due_date"]) if reminder["due_date"] else None
            remaining = due_km - mileage if due_km is not None and mileage is not None else None
            remaining_days = (due_date - now.astimezone(ZoneInfo(self.preferences(owner_id).timezone)).date()).days if due_date else None
            value = remaining if remaining is not None else remaining_days
            window = self.policy.due_soon_km if remaining is not None else self.policy.due_soon_days
            if value is None or value > window:
                continue
            status = "OVERDUE" if value < 0 else "DUE" if value == 0 else "DUE_SOON"
            kind = EventType("OWNER_REMINDER_" + status)
            candidates.append(self._candidate(owner_id, vehicle_id, kind, "OWNER", now,
                item=reminder["maintenance_item"], source_id=reminder["id"], due_km=due_km,
                due_date=due_date, mileage=mileage, reason="Owner-requested maintenance reminder.",
                evidence={"owner_reminder_id": reminder["id"], "remaining_km": remaining,
                          "remaining_days": remaining_days}, baseline=reminder["id"]))
        return candidates, rules

    def _queue(self, event, preferences, now):
        history = self.store.notifications(event_id=event["id"])
        for prior in history:
            if (prior["event_type"] != event["event_type"]
                    and prior["status"] in {"PENDING", "DEFERRED", "FAILED"}):
                self.store.set_notification_status(prior["id"], NotificationStatus.CANCELLED.value)
        if event["status"] != EventStatus.OPEN.value or not preferences.enabled or (
                preferences.preferred_channel == "web") or (
                event["event_type"] == EventType.ODOMETER_UPDATE_REQUESTED.value and
                not preferences.odometer_followups_enabled):
            for prior in history:
                if prior["status"] in {"PENDING", "DEFERRED", "FAILED"}:
                    self.store.set_notification_status(prior["id"], NotificationStatus.CANCELLED.value)
            return
        current = [row for row in history if row["event_type"] == event["event_type"]]
        sequence = max((row["sequence"] for row in current), default=-1) + 1
        for prior in current:
            if prior["channel"] != preferences.preferred_channel and prior["status"] in {"PENDING", "DEFERRED"}:
                self.store.set_notification_status(prior["id"], "CANCELLED")
        current = [row for row in current if row["channel"] == preferences.preferred_channel]
        latest = current[-1] if current else None
        if latest and latest["status"] in {"PENDING", "DEFERRED"}:
            if latest["status"] == "DEFERRED" and datetime.fromisoformat(latest["scheduled_for"]) <= now:
                self.store.set_notification_status(latest["id"], "PENDING")
            return
        if latest and latest["status"] == "FAILED":
            return
        if latest and latest["status"] == "SENT" and now - datetime.fromisoformat(latest["last_attempt_at"]) < timedelta(days=self.policy.followup_days):
            return
        scheduled = _delivery_time(now, preferences)
        self.store.save_notification({"id": "notification-" + _identity((event["id"], event["event_type"], sequence)),
            "event_id": event["id"], "owner_id": event["owner_id"], "vehicle_id": event["vehicle_id"],
            "channel": preferences.preferred_channel, "event_type": event["event_type"],
            "created_at": now.isoformat(), "scheduled_for": scheduled.isoformat(),
            "status": "DEFERRED" if scheduled > now else "PENDING", "attempt_count": 0,
            "last_attempt_at": None, "sequence": sequence})

    def evaluate_vehicle(self, owner_id, vehicle_id, now):
        """Evaluate inside the caller's transaction; never invokes a provider."""
        now = utc(now)
        candidates, rules = self._conditions(owner_id, vehicle_id, now)
        prior = {row["id"]: row for row in self.store.proactive_events(owner_id=owner_id, vehicle_id=vehicle_id)}
        active, created, resolved, duplicates = set(), 0, 0, 0
        preferences = self.preferences(owner_id)
        for event in candidates:
            active.add(event["id"])
            old = prior.get(event["id"])
            if old:
                event["created_at"] = old["created_at"]
                if old["status"] == "ACKNOWLEDGED" and old["event_type"] == event["event_type"]:
                    event["status"] = "ACKNOWLEDGED"
                same = all(old[key] == event[key] for key in
                           ("event_type", "status", "priority", "due_km", "due_date", "current_mileage", "reason", "evidence"))
                if same:
                    duplicates += 1
                    self._queue(event, preferences, now)
                    continue
            else:
                created += 1
            self.store.save_proactive_event(event)
            if (event["source_type"] == "MANUFACTURER" and
                    event["event_type"] in ("MAINTENANCE_DUE", "MAINTENANCE_OVERDUE") and
                    (old is None or old["event_type"] != event["event_type"])):
                self.app._business_event("maintenance_due", owner_id, vehicle_id, "system",
                    event["id"] + ":" + event["event_type"], now,
                    {"maintenance_item": event["maintenance_item"],
                     "status": event["event_type"], "source_id": event["source_id"]})
            self._queue(event, preferences, now)
        for event_id, old in prior.items():
            if event_id not in active and old["status"] != "RESOLVED":
                self.store.set_proactive_event_status(event_id, "RESOLVED", now)
                self._queue({**old, "status": "RESOLVED"}, preferences, now)
                resolved += 1
        return rules, created, resolved, duplicates

    def run(self, *, now, owner_id=None, vehicle_id=None):
        now = utc(now)
        started = perf_counter()
        if vehicle_id is not None:
            ids = (vehicle_id,)
            if owner_id is not None:
                self.store.vehicle(owner_id, vehicle_id, now)
        else:
            ids = self.store.all_vehicle_ids() if owner_id is None else tuple(
                vehicle.profile.vehicle_id for vehicle in self.store.vehicles(owner_id, now))
        totals = [0, 0, 0, 0]
        for current_id in ids:
            current_owner = self.store.owner_for_vehicle(current_id)
            if owner_id is not None and current_owner != owner_id:
                raise ValueError("Vehicle is not available for this owner")
            with self.store.transaction():
                result = self.evaluate_vehicle(current_owner, current_id, now)
            for index, value in enumerate(result):
                totals[index] += value
        pending = len(self.store.notifications(owner_id=owner_id, vehicle_id=vehicle_id,
                                                statuses=("PENDING", "DEFERRED")))
        return EvaluationReport(len(ids), totals[0], totals[1], totals[2], totals[3], pending,
                                (perf_counter() - started) * 1000)

    def events(self, owner_id, vehicle_id, *, include_resolved=False, now):
        self.store.vehicle(owner_id, vehicle_id, utc(now))
        rows = self.store.proactive_events(owner_id=owner_id, vehicle_id=vehicle_id)
        return tuple(_event_from_row(row) for row in rows if include_resolved or row["status"] != "RESOLVED")

    def acknowledge(self, owner_id, vehicle_id, event_id, *, now):
        now = utc(now)
        with self.store.transaction():
            event = self.store.proactive_event(event_id, owner_id, vehicle_id)
            if event is None or event["status"] == "RESOLVED":
                raise ValueError("This reminder is not active for this vehicle")
            self.store.set_proactive_event_status(event_id, "ACKNOWLEDGED", now)
            self._queue({**event, "status": "ACKNOWLEDGED"}, self.preferences(owner_id), now)

    def explain(self, owner_id, vehicle_id, event_id, *, now, language="en"):
        self.store.vehicle(owner_id, vehicle_id, utc(now))
        row = self.store.proactive_event(event_id, owner_id, vehicle_id)
        if row is None:
            raise ValueError("Unknown reminder for this vehicle")
        return explain_event(_event_from_row(row), language)

    def dispatch(self, senders: dict[str, NotificationSender], *, now, owner_id=None):
        """Backward-compatible explicit delivery through the leased runner."""
        from carmind.proactive_runner import ProactiveCycleRunner
        report = ProactiveCycleRunner(self.app, senders).deliver(now=now, owner_id=owner_id)
        return DeliveryReport(report.notifications_sent, report.notifications_failed,
                              report.notifications_deferred + report.notifications_cancelled)
