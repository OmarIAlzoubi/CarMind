"""Offline acceptance of one registered manual, retrieval, and grounded use."""

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from carmind.assessment import GroundingValidationError, validate_assessment
from carmind.capabilities import CapabilityRegistry
from carmind.contracts import UserMessage, VehicleProfile
from carmind.evidence import FrozenEvidenceSnapshot
from carmind.manual_eval import evaluate
from carmind.manufacturer_manual import (ManualIndex, build_index,
                                         load_manifest, load_manual_facts, load_source,
                                         resolve_source_file)
from carmind.planner_provider import FakePlannerProvider
from carmind.product import ProductService
from carmind.routing import run_assessment
from carmind.safety import evaluate_safety
from carmind.simulator import freeze_episode, generate_episode
from carmind.tools import execute_tool


NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
FIXTURE_DIRECTORY = Path(__file__).parent / "fixtures" / "manufacturer_manual"
FIXTURE_SOURCE = FIXTURE_DIRECTORY / "manual_source.json"
PROFILE = VehicleProfile("fictional-vehicle", "Example Motors", "Apex GT", 2025, 18500)


def snapshot(profile=PROFILE, text="What does the manual say about tire pressure?"):
    return FrozenEvidenceSnapshot("manual-episode", profile,
                                  UserMessage("manual-message", text, NOW), NOW)


class ManualTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = TemporaryDirectory()
        cls.index_path = Path(cls.temporary.name) / "manual.sqlite3"
        build_index(cls.index_path, source_file=FIXTURE_SOURCE)
        cls.index = ManualIndex(cls.index_path, source_file=FIXTURE_SOURCE)

    @classmethod
    def tearDownClass(cls):
        cls.index.close()
        cls.temporary.cleanup()

    def test_source_registry_and_section_manifest_one_document(self):
        source = load_source(FIXTURE_SOURCE)
        sections = load_manifest(source)
        self.assertEqual(source.model, "Apex GT")
        self.assertEqual(source.model_year, 2025)
        self.assertEqual(source.market, "FICTIONAL")
        self.assertEqual(source.page_count, 1)
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0]["page_start"], 1)
        self.assertEqual(sections[-1]["page_end"], 1)
        self.assertEqual(source.applicability(PROFILE), "unverified")
        self.assertEqual(source.applicability(PROFILE, "SA"), "inapplicable")
        self.assertEqual(source.applicability(VehicleProfile("v", "Ford", "Other", 2023)), "inapplicable")
        self.assertEqual(source.applicability(PROFILE, "FICTIONAL"), "unverified")

    def test_invalid_registry_and_missing_source_fail(self):
        source = load_source(FIXTURE_SOURCE)
        with TemporaryDirectory() as temp:
            path = Path(temp) / "source.json"
            data = {key: getattr(source, key) for key, definition in source.__dataclass_fields__.items()
                    if definition.init}
            data["page_count"] = 0
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_source(path)
            data["page_count"] = 1
            data["source_path"] = "missing.pdf"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(FileNotFoundError):
                load_source(path)
        with self.assertRaises(ValueError):
            source.path("../outside.pdf")

    def test_curated_facts_are_provenanced_but_not_an_active_schedule(self):
        facts = load_manual_facts(self.index.source)
        self.assertEqual(facts["authority"], "TEST_ONLY_FICTIONAL")
        by_id = {item["id"]: item for item in facts["facts"]}
        self.assertEqual(by_id["fictional_tire_pressure"]["physical_pages"], [1])
        self.assertIn("fictional", by_id["fictional_oil_grade"]["value"])

    def test_index_idempotence_and_stale_detection(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "manual.sqlite3"
            first = build_index(path, source_file=FIXTURE_SOURCE)
            self.assertEqual(first["status"], "built")
            self.assertEqual(first["chunks"], 1)
            self.assertEqual(build_index(path, source_file=FIXTURE_SOURCE)["status"], "current")
            import sqlite3
            db = sqlite3.connect(path)
            try:
                db.execute("UPDATE meta SET value='old' WHERE key='index_version'")
                db.commit()
            finally:
                db.close()
            with self.assertRaisesRegex(ValueError, "stale"):
                ManualIndex(path, source_file=FIXTURE_SOURCE)
            rebuilt = build_index(path, source_file=FIXTURE_SOURCE)
            self.assertEqual(rebuilt["status"], "built")

    def test_retrieval_eval_and_page_provenance(self):
        report = evaluate(self.index, FIXTURE_DIRECTORY / "retrieval_cases.json")
        self.assertEqual(report["cases"], 3)
        self.assertEqual(report["top1"], 3)
        self.assertEqual(report["top3"], 3)
        self.assertEqual(report["top5"], 3)
        hits = self.index.search("كم ضغط الكفر؟", PROFILE, top_k=3)
        self.assertEqual(hits[0]["physical_page"], 1)
        self.assertIsNone(hits[0]["printed_page"])
        self.assertEqual(hits[0]["source_id"], self.index.source.source_id)
        self.assertEqual(hits[0]["applicability"], "unverified")
        self.assertTrue(all(item["section"] for item in hits))

    def test_retrieval_boundaries_and_unanswerable(self):
        self.assertEqual(self.index.search("purple flying banana", PROFILE), ())
        self.assertEqual(self.index.search("what oil?", replace(PROFILE, make="Other")), ())
        self.assertEqual(self.index.search("what oil?", PROFILE, market="SA"), ())
        with self.assertRaises(ValueError):
            self.index.search("oil", PROFILE, top_k=6)
        with self.assertRaises(ValueError):
            self.index.search("x" * 241, PROFILE)
        hits = self.index.search("engine oil specification", PROFILE, top_k=5)
        self.assertLessEqual(sum(len(h["text"]) for h in hits), 7000)

    def test_tool_allowlist_and_manual_capability_gating(self):
        self.assertNotIn("manufacturer_manual", CapabilityRegistry().routing_descriptions())
        registry = CapabilityRegistry(include_manual=True)
        self.assertIn("manufacturer_manual", registry.routing_descriptions())
        self.assertEqual(registry.load_capabilities(["manufacturer_manual"]).tool_ids,
                         ("search_manufacturer_manual",))
        denied = execute_tool("search_manufacturer_manual", {"query": "tire pressure"}, snapshot(),
                              manual_index=self.index)
        self.assertEqual(denied.error, "tool_not_allowed")
        allowed = execute_tool("search_manufacturer_manual", {"query": "tire pressure normal load"},
                               snapshot(), manual_index=self.index,
                               allowed_tool_ids=("search_manufacturer_manual",))
        self.assertTrue(allowed.success)
        self.assertEqual(allowed.data["hits"][0]["physical_page"], 1)
        self.assertEqual(len(allowed.data["source"]), 8)
        self.assertEqual(execute_tool("search_manufacturer_manual", {"query": "oil"}, snapshot(),
            manual_index=self.index, manual_market="SA",
            allowed_tool_ids=("search_manufacturer_manual",)).error,
            "unavailable_manufacturer_knowledge")

    def test_planner_receives_only_retrieved_passages_and_cites_source(self):
        question = "How much oil does the manual list?"
        s = snapshot(text=question)
        hit = self.index.search("oil capacity", PROFILE, top_k=1)[0]
        provider = FakePlannerProvider([
            {"type": "tool_call", "tool_id": "search_manufacturer_manual",
             "arguments": {"query": "oil capacity", "top_k": 1}},
            {"type": "final", "assessment": {"observations": [
                {"text": "The TEST-ONLY FICTIONAL source lists an oil capacity of 4 L.",
                 "evidence_ids": [hit["evidence_id"]]}],
                "hypotheses": [], "uncertainties": ["APPLICABILITY_UNVERIFIED"],
                "recommended_action_ids": [], "limitations": []}}
        ])
        run = run_assessment(s, provider, manual_index=self.index)
        self.assertEqual(run.planner.trace.completion_status, "complete")
        self.assertEqual(run.planner.result.source_ids, (self.index.source.source_id,))
        self.assertIn("search_manufacturer_manual", run.planner.trace.tool_ids_called)
        self.assertNotIn("TEST-ONLY FICTIONAL engine oil grade", provider.requests[0][0]["content"])
        self.assertIn("TEST-ONLY FICTIONAL engine oil grade", provider.requests[1][-1]["content"])

    def test_unsupported_manual_number_and_uncited_manufacturer_claim_fail(self):
        s = snapshot()
        hit = self.index.search("tire pressure normal load", PROFILE, top_k=1)[0]
        base = {"observations": [], "hypotheses": [], "uncertainties": ["APPLICABILITY_UNVERIFIED"],
                "recommended_action_ids": [], "limitations": []}
        with self.assertRaises(GroundingValidationError):
            validate_assessment({**base, "observations": [
                {"text": "The manual says 999 psi.", "evidence_ids": [hit["evidence_id"]]}]},
                {hit["evidence_id"]: hit}, {hit["source_id"]}, evaluate_safety(s))
        with self.assertRaises(GroundingValidationError):
            validate_assessment({**base, "observations": [
                {"text": "Example Motors recommends this value.", "evidence_ids": [PROFILE.vehicle_id]}]},
                {PROFILE.vehicle_id: {"vehicle_id": PROFILE.vehicle_id, "make": PROFILE.make}},
                set(), evaluate_safety(s))

    def test_manual_retrieval_cannot_clear_deterministic_stop(self):
        simulated = freeze_episode(generate_episode("sustained_temperature_rise", 42)[0])
        s = replace(simulated, profile=PROFILE,
                    owner_message=UserMessage("manual-question", "What does the manual say about temperature?",
                                              simulated.assessment_at))
        # The existing thresholds are scoped to the fictional simulator vehicle;
        # an unresolved prior stop is the deterministic safety constraint here.
        prior_stop = evaluate_safety(simulated)
        self.assertEqual(prior_stop.disposition.value, "STOP_WHEN_SAFE")
        self.assertEqual(evaluate_safety(s).disposition.value, "UNDETERMINED")
        hit = self.index.search("coolant temperature warning", PROFILE, top_k=1)[0]
        provider = FakePlannerProvider([
            {"type": "tool_call", "tool_id": "search_manufacturer_manual",
             "arguments": {"query": "coolant temperature warning", "top_k": 1}},
            {"type": "final", "assessment": {"observations": [
                {"text": "The manual discusses a coolant temperature warning.",
                 "evidence_ids": [hit["evidence_id"]]}], "hypotheses": [],
                "uncertainties": ["APPLICABILITY_UNVERIFIED"],
                "recommended_action_ids": [], "limitations": []}}
        ])
        run = run_assessment(s, provider, manual_index=self.index, previous_stop=prior_stop)
        self.assertEqual(run.planner.result.safety.disposition.value, "STOP_WHEN_SAFE")


class ConfiguredSourceTests(unittest.TestCase):
    @staticmethod
    def _synthetic_source(root, manufacturer, model):
        directory = root / "manufacturer_knowledge" / manufacturer.casefold() / model.casefold()
        directory.mkdir(parents=True)
        pdf = directory / "manual.pdf"
        writer = PdfWriter()
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                 NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"):
            DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 12 Tf 72 720 Td (Synthetic tire section for retrieval.) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(stream)
        with pdf.open("wb") as output:
            writer.write(output)
        manifest = {"pdf_path": pdf.name, "page_count": 1,
                    "sections": [{"index": 1, "title": "Tires", "page_start": 1,
                                  "page_end": 1, "file": pdf.name}]}
        (directory / "sections_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        source_id = f"synthetic-{manufacturer.casefold()}-{model.casefold()}"
        registry = {"source_id": source_id, "manufacturer": manufacturer, "model": model,
                    "model_year": None, "market": None, "market_basis": "unknown",
                    "variant": "general", "language": "en",
                    "document_title": f"Synthetic {model} document", "document_type": "owner_manual",
                    "revision": None, "pdf_metadata_modified": None,
                    "source_path": str(pdf.relative_to(root)).replace("\\", "/"),
                    "manifest_path": str((directory / "sections_manifest.json").relative_to(root)).replace("\\", "/"),
                    "page_count": 1, "source_status": "unverified",
                    "applicability_note": "Synthetic test fixture only"}
        registry_path = directory / "manual_source.json"
        registry_path.write_text(json.dumps(registry), encoding="utf-8")
        (directory / "manual_facts.json").write_text(json.dumps({"source_id": source_id,
            "authority": "unverified", "facts": [], "severe_usage": {}}), encoding="utf-8")
        return registry_path

    def test_another_vehicle_loads_by_metadata_without_source_edits(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            toyota = self._synthetic_source(root, "Toyota", "Camry")
            with patch("carmind.manufacturer_manual.ROOT", root), patch(
                    "carmind.manufacturer_manual.SOURCE_DIRECTORY", root / "manufacturer_knowledge"):
                self.assertEqual(resolve_source_file(), toyota)
                self.assertEqual(load_source().manufacturer, "Toyota")
                index_path = root / "index.sqlite3"
                self.assertEqual(build_index(index_path)["chunks"], 1)
                index = ManualIndex(index_path)
                try:
                    hits = index.search("tire section", VehicleProfile("v", "Toyota", "Camry", 2025))
                    self.assertEqual(len(hits), 1)
                    self.assertEqual(hits[0]["document_title"], "Synthetic Camry document")
                    self.assertEqual(hits[0]["manufacturer"], "Toyota")
                    self.assertEqual(hits[0]["applicability"], "unverified")
                    self.assertEqual(load_manual_facts(index.source)["source_id"], index.source.source_id)
                    self.assertEqual(index.search("tire section", VehicleProfile("v2", "BMW", "M3", 2025)), ())
                    evaluation_file = root / "cases.json"
                    evaluation_file.write_text(json.dumps({"source_id": index.source.source_id,
                        "vehicle": {"make": "Toyota", "model": "Camry", "year": 2025},
                        "cases": [{"topic": "synthetic retrieval", "query": "tire section",
                                   "expected_pages": [1]}]}), encoding="utf-8")
                    self.assertEqual(evaluate(index, evaluation_file)["top1"], 1)
                    result = SimpleNamespace(assessment=SimpleNamespace(claims=(
                        {"facts": {hits[0]["evidence_id"]: hits[0]}},)))
                    self.assertEqual(ProductService._manual_sources(result)[0]["title"],
                                     "Synthetic Camry document")
                finally:
                    index.close()
                bmw = self._synthetic_source(root, "BMW", "M3")
                with self.assertRaisesRegex(ValueError, "Multiple manufacturer sources"):
                    resolve_source_file()
                self.assertEqual(load_source(bmw).manufacturer, "BMW")
                with patch.dict("os.environ", {"CARMIND_MANUAL_SOURCE": str(bmw)}):
                    self.assertEqual(load_source().model, "M3")


if __name__ == "__main__":
    unittest.main()
