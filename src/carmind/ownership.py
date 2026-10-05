"""Small ownership contracts; model proposals are never write authorization."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from math import isfinite

from carmind.contracts import VehicleProfile


def utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("An aware timestamp is required.")
    return value.astimezone(timezone.utc)


def identifier(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError("Invalid identifier.")
    return value


def distance_km(reading, unit: str) -> float:
    if type(reading) not in (int, float) or not isfinite(reading) or reading < 0:
        raise ValueError("Distance must be a finite nonnegative number.")
    if unit not in ("km", "mi"):
        raise ValueError("Distance unit must be km or mi.")
    value = float(reading) * (1.609344 if unit == "mi" else 1)
    if not isfinite(value):
        raise ValueError("Distance is too large.")
    return value


class CommandType(str, Enum):
    UPDATE_ODOMETER = "update_odometer"
    RECORD_SERVICE = "record_service_event"
    SET_VEHICLE_FIELD = "set_vehicle_profile_field"
    SELECT_VEHICLE = "select_vehicle"
    ACKNOWLEDGE_REMINDER = "acknowledge_reminder"
    CREATE_OWNER_REMINDER = "create_owner_reminder"


class OdometerConflict(ValueError):
    """A proposed reading contradicts the accepted chronological readings."""


class StaleProposal(ValueError):
    """A reviewed command's vehicle/profile precondition has since changed."""


SERVICE_TYPES = ("oil", "oil_filter", "tires", "battery", "brakes", "coolant", "air_filter", "other")
PROFILE_FIELDS = ("make", "model", "year", "engine", "trim", "market", "vin", "nickname")

# Descriptions express structure, not natural-language intent classification.
COMMAND_SCHEMAS = {
    "update_odometer": {"required": {"reading": "nonnegative number", "unit": "km|mi", "occurred_at": "aware ISO datetime"},
                        "optional": {"supersedes_id": "existing odometer event ID (correction)"}},
    "record_service_event": {"required": {"service_type": list(SERVICE_TYPES), "performed_at": "aware ISO datetime"},
                             "optional": {"odometer": "nonnegative number", "unit": "km|mi, required with odometer",
                                          "notes": "owner note, at most 512 chars", "supersedes_id": "existing service event ID (correction)"}},
    "set_vehicle_profile_field": {"required": {"field": list(PROFILE_FIELDS), "value": "string, or positive integer for year"}},
    "select_vehicle": {"required": {"vehicle_id": "owned vehicle ID"}},
    "acknowledge_reminder": {"required": {"reminder_id": "active reminder ID"}},
    "create_owner_reminder": {"required": {"maintenance_item": list(SERVICE_TYPES)},
                              "optional": {"after_km": "positive km from a fresh saved odometer",
                                           "after_months": "positive calendar months, at most 120"}},
}

COMMAND_PROTOCOL = """
Application ownership mode: you may alternatively return exactly
{"type":"propose_command","command":{"kind":string,"arguments":object,
"certainty":"explicit"|"uncertain","owner_quote":string}}.
If the owner's statement is ambiguous, return exactly
{"type":"request_clarification","question":string} instead of guessing a fact.
Ask one short question ending in ? or ؟. Do not give driving clearance.
Use only supplied command schemas. Quote the current owner's statement supporting
the proposal. Never turn a hypothesis into a service, odometer or vehicle fact.
Uncertain statements require clarification. Even explicit proposals are NOT
authorized writes: only a separate adapter confirmation can approve the exact
proposal. Do not emit SQL, authorization, confirmed flags or arbitrary fact keys.
Use aware ISO timestamps. For words like "today", the owner message timestamp
provides the current date; propose the precise stored value for owner review.
Conversation summaries are prior conversational context, NOT fresh telemetry or
new citable evidence. Prior hypotheses remain unconfirmed. Ownership context and
deterministic maintenance facts are application-owned. Use current read tools
when you need citable persisted facts. Do not infer a repair from a conversation.
"""


@dataclass(frozen=True)
class OwnershipCommand:
    kind: CommandType
    arguments: dict
    certainty: str
    owner_quote: str


