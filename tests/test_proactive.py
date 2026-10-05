"""Offline synthetic proactive ownership journeys; no providers or private manuals."""

from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import BytesIO, StringIO
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from carmind.app import CarMindApp
from carmind.contracts import MaintenanceRecord, UserMessage, VehicleProfile
from carmind.manufacturer_ingestion import (ApplicabilityStatus, ManufacturerIngestionService,
                                             VehicleManualRegistry)
from carmind.manufacturer_knowledge import (KnowledgePack, MaintenanceRule, ManufacturerSource,
                                             ROOT, VehicleKnowledgeProfile)
from carmind.ownership_demo import command_turn, fictional_schedule
from carmind.planner_provider import FakePlannerProvider
from carmind.proactive import (EventStatus, EventType, NotificationPreferences, ProactivePolicy,
                               ProactiveService)
from carmind.product import InboundMessage, ProductService
from carmind.storage import OwnershipStore
from carmind.tools import MaintenanceRequest
from carmind.web import make_handler
from carmind.whatsapp import WhatsAppNotificationSender
from test_manufacturer_ingestion import make_pdf


START = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
SOON = START + timedelta(days=5)


class ProactiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "owner.sqlite3"
        self.store = OwnershipStore(self.path)
        self.addCleanup(self.store.close)
        self.app = CarMindApp(self.store, FakePlannerProvider([]),
                              approved_schedules=(fictional_schedule(),), allow_test_schedules=True)
        self.owner = self.app.create_owner(now=START, owner_id="owner-a")
        self.vehicle = VehicleProfile("vehicle-a", "Fictional", "Everyday", 2024, 15000,
                                      "Fictional gasoline engine")
        self.app.add_vehicle(self.owner, self.vehicle, now=START, market="TEST_ONLY")
        self.session = self.app.start_session(self.owner, now=START, vehicle_id=self.vehicle.vehicle_id)
        with self.store.transaction():
            self.store.record_service("first-oil", self.vehicle.vehicle_id,
                MaintenanceRecord("first-oil", "oil", START, 15000), START, None)

    def reading(self, km, at, *, event_id=None):
        event_id = event_id or f"reading-{at.timestamp()}-{km}"
        with self.store.transaction():
            self.store.record_odometer(event_id, self.vehicle.vehicle_id, km, km, "km", at, at, None)
        return event_id

    def enable(self, **changes):
        values = {**NotificationPreferences(enabled=True, preferred_channel="whatsapp",
                    quiet_enabled=False).__dict__, **changes}
        return self.app.proactive.set_preferences(self.owner, NotificationPreferences(**values), now=START)

    def events(self, at=SOON, *, include_resolved=False):
        return self.app.proactive.events(self.owner, self.vehicle.vehicle_id,
                                         include_resolved=include_resolved, now=at)

    def test_mileage_due_soon_due_overdue_dedup_and_escalation(self):
        self.enable()
        self.reading(24500, SOON)
        first = self.app.evaluate_proactive_state(now=SOON)
        again = self.app.evaluate_proactive_state(now=SOON)
        self.assertEqual((first.vehicles_evaluated, first.maintenance_rules_evaluated,
                          first.events_created, again.events_created, again.duplicate_events_prevented),
                         (1, 1, 1, 0, 1))
        event_id = self.events()[0].event_id
        self.assertEqual(self.events()[0].event_type, EventType.MAINTENANCE_DUE_SOON)
        self.assertEqual(self.events()[0].due_km, 25000)
        self.assertEqual(self.events()[0].source_page, "fixture")
        self.assertEqual(len(self.store.notifications(owner_id=self.owner, statuses=("PENDING",))), 1)
        due_at = SOON + timedelta(days=1)
        self.reading(25000, due_at)
        self.app.evaluate_proactive_state(now=due_at)
        self.assertEqual(self.events(due_at)[0].event_id, event_id)
        self.assertEqual(self.events(due_at)[0].event_type, EventType.MAINTENANCE_DUE)
        self.assertEqual([row["event_type"] for row in self.store.notifications(owner_id=self.owner,
                          statuses=("PENDING",))], ["MAINTENANCE_DUE"])
        overdue_at = due_at + timedelta(days=1)
        self.reading(25100, overdue_at)
        self.app.evaluate_proactive_state(now=overdue_at)
        self.assertEqual(self.events(overdue_at)[0].event_type, EventType.MAINTENANCE_OVERDUE)

    def test_time_due_without_extrapolating_stale_mileage(self):
        due_at = datetime(2026, 7, 1, 12, tzinfo=timezone.utc)
        result = self.app.evaluate_proactive_state(now=due_at)
        types = {event.event_type for event in self.events(due_at)}
        self.assertEqual(result.maintenance_rules_evaluated, 1)
        self.assertIn(EventType.ODOMETER_UPDATE_REQUESTED, types)
        self.assertIn(EventType.MAINTENANCE_DUE, types)
        self.assertEqual(next(e for e in self.events(due_at) if e.source_type == "MANUFACTURER").current_mileage, None)

    def test_date_due_message_does_not_claim_mileage_due(self):
        from carmind.proactive import notification_text

        due_at = datetime(2026, 7, 1, 12, tzinfo=timezone.utc)
        self.reading(24500, due_at)
        self.app.evaluate_proactive_state(now=due_at)
        event = next(e for e in self.events(due_at) if e.source_type == "MANUFACTURER")
        self.assertEqual(event.event_type, EventType.MAINTENANCE_DUE)
        self.assertIn("2026-07-01", notification_text(event))
        self.assertNotIn("25000 km", notification_text(event))

    def test_no_verified_schedule_and_pdf_applicability_do_not_activate_rules(self):
        self.app.schedules = ()
        self.reading(24500, SOON)
        self.assertEqual(self.app.evaluate_proactive_state(now=SOON).maintenance_rules_evaluated, 0)
        self.assertEqual(self.events(), ())
        local = ROOT / ".local"
        local.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=local) as data:
            registry = VehicleManualRegistry(Path(data) / "manufacturer")
            try:
                pdf = Path(data) / "unverified.pdf"
                make_pdf(pdf, "TEST-ONLY FICTIONAL oil schedule every 10000 km")
                ingested = ManufacturerIngestionService(self.store, registry).ingest(
                    self.owner, self.vehicle.vehicle_id, pdf, now=SOON)
                self.assertEqual(ingested.applicability, ApplicabilityStatus.UNVERIFIED)
                conflict = Path(data) / "conflicting.pdf"
                make_pdf(conflict, "Manufacturer: Other Maker\nModel: Other Car\nModel year: 2024\nMarket: TEST_ONLY")
                conflicted = ManufacturerIngestionService(self.store, registry).ingest(
                    self.owner, self.vehicle.vehicle_id, conflict, now=SOON)
                self.assertEqual(conflicted.applicability, ApplicabilityStatus.CONFLICTING)
                self.app.evaluate_proactive_state(now=SOON)
                self.assertFalse(any(event.source_type == "MANUFACTURER" for event in self.events()))
            finally:
                registry.close()

    def test_stale_odometer_resolves_on_update_and_correction_retracts_due(self):
        stale_at = START + timedelta(days=31)
        self.app.evaluate_proactive_state(now=stale_at)
        self.assertEqual(self.events(stale_at)[0].event_type, EventType.ODOMETER_UPDATE_REQUESTED)
        self.assertIsNone(self.events(stale_at)[0].current_mileage)
        bad = self.reading(25100, stale_at)
        self.app.evaluate_proactive_state(now=stale_at)
        self.assertEqual({event.event_type for event in self.events(stale_at)},
                         {EventType.MAINTENANCE_OVERDUE})
        with self.store.transaction():
            self.store.supersede("odometer_events", bad, self.vehicle.vehicle_id, "corrected", stale_at)
            self.store.record_odometer("corrected", self.vehicle.vehicle_id, 19000, 19000,
                                       "km", stale_at, stale_at, None)
        self.app.evaluate_proactive_state(now=stale_at)
        self.assertEqual(self.events(stale_at), ())
        self.assertTrue(all(e.status == EventStatus.RESOLVED for e in self.events(stale_at, include_resolved=True)))

    def test_owner_reminders_need_no_manual_and_service_resolution_is_item_specific(self):
        self.app.schedules = ()
        self.reading(18500, SOON)
        reminder_id = self.app.proactive.add_owner_reminder(self.owner, self.vehicle.vehicle_id,
            "tires", now=SOON, after_km=500)
        self.assertEqual(self.events()[0].event_type, EventType.OWNER_REMINDER_DUE_SOON)
        due_at = SOON + timedelta(days=1)
        self.reading(19000, due_at)
        self.app.evaluate_proactive_state(now=due_at)
        self.assertEqual(self.events(due_at)[0].source_type, "OWNER")
        self.assertEqual(self.events(due_at)[0].source_id, reminder_id)
        with self.store.transaction():
            self.store.record_service("unrelated-oil", self.vehicle.vehicle_id,
                MaintenanceRecord("unrelated-oil", "oil", due_at, 19000), due_at, None)
        self.app.evaluate_proactive_state(now=due_at)
        self.assertEqual(self.events(due_at)[0].status, EventStatus.OPEN)
        with self.store.transaction():
            self.store.record_service("tire-service", self.vehicle.vehicle_id,
                MaintenanceRecord("tire-service", "tires", due_at, 19000), due_at, None)
        self.app.evaluate_proactive_state(now=due_at)
        self.assertEqual(self.events(due_at), ())
        self.assertEqual(self.store.owner_reminders(self.owner, self.vehicle.vehicle_id)[0]["status"], "RESOLVED")
        with self.store.transaction():
            self.store.supersede("services", "tire-service", self.vehicle.vehicle_id, None, due_at)
        self.app.evaluate_proactive_state(now=due_at)
        self.assertEqual(self.events(due_at)[0].source_type, "OWNER")
        self.assertEqual(self.store.owner_reminders(self.owner, self.vehicle.vehicle_id)[0]["status"], "ACTIVE")

    def test_owner_calendar_reminder_and_preference_validation(self):
        self.app.schedules = ()
        self.app.proactive.add_owner_reminder(self.owner, self.vehicle.vehicle_id, "battery",
                                               now=START, after_months=6)
        july = datetime(2026, 7, 1, 12, tzinfo=timezone.utc)
        self.app.evaluate_proactive_state(now=july)
        self.assertIn(EventType.OWNER_REMINDER_DUE, {e.event_type for e in self.events(july)})
        self.assertEqual(self.store.notifications(owner_id=self.owner), [])
        with self.assertRaises(ValueError):
            NotificationPreferences(enabled=True, timezone="not/a/timezone")
        with self.assertRaises(ValueError):
            self.app.proactive.add_owner_reminder(self.owner, self.vehicle.vehicle_id, "oil",
                                                   now=july, after_km=500)

    def test_quiet_hours_defer_then_release_and_failure_does_not_change_event(self):
        self.enable(timezone="Asia/Riyadh", quiet_enabled=True, quiet_start="22:00", quiet_end="08:00")
        quiet_at = datetime(2026, 1, 5, 21, tzinfo=timezone.utc)  # 00:00 in Riyadh
        self.reading(24500, quiet_at)
        self.app.evaluate_proactive_state(now=quiet_at)
        outbox = self.store.notifications(owner_id=self.owner)
        self.assertEqual(outbox[0]["status"], "DEFERRED")
        after_quiet = datetime(2026, 1, 6, 5, tzinfo=timezone.utc)
        self.app.evaluate_proactive_state(now=after_quiet)
        self.assertEqual(self.store.notifications(owner_id=self.owner)[0]["status"], "PENDING")
        class BrokenSender:
            def send(self, notification, text):
                raise RuntimeError("provider failed")
        self.assertEqual(self.app.proactive.dispatch({"whatsapp": BrokenSender()}, now=after_quiet).failed, 1)
        self.assertEqual(self.store.notifications(owner_id=self.owner)[0]["status"], "FAILED")
        self.assertEqual(self.events(after_quiet)[0].status, EventStatus.OPEN)

    def test_acknowledgement_cooldown_and_restart_are_durable(self):
        self.enable()
        self.reading(24500, SOON)
        self.app.evaluate_proactive_state(now=SOON)
        sent = []
        class FakeSender:
            def send(self, notification, text):
                sent.append((notification.owner_id, text))
        self.assertEqual(self.app.proactive.dispatch({"whatsapp": FakeSender()}, now=SOON).sent, 1)
        later = SOON + timedelta(days=15)
        self.reading(24500, later)
        self.app.evaluate_proactive_state(now=later)
        self.assertEqual(len(self.store.notifications(owner_id=self.owner)), 1)
        self.app.proactive.acknowledge(self.owner, self.vehicle.vehicle_id, self.events(later)[0].event_id, now=later)
        self.assertEqual(self.events(later)[0].status, EventStatus.ACKNOWLEDGED)
        self.assertEqual(len(self.store.service_records(self.vehicle.vehicle_id, later)), 1)
        self.store.close()
        self.store = OwnershipStore(self.path)
        self.addCleanup(self.store.close)
        self.app = CarMindApp(self.store, FakePlannerProvider([]),
                              approved_schedules=(fictional_schedule(),), allow_test_schedules=True)
        self.app.evaluate_proactive_state(now=later)
        self.assertEqual(self.events(later)[0].status, EventStatus.ACKNOWLEDGED)
        self.assertEqual(len(self.store.notifications(owner_id=self.owner)), 1)
        self.assertTrue(self.app.proactive.preferences(self.owner).enabled)

    def test_cooldown_creates_one_followup_after_thirty_days(self):
        self.enable()
        self.reading(24500, SOON)
        self.app.evaluate_proactive_state(now=SOON)
        class FakeSender:
            def send(self, notification, text):
                return None
        self.app.proactive.dispatch({"whatsapp": FakeSender()}, now=SOON)
        early = SOON + timedelta(days=29)
        self.reading(24500, early)
        self.app.evaluate_proactive_state(now=early)
        self.assertEqual(len(self.store.notifications(owner_id=self.owner)), 1)
        later = SOON + timedelta(days=30)
        self.reading(24500, later)
        self.app.evaluate_proactive_state(now=later)
        self.assertEqual([n["status"] for n in self.store.notifications(owner_id=self.owner)],
                         ["SENT", "PENDING"])
        self.app.evaluate_proactive_state(now=later)
        self.assertEqual(len(self.store.notifications(owner_id=self.owner)), 2)

    def test_owner_reminder_from_planner_requires_exact_confirmation(self):
        text = "Remind me to rotate the tires in 5000 km."
        self.app.provider = FakePlannerProvider([command_turn("create_owner_reminder",
            {"maintenance_item": "tires", "after_km": 5000}, text)])
        proposed = self.app.handle_message(self.owner, self.session,
            UserMessage("owner-reminder-propose", text, SOON), now=SOON)
        self.assertEqual(proposed.status, "confirmation_required")
        self.assertEqual(self.store.owner_reminders(self.owner, self.vehicle.vehicle_id), [])
        confirmed = self.app.handle_message(self.owner, self.session,
            UserMessage("owner-reminder-confirm", "I confirm this reminder.", SOON), now=SOON,
            confirmation_id=proposed.proposed_commands[0].proposal_id)
        self.assertEqual(confirmed.status, "applied")
        self.assertEqual(self.store.owner_reminders(self.owner, self.vehicle.vehicle_id)[0]["due_km"], 20000)
        replay = self.app.handle_message(self.owner, self.session,
            UserMessage("owner-reminder-replay", "I confirm again.", SOON), now=SOON,
            confirmation_id=proposed.proposed_commands[0].proposal_id)
        self.assertEqual(replay.status, "applied")
        self.assertEqual(len(self.store.owner_reminders(self.owner, self.vehicle.vehicle_id)), 1)

    def test_service_correction_restores_old_due_cycle(self):
        self.reading(25100, SOON)
        self.app.evaluate_proactive_state(now=SOON)
        old_id = self.events()[0].event_id
        service_at = SOON + timedelta(days=1)
        with self.store.transaction():
            self.store.record_odometer("after-service-reading", self.vehicle.vehicle_id, 25300, 25300,
                                       "km", service_at, service_at, None)
            self.store.record_service("new-oil", self.vehicle.vehicle_id,
                MaintenanceRecord("new-oil", "oil", service_at, 25300), service_at, None)
        self.app.evaluate_proactive_state(now=service_at)
        self.assertEqual(self.store.proactive_event(old_id, self.owner, self.vehicle.vehicle_id)["status"], "RESOLVED")
        with self.store.transaction():
            self.store.supersede("services", "new-oil", self.vehicle.vehicle_id, "corrected-service", service_at)
            self.store.record_service("corrected-service", self.vehicle.vehicle_id,
                MaintenanceRecord("corrected-service", "tires", service_at, 25300), service_at, None)
        self.app.evaluate_proactive_state(now=service_at)
        self.assertEqual(self.store.proactive_event(old_id, self.owner, self.vehicle.vehicle_id)["status"], "OPEN")

    def test_multi_vehicle_and_owner_isolation_with_whatsapp_sender(self):
        self.enable()
        other_vehicle = VehicleProfile("vehicle-b", "Fictional", "Everyday", 2024, 15000,
                                       "Fictional gasoline engine")
        self.app.add_vehicle(self.owner, other_vehicle, now=START, market="TEST_ONLY")
        second_owner = self.app.create_owner(now=START, owner_id="owner-b")
        third_vehicle = VehicleProfile("vehicle-c", "Fictional", "Everyday", 2024, 15000,
                                       "Fictional gasoline engine")
        self.app.add_vehicle(second_owner, third_vehicle, now=START, market="TEST_ONLY")
        self.app.proactive.set_preferences(second_owner, NotificationPreferences(enabled=True,
            preferred_channel="whatsapp", quiet_enabled=False), now=START)
        with self.store.transaction():
            for vehicle_id in ("vehicle-b", "vehicle-c"):
                self.store.record_service("service-" + vehicle_id, vehicle_id,
                    MaintenanceRecord("service-" + vehicle_id, "oil", START, 15000), START, None)
                self.store.record_odometer("reading-" + vehicle_id, vehicle_id, 24500, 24500,
                                           "km", SOON, SOON, None)
            self.store.record_odometer("reading-a", "vehicle-a", 24500, 24500, "km", SOON, SOON, None)
        self.app.evaluate_proactive_state(now=SOON)
        self.assertEqual(len(self.store.proactive_events(owner_id=self.owner, vehicle_id="vehicle-a")), 1)
        self.assertEqual(len(self.store.proactive_events(owner_id=self.owner, vehicle_id="vehicle-b")), 1)
        self.assertEqual(len(self.store.proactive_events(owner_id=second_owner, vehicle_id="vehicle-c")), 1)
        with self.assertRaises(ValueError):
            self.app.proactive.events(self.owner, "vehicle-c", now=SOON)
        with self.store.transaction():
            self.store.bind_channel("whatsapp", "recipient-a", self.owner, self.session)
            session_b = "session-b"
            self.store.create_session(session_b, second_owner, START, "vehicle-c")
            self.store.bind_channel("whatsapp", "recipient-b", second_owner, session_b)
        deliveries = []
        sender = WhatsAppNotificationSender(self.store, lambda recipient, text: deliveries.append((recipient, text)))
        self.assertEqual(self.app.proactive.dispatch({"whatsapp": sender}, now=SOON).sent, 3)
        self.assertEqual([recipient for recipient, _ in deliveries].count("recipient-a"), 2)
        self.assertEqual([recipient for recipient, _ in deliveries].count("recipient-b"), 1)

    def test_web_projection_and_acknowledgement_are_vehicle_scoped(self):
        self.reading(24500, SOON)
        self.app.evaluate_proactive_state(now=SOON)
        service = ProductService(self.app)
        service.bind_existing("web", "local", self.owner, self.session)
        overview = service.overview("web", "local", now=SOON)
        event = overview["needs_attention"][0]
        self.assertIn("approaching", event["text_en"])
        self.assertFalse(overview["notification_preferences"]["enabled"])
        service.acknowledge_proactive("web", "local", self.vehicle.vehicle_id, event["id"], now=SOON)
        self.assertEqual(service.overview("web", "local", now=SOON)["needs_attention"][0]["status"], "ACKNOWLEDGED")
        self.assertIn("Due point", service.explain_proactive("web", "local", self.vehicle.vehicle_id,
                                                               event["id"], now=SOON))

    def test_web_preferences_and_ack_routes_use_active_vehicle(self):
        self.reading(24500, SOON)
        self.app.evaluate_proactive_state(now=SOON)
        service = ProductService(self.app)
        service.bind_existing("web", "local", self.owner, self.session)
        handler_type = make_handler(service, web_identity="local")
        handler = object.__new__(handler_type)
        replies = []
        handler.send_response = replies.append
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None

        def post(route, payload):
            body = json.dumps(payload).encode()
            handler.path = route
            handler.headers = {"Content-Length": str(len(body)), "Content-Type": "application/json"}
            handler.rfile, handler.wfile = BytesIO(body), BytesIO()
            with patch("carmind.web.datetime") as clock:
                clock.now.return_value = SOON
                handler.do_POST()
            return json.loads(handler.wfile.getvalue())

        preferences = post("/api/proactive/preferences", {"enabled": True,
            "preferred_channel": "web", "timezone": "Asia/Riyadh", "language": "ar"})
        self.assertTrue(preferences["enabled"])
        event_id = self.events()[0].event_id
        with self.assertRaises(ValueError):
            service.acknowledge_proactive("web", "local", "other-vehicle", event_id, now=SOON)
        self.assertEqual(post("/api/proactive/ack", {"vehicle_id": self.vehicle.vehicle_id,
                         "event_id": event_id})["status"], "acknowledged")
        self.assertEqual(replies, [200, 200])

    def test_milestone_nine_database_migrates_without_losing_owner(self):
        self.store.close()
        connection = sqlite3.connect(self.path)
        try:
            for name in ("notification_claims", "notification_attempts", "notification_outbox",
                         "proactive_events", "owner_reminders", "notification_preferences"):
                connection.execute(f"DROP TABLE {name}")
            connection.execute("PRAGMA user_version=2")
            connection.commit()
        finally:
            connection.close()
        self.store = OwnershipStore(self.path)
        self.addCleanup(self.store.close)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 4)
        self.assertEqual(self.store.owner_for_vehicle(self.vehicle.vehicle_id), self.owner)
        self.assertFalse(ProactiveService(CarMindApp(self.store, FakePlannerProvider([]))).preferences(self.owner).enabled)

    def test_cli_run_list_and_pending_without_provider_or_scheduler(self):
        from carmind.proactive_cli import main
        self.store.close()
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["--db", str(self.path), "run", "--now",
                                   (START + timedelta(days=31)).isoformat()]), 0)
            self.assertEqual(main(["--db", str(self.path), "list"]), 0)
            self.assertEqual(main(["--db", str(self.path), "pending"]), 0)
        self.assertIn("ODOMETER_UPDATE_REQUESTED", output.getvalue())
        self.store = OwnershipStore(self.path)
        self.addCleanup(self.store.close)

    def test_full_synthetic_secretary_journey_with_verified_pdf_and_whatsapp_reply(self):
        local = ROOT / ".local"
        local.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=local) as data:
            registry = VehicleManualRegistry(Path(data) / "manufacturer")
            try:
                pdf = Path(data) / "fictional-schedule.pdf"
                make_pdf(pdf, "Manufacturer: Fictional\nModel: Everyday\nModel year: 2024\n"
                              "Market: TEST_ONLY\nTEST-ONLY FICTIONAL oil service every 5000 km")
                source = ManufacturerIngestionService(self.store, registry).ingest(
                    self.owner, self.vehicle.vehicle_id, pdf,
                    metadata={"document_type": "maintenance_schedule"}, now=START)
                self.assertEqual(source.applicability, ApplicabilityStatus.DOCUMENT_VERIFIED)
                profile = VehicleKnowledgeProfile("Fictional", "Everyday", 2024, "TEST_ONLY",
                                                   "Fictional gasoline engine")
                provenance = ManufacturerSource(source.source_id, "Fictional", "Everyday", 2024,
                    "TEST_ONLY", "TEST-ONLY FICTIONAL schedule", "TEST_ONLY_NON_PRODUCTION_FICTIONAL",
                    "https://example.invalid/test-only-schedule", "2026-01-01")
                rule = MaintenanceRule("synthetic-oil-5000", source.source_id, "oil", "REPLACE",
                    "NORMAL", "TEST-ONLY FICTIONAL schedule", "1", trigger="MILEAGE", interval_km=5000)
                pack = KnowledgePack(profile, (provenance,), (rule,), poc_only=True)
                # This vetted test rule is injected explicitly; ingestion never extracts authoritative rules.
                self.app.schedules = (MaintenanceRequest(pack, profile, operating_condition="NORMAL"),)
                self.app.proactive = ProactiveService(self.app, ProactivePolicy(due_soon_km=2000))
                self.enable(timezone="Asia/Riyadh", language="ar")
                product = ProductService(self.app)
                product.bind_existing("whatsapp", "synthetic-recipient", self.owner, self.session)

                first = self.app.evaluate_proactive_state(now=START)
                self.assertEqual(first.events_created, 0)
                before = START + timedelta(days=1)
                self.reading(17500, before)
                self.assertEqual(self.app.evaluate_proactive_state(now=before).events_created, 0)
                approaching = START + timedelta(days=2)
                self.reading(18500, approaching)
                report = self.app.evaluate_proactive_state(now=approaching)
                event = self.events(approaching)[0]
                self.assertEqual((event.event_type, event.source_id, event.source_page),
                                 (EventType.MAINTENANCE_DUE_SOON, source.source_id, "1"))
                self.assertEqual(report.pending_notifications, 1)
                delivered = []
                sender = WhatsAppNotificationSender(self.store,
                    lambda recipient, text: delivered.append((recipient, text)))
                self.assertEqual(self.app.proactive.dispatch({"whatsapp": sender}, now=approaching).sent, 1)
                self.assertEqual(delivered[0][0], "synthetic-recipient")
                self.assertIn("الصيانة", delivered[0][1])
                explanation = product.handle(InboundMessage("whatsapp", "synthetic-recipient", "why-reminder",
                                                             "Why this reminder?", approaching, locale="en"))
                self.assertIn(source.source_id, explanation.text)
                self.assertEqual(self.app.provider.requests, [])
                self.app.proactive.acknowledge(self.owner, self.vehicle.vehicle_id, event.event_id, now=approaching)
                self.assertEqual(self.events(approaching)[0].status, EventStatus.ACKNOWLEDGED)

                due_at = START + timedelta(days=3)
                self.reading(20000, due_at)
                self.app.evaluate_proactive_state(now=due_at)
                self.assertEqual(self.events(due_at)[0].event_type, EventType.MAINTENANCE_DUE)
                completed_at = START + timedelta(days=4)
                text = "I changed the oil today at 20300 km."
                self.app.provider = FakePlannerProvider([command_turn("record_service_event",
                    {"service_type": "oil", "performed_at": completed_at.isoformat(),
                     "odometer": 20300, "unit": "km"}, text)])
                proposed = product.handle(InboundMessage("whatsapp", "synthetic-recipient", "service-proposal",
                                                         text, completed_at, locale="en"))
                self.assertEqual(proposed.status, "confirmation_required")
                self.assertIn(event.event_id, json.dumps(self.app.provider.requests))
                self.assertEqual(len(self.store.service_records(self.vehicle.vehicle_id, completed_at)), 1)
                confirmed = product.handle(InboundMessage("whatsapp", "synthetic-recipient", "service-confirm",
                    "Confirm the displayed service.", completed_at, locale="en",
                    confirmation_id=proposed.proposal_id))
                self.assertEqual(confirmed.status, "applied")
                self.assertEqual(len(self.store.service_records(self.vehicle.vehicle_id, completed_at)), 2)
                self.assertEqual(self.events(completed_at), ())
                self.assertEqual(self.store.proactive_event(event.event_id, self.owner,
                                                              self.vehicle.vehicle_id)["status"], "RESOLVED")
                request, state = self.app.read_maintenance(self.owner, self.vehicle.vehicle_id,
                                                           now=completed_at)
                self.assertIsNotNone(request)
                self.assertEqual(state[0].due_odometer_km, 25300)
                count = len(self.store.notifications(owner_id=self.owner))
                self.store.close()
                self.store = OwnershipStore(self.path)
                self.addCleanup(self.store.close)
                self.app = CarMindApp(self.store, FakePlannerProvider([]),
                    approved_schedules=(MaintenanceRequest(pack, profile, operating_condition="NORMAL"),),
                    allow_test_schedules=True)
                self.app.proactive = ProactiveService(self.app, ProactivePolicy(due_soon_km=2000))
                replay = self.app.evaluate_proactive_state(now=completed_at)
                self.assertEqual(replay.events_created, 0)
                self.assertEqual(len(self.store.notifications(owner_id=self.owner)), count)
                self.assertEqual(self.store.proactive_event(event.event_id, self.owner,
                                                              self.vehicle.vehicle_id)["status"], "RESOLVED")
            finally:
                registry.close()


if __name__ == "__main__":
    unittest.main()
