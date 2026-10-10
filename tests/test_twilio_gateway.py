"""Signed Twilio edge tests; all transports and planners stay offline."""

from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from urllib.parse import urlencode
import unittest
from unittest.mock import patch

from twilio.base.exceptions import TwilioRestException

from carmind.app import CarMindApp
from carmind.contracts import VehicleProfile
from carmind.planner_provider import FakePlannerProvider
from carmind.product import ProductService
from carmind.proactive_runner import PermanentDeliveryError, TransientDeliveryError
from carmind.proactive import EventType
from carmind.storage import OwnershipStore
from carmind.twilio_gateway import (INBOUND_PATH, STATUS_PATH, TwilioConfig,
                                    TwilioGateway, TwilioWhatsAppTransport, notification_sender)
from carmind.web import make_handler


NOW = datetime(2026, 1, 6, 12, tzinfo=timezone.utc)


class FakeMessages:
    def __init__(self, response=None):
        self.response = response or SimpleNamespace(sid="SMfake123")
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class TwilioGatewayTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = OwnershipStore(Path(temp.name) / "owner.sqlite3")
        self.addCleanup(self.store.close)
        self.service = ProductService(CarMindApp(self.store, FakePlannerProvider([])))
        self.config = TwilioConfig("ACfake", "test-token-for-offline-signatures",
            "whatsapp:+15550000000", "https://car.example.invalid")
        self.gateway = TwilioGateway(self.service, self.config)

    def signed(self, path, fields):
        signature = self.gateway.validator.compute_signature(self.config.url(path), fields)
        return urlencode(fields).encode(), signature

    def test_inbound_signature_and_durable_dedupe(self):
        fields = {"AccountSid": "ACfake", "MessageSid": "SMsame123",
                  "From": "whatsapp:+15550123456", "To": "whatsapp:+15550000000", "Body": "hi"}
        raw, signature = self.signed(INBOUND_PATH, fields)
        with patch.object(self.service, "handle", wraps=self.service.handle) as handle:
            first = self.gateway.inbound(raw, signature, now=NOW)
            second = self.gateway.inbound(raw, signature, now=NOW)
            self.assertEqual(second, "<Response/>")
            self.assertEqual(handle.call_count, 1)
        self.assertIn("<Response><Message>", first)
        self.assertIsNotNone(self.store.binding("whatsapp", fields["From"]))
        other_sender = {**fields, "From": "whatsapp:+15550987654"}
        other_raw, other_signature = self.signed(INBOUND_PATH, other_sender)
        with self.assertRaises(PermissionError):
            self.gateway.inbound(other_raw, other_signature, now=NOW)
        self.assertIsNone(self.store.binding("whatsapp", other_sender["From"]))
        with self.assertRaises(PermissionError):
            self.gateway.inbound(raw, "wrong-signature", now=NOW)
        self.assertEqual(len(self.store.db.execute("SELECT * FROM twilio_inbound_receipts").fetchall()), 1)

    def test_webhook_http_route_rejects_unsigned_before_creating_owner(self):
        fields = {"AccountSid": "ACfake", "MessageSid": "SMweb123",
                  "From": "whatsapp:+15550123456", "To": "whatsapp:+15550000000", "Body": "hi"}
        raw, signature = self.signed(INBOUND_PATH, fields)
        handler_type = make_handler(self.service, twilio_gateway=self.gateway)
        handler = object.__new__(handler_type)
        handler.path = INBOUND_PATH
        statuses = []
        handler.send_response = lambda code: statuses.append(code)
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        handler.rfile = BytesIO(raw)
        handler.wfile = BytesIO()
        handler.headers = {"Content-Length": str(len(raw)),
                           "Content-Type": "application/x-www-form-urlencoded",
                           "X-Twilio-Signature": "invalid"}
        handler.do_POST()
        self.assertEqual(statuses[-1], 403)
        self.assertIsNone(self.store.binding("whatsapp", fields["From"]))
        handler.rfile = BytesIO(raw)
        handler.wfile = BytesIO()
        handler.headers["X-Twilio-Signature"] = signature
        handler.do_POST()
        self.assertEqual(statuses[-1], 200)
        self.assertIn(b"<Response><Message>", handler.wfile.getvalue())

    def test_public_listener_never_exposes_local_web_controls(self):
        handler_type = make_handler(self.service, twilio_gateway=self.gateway, webhook_only=True)
        handler = object.__new__(handler_type)
        statuses = []
        handler.send_response = statuses.append
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        for path, method in (("/", "do_GET"), ("/api/overview", "do_GET"),
                             ("/api/chat", "do_POST"), ("/api/confirm", "do_POST")):
            handler.path, handler.wfile = path, BytesIO()
            getattr(handler, method)()
            self.assertEqual(statuses[-1], 404)

    def test_failed_processing_never_reinvokes_same_sid_and_recovers_product_receipt(self):
        fields = {"AccountSid": "ACfake", "MessageSid": "SMcrash123",
                  "From": "whatsapp:+15550123456", "To": "whatsapp:+15550000000", "Body": "hi"}
        raw, signature = self.signed(INBOUND_PATH, fields)
        original = self.service.handle
        def crash_after_product_receipt(message):
            original(message)
            raise RuntimeError("Synthetic crash after local receipt")
        with patch.object(self.service, "handle", side_effect=crash_after_product_receipt) as handle:
            with self.assertRaises(RuntimeError):
                self.gateway.inbound(raw, signature, now=NOW)
            recovered = self.gateway.inbound(raw, signature, now=NOW + timedelta(minutes=1))
            self.assertIn("<Message>", recovered)
            self.assertEqual(handle.call_count, 1)
        self.assertEqual(self.gateway.inbound(raw, signature, now=NOW), "<Response/>")

    def test_orphaned_claim_abstains_instead_of_repeating_provider_work(self):
        fields = {"AccountSid": "ACfake", "MessageSid": "SMorphan123",
                  "From": "whatsapp:+15550123456", "To": "whatsapp:+15550000000", "Body": "hi"}
        raw, signature = self.signed(INBOUND_PATH, fields)
        with patch.object(self.service, "handle", side_effect=RuntimeError("synthetic failure")) as handle:
            with self.assertRaises(RuntimeError):
                self.gateway.inbound(raw, signature, now=NOW)
            with self.assertRaises(RuntimeError):
                self.gateway.inbound(raw, signature, now=NOW + timedelta(minutes=1))
            response = self.gateway.inbound(raw, signature, now=NOW + timedelta(minutes=11))
            self.assertIn("Please send a new message", response)
            self.assertEqual(handle.call_count, 1)

    def test_malformed_payload_and_config_diagnostics_are_bounded(self):
        from carmind.twilio_gateway import parse_twilio_form
        for raw in (b"x=a&x=b", b"badfield", b"x=" + b"a" * 8192):
            with self.assertRaises(ValueError):
                parse_twilio_form(raw)
        self.assertNotIn(self.config.auth_token, repr(self.config))
        self.assertNotIn(self.config.whatsapp_from, repr(self.config))

    def test_status_replay_and_out_of_order_do_not_regress(self):
        fields = {"AccountSid": "ACfake", "MessageSid": "SMout123", "MessageStatus": "delivered"}
        raw, signature = self.signed(STATUS_PATH, fields)
        self.gateway.status(raw, signature, now=NOW)
        self.gateway.status(raw, signature, now=NOW)
        earlier = {**fields, "MessageStatus": "sent"}
        old_raw, old_signature = self.signed(STATUS_PATH, earlier)
        self.gateway.status(old_raw, old_signature, now=NOW)
        self.assertEqual(self.store.twilio_status("SMout123")["status"], "delivered")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM twilio_delivery_status").fetchone()[0], 1)
        with self.assertRaises(PermissionError):
            self.gateway.status(raw, "bad-signature", now=NOW)

    def test_status_correlates_to_local_notification_attempt(self):
        owner = self.service.app.create_owner(now=NOW, owner_id="owner")
        self.service.app.add_vehicle(owner, VehicleProfile("car", "Fictional", "Everyday", 2024), now=NOW)
        event = self.service.app.proactive._candidate(owner, "car", EventType.ODOMETER_UPDATE_REQUESTED,
            "SYSTEM", NOW, source_id="odometer_freshness", reason="Synthetic fixture")
        with self.store.transaction():
            self.store.save_proactive_event(event)
            self.store.save_notification({"id": "notice", "event_id": event["id"], "owner_id": owner,
                "vehicle_id": "car", "channel": "whatsapp", "event_type": event["event_type"],
                "created_at": NOW.isoformat(), "scheduled_for": NOW.isoformat(), "status": "SENT",
                "attempt_count": 1, "sequence": 0})
            self.store.db.execute("""INSERT INTO notification_attempts
                (notification_id,attempt_number,attempted_at,channel,result,provider_message_id)
                VALUES (?,?,?,?,?,?)""", ("notice", 1, NOW.isoformat(), "whatsapp", "SUCCESS", "SMnotice123"))
        fields = {"AccountSid": "ACfake", "MessageSid": "SMnotice123", "MessageStatus": "delivered"}
        raw, signature = self.signed(STATUS_PATH, fields)
        self.gateway.status(raw, signature, now=NOW)
        self.assertEqual(self.store.twilio_status("SMnotice123")["notification_id"], "notice")

    def test_sender_uses_injected_client_and_classifies_failures(self):
        client = SimpleNamespace(messages=FakeMessages())
        transport = TwilioWhatsAppTransport(self.config, client)
        receipt = transport("whatsapp:+15550123456", "Synthetic reminder")
        self.assertEqual(receipt.provider_message_id, "SMfake123")
        self.assertEqual(client.messages.calls[0]["status_callback"], self.config.url(STATUS_PATH))
        client.messages.response = TwilioRestException(429, "/messages")
        with self.assertRaises(TransientDeliveryError):
            transport("whatsapp:+15550123456", "Synthetic reminder")
        client.messages.response = TwilioRestException(400, "/messages")
        with self.assertRaises(PermanentDeliveryError):
            transport("whatsapp:+15550123456", "Synthetic reminder")
        client.messages.response = TimeoutError()
        with self.assertRaises(TransientDeliveryError):
            transport("whatsapp:+15550123456", "Synthetic reminder")

    def test_sender_reuses_existing_binding_and_receipt_boundary(self):
        owner = self.service.app.create_owner(now=NOW, owner_id="owner")
        session = self.service.app.start_session(owner, now=NOW, session_id="session")
        self.service.bind_existing("whatsapp", "whatsapp:+15550123456", owner, session)
        client = SimpleNamespace(messages=FakeMessages())
        sender = notification_sender(self.store, self.config, client=client)
        receipt = sender.send(SimpleNamespace(owner_id=owner), "Synthetic reminder")
        self.assertEqual(receipt.provider_message_id, "SMfake123")
        self.assertEqual(client.messages.calls[0]["to"], "whatsapp:+15550123456")

    def test_approved_template_configuration_replaces_freeform_body(self):
        import json
        from dataclasses import replace
        config = replace(self.config, content_sid="HX" + "a" * 32)
        client = SimpleNamespace(messages=FakeMessages())
        TwilioWhatsAppTransport(config, client)("whatsapp:+15550123456", "Synthetic\nreminder")
        sent = client.messages.calls[0]
        self.assertNotIn("body", sent)
        self.assertEqual(sent["content_sid"], config.content_sid)
        self.assertEqual(json.loads(sent["content_variables"]), {"1": "Synthetic reminder"})
        with self.assertRaises(ValueError):
            replace(self.config, content_sid="invalid-template")
