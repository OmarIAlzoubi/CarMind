"""Transport-independent ownership application using the existing reasoning stack."""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from hashlib import sha256
import json
import sqlite3
from time import perf_counter
from uuid import uuid4

from carmind.assessment import ValidatedAssessment
from carmind.contracts import MaintenanceRecord, SafetyDisposition, UserMessage, VehicleProfile
from carmind.evidence import FrozenEvidenceSnapshot
from carmind.maintenance import reminder_events
from carmind.manufacturer_knowledge import VehicleKnowledgeProfile
from carmind.ownership import (CommandProposal, CommandType, MutationResult, OwnershipContext,
                               OwnershipCommand, OdometerConflict, StaleProposal, distance_km, identifier, utc)
from carmind.proactive import ProactiveService
from carmind.response import render_user_response
from carmind.routing import ExecutionMode, run_assessment
from carmind.safety import SafetyDecision, evaluate_safety, retain_unresolved_stop
from carmind.storage import OwnershipStore, encode
from carmind.tools import maintenance_results


@dataclass
class AppTrace:
    owner_id: str
    session_id: str
    vehicle_id: str | None = None
    routing: object | None = None
    planner: object | None = None
    ownership_context_characters: int = 0
    proposed_command_ids: list[str] = field(default_factory=list)
    applied_command_ids: list[str] = field(default_factory=list)
    rejected_command_ids: list[str] = field(default_factory=list)
    reminder_changes: list[str] = field(default_factory=list)
    maintenance_refreshed: bool = False
    replayed: bool = False
    current_safety_disposition: str | None = None
    prior_stop_disposition: str | None = None
    effective_safety_disposition: str | None = None
    stop_guidance_applied: bool = False
    error_category: str | None = None
    elapsed_seconds: float = 0


@dataclass(frozen=True)
class TurnResult:
    response: str
    status: str
    assessment: ValidatedAssessment | None = None
    safety: SafetyDecision | None = None
    proposed_commands: tuple[CommandProposal, ...] = ()
    applied_commands: tuple[MutationResult, ...] = ()
    rejected_commands: tuple[MutationResult, ...] = ()
    reminder_changes: tuple[str, ...] = ()
    maintenance_state: tuple = ()
    trace: AppTrace | None = None
    maintenance_applicability: str = "UNKNOWN"


