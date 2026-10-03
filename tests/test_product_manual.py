"""Offline 2023 owner journey, shared channels, and live-startup safeguards."""

from datetime import datetime, timedelta, timezone
from io import BytesIO, StringIO
from contextlib import redirect_stderr
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from carmind.capabilities import CapabilityRegistry
from carmind.composition import compose_app
from carmind.contracts import VehicleProfile
from carmind.manufacturer_manual import ManualIndex, build_index, open_optional_index
from carmind.ownership_demo import command_turn, empty_final
from carmind.planner_provider import FakePlannerProvider
from carmind.planner_provider import ProviderDiagnostic, ProviderFailure
from carmind.product import InboundMessage, ProductService
from carmind.provider_config import load_project_env, require_live_config
from carmind.storage import OwnershipStore
from carmind.web import PAGE, main as web_main, make_handler
from carmind.whatsapp import handle_payload
from carmind.whatsapp_demo import main as whatsapp_main


NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
FIXTURE_SOURCE = Path(__file__).parent / "fixtures" / "manufacturer_manual" / "manual_source.json"


class ProductManualTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = OwnershipStore(Path(self.temp.name) / "owner.sqlite3")
        self.addCleanup(self.store.close)
        index_path = Path(self.temp.name) / "manual.sqlite3"
        build_index(index_path, source_file=FIXTURE_SOURCE)
        self.index = ManualIndex(index_path, source_file=FIXTURE_SOURCE)
        self.addCleanup(self.index.close)
        self.app = compose_app(self.store, provider=FakePlannerProvider([]), manual_index=self.index)
        self.service = ProductService(self.app)
        self.serial = 0

    def say(self, text, *, script=None, channel="whatsapp", at=NOW):
        self.serial += 1
        if script is not None:
            self.app.provider = FakePlannerProvider(script)
        return self.service.handle(InboundMessage(channel, "local-wa" if channel == "whatsapp" else "local-web-user",
                                     f"manual-{self.serial}", text, at))

    def onboard(self):
        text = "سيارتي Example Motors Apex GT موديل 2025 وممشاها 18500 كم"
        draft = {"type": "vehicle_proposal", "owner_quote": text,
                 "vehicle": {"make": "Example Motors", "model": "Apex GT", "year": 2025,
                             "odometer": 18500, "unit": "km", "trim": None,
                             "engine": None, "market": None, "nickname": None}}
        proposal = self.say(text, script=[draft])
        self.assertEqual(proposal.status, "confirmation_required")
        self.assertEqual(proposal.proposal["kind"], "add_vehicle")
        self.say("أكد", at=NOW + timedelta(minutes=1))
        return self.service.overview("whatsapp", "local-wa", now=NOW + timedelta(minutes=1))["vehicle"]["id"]

    def test_greetings_are_local_and_vehicle_aware(self):
        self.app.provider = FakePlannerProvider([])
        hello = self.say("سلام")
        self.assertEqual(hello.status, "complete")
        self.assertIn("وش سيارتك", hello.text)
        self.assertEqual(self.app.provider.requests, [])
        self.onboard()
        self.app.provider = FakePlannerProvider([])
        later = self.say("هلا", at=NOW + timedelta(minutes=2))
        self.assertIn("2025 Example Motors Apex GT", later.text)
        self.assertEqual(self.app.provider.requests, [])

    def test_provider_failure_copy_does_not_leak_diagnostic_or_secret(self):
        diagnostic = ProviderDiagnostic("AuthenticationError", 401, "auth", "invalid_key",
                                        "credential rejected", "req-safe", "authentication")
        with patch.object(self.app.provider, "generate", side_effect=ProviderFailure(diagnostic)):
            reply = self.say("سيارتي Example Motors Apex GT موديل 2025")
        self.assertEqual(reply.status, "unavailable")
        self.assertIn("غير متاحة", reply.text)
        self.assertNotIn("credential", reply.text)

    def test_cancelled_exact_proposal_cannot_be_applied(self):
        text = "سيارتي Example Motors Apex GT موديل 2025 وممشاها 18500 كم"
        draft = {"type": "vehicle_proposal", "owner_quote": text,
                 "vehicle": {"make": "Example Motors", "model": "Apex GT", "year": 2025,
                             "odometer": 18500, "unit": "km"}}
        reply = self.say(text, script=[draft])
        self.service.cancel_proposal("whatsapp", "local-wa", reply.proposal_id)
        with self.assertRaises(ValueError):
            self.service.cancel_proposal("whatsapp", "local-wa", reply.proposal_id)
        denied = self.say("أكد", at=NOW + timedelta(minutes=1))
        self.assertNotEqual(denied.status, "applied")
        self.assertIsNone(self.service.overview("whatsapp", "local-wa", now=NOW)["vehicle"])

    def test_offline_owner_manual_cross_channel_journey(self):
        vehicle_id = self.onboard()
        profile = self.store.vehicle(self.store.binding("whatsapp", "local-wa")["owner_id"], vehicle_id, NOW).profile
        odometer_final = empty_final()
        odometer_final["assessment"]["observations"] = [
            {"text": "The saved mileage is available.", "evidence_ids": [vehicle_id]}]
        mileage = self.say("كم ممشاها؟", at=NOW + timedelta(minutes=2), script=[
            {"type": "tool_call", "tool_id": "get_vehicle_profile", "arguments": {}}, odometer_final])
        self.assertIn("18,500", mileage.text)
        at = NOW + timedelta(minutes=3)
        service_text = "غيرت الزيت اليوم على 18500"
        pending = self.say(service_text, at=at, script=[command_turn("record_service_event", {
            "service_type": "oil", "performed_at": at.isoformat(), "odometer": 18500, "unit": "km"}, service_text)])
        self.assertEqual(pending.status, "confirmation_required")
        self.assertEqual(pending.proposal["kind"], "record_service_event")
        self.say("أكد", at=at + timedelta(minutes=1))
        records = self.store.service_records(vehicle_id, at + timedelta(minutes=1))
        self.assertEqual(len(records), 1)
        history_final = empty_final()
        history_final["assessment"]["observations"] = [
            {"text": "The recorded oil service is available.", "evidence_ids": [records[0].record_id]}]
        history = self.say("متى غيرت الزيت؟", at=at + timedelta(minutes=2), script=[
            {"type": "tool_call", "tool_id": "get_latest_service_record",
             "arguments": {"service_type": "oil"}}, history_final])
        self.assertIn("18,500", history.text)
        self.assertEqual(history.sources, ())
        hit = self.index.search("engine oil specification XM-7", profile, top_k=1)[0]
        manual_final = empty_final()
        manual_final["assessment"].update(
            observations=[{"text": "الدليل التجريبي الخيالي يذكر زيت محرك XM-7.",
                           "evidence_ids": [hit["evidence_id"]]}],
            uncertainties=["APPLICABILITY_UNVERIFIED"])
        manual = self.say("وش الزيت المناسب لسيارتي؟", at=at + timedelta(minutes=3), script=[
            {"type": "tool_call", "tool_id": "search_manufacturer_manual",
             "arguments": {"query": "engine oil specification XM-7", "top_k": 1}},
            manual_final])
        self.assertEqual(manual.status, "complete")
        self.assertIn("غير مؤكدة", manual.text)
        self.assertEqual(manual.sources[0]["source_id"], self.index.source.source_id)
        self.assertEqual(manual.sources[0]["physical_page"], 1)
        owner = self.store.binding("whatsapp", "local-wa")
        self.service.bind_existing("web", "local-web-user", owner["owner_id"], owner["session_id"])
        web = self.service.overview("web", "local-web-user", now=at + timedelta(minutes=4))
        self.assertEqual(web["vehicle"]["label"], "2025 Example Motors Apex GT")
        self.assertEqual(web["odometer"]["km"], 18500)
        self.assertEqual(web["last_service"]["type"], "oil")
        self.assertEqual(web["maintenance"], [])
        self.assertEqual(web["manufacturer"]["status"], "unverified")
        view = self.say("كيف وضع سيارتي؟", at=at + timedelta(minutes=4), script=[
            {"type": "tool_call", "tool_id": "get_vehicle_profile", "arguments": {}}, odometer_final])
        self.assertIn("2025 Example Motors Apex GT", view.text)

    def test_web_api_returns_source_cards_and_safe_proposal(self):
        self.onboard()
        owner = self.store.binding("whatsapp", "local-wa")
        self.service.bind_existing("web", "local-web-user", owner["owner_id"], owner["session_id"])
        vehicle = self.service.overview("web", "local-web-user", now=NOW + timedelta(minutes=1))["vehicle"]
        profile = self.store.vehicle(owner["owner_id"], vehicle["id"], NOW + timedelta(minutes=1)).profile
        hit = self.index.search("tire pressure normal load", profile, top_k=1)[0]
        final = empty_final()
        final["assessment"].update(observations=[{"text": "The TEST-ONLY FICTIONAL source shows 31 psi under normal load.",
                                                      "evidence_ids": [hit["evidence_id"]]}],
                                   uncertainties=["APPLICABILITY_UNVERIFIED"])
        self.app.provider = FakePlannerProvider([
            {"type": "tool_call", "tool_id": "search_manufacturer_manual",
             "arguments": {"query": "tire pressure normal load", "top_k": 1}}, final])
        handler_type = make_handler(self.service, web_identity="local-web-user")
        handler = object.__new__(handler_type)
        data = json.dumps({"message_id": "web-manual", "text": "What tire pressure is in the manual?"}).encode()
        handler.path = "/api/chat"
        handler.rfile = BytesIO(data)
        handler.wfile = BytesIO()
        handler.headers = {"Content-Length": str(len(data)), "Content-Type": "application/json"}
        statuses = []
        handler.send_response = statuses.append
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        with patch("carmind.web.datetime") as clock:
            clock.now.return_value = NOW + timedelta(minutes=2)
            handler.do_POST()
        response = json.loads(handler.wfile.getvalue())
        self.assertEqual(statuses, [200])
        self.assertEqual(response["sources"][0]["physical_page"], 1)
        self.assertIsNone(response["proposal"])

    def test_web_localization_and_responsive_markup(self):
        html = PAGE.read_text(encoding="utf-8")
        for needle in ('name="viewport"', 'max-width:1050px', 'max-width:820px',
                       'max-width:580px', 'dir=\'auto\'', 'source-card',
                       'proposalCard', "lang==='ar'?'rtl':'ltr'", 'T(known?'):
            self.assertIn(needle, html)


