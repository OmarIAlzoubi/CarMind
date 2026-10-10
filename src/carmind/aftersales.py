"""Bounded aftersales records and explicitly simulated appointments."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from carmind.ownership import utc


class InformationNeed(str, Enum):
    GENERAL = "general"
    MAINTENANCE_TIMING = "maintenance_timing"
    MANUFACTURER_SPECIFICATION = "manufacturer_specification"


class MissingInformation(str, Enum):
    CURRENT_MILEAGE = "current_mileage"
    FRESH_MILEAGE = "fresh_mileage"
    MODEL_YEAR = "model_year"
    MARKET = "market"
    APPLICABLE_SOURCE = "applicable_source"


def missing_information(need, profile, *, market, odometer_at, now,
                        source_available=False, stale_after_days=30):
    """Task-specific missing facts, not an intent parser or guessed vehicle facts."""
    need, now = InformationNeed(need), utc(now)
    if need == InformationNeed.GENERAL:
        return ()
    gaps = []
    if need == InformationNeed.MAINTENANCE_TIMING:
        if profile.mileage_km is None:
            gaps.append(MissingInformation.CURRENT_MILEAGE)
        elif odometer_at is None or now - utc(odometer_at) >= timedelta(days=stale_after_days):
            gaps.append(MissingInformation.FRESH_MILEAGE)
    if not profile.year:
        gaps.append(MissingInformation.MODEL_YEAR)
    if not market:
        gaps.append(MissingInformation.MARKET)
    if not source_available:
        gaps.append(MissingInformation.APPLICABLE_SOURCE)
    return tuple(gaps)


@dataclass(frozen=True)
class ServiceRequest:
    request_id: str
    owner_id: str
    vehicle_id: str
    created_at: datetime
    source_channel: str
    intent_type: str
    requested_services: tuple[str, ...]
    symptoms: tuple[str, ...]
    current_mileage_km: float | None
    recent_service_ids: tuple[str, ...]
    safety_disposition: str
    preferred_time_window: str | None = None
    preferred_location: str | None = None
    status: str = "CONFIRMED"
    maintenance_items: tuple[dict, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    odometer_recorded_at: str | None = None

    @classmethod
    def from_data(cls, data):
        data = dict(data)
        data["created_at"] = utc(datetime.fromisoformat(data["created_at"]))
        for key in ("requested_services", "symptoms", "recent_service_ids", "maintenance_items", "evidence_ids"):
            data[key] = tuple(data.get(key, ()))
        return cls(**data)


@dataclass(frozen=True)
class MockSlot:
    slot_id: str
    starts_at: datetime
    label: str


def demo_slots(now: datetime) -> tuple[MockSlot, ...]:
    """Stable UTC demo slots; they do not represent real dealer availability."""
    day = utc(now).date() + timedelta(days=1)
    return tuple(MockSlot(f"demo-{day.isoformat()}-{hour:02d}",
                          datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc),
                          f"{day.isoformat()} {hour:02d}:00 UTC") for hour in (10, 14))


def handoff_packet(request: ServiceRequest, vehicle_label: str, open_questions=()) -> dict:
    """Small serializable packet, with no transcript or external recipient."""
    return {"schema_version": 1, "request_id": request.request_id,
            "vehicle": {"id": request.vehicle_id, "label": vehicle_label,
                        "mileage_km": request.current_mileage_km},
            "intent_type": request.intent_type,
            "requested_services": list(request.requested_services),
            "customer_reported_symptoms": list(request.symptoms),
            "recent_service_ids": list(request.recent_service_ids),
            "maintenance_items": list(request.maintenance_items),
            "evidence_ids": list(request.evidence_ids),
            "odometer_recorded_at": request.odometer_recorded_at,
            "safety_disposition": request.safety_disposition,
            "open_questions": list(open_questions)[:3],
            "preferred_time_window": request.preferred_time_window,
            "preferred_location": request.preferred_location}
