from dataclasses import asdict, replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from carmind.manufacturer_knowledge import (KnowledgePack, MaintenanceRule, load_knowledge,
                                             resolve_knowledge_directory)
from support import PROFILE, SOURCE, RULE, PACK


class KnowledgeTests(unittest.TestCase):
    def test_metadata_required(self):
        for field in ("source_id", "document_title", "source_type", "market", "official_source_reference", "retrieved_at"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                replace(SOURCE, **{field: ""})
        with self.assertRaises(ValueError):
            KnowledgePack(PROFILE, (), (RULE,))

    def test_provenance_required_and_resolved(self):
        for field in ("source_id", "rule_id", "page", "section"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                replace(RULE, **{field: ""})
        with self.assertRaises(ValueError):
            replace(PACK, rules=(replace(RULE, source_id="invented"),))

    def test_exact_matching(self):
        self.assertTrue(PACK.matches(PROFILE))
        for field, value in (("manufacturer", "Other"), ("model", "Other"), ("model_year", 2025), ("market", "Saudi Arabia"), ("engine_variant", "other"), ("transmission", "DCT"), ("knowledge_version", "2")):
            with self.subTest(field=field):
                self.assertFalse(PACK.matches(replace(PROFILE, **{field: value})))

    def test_invalid_intervals_and_triggers(self):
        for changes in ({"interval_km": -1}, {"interval_km": float("nan")}, {"interval_months": 1.5}, {"trigger": "TIME"}, {"trigger": "INVALID"}, {"operating_condition": "maybe"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(RULE, **changes)

    def test_normal_severe_distinct(self):
        severe = replace(RULE, rule_id="severe-oil", operating_condition="SEVERE", interval_km=5000)
        pack = replace(PACK, rules=(RULE, severe))
        self.assertEqual({r.operating_condition for r in pack.rules}, {"NORMAL", "SEVERE"})

    def test_duplicate_identity_rejected(self):
        for changes in ({"rules": (RULE, RULE)}, {"sources": (SOURCE, SOURCE)}, {"sources": (replace(SOURCE, market="Other"),)}):
            with self.assertRaises(ValueError):
                replace(PACK, **changes)

    def test_configured_pack_quarantines_unknown_year(self):
        with TemporaryDirectory() as temp:
            directory = Path(temp) / "manufacturer_knowledge" / "synthetic" / "example"
            directory.mkdir(parents=True)
            profile = replace(PROFILE, model_year=None)
            source = replace(SOURCE, model_year=None)
            (directory / "sources.json").write_text(json.dumps({"sources": [asdict(source)]}), encoding="utf-8")
            (directory / "maintenance_schedule.json").write_text(json.dumps({
                "profile": asdict(profile), "rules": [asdict(RULE)], "poc_only": True}), encoding="utf-8")
            with patch("carmind.manufacturer_knowledge.KNOWLEDGE_DIRECTORY", directory.parent):
                self.assertEqual(resolve_knowledge_directory(), directory)
                pack = load_knowledge()
            self.assertIsNone(pack.profile.model_year)
            self.assertFalse(pack.matches(pack.profile))
            self.assertFalse(pack.matches(replace(pack.profile, model_year=2024)))
            self.assertTrue(pack.poc_only)
            self.assertEqual(len(pack.rules), 1)
            self.assertNotIn("ScenarioTruth", json.dumps(asdict(pack)))

    def test_supported_mileage_and_time_rules(self):
        self.assertEqual(replace(RULE, trigger="MILEAGE", interval_months=None).trigger, "MILEAGE")
        self.assertEqual(replace(RULE, trigger="TIME", interval_km=None).trigger, "TIME")
