"""One bounded proactive evaluation/delivery cycle for an external scheduler."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from re import fullmatch
from time import perf_counter
from uuid import uuid4

from carmind.app import CarMindApp
from carmind.ownership import utc
from carmind.proactive import (EventType, ProactiveService, _delivery_time, _event_from_row,
                               _intent_from_row, notification_text)
from carmind.storage import OwnershipStore


class TransientDeliveryError(Exception):
    """A transport failure for which a later retry may succeed."""

    def __init__(self, category="temporary_provider_failure"):
        self.category = category
        super().__init__(category)


class PermanentDeliveryError(Exception):
    """A transport failure that should not be retried automatically."""

    def __init__(self, category="permanent_provider_failure"):
        self.category = category
        super().__init__(category)


@dataclass(frozen=True)
class DeliveryReceipt:
    provider_message_id: str | None = None


@dataclass(frozen=True)
class DeliveryPolicy:
    batch_limit: int = 50
    lease_seconds: int = 300
    max_attempts: int = 3
    base_backoff_seconds: int = 300
    max_backoff_seconds: int = 21600

    def __post_init__(self):
        if any(type(value) is not int or value < 1 for value in vars(self).values()):
            raise ValueError("Delivery policy values must be positive integers")
        if self.max_backoff_seconds < self.base_backoff_seconds:
            raise ValueError("Maximum backoff must be at least base backoff")


@dataclass(frozen=True)
class CycleReport:
    cycle_id: str
    started_at: datetime
    completed_at: datetime
    mode: str
    dry_run: bool
    owners_evaluated: int = 0
    vehicles_evaluated: int = 0
    maintenance_rules_evaluated: int = 0
    events_created: int = 0
    events_updated: int = 0
    duplicates_prevented: int = 0
    notifications_created: int = 0
    eligible_notifications: int = 0
    notifications_claimed: int = 0
    notifications_sent: int = 0
    notifications_deferred: int = 0
    notifications_failed: int = 0
    notifications_retried: int = 0
    notifications_cancelled: int = 0
    notifications_would_send: int = 0
    claims_lost: int = 0
    remaining_eligible: int = 0
    elapsed_ms: float = 0.0


def _error_category(exc):
    if isinstance(exc, (TransientDeliveryError, PermanentDeliveryError)):
        category = exc.category
        return category if isinstance(category, str) and fullmatch(r"[a-z_]{1,40}", category) else "transport_failure"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, ConnectionError):
        return "connection"
    if isinstance(exc, ValueError):
        return "invalid_destination"
    return "unknown_transport_failure"


class ProactiveCycleRunner:
    """No internal clock loop or provider construction; senders are explicit."""

    def __init__(self, app: CarMindApp, senders=None, policy=DeliveryPolicy()):
        self.app = app
        self.store = app.store
        self.senders = dict(senders or {})
        if any(not isinstance(channel, str) or not hasattr(sender, "send")
               for channel, sender in self.senders.items()):
            raise ValueError("Invalid notification sender registry")
        self.policy = policy

    def _snapshot_runner(self):
        """Preview evaluation against a private in-memory copy, never the real DB."""
        snapshot = OwnershipStore(":memory:")
        self.store.db.backup(snapshot.db)
        app = CarMindApp(snapshot, self.app.provider, mode=self.app.mode,
            router=self.app.router, approved_schedules=self.app.schedules,
            allow_test_schedules=self.app.allow_test_schedules,
            max_model_calls=self.app.max_model_calls, manual_index=self.app.manual_index)
        app.proactive = ProactiveService(app, self.app.proactive.policy)
        return ProactiveCycleRunner(app, self.senders, self.policy)

    def _eligibility(self, row, now):
        current = self.store.db.execute("SELECT status FROM notification_outbox WHERE id=?", (row["id"],)).fetchone()
        event = self.store.proactive_event(row["event_id"], row["owner_id"], row["vehicle_id"])
        preferences = self.app.proactive.preferences(row["owner_id"])
        if (current is None or current["status"] not in ("PENDING", "DEFERRED") or event is None
                or event["status"] != "OPEN" or event["event_type"] != row["event_type"]
                or not preferences.enabled or preferences.preferred_channel != row["channel"]
                or row["channel"] == "web" or (row["event_type"] == EventType.ODOMETER_UPDATE_REQUESTED.value
                and not preferences.odometer_followups_enabled)):
            return "CANCELLED", None, None
        allowed_at = _delivery_time(now, preferences)
        if allowed_at > now:
            return "DEFERRED", allowed_at, None
        return "SEND", None, (event, preferences)

    def evaluate(self, *, now, owner_id=None, vehicle_id=None, dry_run=False):
        now = utc(now)
        if dry_run:
            clone = self._snapshot_runner()
            try:
                report = clone.evaluate(now=now, owner_id=owner_id, vehicle_id=vehicle_id)
                return CycleReport(**{**vars(report), "dry_run": True})
            finally:
                clone.store.close()
        started = perf_counter()
        before_events = {row["id"]: row for row in self.store.proactive_events()}
        before_notifications = self.store.db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0]
        evaluation = self.app.evaluate_proactive_state(now=now, owner_id=owner_id, vehicle_id=vehicle_id)
        after_events = {row["id"]: row for row in self.store.proactive_events()}
        updated = sum(before_events[key] != row for key, row in after_events.items() if key in before_events)
        created_notifications = self.store.db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] - before_notifications
        evaluated_ids = ((vehicle_id,) if vehicle_id is not None else
                         self.store.all_vehicle_ids() if owner_id is None else
                         tuple(v.profile.vehicle_id for v in self.store.vehicles(owner_id, now)))
        owners = len({self.store.owner_for_vehicle(current_id) for current_id in evaluated_ids})
        return CycleReport(uuid4().hex, now, now + timedelta(seconds=perf_counter() - started),
            "evaluate", False, owners,
            evaluation.vehicles_evaluated, evaluation.maintenance_rules_evaluated,
            evaluation.events_created, updated, evaluation.duplicate_events_prevented,
            created_notifications, elapsed_ms=(perf_counter() - started) * 1000)

    def deliver(self, *, now, owner_id=None, vehicle_id=None, limit=None, dry_run=False):
        now = utc(now)
        limit = self.policy.batch_limit if limit is None else limit
        if type(limit) is not int or limit < 1:
            raise ValueError("Delivery limit must be a positive integer")
        started = perf_counter()
        cycle_id = uuid4().hex
        channels = tuple(sorted(self.senders))
        ready = self.store.eligible_notifications(now, owner_id=owner_id,
                                                   vehicle_id=vehicle_id, channels=channels)
        if dry_run:
            outcomes = [self._eligibility(row, now)[0] for row in ready[:limit]]
            return CycleReport(cycle_id, now, now, "deliver", True,
                eligible_notifications=len(ready),
                notifications_would_send=outcomes.count("SEND"),
                remaining_eligible=max(0, len(ready) - limit),
                elapsed_ms=(perf_counter() - started) * 1000)
        lease_until = now + timedelta(seconds=self.policy.lease_seconds)
        claimed = self.store.claim_notifications(now, lease_until, cycle_id,
            limit=limit, owner_id=owner_id, vehicle_id=vehicle_id, channels=channels)
        counts = {"sent": 0, "deferred": 0, "failed": 0, "retried": 0,
                  "cancelled": 0, "lost": 0}
        for row, token in claimed:
            outcome, scheduled, context = self._eligibility(row, now)
            if outcome in ("CANCELLED", "DEFERRED"):
                completed_at = now + timedelta(seconds=perf_counter() - started)
                finished = self.store.finalize_claim(row["id"], token, completed_at, outcome,
                    scheduled_for=scheduled.isoformat() if scheduled else None)
                counts["cancelled" if outcome == "CANCELLED" else "deferred"] += int(finished)
                counts["lost"] += int(not finished)
                continue
            event, preferences = context
            vehicle = self.store.vehicle(row["owner_id"], row["vehicle_id"], now)
            label = vehicle.nickname or f"{vehicle.profile.year} {vehicle.profile.make} {vehicle.profile.model}"
            text = f"{label}: {notification_text(_event_from_row(event), preferences.language)}"
            result, category, message_id = "SUCCESS", None, None
            try:
                receipt = self.senders[row["channel"]].send(_intent_from_row(row), text)
                if isinstance(receipt, DeliveryReceipt) and receipt.provider_message_id:
                    candidate = receipt.provider_message_id
                    if fullmatch(r"[A-Za-z0-9._:-]{1,128}", candidate):
                        message_id = candidate
            except Exception as exc:
                category = _error_category(exc)
                result = ("TRANSIENT_FAILURE" if isinstance(exc, (TransientDeliveryError,
                          TimeoutError, ConnectionError)) else "PERMANENT_FAILURE")
            attempts = row["attempt_count"] + 1
            if result == "SUCCESS":
                status, next_attempt = "SENT", None
            elif result == "TRANSIENT_FAILURE" and attempts < self.policy.max_attempts:
                delay = min(self.policy.base_backoff_seconds * (2 ** (attempts - 1)),
                            self.policy.max_backoff_seconds)
                status, next_attempt = "PENDING", now + timedelta(seconds=delay)
            else:
                status, next_attempt = "FAILED", None
            completed_at = now + timedelta(seconds=perf_counter() - started)
            finished = self.store.finalize_claim(row["id"], token, completed_at, status,
                result=result, error_category=category, provider_message_id=message_id,
                next_attempt_at=next_attempt.isoformat() if next_attempt else None,
                attempted_at=now)
            counts["lost"] += int(not finished)
            if finished:
                counts["sent" if status == "SENT" else "retried" if status == "PENDING" else "failed"] += 1
        remaining = len(self.store.eligible_notifications(
            now + timedelta(seconds=perf_counter() - started), owner_id=owner_id,
            vehicle_id=vehicle_id, channels=channels))
        return CycleReport(cycle_id, now, now + timedelta(seconds=perf_counter() - started), "deliver", False,
            eligible_notifications=len(ready), notifications_claimed=len(claimed),
            notifications_sent=counts["sent"], notifications_deferred=counts["deferred"],
            notifications_failed=counts["failed"], notifications_retried=counts["retried"],
            notifications_cancelled=counts["cancelled"], claims_lost=counts["lost"],
            remaining_eligible=remaining, elapsed_ms=(perf_counter() - started) * 1000)

    def cycle(self, *, now, owner_id=None, vehicle_id=None, limit=None, dry_run=False):
        now = utc(now)
        if dry_run:
            clone = self._snapshot_runner()
            try:
                report = clone._cycle(now=now, owner_id=owner_id, vehicle_id=vehicle_id,
                                      limit=limit, preview_delivery=True)
                return CycleReport(**{**vars(report), "dry_run": True})
            finally:
                clone.store.close()
        return self._cycle(now=now, owner_id=owner_id, vehicle_id=vehicle_id, limit=limit)

    def _cycle(self, *, now, owner_id=None, vehicle_id=None, limit=None, preview_delivery=False):
        started = perf_counter()
        evaluated = self.evaluate(now=now, owner_id=owner_id, vehicle_id=vehicle_id)
        delivered = self.deliver(now=now, owner_id=owner_id, vehicle_id=vehicle_id,
                                 limit=limit, dry_run=preview_delivery)
        fields = {**vars(delivered)}
        fields.update(owners_evaluated=evaluated.owners_evaluated,
                      vehicles_evaluated=evaluated.vehicles_evaluated,
                      maintenance_rules_evaluated=evaluated.maintenance_rules_evaluated,
                      events_created=evaluated.events_created, events_updated=evaluated.events_updated,
                      duplicates_prevented=evaluated.duplicates_prevented,
                      notifications_created=evaluated.notifications_created,
                      cycle_id=evaluated.cycle_id,
                      completed_at=now + timedelta(seconds=perf_counter() - started),
                      mode="cycle", dry_run=preview_delivery,
                      elapsed_ms=(perf_counter() - started) * 1000)
        return CycleReport(**fields)
