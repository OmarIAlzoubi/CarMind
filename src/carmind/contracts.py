"""Interface-independent data contracts for CarMind Core."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


@dataclass(frozen=True)
class VehicleProfile:
    """The user's vehicle, with optional mileage and VIN."""

    vehicle_id: str
    make: str
    model: str
    year: int
    mileage_km: float | None = None
    engine: str | None = None
    vin: str | None = None

    def __post_init__(self) -> None:
        if not self.vehicle_id.strip():
            raise ValueError("vehicle_id must not be blank")
        if self.year <= 0:
            raise ValueError("year must be positive")
        if self.mileage_km is not None and self.mileage_km < 0:
            raise ValueError("mileage_km must not be negative")


@dataclass(frozen=True)
class UserMessage:
    """A natural-language message from any interface."""

    message_id: str
    text: str
    timestamp: datetime

    def __post_init__(self) -> None:
        if not self.message_id.strip():
            raise ValueError("message_id must not be blank")
        if not self.text.strip():
            raise ValueError("text must not be blank")


@dataclass
class MaintenanceRecord:
    """A service performed on the vehicle."""

    record_id: str
    service_type: str
    performed_at: datetime
    odometer_km: float | None = None
    notes: str | None = None

    def __post_init__(self) -> None:
        if not self.record_id.strip():
            raise ValueError("record_id must not be blank")
        if self.odometer_km is not None and self.odometer_km < 0:
            raise ValueError("odometer_km must not be negative")


@dataclass
class DiagnosticCodeRecord:
    """A historical code observation, without diagnostic interpretation."""

    code: str
    observed_at: datetime
    source: str
    active: bool

    def __post_init__(self) -> None:
        if not self.code.strip():
            raise ValueError("code must not be blank")


class ObservationSource(str, Enum):
    USER = "USER"
    SIMULATOR = "SIMULATOR"
    OBD = "OBD"
    SERVICE_RECORD = "SERVICE_RECORD"
    SYSTEM = "SYSTEM"


@dataclass(frozen=True)
class Observation:
    """One piece of vehicle evidence and its origin."""

    observation_id: str
    name: str
    value: str | int | float | bool
    unit: str | None
    timestamp: datetime
    source: ObservationSource

    def __post_init__(self) -> None:
        if not self.observation_id.strip():
            raise ValueError("observation_id must not be blank")


@dataclass
class VehicleContext:
    """The information currently available about one vehicle."""

    profile: VehicleProfile
    maintenance_records: list[MaintenanceRecord] = field(default_factory=list)
    diagnostic_codes: list[DiagnosticCodeRecord] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)


@dataclass
class ConversationContext:
    """Recent conversational context, independent of persistent memory."""

    recent_messages: list[UserMessage] = field(default_factory=list)


class SafetyDisposition(str, Enum):
    # Absence of a triggered rule is not a determination of driving safety.
    NO_RULE_TRIGGERED = "NO_RULE_TRIGGERED"
    SERVICE_REVIEW = "SERVICE_REVIEW"
    STOP_WHEN_SAFE = "STOP_WHEN_SAFE"
    UNDETERMINED = "UNDETERMINED"


@dataclass
class Assessment:
    """A structured internal result, not a conversational response."""

    observations: list[str] = field(default_factory=list)
    hypotheses: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    uncertainties: list[str] = field(default_factory=list)
    safety_disposition: SafetyDisposition = SafetyDisposition.UNDETERMINED
    recommended_action_ids: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
