"""Deterministic read-only evidence tools; no model or simulator dependencies."""

from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
import json
from math import isfinite
import sqlite3
from types import MappingProxyType

from carmind.contracts import Observation, VehicleContext
from carmind.evidence import FrozenEvidenceSnapshot
from carmind.maintenance import ReminderPolicy
from carmind.manufacturer_knowledge import KnowledgePack, VehicleKnowledgeProfile
from datetime import date


WHEELS = ("front_left", "front_right", "rear_left", "rear_right")
TIRE_NAMES = tuple(f"{wheel}_tire_pressure" for wheel in WHEELS)
ENGINE_NAMES = ("engine_rpm", "engine_vibration", "engine_noise", "engine_warning", "operating_state")
ELECTRICAL_NAMES = ("charging_voltage", "electrical_warning", "accessory_voltage", "electrical_symptom")
FUEL_NAMES = ("fuel_consumption", "average_trip_duration", "idle_time_ratio")


def _manual_source_payload(source):
    return {key: getattr(source, key) for key in (
        "source_id", "document_title", "document_type", "manufacturer", "model",
        "model_year", "market", "source_status")}


@dataclass(frozen=True)
class ToolDefinition:
    tool_id: str
    description: str
    evidence_description: str
    argument_names: tuple[str, ...] = ()
    read_only: bool = True

    @property
    def argument_schema(self) -> dict:
        """The small schema subset enforced by execute_tool; all fields optional."""
        properties = {
            "wheel": {"type": "string", "enum": list(WHEELS)},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "service_type": {"type": "string", "minLength": 1},
            "rule_id": {"type": "string", "minLength": 1},
            "source_id": {"type": "string", "minLength": 1},
            "query": {"type": "string", "minLength": 1, "maxLength": 240},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 5},
        }
        return {
            "type": "object",
            "properties": {name: properties[name] for name in self.argument_names},
            "required": ["query"] if self.tool_id == "search_manufacturer_manual" else [],
            "additionalProperties": False,
        }


