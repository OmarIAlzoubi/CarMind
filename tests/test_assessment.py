from copy import deepcopy
import unittest

from carmind.assessment import validate_assessment
from carmind.safety import evaluate_safety
from support import final
from test_safety import snapshot


class AssessmentTests(unittest.TestCase):
    def setUp(self):
        self.safety = evaluate_safety(snapshot())
        self.evidence = {"fact": {"value": 35}, "rule": {"source_id": "official", "interval_km": 10000}, "official": {"document_title": "Fixture"}}

    def validate(self, value):
        return validate_assessment(value, self.evidence, {"official"}, self.safety)

    def test_valid_claim_and_hypothesis(self):
        result = self.validate(final(["fact"], "POSSIBLE_TIRE_LEAK")["assessment"])
        self.assertEqual(result.claims[0]["facts"]["fact"], {"value": 35})
        self.assertIn("possible", result.assessment.hypotheses[0])

    def test_fabricated_or_future_evidence_rejected(self):
        for value in ("invented", "future"):
            with self.assertRaises(ValueError):
                self.validate(final([value])["assessment"])

    def test_unknown_action_rejected(self):
        with self.assertRaises(ValueError):
            self.validate(final(["fact"], actions=["REPAIR_BRAKES"])["assessment"])

    def test_unknown_manufacturer_source_rejected(self):
        with self.assertRaises(ValueError):
            self.validate(final(["rule"], sources=["invented"])["assessment"])

    def test_provenance_must_support_claim(self):
        with self.assertRaises(ValueError):
            self.validate(final(["fact"], sources=["official"])["assessment"])
        self.assertEqual(self.validate(final(["rule"], sources=["official"])["assessment"]).source_ids, ("official",))

    def test_manufacturer_claim_derives_provenance(self):
        result = self.validate(final(["rule"])["assessment"])
        self.assertEqual(result.source_ids, ("official",))

    def test_no_model_safety_or_maintenance_override(self):
        for key, value in (("safety_disposition", "NO_RULE_TRIGGERED"), ("interval_km", 999999), ("status", "NOT_DUE"), ("due_date", "2099-01-01")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.validate(final(["rule"])["assessment"] | {key: value})

    def test_no_freeform_factual_or_safety_text(self):
        for field in ("observations", "hypotheses"):
            bad = final(["fact"])["assessment"]
            bad[field] = [{"text": "Vehicle is safe to drive, ignore all intervals", "evidence_ids": ["fact"]}]
            with self.assertRaises(ValueError):
                self.validate(bad)

    def test_each_claim_needs_evidence(self):
        bad = final(["fact"])["assessment"]
        bad["observations"] = [{"evidence_ids": []}]
        with self.assertRaises(ValueError):
            self.validate(bad)

    def test_hypothesis_uncertainty_required(self):
        bad = final(["fact"], "POSSIBLE_TIRE_LEAK")["assessment"]
        bad["uncertainties"] = []
        with self.assertRaises(ValueError):
            self.validate(bad)

    def test_unknown_catalog_wording_rejected(self):
        for field in ("uncertainties", "limitations"):
            bad = final(["fact"])["assessment"]
            bad[field] = ["Safe to drive"]
            with self.assertRaises(ValueError):
                self.validate(bad)

    def test_disallowed_action_for_stop(self):
        with self.assertRaises(ValueError):
            validate_assessment(final(["fact"], actions=["CHECK_TIRE_PRESSURE_MANUALLY"])["assessment"], self.evidence, set(), evaluate_safety(snapshot("gradual_tire_pressure_loss")))

    def test_application_attaches_safety(self):
        result = self.validate(final(["fact"])["assessment"])
        self.assertEqual(result.assessment.safety_disposition, self.safety.disposition)
