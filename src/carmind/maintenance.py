"""Deterministic maintenance status and reminder events, without delivery."""

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
import json
from math import isfinite

from carmind.contracts import MaintenanceRecord
from carmind.manufacturer_knowledge import KnowledgePack, VehicleKnowledgeProfile


@dataclass(frozen=True)
class ReminderPolicy:
    upcoming_km: float = 1000
    upcoming_days: int = 30

    def __post_init__(self):
        if type(self.upcoming_km) not in (int, float) or not isfinite(self.upcoming_km) or self.upcoming_km < 0 or type(self.upcoming_days) is not int or self.upcoming_days < 0:
            raise ValueError("Invalid reminder policy")


@dataclass(frozen=True)
class MaintenanceReminder:
    reminder_id: str
    vehicle_id: str
    maintenance_item: str
    status: str
    due_odometer_km: float | None
    due_date: date | None
    remaining_km: float | None
    remaining_days: int | None
    manufacturer_rule_id: str
    source_id: str
    reason: str
    generated_at: datetime
    last_service_id: str | None = None


def add_months(value: date, months: int) -> date:
    year, month = divmod(value.year * 12 + value.month - 1 + months, 12)
    return date(year, month + 1, min(value.day, monthrange(year, month + 1)[1]))


def evaluate_maintenance(pack: KnowledgePack, profile: VehicleKnowledgeProfile,
                         vehicle_id: str, current_at: datetime, odometer_km: float | None,
                         history: tuple[MaintenanceRecord, ...] | list[MaintenanceRecord] = (),
                         operating_condition: str = "UNKNOWN", policy: ReminderPolicy = ReminderPolicy(),
                         in_service_date: date | None = None) -> tuple[MaintenanceReminder, ...]:
    if current_at.utcoffset() is None or not vehicle_id.strip():
        raise ValueError("Vehicle ID and timezone-aware current time required")
    if odometer_km is not None and (type(odometer_km) not in (float, int) or not isfinite(odometer_km) or odometer_km < 0):
        raise ValueError("Invalid odometer")
    if operating_condition not in {"NORMAL", "SEVERE", "UNKNOWN"}:
        raise ValueError("Invalid operating condition")
    if in_service_date is not None and in_service_date > current_at.date():
        raise ValueError("Future in-service date")
    visible = []
    for record in history:
        if record.performed_at.utcoffset() is None:
            raise ValueError("Timezone-aware service dates required")
        if record.performed_at > current_at:
            continue
        if record.odometer_km is not None and (type(record.odometer_km) not in (int, float) or not isfinite(record.odometer_km) or record.odometer_km < 0 or (odometer_km is not None and record.odometer_km > odometer_km)):
            raise ValueError("Service odometer contradicts current odometer")
        visible.append(record)
    if len({r.record_id for r in visible}) != len(visible):
        raise ValueError("Duplicate service record IDs")
    results = []
    for rule in pack.rules:
        matching = sorted((r for r in visible if r.service_type == rule.maintenance_item), key=lambda r: (r.performed_at, r.record_id))
        last = matching[-1] if matching else None
        due_km = due_date = remaining_km = remaining_days = None
        reason = "Calculated from stored schedule rule and explicit service history or in-service date."
        status = "UNKNOWN"
        applicable = pack.matches(profile) and rule.operating_condition == operating_condition and operating_condition != "UNKNOWN"
        if not applicable:
            reason = "Unverified or mismatched vehicle identity or operating condition."
        elif last is None and in_service_date is None:
            reason = "No matching service record or explicit in-service date; service is not inferred."
        else:
            km_interval = (rule.repeat_km or rule.interval_km) if last else (rule.first_due_km or rule.interval_km)
            month_interval = (rule.repeat_months or rule.interval_months) if last else (rule.first_due_months or rule.interval_months)
            baseline_km = last.odometer_km if last else 0
            baseline_date = last.performed_at.date() if last else in_service_date
            if km_interval is not None and baseline_km is not None:
                due_km = baseline_km + km_interval
                if odometer_km is not None:
                    remaining_km = due_km - odometer_km
            if month_interval is not None and baseline_date is not None:
                due_date = add_months(baseline_date, month_interval)
                remaining_days = (due_date - current_at.date()).days
            dimensions = []
            if rule.trigger != "TIME":
                dimensions.append((remaining_km, policy.upcoming_km))
            if rule.trigger != "MILEAGE":
                dimensions.append((remaining_days, policy.upcoming_days))
            values = [None if v is None else (3 if v < 0 else 2 if v == 0 else 1 if v <= window else 0) for v, window in dimensions]
            if rule.trigger == "WHICHEVER_FIRST" and any(v in (2, 3) for v in values):
                rank = max(v for v in values if v is not None)
            elif any(v is None for v in values):
                rank = None
            else:
                rank = min(values) if rule.trigger == "AND" else max(values)
            status = "UNKNOWN" if rank is None else ("NOT_DUE", "UPCOMING", "DUE", "OVERDUE")[rank]
            if status == "UNKNOWN":
                reason = "Insufficient mileage, date or repeat interval for a definitive status."
        identity = json.dumps([vehicle_id, profile.__dict__, rule.rule_id, status, due_km, str(due_date), last.record_id if last else None], sort_keys=True)
        results.append(MaintenanceReminder("reminder:" + sha256(identity.encode()).hexdigest()[:24], vehicle_id,
                       rule.maintenance_item, status, due_km, due_date, remaining_km, remaining_days,
                       rule.rule_id, rule.source_id, reason, current_at, last.record_id if last else None))
    return tuple(results)


def reminder_events(results: tuple[MaintenanceReminder, ...]) -> tuple[MaintenanceReminder, ...]:
    return tuple(r for r in results if r.status in {"UPCOMING", "DUE", "OVERDUE"})