_DEFINITIONS = (
    ToolDefinition("search_manufacturer_manual", "Search registered manufacturer documents for this vehicle and return page-grounded excerpts.",
                   "Each hit retains its document source and physical page; applicability may be unverified.",
                   ("query", "top_k")),
    ToolDefinition("get_manufacturer_maintenance_schedule", "Read the supplied manufacturer schedule with exact applicability.", "Rule and document provenance; unknown model year cannot match a vehicle."),
    ToolDefinition("get_manufacturer_maintenance_rule", "Read manufacturer rules, optionally by exact ID.", "Manufacturer intervals, condition, action and page references.", ("rule_id",)),
    ToolDefinition("get_maintenance_source_reference", "Read official source metadata, optionally by ID.", "Document identity and official references.", ("source_id",)),
    ToolDefinition("get_due_maintenance_items", "Read deterministically calculated due or overdue items.", "Application-calculated status, due points and manufacturer provenance."),
    ToolDefinition("get_upcoming_maintenance_items", "Read deterministically calculated upcoming items.", "Reminder policy is distinct from manufacturer intervals."),
    ToolDefinition("get_vehicle_profile", "Read the vehicle profile available at assessment.", "Vehicle identity, specifications and reported mileage; no external lookup."),
    ToolDefinition("get_engine_observations", "Read available engine symptom and operating-state observations.", "Engine RPM, vibration, noise, warning and operating-state evidence."),
    ToolDefinition("get_engine_recent_history", "Read the latest engine observations, in chronological order; default limit 5.", "Most recent engine evidence with original timestamps and IDs.", ("limit",)),
    ToolDefinition("get_coolant_history", "Read coolant temperature observations through the cutoff.", "Coolant temperatures with units, timestamps and source IDs."),
    ToolDefinition("get_cooling_summary", "Read compact arithmetic trends in coolant temperature.", "Count, endpoints and times, extrema, change and supporting observation IDs per unit; raw readings are in coolant history."),
    ToolDefinition("get_battery_voltage_history", "Read battery voltage observations through the cutoff.", "Battery voltage readings with original evidence IDs."),
    ToolDefinition("get_starting_voltage_summary", "Summarize voltage readings paired with a starting operating state at the same timestamp.", "Starting voltage statistics and the voltage/state evidence supporting them; no battery diagnosis."),
    ToolDefinition("get_electrical_observations", "Read general electrical evidence independently of starting behavior.", "Charging/accessory voltage, electrical warnings and reported electrical symptoms."),
    ToolDefinition("get_tire_pressure_history", "Read individual tire pressure observations in chronological order, optionally for one wheel.", "Raw per-wheel readings with values, units, timestamps and observation IDs for detailed sequence inspection.", ("wheel",)),
    ToolDefinition("get_tire_pressure_summary", "Read compact tire pressure trends by wheel, optionally for one wheel.", "Counts, first and last values and times, extrema, changes and supporting observation IDs; no raw reading list or pressure verdict.", ("wheel",)),
    ToolDefinition("get_fuel_consumption_history", "Read recorded fuel consumption over time.", "Consumption measurements with their original units and timestamps."),
    ToolDefinition("get_trip_duration_history", "Read average trip duration observations.", "Recorded trip-duration context; no inferred causes."),
    ToolDefinition("get_idle_time_history", "Read observed idle-time ratios.", "Idle-time context with original timestamps and evidence IDs."),
    ToolDefinition("get_fuel_usage_summary", "Read compact trends in consumption, trip duration and idle time.", "Count, endpoints and times, extrema, change and source IDs per measurement and unit; detailed readings remain in the history tools."),
    ToolDefinition("get_service_history", "Read services recorded by the assessment cutoff; optionally filter exact service type.", "Service records, dates, odometers and notes; missing history is not proof of no servicing.", ("service_type",)),
    ToolDefinition("get_latest_service_record", "Read the most recent recorded service, optionally of an exact type.", "One dated service record with its record ID.", ("service_type",)),
    ToolDefinition("get_maintenance_odometer_context", "Read reported current mileage and available service odometers.", "Mileage and service records only; no maintenance interval or due-date calculation."),
    ToolDefinition("get_diagnostic_code_history", "Read previously recorded diagnostic codes without decoding them.", "Code, observation date, source and recorded active flag; no DTC meanings."),
    ToolDefinition("get_active_diagnostic_codes", "Read code records marked active in the supplied as-of context.", "Recorded active flags are not a live scan or a reconstructed historical state."),
    ToolDefinition("get_trip_readiness_evidence", "Gather available profile, tire, battery, cooling and service evidence for a trip discussion.", "Partial evidence with explicit missing categories; no readiness verdict."),
)
TOOL_CATALOG = MappingProxyType({item.tool_id: item for item in _DEFINITIONS})


@dataclass(frozen=True)
class ToolResult:
    tool_id: str
    success: bool
    evidence_ids: tuple[str, ...]
    data: dict
    error: str | None = None


def list_tools() -> tuple[ToolDefinition, ...]:
    return tuple(TOOL_CATALOG[key] for key in sorted(TOOL_CATALOG))


def _arguments_valid(definition: ToolDefinition, arguments: object) -> bool:
    if not isinstance(arguments, dict) or set(arguments) - set(definition.argument_names):
        return False
    for name, value in arguments.items():
        if name == "wheel" and (not isinstance(value, str) or value not in WHEELS):
            return False
        if name == "limit" and (type(value) is not int or not 1 <= value <= 100):
            return False
        if name in {"service_type", "rule_id", "source_id"} and (not isinstance(value, str) or not value.strip()):
            return False
        if name == "query" and (not isinstance(value, str) or not value.strip() or len(value) > 240):
            return False
        if name == "top_k" and (type(value) is not int or not 1 <= value <= 5):
            return False
    if definition.tool_id == "search_manufacturer_manual" and "query" not in arguments:
        return False
    return True


def _visible(timestamp: datetime, cutoff: datetime) -> bool:
    if timestamp.utcoffset() is None:
        raise ValueError("Context timestamps must be timezone-aware")
    return timestamp <= cutoff


def _public_observations(snapshot: FrozenEvidenceSnapshot, context: VehicleContext | None) -> tuple[Observation, ...]:
    by_id = {}
    for item in (*snapshot.observations, *(context.observations if context else ())):
        if not _visible(item.timestamp, snapshot.assessment_at):
            continue
        if item.observation_id in by_id and by_id[item.observation_id] != item:
            raise ValueError("Conflicting observation ID")
        by_id[item.observation_id] = item
    return tuple(sorted(by_id.values(), key=lambda item: (item.timestamp, item.observation_id)))


