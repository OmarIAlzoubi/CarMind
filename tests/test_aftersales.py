"""Offline aftersales workflow and ownership boundaries."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from dataclasses import replace
from carmind.aftersales import demo_slots, InformationNeed, MissingInformation, missing_information
from carmind.aftersales_demo import run_demo
from carmind.app import CarMindApp
from carmind.contracts import VehicleProfile
from carmind.ownership_demo import command_turn, fictional_schedule
from carmind.proactive import NotificationPreferences
from carmind.proactive_runner import ProactiveCycleRunner
from carmind.planner_provider import FakePlannerProvider
from carmind.product import InboundMessage, ProductService
from carmind.storage import OwnershipStore
from carmind.router_provider import FakeCapabilityRouter
from carmind.routing import ExecutionMode
from carmind.simulator import freeze_episode, generate_episode
from carmind.safety import evaluate_safety
from carmind.ownership import parse_command


NOW = datetime(2026, 1, 6, 12, tzinfo=timezone.utc)


class AftersalesTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = OwnershipStore(Path(temp.name) / "owner.sqlite3")
        self.addCleanup(self.store.close)
        self.app = CarMindApp(self.store, FakePlannerProvider([]))
        self.service = ProductService(self.app)
        self.owner = self.app.create_owner(now=NOW, owner_id="owner")
        self.app.add_vehicle(self.owner, VehicleProfile("car", "Fictional", "Everyday", 2024, 18500), now=NOW)
        self.session = self.app.start_session(self.owner, now=NOW, vehicle_id="car", session_id="session")
        self.service.bind_existing("whatsapp", "whatsapp:+15550100000", self.owner, self.session)
        self.number = 0

    def send(self, text, action=None, *, at=NOW, confirmation_id=None, sid=None):
        self.number += 1
        if action:
            self.app.provider = FakePlannerProvider([action])
        return self.service.handle(InboundMessage("whatsapp", "whatsapp:+15550100000",
            sid or f"sid-{self.number}", text, at, confirmation_id=confirmation_id))

    def test_service_request_booking_and_handoff_are_confirmed_and_local(self):
        text = "I want to get the oil checked"
        proposed = self.send(text, command_turn("create_service_request",
            {"intent_type": "inspection", "requested_services": ["oil"], "symptoms": []}, text))
        self.assertEqual(proposed.status, "confirmation_required")
        self.assertEqual(self.store.service_requests(self.owner, "car"), [])
        self.assertEqual([e["event_type"] for e in self.store.business_events(self.owner, "car")], ["service_interest"])
        saved = self.send("confirm", at=NOW + timedelta(minutes=1), confirmation_id=proposed.proposal_id)
        self.assertEqual(saved.status, "applied")
        self.assertIn("No dealer booking", saved.text)
        request_id = self.store.service_requests(self.owner, "car")[0]["id"]
        self.assertEqual(self.store.service_request("different-owner", "car", request_id), None)
        self.assertEqual(self.store.demo_bookings(self.owner, "car"), [])

        slot = demo_slots(NOW + timedelta(minutes=2))[0]
        booking_text = "Book that demo slot"
        booking = self.send(booking_text, command_turn("book_demo_slot",
            {"service_request_id": request_id, "slot_id": slot.slot_id}, booking_text),
            at=NOW + timedelta(minutes=2))
        self.assertEqual(booking.status, "confirmation_required")
        self.assertIn("SIMULATED", booking.text)
        self.assertEqual(self.store.demo_bookings(self.owner, "car"), [])
        confirmed = self.send("confirm", confirmation_id=booking.proposal_id, at=NOW + timedelta(minutes=3))
        self.assertEqual(confirmed.status, "applied")
        self.assertIn("SIMULATION", confirmed.text)
        self.assertEqual(len(self.store.demo_bookings(self.owner, "car")), 1)

        handoff_text = "I want a person to review this"
        handoff = self.send(handoff_text, command_turn("request_human_handoff",
            {"reason": "customer requests follow-up", "service_request_id": request_id}, handoff_text),
            at=NOW + timedelta(minutes=4))
        self.assertEqual(self.store.handoffs(self.owner, "car"), [])
        self.send("confirm", confirmation_id=handoff.proposal_id, at=NOW + timedelta(minutes=5))
        packet = self.store.handoffs(self.owner, "car")[0]["packet"]
        self.assertEqual(packet["request_id"], request_id)
        self.assertNotIn("transcript", packet)
        events = [e["event_type"] for e in self.store.business_events(self.owner, "car")]
        self.assertEqual(events, ["service_interest", "service_request_created", "booking_intent", "human_handoff_requested"])
        self.send("confirm", confirmation_id=handoff.proposal_id, at=NOW + timedelta(minutes=5), sid="handoff-replay")
        self.assertEqual(len(self.store.handoffs(self.owner, "car")), 1)
        self.assertEqual(len(self.store.business_events(self.owner, "car")), 4)

    def test_ambiguous_vehicle_asks_and_explicit_name_resolves_within_owner(self):
        self.app.add_vehicle(self.owner, VehicleProfile("family", "Fictional", "Family", 2025, 9000), now=NOW)
        with self.store.transaction():
            self.store.db.execute("UPDATE sessions SET selection_explicit=0 WHERE id=?", (self.session,))
        ambiguous = self.send("What should I check?", at=NOW + timedelta(minutes=1))
        self.assertEqual(ambiguous.status, "clarification_required")
        self.assertEqual(self.store.session(self.session, self.owner)["active_vehicle_id"], "car")
        named = self.send("Tell me about the Family", at=NOW + timedelta(minutes=2),
                          action={"type": "request_clarification", "question": "What would you like to know?"})
        self.assertEqual(self.store.session(self.session, self.owner)["active_vehicle_id"], "family")
        self.assertEqual(named.active_vehicle_id, "family")

    def test_information_gaps_depend_on_the_task_without_inferring_specs(self):
        profile = VehicleProfile("unknown-mileage", "Fictional", "Everyday", 2024)
        self.assertEqual(missing_information(InformationNeed.GENERAL, profile,
            market=None, odometer_at=None, now=NOW), ())
        gaps = missing_information(InformationNeed.MAINTENANCE_TIMING, profile,
            market=None, odometer_at=None, now=NOW)
        self.assertEqual(gaps, (MissingInformation.CURRENT_MILEAGE, MissingInformation.MARKET,
                                MissingInformation.APPLICABLE_SOURCE))
        profile = replace(profile, mileage_km=1000)
        gaps = missing_information(InformationNeed.MAINTENANCE_TIMING, profile,
            market="TEST_ONLY", odometer_at=NOW - timedelta(days=91), now=NOW, source_available=True)
        self.assertEqual(gaps, (MissingInformation.FRESH_MILEAGE,))
        self.assertIsNone(profile.engine)

    def test_routed_commands_require_aftersales_capability_and_can_expand(self):
        import json
        text = "I want an inspection"
        command = command_turn("create_service_request", {"intent_type": "inspection",
            "requested_services": ["inspection"], "symptoms": []}, text)
        self.app.mode = ExecutionMode.ROUTED
        self.app.router = FakeCapabilityRouter(selected=["tires"])
        self.app.provider = FakePlannerProvider([command,
            {"type": "request_clarification", "question": "What would you like checked?"}])
        reply = self.send(text)
        self.assertEqual(reply.status, "clarification_required")
        first = json.loads(self.app.provider.requests[0][1]["content"])
        self.assertNotIn("create_service_request", first["command_schemas"])
        self.assertEqual(self.store.business_events(self.owner), [])
        self.app.provider = FakePlannerProvider([
            {"type": "expand_capabilities", "capability_ids": ["aftersales"]}, command])
        reply = self.send(text, at=NOW + timedelta(minutes=1))
        self.assertEqual(reply.status, "confirmation_required")
        second = json.loads(self.app.provider.requests[1][1]["content"])
        self.assertIn("create_service_request", second["command_schemas"])

    def test_confirmation_displays_all_request_fields_and_no_raw_business_payload(self):
        text = "I want an inspection for a rattle, Tuesday morning, north branch"
        args = {"intent_type": "inspection", "requested_services": ["inspection"],
                "symptoms": ["rattle"], "preferred_time_window": "Tuesday morning",
                "preferred_location": "north branch"}
        proposed = self.send(text, command_turn("create_service_request", args, text), sid="same-request")
        for value in ("inspection", "rattle", "Tuesday morning", "north branch"):
            self.assertIn(value, proposed.text)
        self.assertEqual(self.send(text, sid="same-request"), proposed)
        self.send("confirm", confirmation_id=proposed.proposal_id, at=NOW + timedelta(minutes=1))
        for event in self.store.business_events(self.owner):
            self.assertEqual(event["external_sharing_consent"], 0)
            self.assertNotIn("symptoms", event["payload"])
            self.assertNotIn("rattle", str(event["payload"]))
        invalid = command_turn("create_service_request", {**args, "safety_disposition": "safe"}, text)["command"]
        with self.assertRaises(ValueError):
            parse_command(invalid, text)

    def test_aftersales_confirmation_cannot_clear_prior_stop(self):
        snapshot = freeze_episode(generate_episode("sustained_temperature_rise", 42)[0])
        stop = evaluate_safety(snapshot)
        with self.store.transaction():
            self.store.save_stop("car", stop, NOW)
        text = "I want a cooling inspection"
        proposed = self.send(text, command_turn("create_service_request", {
            "intent_type": "inspection", "requested_services": ["inspection"], "symptoms": []}, text))
        self.assertIsNotNone(proposed.safety_notice)
        confirmed = self.send("confirm", confirmation_id=proposed.proposal_id, at=NOW + timedelta(minutes=1))
        self.assertIsNotNone(confirmed.safety_notice)
        request = self.store.service_requests(self.owner, "car")[0]
        self.assertEqual(request["data"]["safety_disposition"], "STOP_WHEN_SAFE")
        self.assertIsNotNone(self.store.previous_stop("car"))

    def test_migration_from_previous_schema_keeps_vehicle(self):
        import sqlite3
        path = self.store.db.execute("PRAGMA database_list").fetchone()[2]
        self.store.close()
        db = sqlite3.connect(path)
        try:
            for table in ("twilio_delivery_status", "twilio_inbound_receipts", "business_events",
                          "handoff_requests", "demo_bookings", "service_requests"):
                db.execute(f"DROP TABLE {table}")
            db.execute("ALTER TABLE sessions DROP COLUMN selection_explicit")
            db.execute("PRAGMA user_version=4")
            db.commit()
        finally:
            db.close()
        migrated = OwnershipStore(path)
        try:
            self.assertEqual(migrated.db.execute("PRAGMA user_version").fetchone()[0], 5)
            self.assertEqual(migrated.vehicle(self.owner, "car", NOW).profile.model, "Everyday")
            self.assertEqual(migrated.service_requests(self.owner, "car"), [])
        finally:
            migrated.close()

    def test_public_synthetic_enterprise_journey(self):
        with TemporaryDirectory() as directory:
            report = run_demo(Path(directory) / "demo.sqlite3")
        self.assertEqual(report["mode"], "OFFLINE_SYNTHETIC")
        self.assertEqual(report["conversation"][0]["status"], "complete")
        self.assertEqual(report["maintenance"][0]["status"], "DUE")
        self.assertEqual(len(report["service_requests"]), 1)
        self.assertEqual(len(report["demo_bookings"]), 1)
        self.assertEqual(len(report["handoffs"]), 1)
        self.assertEqual(report["event_counts"]["service_interest"], 1)
        self.assertEqual(report["event_counts"]["service_request_created"], 1)
        self.assertEqual(report["event_counts"]["booking_intent"], 1)
        self.assertNotIn("+1555", str(report))

    def test_proactive_due_interest_then_confirmed_completion_cancels_stale_delivery(self):
        from types import SimpleNamespace
        with TemporaryDirectory() as directory:
            path = Path(directory) / "demo.sqlite3"
            report = run_demo(path)
            store = OwnershipStore(path)
            try:
                app = CarMindApp(store, FakePlannerProvider([]),
                    approved_schedules=(fictional_schedule(),), allow_test_schedules=True)
                service = ProductService(app)
                at = NOW + timedelta(minutes=11)
                app.proactive.set_preferences("fictional-owner", NotificationPreferences(
                    enabled=True, preferred_channel="console", quiet_enabled=False), now=at)
                app.evaluate_proactive_state(now=at)
                app.evaluate_proactive_state(now=at)
                self.assertEqual(store.business_event_counts("fictional-owner")["maintenance_due"], 1)
                self.assertEqual(report["event_counts"]["service_interest"], 1)
                self.assertTrue(store.notifications(statuses=("PENDING",)))
                text = "I changed the oil at 25000 km just now"
                app.provider = FakePlannerProvider([command_turn("record_service_event", {
                    "service_type": "oil", "performed_at": at.isoformat(), "odometer": 25000, "unit": "km"}, text)])
                proposed = service.handle(InboundMessage("whatsapp", "whatsapp:+15550000000", "completion", text, at))
                self.assertEqual(proposed.status, "confirmation_required")
                service.handle(InboundMessage("whatsapp", "whatsapp:+15550000000", "completion-confirm", "confirm", at,
                                               confirmation_id=proposed.proposal_id))
                events = store.proactive_events(owner_id="fictional-owner")
                self.assertTrue(all(event["status"] == "RESOLVED" for event in events if event["event_type"] == "MAINTENANCE_DUE"))
                self.assertEqual(store.business_event_counts("fictional-owner")["maintenance_completed"], 1)
                sent = []
                runner = ProactiveCycleRunner(app, {"console": SimpleNamespace(send=lambda *args: sent.append(args))})
                runner.deliver(now=at)
                self.assertEqual(sent, [])
            finally:
                store.close()
