"""Offline acceptance suite for persistent ownership and safe application writes."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from carmind.app import CarMindApp
from carmind.contracts import SafetyDisposition, UserMessage, VehicleProfile
from carmind.ownership import distance_km, parse_command
from carmind.ownership_demo import command_turn, empty_final, fictional_schedule, run_demo
from carmind.planner_provider import FakePlannerProvider
from carmind.router_provider import FakeCapabilityRouter
from carmind.routing import ExecutionMode
from carmind.safety import evaluate_safety
from carmind.simulator import freeze_episode, generate_episode, list_scenarios
from carmind.storage import OwnershipStore
from carmind.tools import execute_tool
from carmind.tools import MaintenanceRequest
from test_cross_scenario_validation import SCENARIO_FIXTURES, scenario_candidate


NOW = datetime(2026, 1, 6, 12, tzinfo=timezone.utc)
START = NOW - timedelta(days=5)


class OwnershipStorePathTests(unittest.TestCase):
    def test_missing_nested_database_parent_is_created(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "missing" / "nested" / "owner.sqlite3"
            self.assertFalse(path.parent.exists())
            store = OwnershipStore(path)
            try:
                self.assertTrue(path.is_file())
                self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], 4)
            finally:
                store.close()

    def test_memory_database_does_not_create_parent(self):
        with patch.object(Path, "mkdir", side_effect=AssertionError("No directory should be created")):
            store = OwnershipStore(":memory:")
            try:
                self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], 4)
            finally:
                store.close()

    def test_sqlite_uri_does_not_create_parent(self):
        with patch.object(Path, "mkdir", side_effect=AssertionError("No directory should be created")):
            try:
                store = OwnershipStore("file::memory:?cache=shared")
            except sqlite3.Error:
                pass  # The existing SQLite URI behavior depends on its connection settings.
            else:
                store.close()


class OwnershipAppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "ownership.sqlite3"
        self.store = OwnershipStore(self.path)
        self.addCleanup(lambda: self.store.close())
        self.app = CarMindApp(self.store, FakePlannerProvider([]))
        self.owner = self.app.create_owner(now=START, owner_id="owner")
        self.profile = replace(freeze_episode(generate_episode("healthy_vehicle", 42)[0]).profile, mileage_km=None)
        self.app.add_vehicle(self.owner, self.profile, now=START, market="TEST_ONLY", nickname="Test car")
        self.session = self.app.start_session(self.owner, now=START, vehicle_id=self.profile.vehicle_id, session_id="session")
        self.serial = 0

    def message(self, text, at=NOW):
        self.serial += 1
        return UserMessage(f"msg-{self.serial}", text, at)

    def query(self, script=None, text="What do you know?", at=NOW, **kwargs):
        self.app.provider = FakePlannerProvider(script or [empty_final()])
        return self.app.handle_message(self.owner, self.session, self.message(text, at), now=at, **kwargs)

    def propose(self, kind, args, certainty="explicit", at=NOW):
        text = "This is my stated ownership update."
        result = self.query([command_turn(kind, args, text, certainty=certainty)], text, at)
        self.assertEqual(result.status, "confirmation_required", result.response)
        return result.proposed_commands[0]

    def confirm(self, proposal, at=NOW, **kwargs):
        return self.app.handle_message(self.owner, self.session, self.message("I confirm the displayed change.", at),
                                       now=at, confirmation_id=proposal.proposal_id, **kwargs)

    def write(self, kind, args, at=NOW):
        return self.confirm(self.propose(kind, args, at=at), at)

    def odometer(self, km, occurred=NOW, at=NOW, **extra):
        return self.write("update_odometer", {"reading": km, "unit": "km", "occurred_at": occurred.isoformat(), **extra}, at)

    def service(self, occurred=START, km=15000, **extra):
        args = {"service_type": "oil", "performed_at": occurred.isoformat(), **extra}
        if km is not None:
            args.update(odometer=km, unit="km")
        return self.write("record_service_event", args)

    def fixture(self):
        self.app.schedules = (fictional_schedule(),)
        self.app.allow_test_schedules = True

    def test_owner_vehicle_session_restart_and_multiple_vehicles(self):
        other = replace(self.profile, vehicle_id="second", model="Second car")
        self.app.add_vehicle(self.owner, other, now=START)
        self.app.select_vehicle(self.owner, self.session, other.vehicle_id, now=NOW)
        self.store.close()
        self.store = OwnershipStore(self.path)
        self.assertEqual(self.store.session(self.session, self.owner)["active_vehicle_id"], "second")
        self.assertEqual(self.store.vehicle(self.owner, self.profile.vehicle_id, NOW).nickname, "Test car")
        self.assertEqual(self.store.db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 4)

    def test_no_active_vehicle_is_graceful_and_selectable(self):
        session = self.app.start_session(self.owner, now=NOW)
        result = self.app.handle_message(self.owner, session, self.message("Hello"), now=NOW)
        self.assertEqual(result.status, "no_active_vehicle")
        self.assertEqual(self.app.provider.requests, [])
        self.app.select_vehicle(self.owner, session, self.profile.vehicle_id, now=NOW)
        self.assertEqual(self.store.session(session, self.owner)["active_vehicle_id"], self.profile.vehicle_id)

    def test_owner_and_vehicle_scope_checked(self):
        outsider = self.app.create_owner(now=NOW)
        result = self.app.handle_message(outsider, self.session, self.message("Hello"), now=NOW)
        self.assertEqual(result.status, "error")
        with self.assertRaises(ValueError):
            self.app.select_vehicle(self.owner, self.session, "missing", now=NOW)

    def test_proposal_is_not_a_write_and_text_yes_is_not_authority(self):
        proposal = self.propose("update_odometer", {"reading": 100, "unit": "km", "occurred_at": NOW.isoformat()})
        self.assertEqual(self.store.odometer_events(self.profile.vehicle_id), [])
        self.query(text="yes")
        self.assertEqual(self.store.odometer_events(self.profile.vehicle_id), [])
        self.assertTrue(self.confirm(proposal).applied_commands[0].applied)

    def test_uncertain_statement_is_not_persisted_or_confirmable(self):
        text = "I think maybe they changed the oil."
        result = self.query([command_turn("record_service_event", {"service_type": "oil", "performed_at": NOW.isoformat()}, text, certainty="uncertain")], text)
        self.assertEqual(result.status, "clarification_required")
        self.assertEqual(result.proposed_commands, ())
        self.assertEqual(self.store.service_records(self.profile.vehicle_id, NOW), [])
        self.assertEqual(result.rejected_commands[0].reason, "uncertain_owner_statement")

    def test_command_schema_rejects_extra_permissions_and_arbitrary_facts(self):
        raw = command_turn("update_odometer", {"reading": 100, "unit": "km", "occurred_at": NOW.isoformat()}, "owner")["command"]
        for modified in (dict(raw, authorized=True), dict(raw, kind="battery_failed"), dict(raw, owner_quote="invented"),
                         dict(raw, arguments=dict(raw["arguments"], confirmed=True))):
            with self.subTest(modified=modified), self.assertRaises(ValueError):
                parse_command(modified, "owner")

    def test_command_cannot_be_used_by_standalone_planner(self):
        from carmind.planner import run_full_planner
        snapshot = freeze_episode(generate_episode("healthy_vehicle", 42)[0])
        script = command_turn("select_vehicle", {"vehicle_id": "x"}, snapshot.owner_message.text)
        result = run_full_planner(snapshot, FakePlannerProvider([script]), max_model_calls=1)
        self.assertIsNone(result.proposed_command)
        self.assertEqual(result.trace.completion_status, "incomplete")

    def test_odometer_initial_newer_historical_rollback_and_restart(self):
        self.assertEqual(self.odometer(20000, START).status, "applied")
        self.assertEqual(self.odometer(25000).status, "applied")
        self.assertEqual(self.odometer(22000, START + timedelta(days=1)).status, "applied")
        conflict = self.odometer(18000, NOW + timedelta(minutes=1), NOW + timedelta(minutes=1))
        self.assertEqual(conflict.status, "error")
        self.assertIn("Odometer conflict", conflict.response)
        self.assertEqual(conflict.rejected_commands[0].reason, "odometer_conflict")
        self.store.close()
        self.store = OwnershipStore(self.path)
        self.assertEqual(self.store.vehicle(self.owner, self.profile.vehicle_id, NOW).profile.mileage_km, 25000)
        self.assertEqual(len(self.store.odometer_events(self.profile.vehicle_id)), 3)

    def test_same_timestamp_conflicting_odometer_rejected(self):
        self.odometer(200)
        self.assertEqual(self.odometer(201).status, "error")

    def test_miles_normalized_and_invalid_units_numbers_rejected(self):
        self.assertEqual(distance_km(0, "km"), 0)
        self.assertEqual(distance_km(1_000_000_000, "km"), 1_000_000_000)
        result = self.write("update_odometer", {"reading": 100, "unit": "mi", "occurred_at": NOW.isoformat()})
        self.assertEqual(result.status, "applied")
        self.assertAlmostEqual(self.store.vehicle(self.owner, self.profile.vehicle_id, NOW).profile.mileage_km, 160.9344)
        for number, unit in ((-1, "km"), (True, "km"), (float("nan"), "km"), (float("inf"), "mi"), (1, "miles")):
            with self.subTest(number=number, unit=unit), self.assertRaises(ValueError):
                distance_km(number, unit)

    def test_future_reading_or_service_not_saved(self):
        self.assertEqual(self.odometer(100, NOW + timedelta(days=1)).status, "error")
        self.assertEqual(self.service(NOW + timedelta(days=1), 100).status, "error")
        self.assertFalse(self.store.odometer_events(self.profile.vehicle_id))
        self.assertFalse(self.store.service_records(self.profile.vehicle_id, NOW))

    def test_service_read_tool_idempotency_and_provenance(self):
        proposal = self.propose("record_service_event", {"service_type": "oil", "performed_at": START.isoformat(), "odometer": 15000, "unit": "km"})
        first = self.confirm(proposal)
        second = self.confirm(proposal)
        self.assertEqual(first.applied_commands, second.applied_commands)
        self.assertTrue(second.trace.replayed)
        context = self.store.vehicle_context(self.owner, self.profile.vehicle_id, NOW)
        snapshot = replace(freeze_episode(generate_episode("healthy_vehicle", 42)[0]), profile=context.profile)
        result = execute_tool("get_service_history", {}, snapshot, context, allowed_tool_ids=("get_service_history",))
        self.assertTrue(result.success)
        self.assertEqual(len(result.data["records"]), 1)
        self.assertEqual(result.evidence_ids, (first.applied_commands[0].entity_id,))
        self.assertEqual(first.applied_commands[0].provenance, "USER_REPORTED_CONFIRMED")
        stored = self.store.proposal(proposal.proposal_id, self.session)
        self.assertNotIn("owner_quote", json.loads(stored["payload"]))

    def test_odometer_acknowledgement_and_correction_confirmation_replays_are_idempotent(self):
        reading = self.propose("update_odometer", {"reading": 20000, "unit": "km", "occurred_at": NOW.isoformat()})
        first_reading = self.confirm(reading)
        replayed_reading = self.confirm(reading)
        self.assertTrue(replayed_reading.trace.replayed)
        self.assertEqual(len(self.store.odometer_events(self.profile.vehicle_id)), 1)

        original = self.service().applied_commands[0].entity_id
        correction = self.propose("record_service_event", {
            "service_type": "oil", "performed_at": NOW.isoformat(), "odometer": 20000,
            "unit": "km", "supersedes_id": original,
        })
        first_correction = self.confirm(correction)
        replayed_correction = self.confirm(correction)
        self.assertTrue(replayed_correction.trace.replayed)
        self.assertEqual(first_correction.applied_commands, replayed_correction.applied_commands)
        self.assertEqual(len(self.store.service_records(self.profile.vehicle_id, NOW)), 1)

        self.fixture()
        self.service()
        later = NOW + timedelta(minutes=1)
        self.odometer(29200, later, later)
        self.query(at=later)
        reminder_id = self.store.reminders(self.profile.vehicle_id, active_only=True)[0]["id"]
        acknowledgement = self.propose("acknowledge_reminder", {"reminder_id": reminder_id}, at=later)
        first_ack = self.confirm(acknowledgement, at=later)
        replayed_ack = self.confirm(acknowledgement, at=later)
        self.assertTrue(replayed_ack.trace.replayed)
        self.assertEqual(first_ack.applied_commands, replayed_ack.applied_commands)
        self.assertEqual(self.store.reminders(self.profile.vehicle_id)[0]["lifecycle"], "ACKNOWLEDGED")

    def test_confirmations_are_session_scoped_and_expire(self):
        proposal = self.propose("select_vehicle", {"vehicle_id": self.profile.vehicle_id})
        other = self.app.start_session(self.owner, now=NOW, vehicle_id=self.profile.vehicle_id)
        result = self.app.handle_message(self.owner, other, self.message("yes"), now=NOW, confirmation_id=proposal.proposal_id)
        self.assertEqual(result.status, "error")
        result = self.confirm(proposal, NOW + timedelta(days=2))
        self.assertEqual(result.status, "error")
        self.assertNotIn("owner_quote", json.loads(self.store.proposal(proposal.proposal_id, self.session)["payload"]))

    def test_changed_active_vehicle_blocks_stale_confirmation(self):
        proposal = self.propose("update_odometer", {"reading": 200, "unit": "km", "occurred_at": NOW.isoformat()})
        other = replace(self.profile, vehicle_id="other")
        self.app.add_vehicle(self.owner, other, now=NOW)
        self.app.select_vehicle(self.owner, self.session, "other", now=NOW)
        self.assertEqual(self.confirm(proposal).status, "error")
        self.assertFalse(self.store.odometer_events(self.profile.vehicle_id))

    def test_stale_profile_proposal_cannot_overwrite_a_newer_confirmed_value(self):
        stale = self.propose("set_vehicle_profile_field", {"field": "nickname", "value": "First name"})
        current = self.propose("set_vehicle_profile_field", {"field": "nickname", "value": "Latest name"})
        self.assertEqual(self.confirm(current).status, "applied")
        result = self.confirm(stale)
        self.assertEqual(result.status, "error")
        self.assertEqual(result.trace.error_category, "stale_proposal")
        self.assertEqual(self.store.vehicle(self.owner, self.profile.vehicle_id, NOW).nickname, "Latest name")

    def test_odometer_correction_preserves_old_event(self):
        old = self.odometer(20000).applied_commands[0].entity_id
        new = self.odometer(19000, supersedes_id=old)
        self.assertEqual(new.status, "applied")
        self.assertEqual(self.store.vehicle(self.owner, self.profile.vehicle_id, NOW).profile.mileage_km, 19000)
        row = self.store.db.execute("SELECT * FROM odometer_events WHERE id=?", (old,)).fetchone()
        self.assertEqual(row["replacement_id"], new.applied_commands[0].entity_id)
        self.assertIsNotNone(row["superseded_at"])

    def test_service_correction_supersedes_associated_odometer(self):
        old = self.service().applied_commands[0].entity_id
        result = self.service(km=14000, supersedes_id=old)
        self.assertEqual(result.status, "applied")
        self.assertEqual(len(self.store.service_records(self.profile.vehicle_id, NOW)), 1)
        self.assertEqual(self.store.vehicle(self.owner, self.profile.vehicle_id, NOW).profile.mileage_km, 14000)
        blocked = self.odometer(12000, supersedes_id=result.applied_commands[0].entity_id + "-reading")
        self.assertEqual(blocked.status, "error")

    def test_invalid_correction_rolls_back_supersession(self):
        old = self.odometer(20000, START).applied_commands[0].entity_id
        self.odometer(25000)
        result = self.odometer(26000, START, supersedes_id=old)
        self.assertEqual(result.status, "error")
        self.assertEqual(len(self.store.odometer_events(self.profile.vehicle_id)), 2)

    def test_mutation_and_reminder_refresh_are_atomic(self):
        proposal = self.propose("record_service_event", {"service_type": "oil", "performed_at": START.isoformat(), "odometer": 1000, "unit": "km"})
        with patch.object(self.app, "_refresh", side_effect=sqlite3.OperationalError("private SQL details")):
            result = self.confirm(proposal)
        self.assertEqual(result.status, "error")
        self.assertNotIn("SQL", result.response)
        self.assertFalse(self.store.service_records(self.profile.vehicle_id, NOW))
        self.assertFalse(self.store.odometer_events(self.profile.vehicle_id))
        self.assertEqual(self.store.proposal(proposal.proposal_id, self.session)["state"], "PENDING")
        self.assertEqual(self.confirm(proposal).status, "applied")

    def test_pending_confirmation_survives_restart(self):
        pending = self.propose("record_service_event", {"service_type": "oil", "performed_at": START.isoformat()})
        self.store.close()
        self.store = OwnershipStore(self.path)
        self.app = CarMindApp(self.store, FakePlannerProvider([]))
        result = self.confirm(pending)
        self.assertEqual(result.status, "applied")
        self.assertEqual(len(self.store.service_records(self.profile.vehicle_id, NOW)), 1)

    def test_parallel_pending_proposals_require_their_own_ids(self):
        odometer = self.propose("update_odometer", {"reading": 1200, "unit": "km", "occurred_at": NOW.isoformat()})
        service = self.propose("record_service_event", {"service_type": "oil", "performed_at": START.isoformat()})
        self.assertNotEqual(odometer.proposal_id, service.proposal_id)
        result = self.confirm(service)
        self.assertEqual(result.status, "applied")
        self.assertFalse(self.store.odometer_events(self.profile.vehicle_id))
        self.assertEqual(self.confirm(odometer).status, "applied")

    def test_unknown_manufacturer_never_creates_reminders(self):
        self.service()
        self.odometer(100000)
        result = self.query()
        self.assertEqual(result.maintenance_state, ())
        self.assertIn("No verified maintenance schedule", result.response)
        self.assertEqual(self.store.reminders(self.profile.vehicle_id), [])

    def test_fixture_requires_explicit_opt_in_and_exact_market(self):
        self.app.schedules = (fictional_schedule(),)
        self.service()
        self.odometer(24500)
        self.assertFalse(self.query().maintenance_state)
        self.app.allow_test_schedules = True
        self.assertEqual(self.query().maintenance_state[0].status, "UPCOMING")
        self.write("set_vehicle_profile_field", {"field": "market", "value": "Saudi Arabia"})
        self.assertFalse(self.query().maintenance_state)
        self.assertEqual(self.store.reminders(self.profile.vehicle_id, active_only=True), [])

    def test_unknown_year_schedule_not_activated_even_with_fixture_opt_in(self):
        fixture = fictional_schedule()
        pack = replace(fixture.pack,
                       profile=replace(fixture.pack.profile, model_year=None),
                       sources=(replace(fixture.pack.sources[0], model_year=None),))
        self.app.schedules = (MaintenanceRequest(pack, pack.profile),)
        self.app.allow_test_schedules = True
        self.assertFalse(self.query().maintenance_state)
        self.assertFalse(self.store.reminders(self.profile.vehicle_id))

    def test_reminder_cycle_dedup_ack_update_complete_restart(self):
        self.fixture()
        self.service()
        self.odometer(24200)
        self.assertEqual(self.query().maintenance_state[0].status, "UPCOMING")
        reminder = self.store.reminders(self.profile.vehicle_id)[0]
        self.assertEqual(self.query().reminder_changes, ())
        self.assertEqual(self.write("acknowledge_reminder", {"reminder_id": reminder["id"]}).status, "applied")
        later = NOW + timedelta(minutes=1)
        self.assertEqual(self.odometer(25000, later, later).maintenance_state[0].status, "DUE")
        current = self.store.reminders(self.profile.vehicle_id)[0]
        self.assertEqual(current["id"], reminder["id"])
        self.assertEqual(current["lifecycle"], "ACKNOWLEDGED")
        self.assertEqual(current["facts"]["source_id"], "test-only-source")
        result = self.write("record_service_event", {"service_type": "oil", "performed_at": later.isoformat(), "odometer": 25000, "unit": "km"}, later)
        self.assertEqual(result.maintenance_state[0].status, "NOT_DUE")
        self.store.close()
        self.store = OwnershipStore(self.path)
        self.assertEqual(self.store.reminders(self.profile.vehicle_id)[0]["lifecycle"], "COMPLETED")
        self.assertEqual(len(self.store.service_records(self.profile.vehicle_id, later)), 2)
        self.assertEqual(self.store.vehicle(self.owner, self.profile.vehicle_id, later).profile.mileage_km, 25000)

    def test_odometer_correction_backwards_closes_due_reminder(self):
        self.fixture()
        self.service()
        event = self.odometer(25000).applied_commands[0].entity_id
        self.assertEqual(self.query().maintenance_state[0].status, "DUE")
        result = self.odometer(20000, supersedes_id=event)
        self.assertEqual(result.maintenance_state[0].status, "NOT_DUE")
        reminder = self.store.reminders(self.profile.vehicle_id)[0]
        self.assertEqual(reminder["lifecycle"], "COMPLETED")
        self.assertEqual(reminder["reason"], "no_longer_due_or_cycle_replaced")

    def test_owner_vehicle_isolation_for_services_reminders_conversation_and_commands(self):
        self.fixture()
        self.service()
        self.odometer(24200)
        self.query(text="Vehicle A conversation marker")
        self.assertEqual(len(self.store.reminders(self.profile.vehicle_id, active_only=True)), 1)
        profile_b = replace(self.profile, vehicle_id="vehicle-b", make="Other", model="Family", mileage_km=None)
        self.app.add_vehicle(self.owner, profile_b, now=NOW, nickname="B")
        session_b = self.app.start_session(self.owner, now=NOW, vehicle_id=profile_b.vehicle_id)
        text = "Vehicle B conversation marker"
        provider = FakePlannerProvider([empty_final()])
        self.app.provider = provider
        result_b = self.app.handle_message(self.owner, session_b, self.message(text), now=NOW)
        initial = json.loads(provider.requests[0][1]["content"])
        self.assertEqual(initial["vehicle_profile"]["vehicle_id"], profile_b.vehicle_id)
        self.assertNotIn("Vehicle A conversation marker", json.dumps(initial))
        self.assertNotIn("15000", json.dumps(initial))
        self.assertEqual(self.store.service_records(profile_b.vehicle_id, NOW), [])
        self.assertEqual(self.store.odometer_events(profile_b.vehicle_id), [])
        self.assertEqual(self.store.reminders(profile_b.vehicle_id), [])
        proposal_message = "I own this second reading."
        proposal_provider = FakePlannerProvider([command_turn("update_odometer", {"reading": 900, "unit": "km", "occurred_at": NOW.isoformat()}, proposal_message)])
        self.app.provider = proposal_provider
        proposal_result = self.app.handle_message(self.owner, session_b, self.message(proposal_message), now=NOW)
        self.assertEqual(proposal_result.proposed_commands[0].command.arguments["reading"], 900)
        self.app.select_vehicle(self.owner, session_b, self.profile.vehicle_id, now=NOW)
        stale = self.app.handle_message(self.owner, session_b, self.message("yes"), now=NOW,
                                        confirmation_id=proposal_result.proposed_commands[0].proposal_id)
        self.assertEqual(stale.status, "error")
        self.assertFalse(self.store.odometer_events(profile_b.vehicle_id))
        outsider = self.app.create_owner(now=NOW)
        outsider_session = self.app.start_session(outsider, now=NOW)
        denied = self.app.handle_message(outsider, outsider_session, self.message("vehicle A"), now=NOW,
                                         confirmation_id=proposal_result.proposed_commands[0].proposal_id)
        self.assertIn(denied.status, {"error", "no_active_vehicle"})
        self.assertFalse(self.store.odometer_events(profile_b.vehicle_id))
        self.assertEqual(result_b.status, "complete")

    def test_unrelated_service_does_not_reset_oil_reminder(self):
        self.fixture()
        self.service()
        self.odometer(25000)
        self.service(NOW, 25000, service_type="tires")
        self.assertEqual(self.query().maintenance_state[0].status, "DUE")

    def test_missing_service_baseline_stays_unknown(self):
        self.fixture()
        self.odometer(25000)
        self.assertEqual(self.query().maintenance_state[0].status, "UNKNOWN")
        self.assertFalse(self.store.reminders(self.profile.vehicle_id))

    def test_time_based_reminder_refresh_on_query(self):
        self.fixture()
        self.service()
        later = datetime(2026, 7, 10, tzinfo=timezone.utc)
        result = self.query(at=later)
        self.assertEqual(result.maintenance_state[0].status, "OVERDUE")
        self.assertEqual(len(self.store.reminders(self.profile.vehicle_id, active_only=True)), 1)

    def test_provider_failure_and_invalid_final_are_safe(self):
        failed = self.query([RuntimeError("private provider payload")])
        self.assertEqual(failed.status, "incomplete")
        self.assertNotIn("private", failed.response)
        invalid = self.query([{"type": "final", "assessment": {}}, {"type": "final", "assessment": {}}])
        self.assertEqual(invalid.status, "incomplete")
        self.assertIn("couldn't complete", invalid.response)
        self.assertFalse(self.store.service_records(self.profile.vehicle_id, NOW))

    def test_unknown_ids_and_closed_storage_fail_gracefully(self):
        result = self.confirm(type("Proposal", (), {"proposal_id": "missing"})())
        self.assertEqual(result.status, "error")
        self.store.close()
        result = self.query()
        self.assertEqual(result.trace.error_category, "storage_unavailable")
        self.assertEqual(result.status, "error")

    def test_invalid_times_and_nonfrozen_evidence_fail_without_provider(self):
        for now, snapshot in ((datetime(2026, 1, 6), None), (NOW, object())):
            result = self.app.handle_message(self.owner, self.session, self.message("Hello"), now=now, snapshot=snapshot)
            self.assertEqual(result.status, "error")
        self.assertEqual(self.app.provider.requests, [])

    def test_context_is_bounded_history_remains_available_via_tool(self):
        for i in range(12):
            self.service(START + timedelta(hours=i), None, notes=f"service-{i}")
        for i in range(10):
            self.query(text=f"Conversation {i}")
        initial = json.loads(self.app.provider.requests[0][1]["content"])
        self.assertEqual(len(initial["service_history"]["records"]), 8)
        self.assertLessEqual(len(initial["ownership_context"]["recent_turns"]), 6)
        self.assertNotIn("recent_services", initial["ownership_context"])
        self.assertNotIn("Conversation 0", json.dumps(initial))
        self.assertEqual(len(self.store.service_records(self.profile.vehicle_id, NOW)), 12)
        self.query([{"type": "tool_call", "tool_id": "get_service_history", "arguments": {}}, empty_final()])
        payload = json.loads(self.app.provider.requests[1][-1]["content"])
        self.assertEqual(len(payload["tool_result"]["data"]["records"]), 12)

    def test_proposal_message_replay_does_not_call_provider_again(self):
        message = self.message("My mileage is 200 km.")
        self.app.provider = FakePlannerProvider([command_turn("update_odometer", {"reading": 200, "unit": "km", "occurred_at": NOW.isoformat()}, message.text)])
        first = self.app.handle_message(self.owner, self.session, message, now=NOW)
        second = self.app.handle_message(self.owner, self.session, message, now=NOW)
        self.assertEqual(first.proposed_commands, second.proposed_commands)
        self.assertEqual(len(self.app.provider.requests), 1)
        self.confirm(first.proposed_commands[0])
        third = self.app.handle_message(self.owner, self.session, message, now=NOW)
        self.assertFalse(third.proposed_commands)
        self.assertIn("already recorded", third.response)

    def test_confirmation_replay_survives_restart(self):
        proposal = self.propose("record_service_event", {"service_type": "oil", "performed_at": START.isoformat()})
        before = self.confirm(proposal)
        self.store.close()
        self.store = OwnershipStore(self.path)
        self.app = CarMindApp(self.store, FakePlannerProvider([]))
        after = self.confirm(proposal)
        self.assertEqual(before.applied_commands, after.applied_commands)
        self.assertTrue(after.trace.replayed)
        self.assertEqual(self.app.provider.requests, [])
        self.assertEqual(len(self.store.service_records(self.profile.vehicle_id, NOW)), 1)

    def test_malformed_model_write_repairs_without_mutating(self):
        text = "My odometer reads minus ten."
        malformed = command_turn("update_odometer", {"reading": -10, "unit": "km", "occurred_at": NOW.isoformat()}, text)
        result = self.query([malformed, empty_final()], text)
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.trace.planner.repair_status, "repair_succeeded")
        self.assertFalse(self.store.odometer_events(self.profile.vehicle_id))
        self.assertFalse(result.proposed_commands)

    def test_database_foreign_keys_and_rollback(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.transaction():
                self.store.create_owner("rollback-owner", NOW)
                self.store.add_vehicle("not-an-owner", replace(self.profile, vehicle_id="bad"), NOW)
        self.assertIsNone(self.store.db.execute("SELECT id FROM owners WHERE id='rollback-owner'").fetchone())

    def test_unknown_database_schema_version_fails_closed(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "future.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute("PRAGMA user_version = 99")
            connection.close()
            with self.assertRaisesRegex(ValueError, "Unsupported ownership database version"):
                OwnershipStore(path)

    def test_confirmed_profile_field_provenance_and_selection_command(self):
        self.assertEqual(self.write("set_vehicle_profile_field", {"field": "nickname", "value": "Family car"}).status, "applied")
        self.assertEqual(self.store.vehicle(self.owner, self.profile.vehicle_id, NOW).nickname, "Family car")
        other = replace(self.profile, vehicle_id="second")
        self.app.add_vehicle(self.owner, other, now=NOW)
        self.assertEqual(self.write("select_vehicle", {"vehicle_id": "second"}).status, "applied")
        self.assertEqual(self.store.session(self.session, self.owner)["active_vehicle_id"], "second")

    def test_app_full_and_routed_share_identical_deterministic_maintenance(self):
        self.fixture()
        self.service()
        self.odometer(24400)
        full = self.query()
        self.app.mode = ExecutionMode.ROUTED
        self.app.router = FakeCapabilityRouter(selected=["maintenance", "service_history"])
        routed = self.query()
        self.assertEqual(full.maintenance_state, routed.maintenance_state)
        self.assertEqual(full.safety, routed.safety)
        self.assertEqual(full.maintenance_applicability, "APPLICABLE")
        self.assertEqual(full.trace.planner.exposed_capability_count, 10)
        self.assertEqual(routed.trace.planner.exposed_capability_count, 2)

    def test_safety_followup_hides_model_clearance_and_service_cannot_clear_warning(self):
        snapshot = freeze_episode(generate_episode("gradual_tire_pressure_loss", 42)[0])
        self.query(snapshot=snapshot)
        unsafe = empty_final()
        unsafe["assessment"]["observations"] = [{"text": "You may continue driving.", "evidence_ids": [self.profile.vehicle_id]}]
        followup = self.query([unsafe], "Can I drive now?")
        self.assertEqual(followup.safety.disposition, SafetyDisposition.STOP_WHEN_SAFE)
        self.assertNotIn("You may continue", followup.response)
        self.assertIn("Do not treat", followup.response)
        self.service(km=None)
        again = self.query(text="Is the warning cleared?")
        self.assertEqual(again.safety.disposition, SafetyDisposition.STOP_WHEN_SAFE)

    def test_stop_warning_suppresses_model_clearance_even_with_fresh_unrelated_evidence(self):
        tire = freeze_episode(generate_episode("gradual_tire_pressure_loss", 42)[0])
        self.query(snapshot=tire)
        healthy = freeze_episode(generate_episode("healthy_vehicle", 7)[0])
        unsafe = empty_final()
        unsafe["assessment"]["observations"] = [{"text": "You may continue driving; the car can be used normally.",
                                                  "evidence_ids": [self.profile.vehicle_id]}]
        followup = self.query([unsafe, empty_final()], "The warning disappeared and it feels fine now.", snapshot=healthy)
        self.assertEqual(followup.safety.disposition, SafetyDisposition.STOP_WHEN_SAFE)
        self.assertIn("disallowed safety wording", followup.trace.planner.validation_failures[0])
        self.assertNotIn("you may continue driving", followup.response.lower())
        self.assertNotIn("the car can be used normally", followup.response.lower())
        self.assertNotIn("used normally", followup.response.lower())
        self.assertIn("Do not treat", followup.response)

    def test_future_snapshot_rejected_and_telemetry_cannot_update_owned_mileage(self):
        snapshot = freeze_episode(generate_episode("gradual_tire_pressure_loss", 42)[0])
        result = self.query(snapshot=replace(snapshot, assessment_at=NOW + timedelta(hours=1)))
        self.assertEqual(result.status, "error")
        self.query(snapshot=snapshot)
        self.assertIsNone(self.store.vehicle(self.owner, self.profile.vehicle_id, NOW).profile.mileage_km)
        self.assertFalse(self.store.odometer_events(self.profile.vehicle_id))

    def test_vehicle_selection_does_not_transfer_another_cars_warning(self):
        snapshot = freeze_episode(generate_episode("gradual_tire_pressure_loss", 42)[0])
        self.query(snapshot=snapshot)
        other = replace(self.profile, vehicle_id="other")
        self.app.add_vehicle(self.owner, other, now=NOW)
        switched = self.write("select_vehicle", {"vehicle_id": "other"})
        self.assertEqual(switched.trace.vehicle_id, "other")
        self.assertEqual(switched.safety.disposition, SafetyDisposition.UNDETERMINED)
        self.assertIsNotNone(self.store.previous_stop(self.profile.vehicle_id))
        self.assertIsNone(self.store.previous_stop("other"))
        self.app.select_vehicle(self.owner, self.session, self.profile.vehicle_id, now=NOW)
        returned = self.query(text="Can I drive now?")
        self.assertEqual(returned.safety.disposition, SafetyDisposition.STOP_WHEN_SAFE)

    def test_unresolved_stop_follows_vehicle_across_owner_sessions_and_restart(self):
        snapshot = freeze_episode(generate_episode("gradual_tire_pressure_loss", 42)[0])
        self.query(snapshot=snapshot)
        other_session = self.app.start_session(self.owner, now=NOW, vehicle_id=self.profile.vehicle_id)
        next_message = UserMessage("new-session-follow-up", "Can I drive now?", NOW)
        result = self.app.handle_message(self.owner, other_session, next_message, now=NOW)
        self.assertEqual(result.safety.disposition, SafetyDisposition.STOP_WHEN_SAFE)
        self.assertEqual(result.trace.prior_stop_disposition, "STOP_WHEN_SAFE")
        self.assertTrue(result.trace.stop_guidance_applied)

        self.store.close()
        self.store = OwnershipStore(self.path)
        self.app = CarMindApp(self.store, FakePlannerProvider([]))
        reopened = self.app.handle_message(self.owner, other_session,
                                           UserMessage("restarted-follow-up", "Can I drive now?", NOW), now=NOW)
        self.assertEqual(reopened.safety.disposition, SafetyDisposition.STOP_WHEN_SAFE)
        self.assertEqual(reopened.trace.effective_safety_disposition, "STOP_WHEN_SAFE")

    def test_end_to_end_two_vehicle_acceptance_journey(self):
        self.fixture()
        self.service()
        self.odometer(24200)
        self.assertEqual(self.query().maintenance_state[0].status, "UPCOMING")
        later = NOW + timedelta(minutes=1)
        self.assertEqual(self.odometer(25000, later, later).maintenance_state[0].status, "DUE")
        completed = self.write("record_service_event", {
            "service_type": "oil", "performed_at": later.isoformat(), "odometer": 25000, "unit": "km",
        }, later)
        self.assertEqual(completed.maintenance_state[0].status, "NOT_DUE")
        self.assertEqual(self.store.reminders(self.profile.vehicle_id)[0]["lifecycle"], "COMPLETED")

        tire_snapshot = freeze_episode(generate_episode("gradual_tire_pressure_loss", 42)[0])
        stop = self.query(text="The tire warning is concerning.", at=later, snapshot=tire_snapshot)
        self.assertEqual(stop.safety.disposition, SafetyDisposition.STOP_WHEN_SAFE)
        follow = self.query(text="It feels fine now.", at=later + timedelta(minutes=1))
        self.assertEqual(follow.safety.disposition, SafetyDisposition.STOP_WHEN_SAFE)

        stale_a = self.propose("update_odometer", {
            "reading": 26000, "unit": "km", "occurred_at": (later + timedelta(minutes=2)).isoformat(),
        }, at=later + timedelta(minutes=2))
        vehicle_b = replace(self.profile, vehicle_id="acceptance-b", make="Other", model="Family", mileage_km=None)
        self.app.add_vehicle(self.owner, vehicle_b, now=later + timedelta(minutes=3))
        self.app.select_vehicle(self.owner, self.session, vehicle_b.vehicle_id, now=later + timedelta(minutes=3))
        self.assertIsNone(self.store.previous_stop(vehicle_b.vehicle_id))
        b_time = later + timedelta(minutes=4)
        b_text = "My other car reads 900 km."
        self.app.provider = FakePlannerProvider([command_turn("update_odometer", {
            "reading": 900, "unit": "km", "occurred_at": b_time.isoformat(),
        }, b_text)])
        b_proposal = self.app.handle_message(self.owner, self.session, self.message(b_text, b_time), now=b_time)
        self.assertEqual(b_proposal.status, "confirmation_required")
        self.assertEqual(self.confirm(stale_a, at=b_time + timedelta(minutes=1)).status, "error")
        self.assertEqual(len(self.store.odometer_events(vehicle_b.vehicle_id)), 0)
        self.assertEqual(self.confirm(b_proposal.proposed_commands[0], at=b_time + timedelta(minutes=2)).status, "applied")

        self.store.close()
        self.store = OwnershipStore(self.path)
        self.app = CarMindApp(self.store, FakePlannerProvider([]))
        replay = self.confirm(b_proposal.proposed_commands[0], at=b_time + timedelta(minutes=3))
        self.assertTrue(replay.trace.replayed)
        self.assertEqual(len(self.store.odometer_events(vehicle_b.vehicle_id)), 1)
        self.app.select_vehicle(self.owner, self.session, self.profile.vehicle_id, now=b_time + timedelta(minutes=4))
        final = self.query(text="Can I drive now?", at=b_time + timedelta(minutes=5))
        self.assertEqual(final.safety.disposition, SafetyDisposition.STOP_WHEN_SAFE)
        self.assertEqual(len(self.store.service_records(self.profile.vehicle_id, b_time)), 2)
        self.assertEqual(self.store.service_records(vehicle_b.vehicle_id, b_time), [])
        self.assertEqual(len(self.store.odometer_events(self.profile.vehicle_id)), 4)
        self.assertEqual(self.store.vehicle(self.owner, self.profile.vehicle_id, b_time).profile.mileage_km, 25000)
        self.assertEqual(self.store.vehicle(self.owner, vehicle_b.vehicle_id, b_time).profile.mileage_km, 900)

    def test_backdated_processing_cannot_expose_future_conversation_or_state(self):
        self.query(at=NOW + timedelta(days=1))
        result = self.query(at=NOW)
        self.assertEqual(result.status, "error")
        self.assertEqual(self.app.provider.requests, [])

    def test_switch_and_target_safety_loading_are_atomic(self):
        self.app.add_vehicle(self.owner, replace(self.profile, vehicle_id="other"), now=NOW)
        proposal = self.propose("select_vehicle", {"vehicle_id": "other"})
        original = self.store.previous_stop
        def unavailable(vehicle_id):
            if vehicle_id == "other":
                raise sqlite3.OperationalError("unavailable")
            return original(vehicle_id)
        with patch.object(self.store, "previous_stop", side_effect=unavailable):
            result = self.confirm(proposal)
        self.assertEqual(result.status, "error")
        self.assertEqual(self.store.session(self.session, self.owner)["active_vehicle_id"], self.profile.vehicle_id)
        self.assertEqual(self.store.proposal(proposal.proposal_id, self.session)["state"], "PENDING")

    def test_fake_demo_runs_real_app_and_restart(self):
        output = []
        run_demo(Path(self.tmp.name) / "demo.sqlite3", output.append)
        transcript = "\n".join(output)
        self.assertIn("TEST_ONLY / NON_PRODUCTION / FICTIONAL", transcript)
        self.assertIn("Diagnostic safety: STOP_WHEN_SAFE", transcript)
        self.assertIn("Restart: My demo car; 25000 km; 2 service records", transcript)
        self.assertNotIn("could not be applied", transcript)


class OwnershipDiagnosticTests(unittest.TestCase):
    def test_all_five_scenarios_safety_grounding_and_no_hypothesis_fact_leakage(self):
        for scenario in list_scenarios():
            with self.subTest(scenario=scenario), TemporaryDirectory() as directory:
                snapshot = freeze_episode(generate_episode(scenario, 42)[0])
                spec = SCENARIO_FIXTURES[scenario]
                ids = [o.observation_id for o in snapshot.observations if o.name in spec[2]]
                provider = FakePlannerProvider([{"type": "tool_call", "tool_id": spec[1], "arguments": {}}, scenario_candidate(snapshot, spec, ids=ids)])
                store = OwnershipStore(Path(directory) / "state.sqlite3")
                try:
                    app = CarMindApp(store, provider, mode=ExecutionMode.ROUTED, router=FakeCapabilityRouter(selected=[spec[0]]))
                    now = snapshot.assessment_at
                    owner = app.create_owner(now=now)
                    app.add_vehicle(owner, snapshot.profile, now=now)
                    session = app.start_session(owner, now=now, vehicle_id=snapshot.profile.vehicle_id)
                    result = app.handle_message(owner, session, snapshot.owner_message, now=now, snapshot=snapshot)
                    self.assertEqual(result.status, "complete", result.response)
                    self.assertEqual(result.safety.disposition, evaluate_safety(snapshot).disposition)
                    self.assertEqual(result.trace.planner.validation_failures, [])
                    self.assertEqual(set(result.assessment.assessment.evidence_ids), set(ids))
                    self.assertEqual(store.service_records(snapshot.profile.vehicle_id, now), [])
                    self.assertEqual(len(store.odometer_events(snapshot.profile.vehicle_id)), 1)
                    self.assertEqual(store.reminders(snapshot.profile.vehicle_id), [])
                    self.assertNotIn("ScenarioTruth", json.dumps(provider.requests))
                    self.assertNotIn(scenario, json.dumps(provider.requests))
                    self.assertNotIn(ids[0], result.response)
                    self.assertNotIn("{", result.response)
                    app.provider = FakePlannerProvider([empty_final()])
                    follow = UserMessage("followup", "What should I do? Can I keep driving?", now + timedelta(minutes=1))
                    followup = app.handle_message(owner, session, follow, now=follow.timestamp)
                    initial = json.loads(app.provider.requests[0][1]["content"])
                    prior = initial["ownership_context"]["previous_assessment"]
                    self.assertTrue(prior["observations"])
                    self.assertIn("unconfirmed_hypotheses", prior)
                    if result.safety.disposition == SafetyDisposition.STOP_WHEN_SAFE:
                        self.assertEqual(followup.safety.disposition, SafetyDisposition.STOP_WHEN_SAFE)
                        self.assertIn("Do not treat", followup.response)
                        store.close()
                        store = OwnershipStore(Path(directory) / "state.sqlite3")
                        app = CarMindApp(store, FakePlannerProvider([empty_final()]))
                        second = UserMessage("after-restart", "Can I drive?", follow.timestamp)
                        restarted = app.handle_message(owner, session, second, now=second.timestamp)
                        self.assertEqual(restarted.safety.disposition, SafetyDisposition.STOP_WHEN_SAFE)
                    # Only summary/provenance and deterministic constraint, no raw samples.
                    dump = "\n".join(store.db.iterdump())
                    self.assertNotIn('"value":', dump)
                    self.assertNotIn("SIMULATOR", dump)
                finally:
                    store.close()


if __name__ == "__main__":
    unittest.main()