def _observation_data(items: tuple[Observation, ...], summarize: bool = False,
                      *, include_observations: bool = True) -> dict:
    data = {}
    if include_observations:
        data["observations"] = [
            {**asdict(item), "timestamp": item.timestamp.isoformat(), "source": item.source.value}
            for item in items
        ]
    if summarize:
        groups = {}
        for item in items:
            if type(item.value) in (int, float) and isfinite(item.value):
                groups.setdefault((item.name, item.unit), []).append(item)
        data["summaries"] = [
            {
                "name": name, "unit": unit, "count": len(group),
                "first": group[0].value, "last": group[-1].value,
                "minimum": min(item.value for item in group),
                "maximum": max(item.value for item in group),
                "change": group[-1].value - group[0].value,
                "evidence_ids": [item.observation_id for item in group],
            }
            for (name, unit), group in groups.items()
        ]
    return data


def _service_records(context: VehicleContext | None, cutoff: datetime, service_type: str | None = None) -> list[dict]:
    records = []
    for item in context.maintenance_records if context else ():
        if _visible(item.performed_at, cutoff) and (service_type is None or item.service_type == service_type):
            records.append({**asdict(item), "performed_at": item.performed_at.isoformat()})
    return sorted(records, key=lambda item: (datetime.fromisoformat(item["performed_at"]), item["record_id"]))


def _code_records(context: VehicleContext | None, cutoff: datetime, active_only: bool) -> list[dict]:
    records = []
    for item in context.diagnostic_codes if context else ():
        if not _visible(item.observed_at, cutoff) or (active_only and not item.active):
            continue
        # The existing contract has no record ID. Derive a stable evidence reference.
        identity = json.dumps([context.profile.vehicle_id, item.code, item.observed_at.isoformat(), item.source])
        evidence_id = "code:" + sha256(identity.encode()).hexdigest()
        records.append({**asdict(item), "observed_at": item.observed_at.isoformat(), "evidence_id": evidence_id})
    return sorted(records, key=lambda item: (datetime.fromisoformat(item["observed_at"]), item["evidence_id"]))


