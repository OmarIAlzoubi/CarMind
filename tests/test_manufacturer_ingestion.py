"""Public synthetic manufacturer onboarding; no private source or network."""

import base64
from datetime import datetime, timedelta, timezone
from contextlib import redirect_stdout
from io import BytesIO
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from carmind.capabilities import CapabilityRegistry
from carmind.composition import compose_app
from carmind.contracts import VehicleProfile
from carmind.manufacturer_ingestion import (ApplicabilityStatus, DuplicateDocumentError,
    IngestionStatus, ManufacturerIngestionService, VehicleManualRegistry)
from carmind.manufacturer_manual import MAX_RETURNED_CHARS
from carmind.manufacturer_knowledge import ROOT
from carmind.manual_cli import main as manual_main
from carmind.ownership_demo import empty_final
from carmind.planner_provider import FakePlannerProvider
from carmind.product import InboundMessage, ProductService
from carmind.storage import OwnershipStore
from carmind.web import make_handler


NOW = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
FIXTURE = ROOT / "tests" / "fixtures" / "manufacturer_manual" / "manual.pdf"


def make_pdf(path, text):
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"):
        DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    lines = [b"BT /F1 11 Tf 50 750 Td"]
    for line in text.splitlines():
        lines.append(b"(" + line.encode("ascii") + b") Tj 0 -20 Td")
    lines.append(b"ET")
    stream.set_data(b"\n".join(lines))
    page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as output:
        writer.write(output)


