"""Scripted offline journey. Fixtures demonstrate plumbing, not AI intelligence."""

from dataclasses import replace
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from carmind.app import CarMindApp
from carmind.contracts import UserMessage
from carmind.manufacturer_knowledge import KnowledgePack, MaintenanceRule, ManufacturerSource, VehicleKnowledgeProfile
from carmind.planner_provider import FakePlannerProvider
from carmind.router_provider import FakeCapabilityRouter
from carmind.routing import ExecutionMode
from carmind.simulator import freeze_episode, generate_episode
from carmind.storage import OwnershipStore
from carmind.tools import MaintenanceRequest


def fictional_schedule():
    """TEST_ONLY NON_PRODUCTION FICTIONAL; deliberately never loaded by default."""
    identity = VehicleKnowledgeProfile("Fictional", "Everyday", 2024, "TEST_ONLY", "Fictional gasoline engine")
    source = ManufacturerSource("test-only-source", "Fictional", "Everyday", 2024, "TEST_ONLY",
                                "TEST_ONLY NON_PRODUCTION FICTIONAL maintenance fixture",
                                "TEST_ONLY_NON_PRODUCTION_FICTIONAL", "https://example.invalid/test-only", "2026-01-01")
    rule = MaintenanceRule("test-only-oil-rule", source.source_id, "oil", "REPLACE", "NORMAL",
                           "TEST_ONLY NON_PRODUCTION FICTIONAL", "fixture", interval_km=10000, interval_months=6)
    pack = KnowledgePack(identity, (source,), (rule,), poc_only=True)
    return MaintenanceRequest(pack, identity, operating_condition="NORMAL")


def command_turn(kind, arguments, owner_text, *, certainty="explicit"):
    return {"type": "propose_command", "command": {"kind": kind, "arguments": arguments,
                                                   "certainty": certainty, "owner_quote": owner_text}}


def empty_final():
    return {"type": "final", "assessment": {"observations": [], "hypotheses": [],
            "uncertainties": ["MISSING_EVIDENCE"], "recommended_action_ids": [], "limitations": []}}


def run_demo(path, output=print):
    with ExitStack() as cleanup:
        _run_demo(path, output, cleanup)


def _run_demo(path, output, cleanup):
    output("OFFLINE SCRIPTED DEMO — fake provider, no live intelligence or API calls.")
    output("Maintenance data: TEST_ONLY / NON_PRODUCTION / FICTIONAL. Not manufacturer guidance.")
    snapshot = freeze_episode(generate_episode("gradual_tire_pressure_loss", 42)[0])
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    now = snapshot.assessment_at
    store = OwnershipStore(path)
    cleanup.callback(store.close)
    app = CarMindApp(store, FakePlannerProvider([]), approved_schedules=(fictional_schedule(),), allow_test_schedules=True)
    owner = app.create_owner(now=start, owner_id="demo-owner")
    app.add_vehicle(owner, replace(snapshot.profile, mileage_km=None), now=start, market="TEST_ONLY", nickname="My demo car")
    session = app.start_session(owner, now=start, session_id="demo-session")
    app.select_vehicle(owner, session, snapshot.profile.vehicle_id, now=start)
    output("Created owner, added and selected My demo car.")
    counter = 0

    def say(text, script, at=now, evidence=None):
        nonlocal counter
        counter += 1
        app.provider = FakePlannerProvider(script)
        result = app.handle_message(owner, session, UserMessage(f"demo-{counter}", text, at), now=at, snapshot=evidence)
        output("User: " + text)
        output("CarMind: " + result.response)
        if result.proposed_commands:
            counter += 1
            result = app.handle_message(owner, session, UserMessage(f"demo-{counter}", "I confirm the displayed change.", at),
                                        now=at, confirmation_id=result.proposed_commands[0].proposal_id)
            output("Owner explicitly confirms the displayed proposal.")
            output("CarMind: " + result.response)
        return result

    text = "My odometer was 15000 km on January 1."
    say(text, [command_turn("update_odometer", {"reading": 15000, "unit": "km", "occurred_at": start.isoformat()}, text)])
    text = "I changed the oil on January 1 at 15000 km."
    service = say(text, [command_turn("record_service_event", {"service_type": "oil", "performed_at": start.isoformat(), "odometer": 15000, "unit": "km"}, text)])
    history_answer = empty_final()
    history_answer["assessment"]["observations"] = [{"text": "Your recorded oil service was on January 1 at 15000 km.",
                                                    "evidence_ids": [service.applied_commands[0].entity_id]}]
    say("When did I last change the oil?", [{"type": "tool_call", "tool_id": "get_latest_service_record", "arguments": {"service_type": "oil"}}, history_answer])
    text = "My odometer is now 24200 km."
    say(text, [command_turn("update_odometer", {"reading": 24200, "unit": "km", "occurred_at": now.isoformat()}, text)])
    result = say("What maintenance is coming up?", [empty_final()])
    output("Deterministic maintenance: " + ", ".join(r.status for r in result.maintenance_state))
    later = now + timedelta(minutes=1)
    text = "The odometer now reads 25000 km."
    say(text, [command_turn("update_odometer", {"reading": 25000, "unit": "km", "occurred_at": later.isoformat()}, text)], later)
    text = "I changed the oil just now at 25000 km."
    say(text, [command_turn("record_service_event", {"service_type": "oil", "performed_at": later.isoformat(), "odometer": 25000, "unit": "km"}, text)], later)
    output(f"Active reminders after service: {len(store.reminders(snapshot.profile.vehicle_id, active_only=True))}.")
    app.mode, app.router = ExecutionMode.ROUTED, FakeCapabilityRouter(selected=["tires"])
    ids = [o.observation_id for o in snapshot.observations if o.name == "rear_left_tire_pressure"]
    final = empty_final()
    final["assessment"].update(observations=[{"text": "Rear-left tire pressure fell across the available readings.", "evidence_ids": ids}],
                               hypotheses=[{"hypothesis_id": "POSSIBLE_TIRE_LEAK", "evidence_ids": ids}],
                               uncertainties=["CAUSE_UNCONFIRMED"], recommended_action_ids=["ARRANGE_SERVICE_REVIEW"],
                               limitations=["NO_PHYSICAL_INSPECTION"])
    diagnosis = say(snapshot.owner_message.text, [{"type": "tool_call", "tool_id": "get_tire_pressure_history", "arguments": {"wheel": "rear_left"}}, final], later, snapshot)
    output("Diagnostic safety: " + diagnosis.safety.disposition.value)
    followup_answer = empty_final()
    followup_answer["assessment"]["recommended_action_ids"] = ["ARRANGE_SERVICE_REVIEW"]
    followup = say("Can I keep driving?", [followup_answer], later + timedelta(minutes=1))
    output(f"Bounded ownership context: {followup.trace.ownership_context_characters} characters.")
    store.close()
    store = OwnershipStore(path)
    cleanup.callback(store.close)
    restored = store.vehicle(owner, snapshot.profile.vehicle_id, later)
    output(f"Restart: {restored.nickname}; {restored.profile.mileage_km:g} km; {len(store.service_records(restored.profile.vehicle_id, later))} service records; {len(store.reminders(restored.profile.vehicle_id))} reminder lifecycle retained.")
    output("Unresolved stop warning retained: " + str(store.previous_stop(restored.profile.vehicle_id) is not None))
    store.close()


def main():
    # Each invocation is an isolated, reproducible journey, with an actual close /
    # reopen within it. Real adapters choose their own durable local database path.
    with TemporaryDirectory(prefix="carmind-ownership-") as directory:
        run_demo(Path(directory) / "ownership.sqlite3")
    return 0