def parse_command(raw: dict, owner_text: str) -> OwnershipCommand:
    if not isinstance(raw, dict) or set(raw) != {"kind", "arguments", "certainty", "owner_quote"}:
        raise ValueError("Invalid ownership command fields.")
    try:
        kind = CommandType(raw["kind"])
    except (ValueError, TypeError):
        raise ValueError("Unknown ownership command.") from None
    spec = COMMAND_SCHEMAS[kind.value]
    args = raw["arguments"]
    if not isinstance(args, dict) or not set(spec["required"]) <= args.keys() or set(args) - set(spec["required"]) - set(spec.get("optional", {})):
        raise ValueError("Invalid ownership command arguments.")
    if raw["certainty"] not in ("explicit", "uncertain"):
        raise ValueError("Invalid ownership certainty.")
    quote = raw["owner_quote"]
    if not isinstance(quote, str) or not quote.strip() or len(quote) > 512 or quote not in owner_text:
        raise ValueError("Proposal must reference the current owner's words.")
    args = dict(args)
    for key in ("occurred_at", "performed_at"):
        if key in args:
            try:
                args[key] = utc(datetime.fromisoformat(args[key])).isoformat()
            except (ValueError, TypeError):
                raise ValueError("Invalid command timestamp.") from None
    if kind == CommandType.UPDATE_ODOMETER:
        distance_km(args["reading"], args["unit"])
    elif kind == CommandType.RECORD_SERVICE:
        if args["service_type"] not in SERVICE_TYPES:
            raise ValueError("Unknown service category.")
        if ("odometer" in args) != ("unit" in args):
            raise ValueError("Service distance requires an explicit unit.")
        if "odometer" in args:
            distance_km(args["odometer"], args["unit"])
        if "notes" in args and (not isinstance(args["notes"], str) or len(args["notes"]) > 512):
            raise ValueError("Invalid service note.")
    elif kind == CommandType.SET_VEHICLE_FIELD:
        name, value = args["field"], args["value"]
        if name not in PROFILE_FIELDS:
            raise ValueError("Vehicle field is not writable.")
        if name == "year":
            if type(value) is not int or value <= 0:
                raise ValueError("Vehicle year must be positive.")
        elif not isinstance(value, str) or not value.strip() or len(value) > 120:
            raise ValueError("Invalid vehicle field value.")
    elif kind == CommandType.CREATE_OWNER_REMINDER:
        if args["maintenance_item"] not in SERVICE_TYPES or ("after_km" in args) == ("after_months" in args):
            raise ValueError("Owner reminder needs one supported maintenance item and due dimension.")
        if "after_km" in args:
            distance_km(args["after_km"], "km")
            if args["after_km"] <= 0:
                raise ValueError("Owner reminder distance must be positive.")
        if "after_months" in args and (type(args["after_months"]) is not int or not 1 <= args["after_months"] <= 120):
            raise ValueError("Owner reminder months must be between 1 and 120.")
    for key in ("supersedes_id", "vehicle_id", "reminder_id"):
        if key in args:
            identifier(args[key])
    return OwnershipCommand(kind, args, raw["certainty"], quote)


@dataclass(frozen=True)
class OwnedVehicle:
    profile: VehicleProfile
    owner_id: str
    trim: str | None
    market: str | None
    nickname: str | None
    odometer_unit: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class MutationResult:
    applied: bool
    reason: str
    entity_id: str | None
    changed_fields: tuple[str, ...]
    timestamp: datetime
    provenance: str = "USER_REPORTED_CONFIRMED"


@dataclass(frozen=True)
class CommandProposal:
    proposal_id: str
    command: OwnershipCommand
    expires_at: datetime


@dataclass(frozen=True)
class OwnershipContext:
    vehicle: dict
    recent_services: tuple[dict, ...]
    maintenance_applicability: str
    reminders: tuple[dict, ...]
    recent_turns: tuple[dict, ...]
    previous_assessment: dict | None
    retention: dict = field(default_factory=lambda: {"turns": 6, "services": 8, "reminders": 8})
    active_event: dict | None = None