class CarMindApp:
    """A local service, not an authentication boundary. No network clients created.

    Providers/routers and vetted manufacturer schedules are injected by trusted
    code. Interfaces must authenticate owners before calling this local MVP.
    """

    def __init__(self, store: OwnershipStore, provider, *, mode=ExecutionMode.FULL,
                 router=None, approved_schedules=(), allow_test_schedules=False,
                 max_model_calls=4, manual_index=None):
        if type(max_model_calls) is not int or max_model_calls < 0:
            raise ValueError("max_model_calls must be a nonnegative integer")
        self.store, self.provider = store, provider
        self.mode, self.router = ExecutionMode(mode), router
        self.schedules = tuple(approved_schedules)
        self.allow_test_schedules = allow_test_schedules
        self.max_model_calls = max_model_calls
        self.manual_index = manual_index
        self.proactive = ProactiveService(self)

    def evaluate_proactive_state(self, *, now, owner_id=None, vehicle_id=None):
        """Explicit scheduler/operator entry point; it never sends messages."""
        return self.proactive.run(now=now, owner_id=owner_id, vehicle_id=vehicle_id)

    def create_owner(self, *, now, owner_id=None):
        owner_id = identifier(owner_id or str(uuid4()))
        with self.store.transaction():
            self.store.create_owner(owner_id, now)
        return owner_id

    def add_vehicle(self, owner_id, profile: VehicleProfile, *, now, trim=None, market=None, nickname=None, unit="km"):
        self._validate_vehicle(profile, trim=trim, market=market, nickname=nickname, unit=unit)
        with self.store.transaction():
            self.store.add_vehicle(owner_id, profile, now, trim=trim, market=market, nickname=nickname, unit=unit)
            if profile.mileage_km is not None:
                self.store.record_odometer(str(uuid4()), profile.vehicle_id, profile.mileage_km, profile.mileage_km,
                                           "km", now, now, None)
        return profile.vehicle_id

    @staticmethod
    def _validate_vehicle(profile, *, trim=None, market=None, nickname=None, unit="km"):
        if type(profile) is not VehicleProfile or type(profile.year) is not int:
            raise ValueError("Invalid vehicle profile.")
        for value in (profile.make, profile.model):
            if not isinstance(value, str) or not value.strip() or len(value) > 120:
                raise ValueError("Vehicle make and model are required.")
        for value in (profile.engine, profile.vin, trim, market, nickname):
            if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 120):
                raise ValueError("Invalid vehicle detail.")
        distance_km(profile.mileage_km if profile.mileage_km is not None else 0, "km")
        distance_km(0, unit)

    def propose_vehicle(self, owner_id, session_id, message, *, now, make, model, year,
                        odometer=None, unit=None, trim=None, market=None, nickname=None, engine=None):
        """Persist a reviewed vehicle draft; semantic extraction never writes a vehicle."""
        now = utc(now)
        if (odometer is None) != (unit is None):
            raise ValueError("Vehicle odometer needs an explicit unit.")
        profile = VehicleProfile(str(uuid4()), make, model, year, None, engine)
        self._validate_vehicle(profile, trim=trim, market=market, nickname=nickname, unit=unit or "km")
        if odometer is not None:
            distance_km(odometer, unit)
        self.store.session(session_id, owner_id)
        prior = self.store.vehicle_draft_for_message(session_id, message.message_id)
        if prior:
            return prior["id"], json.loads(prior["payload"])
        payload = {"vehicle_id": profile.vehicle_id, "make": make, "model": model, "year": year,
                   "engine": engine, "trim": trim, "market": market, "nickname": nickname,
                   "odometer": odometer, "unit": unit}
        with self.store.transaction():
            self.store.save_vehicle_draft("vehicle-" + str(uuid4()), owner_id, session_id,
                                          message.message_id, payload, now + timedelta(days=1))
        row = self.store.vehicle_draft_for_message(session_id, message.message_id)
        return row["id"], payload

    def confirm_vehicle(self, owner_id, session_id, proposal_id, message, *, now):
        """Confirm and select one exact draft in the same transaction."""
        now = utc(now)
        with self.store.transaction():
            row = self.store.vehicle_draft(proposal_id, session_id)
            if row is None or row["owner_id"] != owner_id:
                raise ValueError("The vehicle proposal is not available in this session.")
            if row["state"] == "APPLIED":
                return row["result_vehicle_id"], False
            if row["state"] != "PENDING" or now > datetime.fromisoformat(row["expires_at"]):
                raise ValueError("The vehicle proposal has expired.")
            data = json.loads(row["payload"])
            profile = VehicleProfile(data["vehicle_id"], data["make"], data["model"], data["year"],
                                     None, data.get("engine"))
            self._validate_vehicle(profile, trim=data.get("trim"), market=data.get("market"),
                                   nickname=data.get("nickname"), unit=data.get("unit") or "km")
            reading = data.get("odometer")
            if reading is not None:
                distance_km(reading, data["unit"])
            self.store.add_vehicle(owner_id, profile, now, trim=data.get("trim"), market=data.get("market"),
                                   nickname=data.get("nickname"), unit=data.get("unit") or "km")
            if reading is not None:
                self.store.record_odometer(proposal_id + "-reading", profile.vehicle_id,
                                           distance_km(reading, data["unit"]), reading, data["unit"],
                                           now, now, row["message_id"])
            self.store.select_vehicle(session_id, owner_id, profile.vehicle_id, now)
            self._refresh(owner_id, profile.vehicle_id, message, now)
            self.store.finish_vehicle_draft(proposal_id, profile.vehicle_id)
        return profile.vehicle_id, True

    def start_session(self, owner_id, *, now, vehicle_id=None, session_id=None):
        session_id = identifier(session_id or str(uuid4()))
        with self.store.transaction():
            self.store.create_session(session_id, owner_id, now, vehicle_id)
        return session_id

    def select_vehicle(self, owner_id, session_id, vehicle_id, *, now):
        """Explicit interface selection, including sessions without an active car."""
        with self.store.transaction():
            self._vehicle_at(owner_id, vehicle_id, now)
            self.store.select_vehicle(session_id, owner_id, vehicle_id, now)

    def _vehicle_at(self, owner_id, vehicle_id, now):
        vehicle = self.store.vehicle(owner_id, vehicle_id, now)
        if utc(now) < vehicle.updated_at:
            raise ValueError("Application time precedes current vehicle state.")
        return vehicle

    def _maintenance_request(self, vehicle):
        p = vehicle.profile
        if vehicle.market is None:
            return None
        matches = []
        for request in self.schedules:
            pack = request.pack
            test_only = all(s.source_type == "TEST_ONLY_NON_PRODUCTION_FICTIONAL" for s in pack.sources)
            if pack.poc_only and not (self.allow_test_schedules and test_only):
                continue
            if test_only and not self.allow_test_schedules:
                continue
            # Version is selected by trusted configuration; identity comes from ownership.
            actual = VehicleKnowledgeProfile(p.make, p.model, p.year, vehicle.market, p.engine,
                                             knowledge_version=pack.profile.knowledge_version)
            if request.profile == actual and pack.matches(actual):
                matches.append(request)
        # Ambiguous configured schedules fail closed, rather than choosing a guessed version.
        return matches[0] if len(matches) == 1 else None

    def _refresh(self, owner_id, vehicle_id, message, now):
        vehicle = self._vehicle_at(owner_id, vehicle_id, now)
        context = self.store.vehicle_context(owner_id, vehicle_id, now)
        snapshot = FrozenEvidenceSnapshot("ownership-refresh", vehicle.profile, message, now)
        request = self._maintenance_request(vehicle)
        state = maintenance_results(request, snapshot, context) if request else ()
        old = {r["id"]: r for r in self.store.reminders(vehicle_id)}
        active = set()
        changes = []
        for reminder in reminder_events(state):
            facts = json.loads(encode(asdict(reminder)))
            # Cycle identity excludes the changing UPCOMING/DUE/OVERDUE status and
            # due-point changes. An acknowledged cycle remains acknowledged.
            key = (vehicle_id, reminder.source_id, reminder.manufacturer_rule_id, reminder.last_service_id)
            rid = "reminder-" + sha256(encode(key).encode()).hexdigest()[:24]
            active.add(rid)
            prior = old.get(rid)
            lifecycle = "ACKNOWLEDGED" if prior and prior["lifecycle"] == "ACKNOWLEDGED" else "ACTIVE"
            comparable = {k: v for k, v in facts.items() if k not in ("generated_at", "reminder_id")}
            previous = {k: v for k, v in prior["facts"].items() if k not in ("generated_at", "reminder_id")} if prior else None
            if comparable != previous or prior["lifecycle"] != lifecycle:
                self.store.save_reminder(rid, vehicle_id, facts, lifecycle, "deterministic_maintenance", now)
                changes.append(rid)
        for rid, prior in old.items():
            if rid not in active and prior["lifecycle"] != "COMPLETED":
                # COMPLETED is a lifecycle closure, not proof that service occurred.
                reason = "source_unavailable" if request is None else "no_longer_due_or_cycle_replaced"
                self.store.save_reminder(rid, vehicle_id, prior["facts"], "COMPLETED", reason, now)
                changes.append(rid)
        self.store.touch_vehicle(vehicle_id, now)
        return request, state, tuple(changes)

    def _previous_stop(self, vehicle_id):
        row = self.store.previous_stop(vehicle_id)
        if not row:
            return None
        data = json.loads(row["decision"])
        data["disposition"] = SafetyDisposition(data["disposition"])
        for key in ("rule_ids", "evidence_ids", "limitations", "user_limitations"):
            data[key] = tuple(data[key])
        return SafetyDecision(**data)

    def read_maintenance(self, owner_id, vehicle_id, *, now):
        """Verified applicable schedule and deterministic state for product reads."""
        vehicle = self._vehicle_at(owner_id, vehicle_id, now)
        request = self._maintenance_request(vehicle)
        if request is None:
            return None, ()
        context = self.store.vehicle_context(owner_id, vehicle_id, now)
        snapshot = FrozenEvidenceSnapshot("maintenance-read", vehicle.profile,
                                          UserMessage("maintenance-read", "Maintenance status", now), now)
        return request, maintenance_results(request, snapshot, context)

    def unresolved_safety(self, owner_id, vehicle_id, *, now):
        """Read an existing deterministic stop constraint without reevaluating it."""
        self._vehicle_at(owner_id, vehicle_id, now)
        return self._previous_stop(vehicle_id)

    def ownership_context(self, owner_id, session_id, vehicle_id, now, request, related_event_id=None):
        vehicle = self.store.vehicle(owner_id, vehicle_id, now)
        turns = self.store.recent_turns(session_id, vehicle_id)
        previous = next((json.loads(t["summary"]) for t in reversed(turns) if t["summary"]), None)
        metadata = {"vehicle_id": vehicle_id, "nickname": vehicle.nickname, "trim": vehicle.trim,
                    "market": vehicle.market, "odometer_unit": vehicle.odometer_unit}
        reminders = tuple({"reminder_id": r["id"], "lifecycle": r["lifecycle"],
                           **{k: r["facts"][k] for k in ("maintenance_item", "status", "due_date", "due_odometer_km", "source_id", "manufacturer_rule_id")}}
                          for r in self.store.reminders(vehicle_id, active_only=True)[:8])
        services = tuple(asdict(r) for r in self.store.service_records(vehicle_id, now, limit=8))
        event = self.store.proactive_event(related_event_id, owner_id, vehicle_id) if related_event_id else None
        active_event = ({key: event[key] for key in ("id", "event_type", "maintenance_item", "due_km", "due_date",
                                                   "source_type", "source_id", "rule_id", "source_page")}
                        if event and event["status"] != "RESOLVED" else None)
        return OwnershipContext(metadata, services, "APPLICABLE" if request else "UNKNOWN", reminders,
                                tuple({"text": t["text"], "timestamp": t["timestamp"], "status": t["status"]} for t in turns),
                                previous, active_event=active_event)

    def _odometer_write(self, event_id, vehicle_id, args, now, message_id):
        occurred = datetime.fromisoformat(args["occurred_at"])
        if occurred > now:
            raise ValueError("A future odometer reading cannot be recorded.")
        km = distance_km(args["reading"], args["unit"])
        for old in self.store.odometer_events(vehicle_id):
            time = datetime.fromisoformat(old["occurred_at"])
            if (time <= occurred and old["km"] > km) or (time >= occurred and old["km"] < km):
                raise OdometerConflict("Odometer conflict: this reading contradicts the accepted timeline.")
        self.store.record_odometer(event_id, vehicle_id, km, args["reading"], args["unit"], occurred, now, message_id)

    def _apply(self, row, command, owner_id, session_id, now):
        args, kind, vehicle_id = command.arguments, command.kind, row["vehicle_id"]
        self.store.vehicle(owner_id, vehicle_id, now)
        entity = row["id"] + "-event"
        if kind == CommandType.UPDATE_ODOMETER:
            if "supersedes_id" in args:
                # A service-linked reading must be corrected through that service.
                if args["supersedes_id"].endswith("-reading"):
                    raise ValueError("Correct the service record associated with this reading.")
                self.store.supersede("odometer_events", args["supersedes_id"], vehicle_id, entity, now)
            self._odometer_write(entity, vehicle_id, args, now, row["message_id"])
            fields = ("odometer_history", "current_odometer")
        elif kind == CommandType.RECORD_SERVICE:
            performed = datetime.fromisoformat(args["performed_at"])
            if performed > now:
                raise ValueError("A future service cannot be recorded as completed.")
            if "supersedes_id" in args:
                old_id = args["supersedes_id"]
                self.store.supersede("services", old_id, vehicle_id, entity, now)
                if any(r["id"] == old_id + "-reading" for r in self.store.odometer_events(vehicle_id)):
                    self.store.supersede("odometer_events", old_id + "-reading", vehicle_id,
                                         entity + "-reading" if "odometer" in args else None, now)
            km = distance_km(args["odometer"], args["unit"]) if "odometer" in args else None
            record = MaintenanceRecord(entity, args["service_type"], performed, km, args.get("notes"))
            if km is not None:
                self._odometer_write(entity + "-reading", vehicle_id,
                                     {"reading": args["odometer"], "unit": args["unit"], "occurred_at": args["performed_at"]}, now, row["message_id"])
            self.store.record_service(entity, vehicle_id, record, now, row["message_id"])
            fields = ("service_history", "odometer_history") if km is not None else ("service_history",)
        elif kind == CommandType.SET_VEHICLE_FIELD:
            self.store.set_vehicle_field(vehicle_id, args["field"], args["value"], now)
            entity, fields = vehicle_id, (args["field"],)
        elif kind == CommandType.SELECT_VEHICLE:
            self._vehicle_at(owner_id, args["vehicle_id"], now)
            self.store.select_vehicle(session_id, owner_id, args["vehicle_id"], now)
            entity, fields = args["vehicle_id"], ("active_vehicle",)
        elif kind == CommandType.ACKNOWLEDGE_REMINDER:
            reminder = next((r for r in self.store.reminders(vehicle_id, active_only=True) if r["id"] == args["reminder_id"]), None)
            if reminder is None:
                raise ValueError("This reminder is no longer active.")
            self.store.save_reminder(reminder["id"], vehicle_id, reminder["facts"], "ACKNOWLEDGED", "owner_acknowledged", now)
            entity, fields = reminder["id"], ("lifecycle",)
        elif kind == CommandType.CREATE_OWNER_REMINDER:
            entity = self.proactive.add_owner_reminder(owner_id, vehicle_id, args["maintenance_item"], now=now,
                after_km=args.get("after_km"), after_months=args.get("after_months"), within_transaction=True)
            fields = ("owner_reminder",)
        else:
            raise ValueError("Unsupported ownership command.")
        return MutationResult(True, "confirmed_owner_fact", entity, fields, now)

    @staticmethod
    def _proposal_from_row(row):
        data = json.loads(row["payload"])
        return CommandProposal(row["id"], OwnershipCommand(CommandType(data["kind"]), data["arguments"], data["certainty"], data.get("owner_quote", "")),
                               datetime.fromisoformat(row["expires_at"]))

    def _confirm(self, proposal_id, owner_id, session_id, message, now, trace, safety):
        replayed = False
        with self.store.transaction():
            # Read and write under one immediate transaction so concurrent
            # confirmations cannot both observe PENDING and apply the same command.
            row = self.store.proposal(proposal_id, session_id)
            if row is None:
                raise ValueError("The requested change is not available in this session.")
            if row["result"]:
                raw = json.loads(row["result"])
                raw["timestamp"] = datetime.fromisoformat(raw["timestamp"])
                raw["changed_fields"] = tuple(raw["changed_fields"])
                result = MutationResult(**raw)
                replayed = True
            else:
                if row["state"] != "PENDING" or now > datetime.fromisoformat(row["expires_at"]):
                    raise ValueError("This proposed change has expired. Please describe it again.")
                command = self._proposal_from_row(row).command
                if command.certainty != "explicit":
                    raise ValueError("Uncertain ownership information requires a new explicit statement.")
                session = self.store.session(session_id, owner_id)
                if session["active_vehicle_id"] != row["vehicle_id"]:
                    raise StaleProposal("The selected vehicle changed. Please review and propose the change again.")
                stored = json.loads(row["payload"])
                precondition = stored.get("preconditions", {}).get("profile_field")
                if precondition:
                    vehicle = self.store.vehicle(owner_id, row["vehicle_id"], now)
                    current = {**asdict(vehicle.profile), "trim": vehicle.trim, "market": vehicle.market,
                               "nickname": vehicle.nickname}.get(precondition["field"])
                    if current != precondition["expected_value"]:
                        raise StaleProposal("That vehicle detail changed while you reviewed the proposal. Please describe the update again.")
                result = self._apply(row, command, owner_id, session_id, now)
                selected_vehicle = self.store.session(session_id, owner_id)["active_vehicle_id"]
                if selected_vehicle != row["vehicle_id"]:
                    selected = self.store.vehicle(owner_id, selected_vehicle, now)
                    selected_snapshot = FrozenEvidenceSnapshot("selected-vehicle", selected.profile, message, now)
                    safety = retain_unresolved_stop(evaluate_safety(selected_snapshot), self._previous_stop(selected_vehicle))
                _, state, changes = self._refresh(owner_id, selected_vehicle, message, now)
                self.proactive.evaluate_vehicle(owner_id, selected_vehicle, now)
                self.store.finish_proposal(proposal_id, result)
        if replayed:
            trace.replayed = True
            return result, (), (), safety
        trace.vehicle_id = selected_vehicle
        trace.applied_command_ids.append(proposal_id)
        trace.maintenance_refreshed = True
        return result, state, changes, safety

    @staticmethod
    def describe_proposal(command):
        """Display the exact typed values being confirmed; never infer user intent."""
        a = command.arguments
        if command.kind == CommandType.UPDATE_ODOMETER:
            text = f"Record an odometer reading of {a['reading']:g} {a['unit']} at {a['occurred_at']}."
        elif command.kind == CommandType.RECORD_SERVICE:
            text = f"Record {a['service_type'].replace('_', ' ')} service at {a['performed_at']}."
            if "odometer" in a:
                text += f" Odometer: {a['odometer']:g} {a['unit']}."
            if a.get("notes"):
                text += " Note: " + a["notes"]
        elif command.kind == CommandType.SET_VEHICLE_FIELD:
            text = f"Set vehicle {a['field']} to {a['value']}."
        elif command.kind == CommandType.SELECT_VEHICLE:
            text = f"Switch the selected vehicle to {a['vehicle_id']}."
        elif command.kind == CommandType.CREATE_OWNER_REMINDER:
            target = f"in {a['after_km']:g} km" if "after_km" in a else f"after {a['after_months']} months"
            text = f"Create your {a['maintenance_item'].replace('_', ' ')} reminder {target}. This is your reminder, not manufacturer guidance."
        else:
            text = f"Acknowledge maintenance reminder {a['reminder_id']}. This does not record completed service."
        if "supersedes_id" in a:
            text += f" This replaces earlier record {a['supersedes_id']} while preserving its history."
        return text

    @staticmethod
    def _reminder_text(state):
        items = reminder_events(state)
        if not items:
            return ""
        labels = {"UPCOMING": "coming up", "DUE": "due", "OVERDUE": "overdue"}
        lines = []
        for item in items[:3]:
            due = []
            if item.due_odometer_km is not None:
                due.append(f"{item.due_odometer_km:g} km")
            if item.due_date is not None:
                due.append(str(item.due_date))
            lines.append(f"{item.maintenance_item.replace('_', ' ')}: {labels[item.status]} (scheduled at {' / '.join(due)}).")
        return "\n\nMaintenance reminders:\n" + "\n".join(lines)

    def _cited_owned_facts(self, vehicle_id, now, evidence_ids, tool_ids):
        """Present cited stored facts without model prose during an unresolved stop."""
        selected = set(evidence_ids)
        lines = []
        if any(tool in tool_ids for tool in ("get_service_history", "get_latest_service_record")):
            for record in self.store.service_records(vehicle_id, now):
                if record.record_id in selected:
                    text = f"{record.service_type.replace('_', ' ')} service on {record.performed_at.date()}"
                    if record.odometer_km is not None:
                        text += f" at {record.odometer_km:g} km"
                    lines.append("- " + text + ".")
                    if len(lines) == 3:
                        break
        return "From your saved records:\n" + "\n".join(lines) + "\n\n" if lines else ""

    def handle_message(self, owner_id: str, session_id: str, message: UserMessage, *, now,
                       snapshot: FrozenEvidenceSnapshot | None = None, confirmation_id: str | None = None,
                       related_event_id: str | None = None) -> TurnResult:
        """Confirmation is an adapter control, never parsed from chat or model output.

        The adapter must display proposed_commands and confirm the exact proposal
        ID explicitly. A text like 'yes' alone has no mutation authority here.
        """
        started = perf_counter()
        trace = AppTrace(owner_id, session_id)
        safety = None
        valid_now = None
        try:
            now = utc(now)
            valid_now = now
            if type(message) is not UserMessage or len(message.text) > 2000 or utc(message.timestamp) > now:
                raise ValueError("Invalid or future owner message.")
            session = self.store.session(session_id, owner_id)
            if now < datetime.fromisoformat(session["updated_at"]):
                raise ValueError("Application time precedes this session.")
            vehicle_id = session["active_vehicle_id"]
            trace.vehicle_id = vehicle_id
            if vehicle_id is None:
                return TurnResult("Please select one of your vehicles first.", "no_active_vehicle", trace=trace)
            vehicle = self._vehicle_at(owner_id, vehicle_id, now)
            prior_stop = self._previous_stop(vehicle_id)
            trace.prior_stop_disposition = prior_stop.disposition.value if prior_stop else None
            observations = ()
            if snapshot is not None:
                if type(snapshot) is not FrozenEvidenceSnapshot:
                    raise ValueError("Evidence requires a frozen snapshot.")
                p = snapshot.profile
                if (p.vehicle_id, p.make, p.model, p.year, p.engine) != (vehicle_id, vehicle.profile.make, vehicle.profile.model, vehicle.profile.year, vehicle.profile.engine) or snapshot.assessment_at > now:
                    raise ValueError("Evidence does not match this vehicle or time.")
                observations = snapshot.observations
            public = FrozenEvidenceSnapshot("turn-" + message.message_id, vehicle.profile, message, now, observations)
            current_safety = evaluate_safety(public)
            trace.current_safety_disposition = current_safety.disposition.value
            safety = retain_unresolved_stop(current_safety, prior_stop)
            trace.effective_safety_disposition = safety.disposition.value
            with self.store.transaction():
                self.store.expire_proposals(now)
                if safety.disposition == SafetyDisposition.STOP_WHEN_SAFE and (prior_stop is None or observations):
                    self.store.save_stop(vehicle_id, safety, now)
                    self.store.touch_vehicle(vehicle_id, now)
            if confirmation_id is not None:
                result, state, changes, safety = self._confirm(confirmation_id, owner_id, session_id, message, now, trace, safety)
                trace.effective_safety_disposition = safety.disposition.value
                response = "Recorded your confirmed change." if result.applied else "The change was not recorded."
                if trace.replayed:
                    response = "That confirmation was already handled; no duplicate was recorded."
                response += self._reminder_text(state)
                if safety.disposition == SafetyDisposition.STOP_WHEN_SAFE:
                    response += "\n\n" + safety.approved_text
                    trace.stop_guidance_applied = True
                trace.reminder_changes.extend(changes)
                return TurnResult(response, "applied" if result.applied else "rejected", safety=safety,
                                  applied_commands=(result,) if result.applied else (), rejected_commands=() if result.applied else (result,),
                                  maintenance_state=state, reminder_changes=changes, trace=trace,
                                  maintenance_applicability="APPLICABLE" if state else "UNKNOWN")
            previous = self.store.remembered_turn(session_id, message.message_id)
            pending = self.store.proposal_for_message(session_id, message.message_id)
            if previous or pending:
                trace.replayed = True
                proposals = (self._proposal_from_row(pending),) if pending and pending["state"] == "PENDING" else ()
                response = previous["response"] if previous else "This message was already handled."
                if pending and pending["state"] == "APPLIED":
                    response = "This confirmed change was already recorded; no duplicate was added."
                elif pending and pending["state"] != "PENDING":
                    response = "This proposal is no longer available for confirmation."
                if safety.disposition == SafetyDisposition.STOP_WHEN_SAFE and safety.approved_text not in response:
                    response += "\n\n" + safety.approved_text
                if safety.disposition == SafetyDisposition.STOP_WHEN_SAFE:
                    trace.stop_guidance_applied = True
                return TurnResult(response, "replayed", safety=safety, proposed_commands=proposals, trace=trace)
            with self.store.transaction():
                request, state, changes = self._refresh(owner_id, vehicle_id, message, now)
                self.proactive.evaluate_vehicle(owner_id, vehicle_id, now)
            trace.maintenance_refreshed = True
            trace.reminder_changes.extend(changes)
            context = self.store.vehicle_context(owner_id, vehicle_id, now)
            ownership = asdict(self.ownership_context(owner_id, session_id, vehicle_id, now, request,
                                                       related_event_id))
            # Existing initial service_history already exposes the same bounded records.
            ownership.pop("recent_services")
            if ownership["active_event"] is None:
                ownership.pop("active_event")
            trace.ownership_context_characters = len(encode(ownership))
            manual_index = (self.manual_index.for_vehicle(vehicle_id)
                            if hasattr(self.manual_index, "for_vehicle") else self.manual_index)
            run = run_assessment(public, self.provider, context, request, mode=self.mode, router=self.router,
                                 ownership_context=ownership, previous_stop=prior_stop,
                                 max_model_calls=self.max_model_calls, manual_index=manual_index,
                                 manual_market=vehicle.market)
            trace.routing, trace.planner = run.routing, run.planner.trace
            validated, proposal = run.planner.result, run.planner.proposed_command
            clarification = run.planner.clarification_question
            safety = validated.safety
            # Keep the fresh evaluation distinct from the persisted constraint and
            # the effective decision returned to callers.
            trace.current_safety_disposition = evaluate_safety(public, context).disposition.value
            trace.effective_safety_disposition = safety.disposition.value
            proposals, rejected = (), ()
            status = run.planner.trace.completion_status
            summary = None
            with self.store.transaction():
                if clarification is not None:
                    status = "clarification_required"
                    response = clarification + "\nNo car record was changed."
                    if safety.disposition == SafetyDisposition.STOP_WHEN_SAFE:
                        response += "\n\n" + safety.approved_text
                elif proposal is not None:
                    if proposal.certainty == "uncertain":
                        status = "clarification_required"
                        response = "Please confirm what was actually done before I record it. Nothing has been changed."
                        rejected = (MutationResult(False, "uncertain_owner_statement", None, (), now),)
                        trace.rejected_command_ids.append("uncertain_proposal")
                    else:
                        proposal_id = "command-" + str(uuid4())
                        expires = now + timedelta(days=1)
                        preconditions = None
                        if proposal.kind == CommandType.SET_VEHICLE_FIELD:
                            field = proposal.arguments["field"]
                            current = {**asdict(vehicle.profile), "trim": vehicle.trim, "market": vehicle.market,
                                       "nickname": vehicle.nickname}.get(field)
                            preconditions = {"profile_field": {"field": field, "expected_value": current}}
                        self.store.add_proposal(proposal_id, session_id, vehicle_id, message.message_id, proposal, now, expires,
                                                preconditions)
                        proposals = (CommandProposal(proposal_id, proposal, expires),)
                        trace.proposed_command_ids.append(proposal_id)
                        status = "confirmation_required"
                        response = self.describe_proposal(proposal) + "\nPlease confirm this exact change. Nothing has been changed yet."
                    if safety.disposition == SafetyDisposition.STOP_WHEN_SAFE:
                        response += "\n\n" + safety.approved_text
                else:
                    response = render_user_response(validated).text
                    safe_facts = self._cited_owned_facts(vehicle_id, now,
                                                        validated.assessment.evidence_ids,
                                                        run.planner.trace.tool_ids_called)
                    service_ids = ({record.record_id for record in self.store.service_records(vehicle_id, now)}
                                   if safe_facts else set())
                    if (safe_facts and not validated.assessment.hypotheses
                            and set(validated.assessment.evidence_ids) <= service_ids):
                        # For a service-only read, render the cited persisted facts
                        # rather than unverifiable model embellishment.
                        response = safe_facts + safety.approved_text
                    if prior_stop is not None:
                        # No clearance workflow exists yet; new telemetry does not
                        # resolve the prior concern. Cited stored service facts can
                        # be shown through deterministic wording; model prose cannot.
                        response = safe_facts + "The earlier stop warning remains unresolved.\n\n" + safety.approved_text
                        if validated.action_wording:
                            response += "\n\n" + "\n".join(validated.action_wording)
                    if status != "complete":
                        response = "I couldn't complete this assessment. No ownership facts were changed.\n\n" + response
                    else:
                        summary = {"assessed_at": now.isoformat(), "observations": validated.assessment.observations[:3],
                                   "unconfirmed_hypotheses": validated.assessment.hypotheses[:3],
                                   "uncertainties": validated.assessment.uncertainties[:3],
                                   "safety_disposition": safety.disposition.value}
                        summary["observations"] = [text[:512] for text in summary["observations"]]
                    if request is None:
                        response += "\n\nNo verified maintenance schedule matches this vehicle. Your recorded services remain available."
                    response += self._reminder_text(state)
                if safety.disposition == SafetyDisposition.STOP_WHEN_SAFE:
                    trace.stop_guidance_applied = True
                self.store.remember_turn(session_id, vehicle_id, message, response, status, summary)
            return TurnResult(response, status, validated if proposal is None and clarification is None else None, safety, proposals,
                              rejected_commands=rejected, reminder_changes=changes, maintenance_state=state, trace=trace,
                              maintenance_applicability="APPLICABLE" if request else "UNKNOWN")
        except (ValueError, TypeError, sqlite3.Error, OSError) as error:
            trace.error_category = "storage_unavailable" if isinstance(error, (sqlite3.Error, OSError)) else "invalid_request"
            if isinstance(error, OdometerConflict):
                trace.error_category = "odometer_conflict"
            if isinstance(error, StaleProposal):
                trace.error_category = "stale_proposal"
            # Raw exception/provider/SQL data is never returned to the interface.
            reason = "Storage is unavailable; the requested change was not applied." if trace.error_category == "storage_unavailable" else "The request could not be applied. Please check the selected vehicle, confirmation and event details."
            if isinstance(error, StaleProposal):
                reason = str(error)
            elif isinstance(error, ValueError) and str(error).startswith(("Odometer conflict:", "A future ", "This proposed change", "Uncertain ownership", "Correct the service")):
                reason = str(error)
            if safety and safety.disposition == SafetyDisposition.STOP_WHEN_SAFE:
                reason += "\n\n" + safety.approved_text
                trace.effective_safety_disposition = safety.disposition.value
                trace.stop_guidance_applied = True
            rejected = (MutationResult(False, trace.error_category, None, (), valid_now),) if confirmation_id and valid_now else ()
            if confirmation_id:
                trace.rejected_command_ids.append(confirmation_id)
            return TurnResult(reason, "error", safety=safety, rejected_commands=rejected, trace=trace)
        finally:
            trace.elapsed_seconds = perf_counter() - started