def execute_tool(
    tool_id: str,
    arguments: dict,
    snapshot: FrozenEvidenceSnapshot,
    vehicle_context: VehicleContext | None = None,
    *,
    allowed_tool_ids: tuple[str, ...] = (),
    maintenance_request=None,
    manual_index=None,
    manual_market=None,
) -> ToolResult:
    """Execute an explicitly allowed tool over public evidence only.

    The application supplies the allowlist, not the planner. VehicleContext must
    represent stored knowledge as of the snapshot cutoff: these contracts do not
    model later edits to older records. Future-dated records are filtered here.
    Result dictionaries are detached copies; modifying them cannot alter inputs.
    """
    def failure(error: str) -> ToolResult:
        return ToolResult(tool_id, False, (), {}, error)

    if not isinstance(tool_id, str) or tool_id not in TOOL_CATALOG:
        return failure("unknown_tool")
    if tool_id not in allowed_tool_ids:
        return failure("tool_not_allowed")
    definition = TOOL_CATALOG[tool_id]
    if not definition.read_only:
        return failure("tool_not_read_only")
    if not _arguments_valid(definition, arguments):
        return failure("invalid_arguments")
    if type(snapshot) is not FrozenEvidenceSnapshot:
        return failure("invalid_snapshot")
    if vehicle_context is not None and (
        type(vehicle_context) is not VehicleContext or vehicle_context.profile != snapshot.profile
    ):
        return failure("context_profile_mismatch")

    try:
        if tool_id == "search_manufacturer_manual":
            if manual_index is None:
                return failure("unavailable_manufacturer_knowledge")
            from carmind.manufacturer_manual import ManualIndex
            from carmind.manufacturer_ingestion import VehicleManualIndex
            if not isinstance(manual_index, (ManualIndex, VehicleManualIndex)):
                return failure("unavailable_manufacturer_knowledge")
            hits = manual_index.search(arguments["query"], snapshot.profile,
                                       market=manual_market, top_k=arguments.get("top_k", 3))
            if not hits:
                return failure("unavailable_manufacturer_knowledge")
            source_map = ({manual_index.source.source_id: manual_index.source}
                          if isinstance(manual_index, ManualIndex) else manual_index.sources)
            sources = [_manual_source_payload(source_map[source_id])
                       for source_id in dict.fromkeys(hit["source_id"] for hit in hits)]
            return ToolResult(tool_id, True, tuple(hit["evidence_id"] for hit in hits),
                              {"source": sources[0], "sources": sources,
                               "hits": list(hits)})
        if tool_id in MAINTENANCE_TOOLS:
            return _maintenance_tool(tool_id, arguments, snapshot, vehicle_context, maintenance_request)
        if tool_id == "get_vehicle_profile":
            return ToolResult(tool_id, True, (snapshot.profile.vehicle_id,), {"profile": asdict(snapshot.profile)})
        if tool_id in ("get_service_history", "get_latest_service_record", "get_maintenance_odometer_context"):
            records = _service_records(vehicle_context, snapshot.assessment_at, arguments.get("service_type"))
            if tool_id == "get_latest_service_record":
                records = records[-1:]
            data = {"records": records}
            ids = [item["record_id"] for item in records]
            if tool_id == "get_maintenance_odometer_context":
                data["mileage_km"] = snapshot.profile.mileage_km
                if snapshot.profile.mileage_km is not None:
                    ids.append(snapshot.profile.vehicle_id)
            if not ids:
                return failure("unavailable_evidence")
            return ToolResult(tool_id, True, tuple(ids), data)
        if tool_id in ("get_diagnostic_code_history", "get_active_diagnostic_codes"):
            records = _code_records(vehicle_context, snapshot.assessment_at, tool_id == "get_active_diagnostic_codes")
            if not records:
                return failure("unavailable_evidence")
            return ToolResult(tool_id, True, tuple(item["evidence_id"] for item in records), {"records": records})

        observations = _public_observations(snapshot, vehicle_context)
        if tool_id == "get_trip_readiness_evidence":
            categories = {"tires": TIRE_NAMES, "battery": ("battery_voltage",), "cooling": ("coolant_temperature",)}
            data = {"profile": asdict(snapshot.profile), "missing_categories": []}
            ids = [snapshot.profile.vehicle_id]
            for category, names in categories.items():
                selected = tuple(item for item in observations if item.name in names)
                data[category] = _observation_data(selected)
                ids.extend(item.observation_id for item in selected)
                if not selected:
                    data["missing_categories"].append(category)
            records = _service_records(vehicle_context, snapshot.assessment_at)
            data["service_records"] = records
            ids.extend(item["record_id"] for item in records)
            if not records:
                data["missing_categories"].append("service_history")
            return ToolResult(tool_id, True, tuple(ids), data)

        names_by_tool = {
            "get_engine_observations": ENGINE_NAMES,
            "get_engine_recent_history": ENGINE_NAMES,
            "get_coolant_history": ("coolant_temperature",),
            "get_cooling_summary": ("coolant_temperature",),
            "get_battery_voltage_history": ("battery_voltage",),
            "get_starting_voltage_summary": ("battery_voltage", "operating_state"),
            "get_electrical_observations": ELECTRICAL_NAMES,
            "get_tire_pressure_history": TIRE_NAMES,
            "get_tire_pressure_summary": TIRE_NAMES,
            "get_fuel_consumption_history": ("fuel_consumption",),
            "get_trip_duration_history": ("average_trip_duration",),
            "get_idle_time_history": ("idle_time_ratio",),
            "get_fuel_usage_summary": FUEL_NAMES,
        }
        selected = tuple(item for item in observations if item.name in names_by_tool[tool_id])
        if "wheel" in arguments:
            selected = tuple(item for item in selected if item.name == f"{arguments['wheel']}_tire_pressure")
        if tool_id == "get_engine_recent_history":
            selected = selected[-arguments.get("limit", 5):]
        if tool_id == "get_starting_voltage_summary":
            starting_times = {item.timestamp for item in selected if item.name == "operating_state" and item.value == "starting"}
            voltage_times = {item.timestamp for item in selected if item.name == "battery_voltage"}
            paired_times = starting_times & voltage_times
            selected = tuple(item for item in selected if item.timestamp in paired_times)
        if not selected:
            return failure("unavailable_evidence")
        compact_summary = tool_id in {"get_tire_pressure_summary", "get_cooling_summary", "get_fuel_usage_summary"}
        data = _observation_data(selected, tool_id.endswith("summary"),
                                 include_observations=not compact_summary)
        if compact_summary:
            # The summary cites the same cutoff-visible observations without
            # repeating their full records; history remains the raw view.
            if not data["summaries"]:
                return failure("unavailable_evidence")
            for summary in data["summaries"]:
                support = set(summary["evidence_ids"])
                group = [item for item in selected if item.observation_id in support]
                summary["first_at"] = group[0].timestamp.isoformat()
                summary["last_at"] = group[-1].timestamp.isoformat()
            supported_ids = {identifier for summary in data["summaries"] for identifier in summary["evidence_ids"]}
            selected = tuple(item for item in selected if item.observation_id in supported_ids)
        return ToolResult(tool_id, True, tuple(item.observation_id for item in selected), data)
    except (ValueError, sqlite3.Error, OSError):
        return failure("invalid_evidence")


