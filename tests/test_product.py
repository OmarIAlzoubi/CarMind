"""Offline product boundary, persistence, and cross-channel acceptance tests."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from carmind.composition import compose_app
from carmind.contracts import UserMessage, VehicleProfile
from carmind.evidence import FrozenEvidenceSnapshot
from carmind.ownership_demo import command_turn, empty_final, fictional_schedule
from carmind.planner_provider import FakePlannerProvider
from carmind.product import InboundMessage, ProductService
from carmind.simulator import freeze_episode, generate_episode
from carmind.storage import OwnershipStore
from carmind.web import make_handler
from carmind.whatsapp import handle_payload, normalize_payload, twiml


NOW = datetime(2026, 1, 6, 12, tzinfo=timezone.utc)


def onboarding(text, *, odometer=18500, unit="km"):
    return {"type": "vehicle_proposal", "owner_quote": text,
            "vehicle": {"make": "Hyundai", "model": "Elantra N", "year": 2024,
                        "odometer": odometer, "unit": unit, "trim": None,
                        "engine": None, "market": None, "nickname": None}}


class ProductTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "product.sqlite3"
        self.store = OwnershipStore(self.path)
        self.addCleanup(lambda: self.store.close())
        self.app = compose_app(self.store, provider=FakePlannerProvider([]))
        self.service = ProductService(self.app)
        self.count = 0

    def send(self, text, *, script=None, channel="whatsapp", user="owner-a", at=NOW,
             message_id=None, confirmation_id=None, snapshot=None):
        self.count += 1
        if script is not None:
            self.app.provider = FakePlannerProvider(script)
        return self.service.handle(InboundMessage(channel, user, message_id or f"m-{self.count}",
                                                  text, at, confirmation_id=confirmation_id,
                                                  snapshot=snapshot))

    def onboard(self, *, user="owner-a", channel="whatsapp", text=None, at=NOW):
        text = text or "سيارتي Hyundai Elantra N موديل 2024 وممشاها 18500 كم"
        pending = self.send(text, user=user, channel=channel, at=at, script=[onboarding(text)])
        self.assertEqual(pending.status, "confirmation_required")
        self.assertIsNotNone(pending.proposal_id)
        return pending

    def confirm(self, *, user="owner-a", channel="whatsapp", at=NOW, text="أكد", proposal_id=None):
        return self.send(text, user=user, channel=channel, at=at, confirmation_id=proposal_id)

    def test_onboarding_confirmation_restart_and_exact_replay(self):
        pending = self.onboard()
        self.assertEqual(self.service.overview("whatsapp", "owner-a", now=NOW)["vehicle"], None)
        applied = self.confirm(at=NOW + timedelta(minutes=1))
        self.assertEqual(applied.status, "applied")
        self.assertEqual(self.service.overview("whatsapp", "owner-a", now=NOW + timedelta(minutes=1))["odometer"]["km"], 18500)
        original = self.send("أكد", at=NOW + timedelta(minutes=1), message_id="duplicate")
        replay = self.send("altered body", at=NOW + timedelta(minutes=1), message_id="duplicate")
        self.assertEqual(replay, original)
        self.assertEqual(len(self.store.vehicles(self.store.binding("whatsapp", "owner-a")["owner_id"], NOW + timedelta(minutes=1))), 1)
        self.store.close()
        self.store = OwnershipStore(self.path)
        self.service = ProductService(compose_app(self.store, provider=FakePlannerProvider([])))
        self.assertEqual(self.service.overview("whatsapp", "owner-a", now=NOW + timedelta(minutes=2))["vehicle"]["id"], applied.active_vehicle_id)
        self.assertEqual(self.store.vehicle_draft(pending.proposal_id, self.store.binding("whatsapp", "owner-a")["session_id"])["state"], "APPLIED")

    def test_missing_unit_or_identity_requests_clarification_without_write(self):
        text = "My car is a 2024 Hyundai Elantra N with 18,500 on the clock"
        answer = self.send(text, script=[onboarding(text, unit=None)])
        self.assertEqual(answer.status, "clarification_required")
        self.assertEqual(self.service.overview("whatsapp", "owner-a", now=NOW)["vehicle"], None)
        text2 = "I have a Hyundai"
        answer2 = self.send(text2, script=[{"type": "clarification", "question": "What model and year is it?"}])
        self.assertEqual(answer2.status, "clarification_required")
        self.assertIn("model", answer2.text)

    def test_malformed_onboarding_output_is_a_clarification_not_a_write(self):
        response = self.send("My car is a Hyundai", script=[["unexpected", "shape"]])
        self.assertEqual(response.status, "clarification_required")
        self.assertIsNone(self.service.overview("whatsapp", "owner-a", now=NOW)["vehicle"])

    def test_explicit_miles_preserve_original_unit_and_canonical_km(self):
        text = "My car is a 2024 Hyundai Elantra N at 10,000 mi"
        pending = self.send(text, script=[onboarding(text, odometer=10000, unit="mi")])
        self.assertIn("10,000 mi", pending.text)
        self.confirm(at=NOW + timedelta(minutes=1), text="confirm")
        view = self.service.overview("whatsapp", "owner-a", now=NOW + timedelta(minutes=1))
        self.assertEqual(view["odometer"]["original_reading"], 10000)
        self.assertEqual(view["odometer"]["original_unit"], "mi")
        self.assertAlmostEqual(view["odometer"]["km"], 16093.44)

    def test_vehicle_confirmation_copy_shows_every_nonempty_draft_detail(self):
        text = "My 2024 Hyundai Elantra N, trim Track, at 18,500 km. Call it Blue."
        data = onboarding(text)
        data["vehicle"].update(trim="Track", engine="2.0 turbo", market="SA",
                               nickname="Blue")
        reply = self.send(text, script=[data])
        for value in ("Track", "2.0 turbo", "SA", "Blue", "18,500 km"):
            self.assertIn(value, reply.text)

    def test_stale_and_cross_owner_vehicle_confirmation_rejected(self):
        pending = self.onboard()
        other = self.confirm(user="owner-b", proposal_id=pending.proposal_id)
        self.assertEqual(other.status, "error")
        self.assertEqual(self.service.overview("whatsapp", "owner-b", now=NOW)["vehicle"], None)
        expired = self.confirm(at=NOW + timedelta(days=2), proposal_id=pending.proposal_id)
        self.assertEqual(expired.status, "error")
        self.assertEqual(self.service.overview("whatsapp", "owner-a", now=NOW + timedelta(days=2))["vehicle"], None)

    def test_channel_message_ids_are_namespaced_and_retries_do_not_call_provider(self):
        text = "My car is a 2024 Hyundai Elantra N at 18,500 km"
        provider = FakePlannerProvider([onboarding(text)])
        self.app.provider = provider
        first = self.send(text, message_id="same")
        second = self.send(text, message_id="same")
        self.assertEqual(first, second)
        self.assertEqual(len(provider.requests), 1)
        other = self.send(text, user="owner-b", message_id="same", script=[onboarding(text)])
        self.assertNotEqual(first.proposal_id, other.proposal_id)
        web = self.send(text, channel="web", user="owner-a", message_id="same", script=[onboarding(text)])
        self.assertNotEqual(web.proposal_id, first.proposal_id)

    def test_unsupported_schedule_keeps_owner_facts_and_does_not_guess(self):
        self.onboard()
        self.confirm(at=NOW + timedelta(minutes=1))
        at = NOW + timedelta(minutes=2)
        text = "غيرت الزيت اليوم على 20000 كم"
        pending = self.send(text, at=at, script=[command_turn("record_service_event", {
            "service_type": "oil", "performed_at": at.isoformat(), "odometer": 20000,
            "unit": "km"}, text)])
        self.assertEqual(pending.status, "confirmation_required")
        self.assertIn("20,000", pending.text)
        self.confirm(at=at + timedelta(minutes=1))
        with patch.object(self.app.provider, "generate", side_effect=AssertionError("No provider on overview")):
            view = self.service.overview("whatsapp", "owner-a", now=at + timedelta(minutes=1))
        self.assertEqual(view["odometer"]["km"], 20000)
        self.assertEqual(view["last_service"]["type"], "oil")
        self.assertEqual(view["last_service"]["km_since"], 0)
        self.assertEqual(view["manufacturer"]["status"], "unverified")
        self.assertEqual(view["maintenance"], [])
        self.assertEqual(view["reminders"], [])
        answer = empty_final()
        answer["assessment"]["uncertainties"] = ["APPLICABILITY_UNVERIFIED"]
        response = self.send("وش الصيانة الجاية؟", at=at + timedelta(minutes=2), script=[answer])
        self.assertIn("ما راح أخمّن", response.text)
        self.assertNotIn("STOP_WHEN_SAFE", response.text)

    def test_fictional_opt_in_schedule_is_labeled_test_only_in_product_view(self):
        profile = replace(freeze_episode(generate_episode("healthy_vehicle", 42)[0]).profile,
                          mileage_km=None)
        owner = self.app.create_owner(now=NOW)
        self.app.add_vehicle(owner, profile, now=NOW, market="TEST_ONLY")
        session = self.app.start_session(owner, now=NOW, vehicle_id=profile.vehicle_id)
        self.service.bind_existing("web", "local", owner, session)
        self.app.schedules = (fictional_schedule(),)
        self.app.allow_test_schedules = True
        view = self.service.overview("web", "local", now=NOW)
        self.assertEqual(view["manufacturer"]["status"], "test_only")

    def test_cross_channel_service_and_odometer_share_authoritative_state(self):
        self.onboard()
        self.confirm(at=NOW + timedelta(minutes=1))
        binding = self.store.binding("whatsapp", "owner-a")
        self.service.bind_existing("web", "browser-a", binding["owner_id"], binding["session_id"])
        at = NOW + timedelta(minutes=2)
        text = "I changed the oil at 20,000 km"
        self.send(text, at=at, script=[command_turn("record_service_event", {
            "service_type": "oil", "performed_at": at.isoformat(), "odometer": 20000,
            "unit": "km"}, text)])
        self.confirm(at=at + timedelta(minutes=1))
        web = self.service.overview("web", "browser-a", now=at + timedelta(minutes=1))
        self.assertEqual(web["last_service"]["odometer_km"], 20000)
        self.assertEqual(web["odometer"]["km"], 20000)
        change_at = at + timedelta(minutes=2)
        update = "My odometer is now 20,200 km"
        proposed = self.send(update, at=change_at, channel="web", user="browser-a",
                             script=[command_turn("update_odometer", {"reading": 20200, "unit": "km",
                              "occurred_at": change_at.isoformat()}, update)])
        self.assertEqual(proposed.status, "confirmation_required")
        self.confirm(at=change_at + timedelta(minutes=1), channel="web", user="browser-a",
                     text="confirm", proposal_id=proposed.proposal_id)
        whatsapp = self.service.overview("whatsapp", "owner-a", now=change_at + timedelta(minutes=1))
        self.assertEqual(whatsapp["odometer"]["km"], 20200)
        self.assertEqual(whatsapp["odometer"]["original_reading"], 20200)
        self.assertEqual(whatsapp["odometer"]["original_unit"], "km")
        self.assertEqual(whatsapp["odometer"]["recorded_at"], change_at.isoformat())
        self.assertEqual(whatsapp["last_service"]["km_since"], 200)
        self.assertEqual(whatsapp["reminders"], web["reminders"])
        answer = empty_final()
        answer["assessment"]["observations"] = [{"text": "The recorded odometer is available.",
                                                     "evidence_ids": [whatsapp["vehicle"]["id"]]}]
        followup = self.send("كم ممشى السيارة عندك؟", at=change_at + timedelta(minutes=2),
                             script=[{"type": "tool_call", "tool_id": "get_vehicle_profile", "arguments": {}}, answer])
        self.assertIn("20,200", followup.text)

    def test_multiple_vehicles_and_owner_isolation(self):
        self.onboard()
        self.confirm(at=NOW + timedelta(minutes=1))
        binding = self.store.binding("whatsapp", "owner-a")
        second = VehicleProfile("second-car", "Fictional", "Family", 2022, 9000)
        self.app.add_vehicle(binding["owner_id"], second, now=NOW + timedelta(minutes=2))
        view = self.service.overview("whatsapp", "owner-a", now=NOW + timedelta(minutes=2))
        self.assertEqual(len(view["vehicles"]), 2)
        self.service.select_vehicle("whatsapp", "owner-a", "second-car", now=NOW + timedelta(minutes=2))
        switched = self.service.overview("whatsapp", "owner-a", now=NOW + timedelta(minutes=2))
        self.assertEqual(switched["odometer"]["km"], 9000)
        self.assertEqual(switched["services"], [])
        self.onboard(user="owner-b", at=NOW + timedelta(minutes=3))
        self.confirm(user="owner-b", at=NOW + timedelta(minutes=4))
        with self.assertRaises(ValueError):
            self.service.overview("whatsapp", "owner-b", now=NOW + timedelta(minutes=4), vehicle_id="second-car")
        with self.assertRaises(ValueError):
            self.service.select_vehicle("whatsapp", "owner-b", "second-car", now=NOW + timedelta(minutes=4))

    def test_safety_stop_is_present_after_unrelated_service_answer(self):
        source = freeze_episode(generate_episode("sustained_temperature_rise", 42)[0])
        owner = self.app.create_owner(now=NOW)
        self.app.add_vehicle(owner, replace(source.profile, mileage_km=None), now=NOW)
        session = self.app.start_session(owner, now=NOW, vehicle_id=source.profile.vehicle_id)
        self.service.bind_existing("whatsapp", "owner-a", owner, session)
        vehicle = self.store.vehicle(owner, source.profile.vehicle_id, NOW)
        at = NOW + timedelta(minutes=2)
        offset = at - source.assessment_at
        observations = tuple(replace(o, timestamp=o.timestamp + offset) for o in source.observations)
        snapshot = FrozenEvidenceSnapshot("high-temp", vehicle.profile,
                                          UserMessage("tire-msg", "The temperature warning came on", at),
                                          at, observations)
        final = empty_final()
        ids = [o.observation_id for o in observations if o.name == "coolant_temperature"]
        final["assessment"].update(observations=[{"text": "Temperature rose in the visible readings.", "evidence_ids": ids}])
        first = self.send("The temperature warning came on", at=at, snapshot=snapshot,
                          script=[{"type": "tool_call", "tool_id": "get_coolant_temperature_history", "arguments": {}}, final])
        self.assertIsNotNone(self.store.previous_stop(vehicle.profile.vehicle_id))
        self.assertIsNotNone(first.safety_notice)
        self.assertFalse(first.safety_notice.startswith("The earlier"))
        service_text = "I changed the oil today at 15,000 km"
        service_at = at + timedelta(minutes=1)
        proposed = self.send(service_text, at=service_at, script=[command_turn("record_service_event", {
            "service_type": "oil", "performed_at": service_at.isoformat(),
            "odometer": 15000, "unit": "km"}, service_text)])
        self.assertIn(proposed.safety_notice, proposed.text)
        self.assertTrue(proposed.safety_notice.startswith("The earlier"))
        self.confirm(at=service_at + timedelta(minutes=1), text="confirm")
        record = self.store.service_records(vehicle.profile.vehicle_id, service_at + timedelta(minutes=1))[0]
        history = empty_final()
        history["assessment"]["observations"] = [{"text": "The recorded service is available.",
                                                   "evidence_ids": [record.record_id]}]
        response = self.send("When did I last change the oil?", at=service_at + timedelta(minutes=2),
                             script=[{"type": "tool_call", "tool_id": "get_latest_service_record",
                                      "arguments": {"service_type": "oil"}}, history])
        self.assertEqual(response.status, "complete")
        self.assertTrue(response.text.startswith("Your last recorded engine oil service"))
        self.assertIn(response.safety_notice, response.text)
        self.assertEqual(self.service.overview("whatsapp", "owner-a", now=service_at + timedelta(minutes=2))
                         ["active_concern"]["unresolved_stop"], True)
        self.service.bind_existing("web", "local", owner, session)
        self.assertEqual(self.service.overview("web", "local", now=service_at + timedelta(minutes=2))
                         ["active_concern"]["unresolved_stop"], True)

    def test_wa_payload_normalization_and_twiml_escaping(self):
        payload = {"From": "wa:+123", "MessageSid": "sid-1", "Body": "سيارتي <Hyundai> & more",
                   "Timestamp": NOW.isoformat()}
        inbound = normalize_payload(payload, received_at=NOW)
        self.assertEqual((inbound.channel, inbound.external_user_id, inbound.external_message_id),
                         ("whatsapp", "wa:+123", "sid-1"))
        self.assertEqual(inbound.timestamp, NOW)
        from carmind.product import ProductReply
        self.assertIn("&lt;", twiml(ProductReply("<bad> & okay", "complete")))
        with self.assertRaises(ValueError):
            normalize_payload({**payload, "Timestamp": "not-a-date"}, received_at=NOW)

    def test_whatsapp_adapter_uses_product_path_and_persisted_deduplication(self):
        text = "سيارتي Hyundai Elantra N موديل 2024 وممشاها 18500 كم"
        provider = FakePlannerProvider([onboarding(text)])
        self.app.provider = provider
        payload = {"From": "wa:+123", "MessageSid": "sid-1", "Body": text,
                   "Timestamp": NOW.isoformat()}
        first = handle_payload(self.service, payload, received_at=NOW)
        duplicate = handle_payload(self.service, payload, received_at=NOW)
        self.assertEqual(first, duplicate)
        self.assertEqual(len(provider.requests), 1)
        self.assertIn("تأكد؟", first.text)
        applied = handle_payload(self.service, {**payload, "MessageSid": "sid-2", "Body": "أكد"},
                                 received_at=NOW)
        self.assertEqual(applied.status, "applied")
        self.assertEqual(self.service.overview("whatsapp", "wa:+123", now=NOW)["odometer"]["km"], 18500)

    def test_stale_service_proposal_after_vehicle_switch_does_not_write(self):
        self.onboard()
        self.confirm(at=NOW + timedelta(minutes=1))
        binding = self.store.binding("whatsapp", "owner-a")
        first_id = self.service.overview("whatsapp", "owner-a", now=NOW + timedelta(minutes=1))["vehicle"]["id"]
        self.app.add_vehicle(binding["owner_id"], VehicleProfile("second", "Fictional", "Other", 2020),
                             now=NOW + timedelta(minutes=2))
        at = NOW + timedelta(minutes=3)
        text = "I changed the oil today"
        pending = self.send(text, at=at, script=[command_turn("record_service_event", {
            "service_type": "oil", "performed_at": at.isoformat()}, text)])
        self.service.select_vehicle("whatsapp", "owner-a", "second", now=at)
        rejected = self.confirm(at=at + timedelta(minutes=1), proposal_id=pending.proposal_id)
        self.assertEqual(rejected.status, "error")
        self.assertEqual(self.store.service_records(first_id, at + timedelta(minutes=1)), [])
        self.assertEqual(self.store.service_records("second", at + timedelta(minutes=1)), [])

    def test_web_handler_api_without_socket_or_provider(self):
        self.onboard()
        self.confirm(at=NOW + timedelta(minutes=1))
        binding = self.store.binding("whatsapp", "owner-a")
        self.service.bind_existing("web", "local", binding["owner_id"], binding["session_id"])
        handler_type = make_handler(self.service, web_identity="local")
        handler = object.__new__(handler_type)
        handler.path = "/api/overview"
        handler.wfile = BytesIO()
        statuses = []
        handler.send_response = lambda code: statuses.append(code)
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        with patch("carmind.web.datetime") as clock:
            clock.now.return_value = NOW + timedelta(minutes=2)
            handler.do_GET()
        self.assertEqual(statuses, [200])
        self.assertEqual(json.loads(handler.wfile.getvalue())["odometer"]["km"], 18500)
        handler.path = "/api/vehicle/select"
        handler.wfile = BytesIO()
        payload = json.dumps({"vehicle_id": "not-owned"}).encode()
        handler.rfile = BytesIO(payload)
        handler.headers = {"Content-Length": str(len(payload)), "Content-Type": "application/json"}
        with patch("carmind.web.datetime") as clock:
            clock.now.return_value = NOW + timedelta(minutes=2)
            handler.do_POST()
        self.assertEqual(statuses[-1], 400)
        for malformed in ({"vehicle_id": ["not-owned"]}, {"vehicle_id": ""}):
            data = json.dumps(malformed).encode()
            handler.rfile = BytesIO(data)
            handler.wfile = BytesIO()
            handler.headers = {"Content-Length": str(len(data)), "Content-Type": "application/json"}
            with patch("carmind.web.datetime") as clock:
                clock.now.return_value = NOW + timedelta(minutes=2)
                handler.do_POST()
            self.assertEqual(statuses[-1], 400)

    def test_confirmation_id_must_be_bounded_text(self):
        for value in ([], "", "x" * 129):
            with self.subTest(value=value), self.assertRaises(ValueError):
                InboundMessage("web", "local", "m-1", "confirm", NOW, confirmation_id=value)

    def test_web_confirmation_endpoint_uses_same_exact_proposal(self):
        text = "My car is a 2024 Hyundai Elantra N at 18,500 km"
        pending = self.send(text, channel="web", user="local", script=[onboarding(text)])
        self.assertEqual(pending.status, "confirmation_required")
        handler_type = make_handler(self.service, web_identity="local")
        handler = object.__new__(handler_type)
        handler.path = "/api/confirm"
        handler.wfile = BytesIO()
        data = json.dumps({"proposal_id": pending.proposal_id, "message_id": "web-confirm-1"}).encode()
        handler.rfile = BytesIO(data)
        handler.headers = {"Content-Length": str(len(data)), "Content-Type": "application/json"}
        statuses = []
        handler.send_response = lambda code: statuses.append(code)
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        with patch("carmind.web.datetime") as clock:
            clock.now.return_value = NOW + timedelta(minutes=1)
            handler.do_POST()
        self.assertEqual(statuses, [200])
        response = json.loads(handler.wfile.getvalue())
        self.assertEqual(response["status"], "applied")
        self.assertEqual(self.service.overview("web", "local", now=NOW + timedelta(minutes=1))["odometer"]["km"], 18500)

    def test_v1_database_migrates_without_losing_owner(self):
        self.app.create_owner(now=NOW, owner_id="old-owner")
        self.store.close()
        import sqlite3
        db = sqlite3.connect(self.path)
        try:
            for table in ("notification_claims", "notification_attempts", "notification_outbox",
                          "proactive_events", "owner_reminders",
                          "notification_preferences", "external_receipts", "vehicle_drafts", "channel_bindings"):
                db.execute(f"DROP TABLE {table}")
            db.execute("PRAGMA user_version=1")
            db.commit()
        finally:
            db.close()
        migrated = OwnershipStore(self.path)
        try:
            self.assertEqual(migrated.db.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertEqual(migrated.db.execute("SELECT id FROM owners").fetchone()[0], "old-owner")
            self.assertEqual(migrated.db.execute("SELECT count(*) FROM channel_bindings").fetchone()[0], 0)
        finally:
            migrated.close()


if __name__ == "__main__":
    unittest.main()
