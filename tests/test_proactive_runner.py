"""Offline operational checks for the run-once proactive delivery boundary."""

from contextlib import redirect_stdout
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
import json
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from carmind.app import CarMindApp
from carmind.contracts import MaintenanceRecord, VehicleProfile
from carmind.ownership_demo import fictional_schedule
from carmind.planner_provider import FakePlannerProvider
from carmind.proactive import NotificationPreferences
from carmind.proactive_cli import main as proactive_main
from carmind.proactive_runner import (DeliveryPolicy, PermanentDeliveryError,
                                      ProactiveCycleRunner, TransientDeliveryError)
from carmind.storage import OwnershipStore
from carmind.whatsapp import WhatsAppNotificationSender


NOW = datetime(2026, 1, 6, 12, tzinfo=timezone.utc)


class FakeSender:
    def __init__(self, failures=()):
        self.failures = list(failures)
        self.calls = []

    def send(self, intent, text):
        self.calls.append((intent, text))
        if self.failures:
            raise self.failures.pop(0)


class ProactiveRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "owner.sqlite3"
        self.store = OwnershipStore(self.path)
        self.addCleanup(self.store.close)
        self.app = CarMindApp(self.store, FakePlannerProvider([]),
                              approved_schedules=(fictional_schedule(),), allow_test_schedules=True)
        self.owner = self.app.create_owner(now=NOW, owner_id="owner-a")
        self.vehicle = VehicleProfile("vehicle-a", "Fictional", "Everyday", 2024, 15000,
                                      "Fictional gasoline engine")
        self.app.add_vehicle(self.owner, self.vehicle, now=NOW, market="TEST_ONLY")
        with self.store.transaction():
            self.store.record_service("baseline-oil", self.vehicle.vehicle_id,
                MaintenanceRecord("baseline-oil", "oil", NOW, 15000), NOW, None)
            self.store.record_odometer("near-due", self.vehicle.vehicle_id, 24500, 24500,
                                       "km", NOW, NOW, None)

    def enable(self, channel="console", **changes):
        values = {**asdict(NotificationPreferences(enabled=True, preferred_channel=channel,
                                                   quiet_enabled=False)), **changes}
        self.app.proactive.set_preferences(self.owner, NotificationPreferences(**values), now=NOW)

    def runner(self, sender=None, policy=None, channel="console"):
        return ProactiveCycleRunner(self.app, {channel: sender or FakeSender()}, policy or DeliveryPolicy())

    def notice(self):
        return self.store.notifications(owner_id=self.owner)[0]

    def test_evaluate_deliver_cycle_restart_and_duplicate_invocation(self):
        self.enable()
        sender = FakeSender()
        runner = self.runner(sender)
        self.assertEqual(runner.evaluate(now=NOW).events_created, 1)
        self.assertEqual(len(self.store.notifications(owner_id=self.owner)), 1)
        delivered = runner.deliver(now=NOW)
        self.assertEqual((delivered.notifications_claimed, delivered.notifications_sent), (1, 1))
        self.assertEqual(len(sender.calls), 1)
        self.assertEqual(self.notice()["status"], "SENT")
        self.assertEqual(runner.deliver(now=NOW).notifications_sent, 0)
        self.assertEqual(runner.cycle(now=NOW).events_created, 0)
        self.store.close()
        reopened = OwnershipStore(self.path)
        self.addCleanup(reopened.close)
        app = CarMindApp(reopened, FakePlannerProvider([]),
                         approved_schedules=(fictional_schedule(),), allow_test_schedules=True)
        replay = ProactiveCycleRunner(app, {"console": sender}).cycle(now=NOW)
        self.assertEqual((replay.events_created, replay.notifications_sent), (0, 0))
        self.assertEqual(len(reopened.notifications(owner_id=self.owner)), 1)
        self.assertEqual(len(sender.calls), 1)

    def test_dry_run_leaves_database_and_sender_untouched(self):
        self.enable()
        sender = FakeSender()
        runner = self.runner(sender)
        preview = runner.cycle(now=NOW, dry_run=True)
        self.assertEqual((preview.events_created, preview.notifications_would_send), (1, 1))
        self.assertEqual(self.store.proactive_events(), [])
        self.assertEqual(self.store.notifications(), [])
        self.assertEqual(sender.calls, [])
        actual = runner.cycle(now=NOW)
        self.assertEqual((actual.events_created, actual.notifications_sent), (1, 1))
        self.assertEqual(len(sender.calls), 1)

    def test_two_connections_claim_one_notification_and_one_send(self):
        self.enable()
        self.app.evaluate_proactive_state(now=NOW)
        second = OwnershipStore(self.path)
        self.addCleanup(second.close)
        lease = NOW + timedelta(minutes=5)
        first_claim = self.store.claim_notifications(NOW, lease, "runner-a", limit=1, channels=("console",))
        second_claim = second.claim_notifications(NOW, lease, "runner-b", limit=1, channels=("console",))
        self.assertEqual(len(first_claim), 1)
        self.assertEqual(second_claim, [])
        sender = FakeSender()
        row, token = first_claim[0]
        sender.send(row, "bounded reminder")
        self.assertTrue(self.store.finalize_claim(row["id"], token, NOW, "SENT", result="SUCCESS"))
        self.assertEqual(ProactiveCycleRunner(CarMindApp(second, FakePlannerProvider([])),
                    {"console": sender}).deliver(now=NOW).notifications_sent, 0)
        self.assertEqual(len(sender.calls), 1)

    def test_crash_expired_lease_reclaims_and_old_token_cannot_finalize(self):
        self.enable()
        self.app.evaluate_proactive_state(now=NOW)
        first = self.store.claim_notifications(NOW, NOW + timedelta(seconds=30),
                                               "runner-a", limit=1, channels=("console",))
        self.assertEqual(len(first), 1)
        self.assertEqual(self.store.claim_notifications(NOW + timedelta(seconds=29),
            NOW + timedelta(minutes=5), "runner-b", limit=1, channels=("console",)), [])
        later = NOW + timedelta(seconds=31)
        second = self.store.claim_notifications(later, later + timedelta(minutes=5),
                                                "runner-b", limit=1, channels=("console",))
        self.assertEqual(len(second), 1)
        notice_id = first[0][0]["id"]
        self.assertFalse(self.store.finalize_claim(notice_id, first[0][1], later, "SENT", result="SUCCESS"))
        self.assertTrue(self.store.finalize_claim(notice_id, second[0][1], later,
                                                  "SENT", result="SUCCESS"))
        self.assertEqual(len(self.store.notification_attempts(notice_id)), 1)

    def test_transient_backoff_and_bounded_attempts(self):
        self.enable()
        self.app.evaluate_proactive_state(now=NOW)
        sender = FakeSender([TransientDeliveryError("temporary_provider_failure")])
        runner = self.runner(sender, DeliveryPolicy(base_backoff_seconds=60,
                                                    max_backoff_seconds=600, max_attempts=3))
        first = runner.deliver(now=NOW)
        self.assertEqual((first.notifications_retried, self.notice()["attempt_count"]), (1, 1))
        self.assertEqual(self.notice()["next_attempt_at"], (NOW + timedelta(seconds=60)).isoformat())
        self.assertEqual(runner.deliver(now=NOW + timedelta(seconds=59)).notifications_claimed, 0)
        self.assertEqual(runner.deliver(now=NOW + timedelta(seconds=60)).notifications_sent, 1)
        self.assertEqual(len(sender.calls), 2)
        self.assertEqual([a["result"] for a in self.store.notification_attempts(self.notice()["id"])],
                         ["TRANSIENT_FAILURE", "SUCCESS"])

        # A separate notice demonstrates the terminal attempt limit.
        with self.store.transaction():
            self.store.set_notification_status(self.notice()["id"], "CANCELLED")
        new_at = NOW + timedelta(days=1)
        with self.store.transaction():
            self.store.record_odometer("due-reading", self.vehicle.vehicle_id, 25000, 25000,
                                       "km", new_at, new_at, None)
        self.app.evaluate_proactive_state(now=new_at)
        failing = self.runner(FakeSender([TimeoutError(), TimeoutError()]),
                              DeliveryPolicy(max_attempts=2, base_backoff_seconds=1,
                                             max_backoff_seconds=2))
        self.assertEqual(failing.deliver(now=new_at).notifications_retried, 1)
        self.assertEqual(failing.deliver(now=new_at + timedelta(seconds=1)).notifications_failed, 1)
        self.assertEqual(self.store.notifications(statuses=("FAILED",))[0]["attempt_count"], 2)

    def test_permanent_failure_is_sanitized_and_event_remains_open(self):
        self.enable(channel="whatsapp")
        self.app.evaluate_proactive_state(now=NOW)
        sender = WhatsAppNotificationSender(self.store, lambda *_: None)
        report = self.runner(sender, channel="whatsapp").deliver(now=NOW)
        self.assertEqual(report.notifications_failed, 1)
        self.assertEqual(self.notice()["status"], "FAILED")
        self.assertEqual(self.store.notification_attempts(self.notice()["id"])[0]["error_category"],
                         "missing_channel_binding")
        self.assertEqual(self.store.proactive_events(owner_id=self.owner)[0]["status"], "OPEN")
        self.assertNotIn("should not send", json.dumps(self.store.notification_attempts(self.notice()["id"])))

    def test_quiet_hours_timezone_boundary_and_scheduled_for(self):
        quiet_at = datetime(2026, 1, 6, 21, tzinfo=timezone.utc)  # 00:00 in Riyadh
        self.enable(timezone="Asia/Riyadh", quiet_enabled=True,
                    quiet_start="22:00", quiet_end="08:00")
        with self.store.transaction():
            self.store.record_odometer("night-reading", self.vehicle.vehicle_id, 24500, 24500,
                                       "km", quiet_at, quiet_at, None)
        self.app.evaluate_proactive_state(now=quiet_at)
        self.assertEqual(self.notice()["status"], "DEFERRED")
        sender = FakeSender()
        runner = self.runner(sender)
        self.assertEqual(runner.deliver(now=quiet_at).notifications_claimed, 0)
        scheduled = datetime.fromisoformat(self.notice()["scheduled_for"])
        self.assertEqual(scheduled, datetime(2026, 1, 7, 5, tzinfo=timezone.utc))
        self.assertEqual(runner.deliver(now=scheduled - timedelta(seconds=1)).notifications_claimed, 0)
        self.assertEqual(runner.deliver(now=scheduled).notifications_sent, 1)
        self.assertEqual(len(sender.calls), 1)

    def test_disabled_changed_channel_and_resolved_event_do_not_send(self):
        self.enable()
        self.app.evaluate_proactive_state(now=NOW)
        sender = FakeSender()
        runner = self.runner(sender)
        self.enable(enabled=False)
        self.assertEqual(runner.deliver(now=NOW).notifications_cancelled, 1)
        self.assertEqual(sender.calls, [])
        self.enable(channel="web")
        self.app.evaluate_proactive_state(now=NOW)
        self.assertEqual(self.store.notifications(statuses=("PENDING", "DEFERRED")), [])
        self.enable(channel="console")
        self.app.evaluate_proactive_state(now=NOW)
        self.assertEqual(len(self.store.notifications(statuses=("PENDING",))), 1)
        service_at = NOW + timedelta(days=1)
        with self.store.transaction():
            self.store.record_odometer("service-reading", self.vehicle.vehicle_id, 24600, 24600,
                                       "km", service_at, service_at, None)
            self.store.record_service("completed-oil", self.vehicle.vehicle_id,
                MaintenanceRecord("completed-oil", "oil", service_at, 24600), service_at, None)
        self.app.evaluate_proactive_state(now=service_at)
        self.assertEqual(runner.deliver(now=service_at).notifications_sent, 0)
        self.assertEqual(sender.calls, [])
        self.assertTrue(all(row["status"] == "CANCELLED" for row in self.store.notifications()))

    def test_no_opt_in_and_channel_change_are_rechecked_at_delivery(self):
        self.app.evaluate_proactive_state(now=NOW)
        self.assertEqual(self.store.notifications(), [])
        self.assertEqual(self.runner().cycle(now=NOW).notifications_sent, 0)
        self.enable()
        self.app.evaluate_proactive_state(now=NOW)
        self.enable(channel="whatsapp")
        console = FakeSender()
        self.assertEqual(self.runner(console).deliver(now=NOW).notifications_cancelled, 1)
        self.assertEqual(console.calls, [])
        self.app.evaluate_proactive_state(now=NOW)
        self.assertEqual([row["channel"] for row in self.store.notifications(statuses=("PENDING",))],
                         ["whatsapp"])

    def test_same_owner_two_vehicles_keep_distinct_delivery_context(self):
        self.enable()
        second = VehicleProfile("vehicle-a2", "Fictional", "Everyday", 2024, 15000,
                                "Fictional gasoline engine")
        self.app.add_vehicle(self.owner, second, now=NOW, market="TEST_ONLY", nickname="Spare car")
        with self.store.transaction():
            self.store.record_service("oil-a2", second.vehicle_id,
                MaintenanceRecord("oil-a2", "oil", NOW, 15000), NOW, None)
            self.store.record_odometer("reading-a2", second.vehicle_id, 24500, 24500,
                                       "km", NOW, NOW, None)
        self.app.evaluate_proactive_state(now=NOW)
        sender = FakeSender()
        runner = self.runner(sender)
        self.assertEqual(runner.deliver(now=NOW, vehicle_id=second.vehicle_id).notifications_sent, 1)
        self.assertEqual(len(sender.calls), 1)
        self.assertIn("Spare car", sender.calls[0][1])
        self.assertEqual(runner.deliver(now=NOW, vehicle_id=self.vehicle.vehicle_id).notifications_sent, 1)
        self.assertIn("Everyday", sender.calls[1][1])
        self.assertNotIn("Spare car", sender.calls[1][1])

    def test_transport_exception_message_is_not_persisted(self):
        self.enable()
        self.app.evaluate_proactive_state(now=NOW)
        sender = FakeSender([RuntimeError("Authorization: test-secret-value")])
        self.assertEqual(self.runner(sender).deliver(now=NOW).notifications_failed, 1)
        stored = json.dumps(self.store.notification_attempts(self.notice()["id"]))
        self.assertNotIn("test-secret-value", stored)
        self.assertIn("unknown_transport_failure", stored)

    def test_multi_owner_multi_vehicle_batch_and_context(self):
        self.enable(channel="whatsapp")
        second_owner = self.app.create_owner(now=NOW, owner_id="owner-b")
        self.app.proactive.set_preferences(second_owner, NotificationPreferences(enabled=True,
            preferred_channel="whatsapp", quiet_enabled=False), now=NOW)
        other = VehicleProfile("vehicle-b", "Fictional", "Everyday", 2024, 15000,
                               "Fictional gasoline engine")
        self.app.add_vehicle(second_owner, other, now=NOW, market="TEST_ONLY", nickname="Second")
        with self.store.transaction():
            self.store.record_service("oil-b", other.vehicle_id,
                MaintenanceRecord("oil-b", "oil", NOW, 15000), NOW, None)
            self.store.record_odometer("reading-b", other.vehicle_id, 24500, 24500,
                                       "km", NOW, NOW, None)
            self.store.create_session("session-a", self.owner, NOW, self.vehicle.vehicle_id)
            self.store.create_session("session-b", second_owner, NOW, other.vehicle_id)
            self.store.bind_channel("whatsapp", "recipient-a", self.owner, "session-a")
            self.store.bind_channel("whatsapp", "recipient-b", second_owner, "session-b")
        self.app.evaluate_proactive_state(now=NOW)
        delivered = []
        sender = WhatsAppNotificationSender(self.store,
            lambda recipient, text: delivered.append((recipient, text)))
        runner = self.runner(sender, channel="whatsapp")
        first = runner.deliver(now=NOW, limit=1)
        self.assertEqual((first.notifications_sent, first.remaining_eligible), (1, 1))
        self.assertEqual(runner.deliver(now=NOW, limit=1).notifications_sent, 1)
        self.assertEqual(len(delivered), 2)
        by_recipient = dict(delivered)
        self.assertIn("Everyday", by_recipient["recipient-a"])
        self.assertIn("Second", by_recipient["recipient-b"])
        self.assertNotIn("Second", by_recipient["recipient-a"])
        self.assertNotIn("Everyday", by_recipient["recipient-b"])

    def test_milestone_ten_database_migrates_with_outbox_intact(self):
        self.enable()
        self.app.evaluate_proactive_state(now=NOW)
        old_id = self.notice()["id"]
        with self.store.transaction():
            self.store.db.execute("UPDATE notification_outbox SET channel='web' WHERE id=?", (old_id,))
        self.store.close()
        db = sqlite3.connect(self.path)
        try:
            db.execute("DROP TABLE notification_attempts")
            db.execute("DROP TABLE notification_claims")
            db.execute("ALTER TABLE notification_outbox DROP COLUMN next_attempt_at")
            db.execute("ALTER TABLE sessions DROP COLUMN selection_explicit")
            db.execute("PRAGMA user_version=3")
            db.commit()
        finally:
            db.close()
        migrated = OwnershipStore(self.path)
        self.addCleanup(migrated.close)
        self.assertEqual(migrated.db.execute("PRAGMA user_version").fetchone()[0], 5)
        self.assertEqual(migrated.notifications()[0]["id"], old_id)
        self.assertEqual(migrated.notifications()[0]["status"], "CANCELLED")
        self.assertEqual(migrated.proactive_events(owner_id=self.owner)[0]["status"], "OPEN")

    def test_cli_run_deliver_cycle_and_json_dry_run(self):
        self.enable()
        self.app.schedules = ()
        self.app.proactive.add_owner_reminder(self.owner, self.vehicle.vehicle_id, "oil",
                                               now=NOW, after_months=1)
        later = NOW + timedelta(days=31)
        self.store.close()
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(proactive_main(["--db", str(self.path), "run", "--now", later.isoformat()]), 0)
        self.assertEqual(json.loads(output.getvalue())["mode"], "evaluate")
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(proactive_main(["--db", str(self.path), "cycle", "--now", later.isoformat(),
                                             "--dry-run", "--console", "--json"]), 0)
        self.assertEqual(json.loads(output.getvalue())["dry_run"], True)
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(proactive_main(["--db", str(self.path), "deliver", "--now", later.isoformat(),
                                             "--console", "--json"]), 0)
        self.assertEqual(json.loads(output.getvalue())["notifications_sent"], 1)
        self.store = OwnershipStore(self.path)
        self.addCleanup(self.store.close)


if __name__ == "__main__":
    unittest.main()