MAINTENANCE_TOOLS = frozenset({"get_manufacturer_maintenance_schedule", "get_manufacturer_maintenance_rule",
                             "get_maintenance_source_reference", "get_due_maintenance_items", "get_upcoming_maintenance_items"})


@dataclass(frozen=True)
class MaintenanceRequest:
    """Application-owned inputs. The planner cannot supply or change these."""
    pack: KnowledgePack
    profile: VehicleKnowledgeProfile
    operating_condition: str = "UNKNOWN"
    policy: ReminderPolicy | None = None
    in_service_date: date | None = None


def maintenance_results(request, snapshot, context):
    from carmind.maintenance import evaluate_maintenance, ReminderPolicy
    from carmind.manufacturer_knowledge import KnowledgePack, VehicleKnowledgeProfile
    if type(request) is not MaintenanceRequest or type(request.pack) is not KnowledgePack or type(request.profile) is not VehicleKnowledgeProfile:
        raise ValueError("Invalid manufacturer knowledge request")
    profile = snapshot.profile
    identity = request.profile
    if (profile.make, profile.model, profile.year, profile.engine) != (identity.manufacturer, identity.model, identity.model_year, identity.engine_variant):
        raise ValueError("Vehicle identity does not match knowledge request")
    return evaluate_maintenance(request.pack, identity, profile.vehicle_id, snapshot.assessment_at,
                                profile.mileage_km, context.maintenance_records if context else (),
                                request.operating_condition, request.policy or ReminderPolicy(), request.in_service_date)


def _maintenance_tool(tool_id, arguments, snapshot, context, request):
    if request is None:
        return ToolResult(tool_id, False, (), {}, "unavailable_manufacturer_knowledge")
    results = maintenance_results(request, snapshot, context)
    pack = request.pack
    data = {}
    ids = []
    if tool_id in {"get_manufacturer_maintenance_schedule", "get_manufacturer_maintenance_rule"}:
        rules = [r for r in pack.rules if "rule_id" not in arguments or r.rule_id == arguments["rule_id"]]
        data = {"profile": asdict(pack.profile), "applicable": pack.matches(request.profile), "poc_only": pack.poc_only,
                "rules": [asdict(r) for r in rules]}
        ids = [r.rule_id for r in rules] + [r.source_id for r in rules]
    elif tool_id == "get_maintenance_source_reference":
        sources = [s for s in pack.sources if "source_id" not in arguments or s.source_id == arguments["source_id"]]
        data = {"sources": [asdict(s) for s in sources]}
        ids = [s.source_id for s in sources]
    else:
        statuses = {"DUE", "OVERDUE"} if tool_id == "get_due_maintenance_items" else {"UPCOMING"}
        items = [r for r in results if r.status in statuses]
        # UNKNOWN stays visible even when there are no due/upcoming results.
        data = {"items": [asdict(r) for r in items], "unknown_items": [asdict(r) for r in results if r.status == "UNKNOWN"]}
        ids = [r.reminder_id for r in results if r.status in statuses | {"UNKNOWN"}]
    data = json.loads(json.dumps(data, default=str))
    if ("rule_id" in arguments or "source_id" in arguments) and not ids:
        return ToolResult(tool_id, False, (), {}, "unknown_manufacturer_reference")
    return ToolResult(tool_id, True, tuple(sorted(set(ids))), data)