class IngestionTests(unittest.TestCase):
    def setUp(self):
        local = ROOT / ".local"
        local.mkdir(exist_ok=True)
        self.temp = TemporaryDirectory(dir=local)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = OwnershipStore(self.root / "owner.sqlite3")
        self.addCleanup(self.store.close)
        self.registry = VehicleManualRegistry(self.root / "manufacturer")
        self.addCleanup(self.registry.close)
        self.ingestion = ManufacturerIngestionService(self.store, self.registry)
        self.app = compose_app(self.store, provider=FakePlannerProvider([]), manual_index=self.registry)
        self.product = ProductService(self.app)
        self.owner, self.session = self.product._identity("web", "fixture-owner", NOW)
        self.profile = VehicleProfile("fictional-car-a", "Example Motors", "Apex GT", 2025)
        self.app.add_vehicle(self.owner, self.profile, now=NOW, market="FICTIONAL")
        self.app.select_vehicle(self.owner, self.session, self.profile.vehicle_id, now=NOW)

    def add(self, path=FIXTURE, *, metadata=None, vehicle_id=None):
        return self.ingestion.ingest(self.owner, vehicle_id or self.profile.vehicle_id, path,
                                     metadata=metadata, now=NOW + timedelta(minutes=1))

    def test_no_manual_then_single_document_grounded_answer_and_restart(self):
        self.assertIsNone(self.registry.for_vehicle(self.profile.vehicle_id))
        self.assertNotIn("manufacturer_manual", CapabilityRegistry(include_manual=False).routing_descriptions())
        before = self.product.handle(InboundMessage("web", "fixture-owner", "before",
            "What does my owner's manual say about tire pressure?", NOW + timedelta(minutes=1)))
        self.assertEqual(before.status, "unavailable")
        result = self.add(metadata={"document_type": "owner_manual", "document_title": "TEST-ONLY FICTIONAL Owner's Manual"})
        self.assertEqual(result.status, IngestionStatus.READY)
        self.assertEqual(result.applicability, ApplicabilityStatus.UNVERIFIED)
        view = self.registry.for_vehicle(self.profile.vehicle_id)
        self.assertIsNotNone(view)
        self.assertEqual(view.sources[result.source_id].ownership_vehicle_id, self.profile.vehicle_id)
        self.assertEqual(view.sources[result.source_id].applicability_status, "UNVERIFIED")
        self.assertIn("manufacturer_manual", CapabilityRegistry(include_manual=True).routing_descriptions())
        hit = view.search("tire pressure normal load", self.profile, market="FICTIONAL")[0]
        self.assertEqual(hit["physical_page"], 1)
        self.assertEqual(hit["source_id"], result.source_id)
        final = empty_final()
        final["assessment"].update(observations=[{
            "text": "The TEST-ONLY FICTIONAL manual lists 31 psi tire pressure.",
            "evidence_ids": [hit["evidence_id"]]}], uncertainties=["APPLICABILITY_UNVERIFIED"])
        self.app.provider = FakePlannerProvider([{"type": "tool_call", "tool_id": "search_manufacturer_manual",
            "arguments": {"query": "tire pressure normal load", "top_k": 1}}, final])
        answer = self.product.handle(InboundMessage("web", "fixture-owner", "after",
            "What does my owner's manual say about tire pressure?", NOW + timedelta(minutes=2)))
        self.assertEqual(answer.status, "complete")
        self.assertEqual(answer.sources[0]["physical_page"], 1)
        self.assertIn("unverified", answer.text)
        self.registry.close()
        self.registry = VehicleManualRegistry(self.root / "manufacturer")
        self.addCleanup(self.registry.close)
        self.app.manual_index = self.registry
        self.assertEqual(self.registry.for_vehicle(self.profile.vehicle_id).search(
            "tire pressure", self.profile)[0]["source_id"], result.source_id)
        self.assertEqual(self.product.overview("web", "fixture-owner", now=NOW + timedelta(minutes=3))
                         ["documents"][0]["status"], "READY")

    def test_multiple_documents_keep_provenance_and_vehicle_isolation(self):
        first = self.add(metadata={"document_type": "owner_manual"})
        second_pdf = self.root / "schedule.pdf"
        make_pdf(second_pdf, "TEST-ONLY FICTIONAL maintenance schedule\nQuasar brake fluid check every 9 moons")
        second = self.add(second_pdf, metadata={"document_type": "maintenance_schedule"})
        view = self.registry.for_vehicle(self.profile.vehicle_id)
        self.assertEqual({first.source_id, second.source_id}, view.sources.keys())
        hits = view.search("TEST ONLY FICTIONAL", self.profile, top_k=5)
        self.assertEqual({first.source_id, second.source_id}, {hit["source_id"] for hit in hits})
        self.assertTrue(all(hit["physical_page"] == 1 and hit["document_title"] for hit in hits))
        other = VehicleProfile("fictional-car-b", "Example Motors", "Apex GT", 2025)
        self.app.add_vehicle(self.owner, other, now=NOW, market="FICTIONAL")
        third = self.add(second_pdf, metadata={"document_type": "maintenance_schedule"}, vehicle_id=other.vehicle_id)
        self.assertNotIn(third.source_id, view.sources)
        self.assertEqual({third.source_id}, self.registry.for_vehicle(other.vehicle_id).sources.keys())
        self.assertEqual(view.search("quasar", other), ())
        self.assertNotIn(third.source_id, {hit["source_id"] for hit in view.search("quasar", self.profile)})
        self.assertLessEqual(sum(len(hit["text"]) for hit in hits), MAX_RETURNED_CHARS)

    def test_registry_cannot_borrow_another_vehicle_source(self):
        other = VehicleProfile("fictional-car-b", "Example Motors", "Apex GT", 2025)
        self.app.add_vehicle(self.owner, other, now=NOW, market="FICTIONAL")
        self.add(vehicle_id=other.vehicle_id)
        foreign = dict(self.registry._read(other.vehicle_id)["sources"][0])
        self.registry._write(self.profile.vehicle_id,
                             {"version": 1, "vehicle_id": self.profile.vehicle_id, "sources": [foreign]})
        self.assertEqual(self.registry.list(self.profile.vehicle_id)[0]["status"], "FAILED")
        self.assertIsNone(self.registry.for_vehicle(self.profile.vehicle_id))

    def test_running_registry_sees_new_document_added_by_cli_process(self):
        self.add()
        self.assertEqual(len(self.registry.for_vehicle(self.profile.vehicle_id).sources), 1)
        second = self.root / "second.pdf"
        make_pdf(second, "TEST-ONLY FICTIONAL quasar reference")
        external = VehicleManualRegistry(self.root / "manufacturer")
        self.addCleanup(external.close)
        ManufacturerIngestionService(self.store, external).ingest(
            self.owner, self.profile.vehicle_id, second, now=NOW + timedelta(minutes=2))
        self.assertEqual(len(self.registry.for_vehicle(self.profile.vehicle_id).sources), 2)

    def test_invalid_image_only_duplicate_and_unknown_applicability(self):
        invalid = self.root / "invalid.pdf"
        invalid.write_bytes(b"not a PDF")
        with self.assertRaisesRegex(ValueError, "PDF"):
            self.add(invalid)
        blank = self.root / "blank.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        with blank.open("wb") as output:
            writer.write(output)
        unavailable = self.add(blank)
        self.assertEqual(unavailable.status, IngestionStatus.TEXT_EXTRACTION_UNAVAILABLE)
        self.assertIsNone(self.registry.for_vehicle(self.profile.vehicle_id))
        ready = self.add()
        self.assertEqual(ready.applicability, ApplicabilityStatus.UNVERIFIED)
        self.assertEqual(self.registry.for_vehicle(self.profile.vehicle_id).search(
            "tire pressure", self.profile, market="FICTIONAL")[0]["applicability"], "unverified")
        with self.assertRaises(DuplicateDocumentError):
            self.add()
        claimed = self.root / "claimed.pdf"
        make_pdf(claimed, "TEST-ONLY FICTIONAL fluid specification")
        self.assertEqual(self.add(claimed, metadata={"model_year": 2025}).applicability,
                         ApplicabilityStatus.USER_PROVIDED)
        request, state = self.app.read_maintenance(self.owner, self.profile.vehicle_id, now=NOW + timedelta(minutes=2))
        self.assertIsNone(request)
        self.assertEqual(state, ())

    def test_conflicting_and_document_verified_metadata(self):
        verified_pdf = self.root / "labeled.pdf"
        make_pdf(verified_pdf, "Manufacturer: Example Motors\nModel: Apex GT\nModel year: 2025\nMarket: FICTIONAL\nTEST-ONLY FICTIONAL tire pressure 31 psi")
        verified = self.add(verified_pdf)
        self.assertEqual(verified.applicability, ApplicabilityStatus.DOCUMENT_VERIFIED)
        hit = self.registry.for_vehicle(self.profile.vehicle_id).search(
            "tire pressure", self.profile, market="FICTIONAL")[0]
        self.assertEqual(hit["applicability"], "verified_applicable")
        conflict_pdf = self.root / "conflict.pdf"
        make_pdf(conflict_pdf, "Manufacturer: Other Maker\nModel: Other Car\nModel year: 2025\nMarket: FICTIONAL\nTEST-ONLY FICTIONAL quasar guidance")
        conflict = self.add(conflict_pdf)
        self.assertEqual(conflict.applicability, ApplicabilityStatus.CONFLICTING)
        self.assertNotIn(conflict.source_id, self.registry.for_vehicle(self.profile.vehicle_id).sources)
        trimmed = VehicleProfile("fictional-trimmed", "Example Motors", "Apex GT", 2025)
        self.app.add_vehicle(self.owner, trimmed, now=NOW, market="FICTIONAL", trim="Sport")
        without_trim = self.add(verified_pdf, vehicle_id=trimmed.vehicle_id)
        self.assertEqual(without_trim.applicability, ApplicabilityStatus.UNVERIFIED)

    def test_web_upload_and_status_are_vehicle_scoped(self):
        handler_type = make_handler(self.product, web_identity="fixture-owner")
        handler = object.__new__(handler_type)
        payload = {"vehicle_id": self.profile.vehicle_id, "filename": "fictional-manual.pdf",
                   "content_base64": base64.b64encode(FIXTURE.read_bytes()).decode("ascii"),
                   "metadata": {"document_type": "owner_manual"}}
        body = json.dumps(payload).encode()
        handler.path = "/api/manual/upload"
        handler.rfile, handler.wfile = BytesIO(body), BytesIO()
        handler.headers = {"Content-Length": str(len(body)), "Content-Type": "application/json"}
        statuses = []
        handler.send_response = statuses.append
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        with patch("carmind.web.datetime") as clock:
            clock.now.return_value = NOW + timedelta(minutes=1)
            handler.do_POST()
        self.assertEqual(statuses, [201])
        self.assertEqual(json.loads(handler.wfile.getvalue())["status"], "READY")
        overview = self.product.overview("web", "fixture-owner", now=NOW + timedelta(minutes=2))
        self.assertEqual(overview["documents"][0]["document_title"], "fictional-manual")
        self.assertEqual(overview["documents"][0]["page_count"], 1)
        payload["filename"] = "../escape.pdf"
        invalid_body = json.dumps(payload).encode()
        handler.rfile, handler.wfile = BytesIO(invalid_body), BytesIO()
        handler.headers["Content-Length"] = str(len(invalid_body))
        with patch("carmind.web.datetime") as clock:
            clock.now.return_value = NOW + timedelta(minutes=2)
            handler.do_POST()
        self.assertEqual(statuses[-1], 400)
        self.assertEqual(len(self.registry.list(self.profile.vehicle_id)), 1)

    def test_cli_add_list_status_and_rebuild_use_same_local_registry(self):
        db_path = self.root / "cli.sqlite3"
        store = OwnershipStore(db_path)
        with store.transaction():
            store.create_owner("cli-owner", NOW)
            store.add_vehicle("cli-owner", VehicleProfile("cli-car", "Example Motors", "Apex GT", 2025), NOW)
        store.close()
        cli_root = self.root / "cli-manufacturer"
        output = StringIO()
        with patch("carmind.manual_cli.registry_for_database", side_effect=lambda _: VehicleManualRegistry(cli_root)), \
             redirect_stdout(output):
            self.assertEqual(manual_main(["--db", str(db_path), "add", "--vehicle-id", "cli-car",
                "--file", str(FIXTURE), "--document-type", "owner_manual"]), 0)
            self.assertEqual(manual_main(["--db", str(db_path), "list"]), 0)
            self.assertEqual(manual_main(["--db", str(db_path), "status", "--vehicle-id", "cli-car"]), 0)
            self.assertEqual(manual_main(["--db", str(db_path), "rebuild", "--vehicle-id", "cli-car"]), 0)
        self.assertIn('"status": "READY"', output.getvalue())
        self.assertIn('"cli-car"', output.getvalue())


if __name__ == "__main__":
    unittest.main()