class LiveConfigTests(unittest.TestCase):
    def test_env_file_uses_allowlist_and_shell_override(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / ".env"
            path.write_text('TYPESAFE_API_KEY=file-value\nXAI_API_KEY=file-secret\nXAI_MODEL=grok-test\nUNRELATED=ignored\n', encoding="utf-8")
            with patch.dict(os.environ, {"TYPESAFE_API_KEY": "shell-value"}, clear=True):
                load_project_env(path)
                self.assertEqual(os.environ["TYPESAFE_API_KEY"], "shell-value")
                self.assertEqual(os.environ["XAI_MODEL"], "grok-test")
                self.assertNotIn("UNRELATED", os.environ)

    def test_live_requires_confirmation_bounds_and_config_before_sdk(self):
        with self.assertRaisesRegex(ValueError, "confirm-live"):
            require_live_config(confirmed=False, max_calls_per_turn=3, timeout_seconds=60)
        with self.assertRaisesRegex(ValueError, "max-calls"):
            require_live_config(confirmed=True, max_calls_per_turn=5, timeout_seconds=60)
        with patch("carmind.provider_config.load_project_env"), patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "TYPESAFE_API_KEY, XAI_API_KEY, XAI_MODEL"):
                require_live_config(confirmed=True, max_calls_per_turn=3, timeout_seconds=60)
        with patch("carmind.web.compose_app", side_effect=AssertionError("SDK constructed")):
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                web_main(["--live", "--db", str(Path("never-created.sqlite3"))])
        with TemporaryDirectory() as temp:
            db = str(Path(temp) / "web.sqlite3")
            with patch("carmind.web.require_live_config") as guard, \
                 patch("carmind.web.compose_app", side_effect=RuntimeError("stopped before provider")) as compose:
                with self.assertRaisesRegex(RuntimeError, "stopped before provider"):
                    web_main(["--live", "--confirm-live-api-use", "--db", db])
                guard.assert_called_once()
                self.assertTrue(compose.call_args.kwargs["live"])
                self.assertEqual(compose.call_args.kwargs["max_model_calls"], 3)
            with patch("carmind.whatsapp_demo.require_live_config") as guard, \
                 patch("carmind.whatsapp_demo.compose_app", side_effect=RuntimeError("stopped before provider")) as compose:
                with self.assertRaisesRegex(RuntimeError, "stopped before provider"):
                    whatsapp_main(["--live", "--confirm-live-api-use", "--db", db])
                guard.assert_called_once()
                self.assertTrue(compose.call_args.kwargs["live"])


class CleanCloneManualTests(unittest.TestCase):
    def test_no_private_source_starts_web_and_product_without_manual_capability(self):
        with TemporaryDirectory() as temp, patch.dict(os.environ, {"CARMIND_MANUAL_SOURCE": ""}), \
             patch("carmind.manufacturer_manual.SOURCE_DIRECTORY", Path(temp) / "absent_sources"):
            self.assertIsNone(open_optional_index(Path(temp) / "absent-index.sqlite3"))
            with patch("carmind.web.HTTPServer") as server, \
                 patch("carmind.web.compose_app", wraps=compose_app) as composed:
                self.assertEqual(web_main(["--db", str(Path(temp) / "web.sqlite3")]), 0)
                server.assert_called_once()
                self.assertIsNone(composed.call_args.kwargs["manual_index"])
            store = OwnershipStore(Path(temp) / "owner.sqlite3")
            try:
                onboarding = {"type": "vehicle_proposal", "owner_quote": "My car is an Example Motors Apex GT 2025",
                              "vehicle": {"make": "Example Motors", "model": "Apex GT", "year": 2025}}
                provider = FakePlannerProvider([onboarding])
                app = compose_app(store, provider=provider)
                service = ProductService(app)
                proposed = service.handle(InboundMessage("web", "clean-owner", "onboard",
                    onboarding["owner_quote"], NOW))
                self.assertEqual(proposed.status, "confirmation_required")
                applied = service.handle(InboundMessage("web", "clean-owner", "confirm",
                    "confirm", NOW + timedelta(minutes=1)))
                self.assertEqual(applied.status, "applied")
                app.provider = FakePlannerProvider([])
                manual = service.handle(InboundMessage("web", "clean-owner", "manual",
                    "What does my owner's manual say about tire pressure?", NOW + timedelta(minutes=2)))
                self.assertEqual(manual.status, "unavailable")
                self.assertIn("don't have manufacturer documentation configured", manual.text)
                self.assertEqual(manual.sources, ())
                self.assertEqual(app.provider.requests, [])
                self.assertIsNone(app.manual_index)
                self.assertNotIn("manufacturer_manual", CapabilityRegistry(
                    include_manual=app.manual_index is not None).routing_descriptions())
                greeting = service.handle(InboundMessage("web", "clean-owner", "hello",
                    "hello", NOW + timedelta(minutes=3)))
                self.assertEqual(greeting.status, "complete")
                self.assertIn("Example Motors Apex GT", greeting.text)
            finally:
                store.close()

    def test_explicit_synthetic_source_enables_rag_without_discovery(self):
        with TemporaryDirectory() as temp, patch.dict(os.environ, {"CARMIND_MANUAL_SOURCE": ""}), \
             patch("carmind.manufacturer_manual.SOURCE_DIRECTORY", Path(temp) / "absent_sources"):
            index_path = Path(temp) / "manual.sqlite3"
            build_index(index_path, source_file=FIXTURE_SOURCE)
            index = open_optional_index(index_path, source_file=FIXTURE_SOURCE)
            try:
                self.assertIsNotNone(index)
                self.assertEqual(index.source.manufacturer, "Example Motors")
                self.assertEqual(index.source.source_status, "test_only_fictional")
                profile = VehicleProfile("fictional", "Example Motors", "Apex GT", 2025)
                self.assertEqual(index.search("tire pressure", profile)[0]["physical_page"], 1)
                self.assertIn("manufacturer_manual", CapabilityRegistry(
                    include_manual=index is not None).routing_descriptions())
            finally:
                index.close()

    def test_live_startup_reaches_provider_composition_without_manual(self):
        with TemporaryDirectory() as temp, patch.dict(os.environ, {"CARMIND_MANUAL_SOURCE": ""}), \
             patch("carmind.manufacturer_manual.SOURCE_DIRECTORY", Path(temp) / "absent_sources"):
            db = str(Path(temp) / "live.sqlite3")
            with patch("carmind.web.require_live_config"), \
                 patch("carmind.web.compose_app", side_effect=RuntimeError("provider composition reached")) as compose:
                with self.assertRaisesRegex(RuntimeError, "provider composition reached"):
                    web_main(["--live", "--confirm-live-api-use", "--db", db])
                self.assertIsNone(compose.call_args.kwargs["manual_index"])
            with patch("carmind.whatsapp_demo.require_live_config"), \
                 patch("carmind.whatsapp_demo.compose_app", side_effect=RuntimeError("provider composition reached")) as compose:
                with self.assertRaisesRegex(RuntimeError, "provider composition reached"):
                    whatsapp_main(["--live", "--confirm-live-api-use", "--db", db])
                self.assertIsNone(compose.call_args.kwargs["manual_index"])


if __name__ == "__main__":
    unittest.main()
