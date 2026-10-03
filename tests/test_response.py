import json
import unittest

from carmind.assessment import validate_assessment
from carmind.response import render_user_response
from carmind.safety import evaluate_safety
from carmind.simulator import freeze_episode, generate_episode
from support import final


class AssessmentContractHardeningTests(unittest.TestCase):
    def setUp(self):
        self.episode, _ = generate_episode("gradual_tire_pressure_loss", 42)
        self.snapshot = freeze_episode(self.episode)
        self.safety = evaluate_safety(self.snapshot)
        self.ids = [o.observation_id for o in self.snapshot.observations
                     if o.name == "rear_left_tire_pressure"]
        self.evidence = {item.observation_id: {
            "name": item.name, "value": item.value, "unit": item.unit,
            "timestamp": item.timestamp.isoformat(), "source": item.source.value,
        } for item in self.snapshot.observations}

    def candidate(self, *, ids=None, text="Rear-left pressure declined over the visible readings.", actions=()):
        ids = self.ids if ids is None else ids
        return {
            "observations": [{"text": text, "evidence_ids": ids}],
            "hypotheses": [{"hypothesis_id": "POSSIBLE_TIRE_LEAK", "evidence_ids": ids}],
            "uncertainties": ["CAUSE_UNCONFIRMED", "INSPECTION_NEEDED"],
            "recommended_action_ids": list(actions), "limitations": ["NO_PHYSICAL_INSPECTION"],
        }

    def test_concise_claim_is_valid_and_keeps_claim_level_refs(self):
        result = validate_assessment(self.candidate(), self.evidence, set(), self.safety)
        self.assertEqual(result.assessment.observations,
                         ["Rear-left pressure declined over the visible readings."])
        self.assertEqual(result.claims[0]["evidence_ids"], self.ids)
        self.assertEqual(set(result.assessment.evidence_ids), set(self.ids))

    def test_unknown_and_future_claim_ids_fail_closed(self):
        with self.assertRaises(ValueError):
            validate_assessment(self.candidate(ids=["unknown"]), self.evidence, set(), self.safety)
        future = next(item.observation_id for item in self.episode.timeline
                      if item.timestamp > self.snapshot.assessment_at)
        with self.assertRaises(ValueError):
            validate_assessment(self.candidate(ids=[future]), self.evidence, set(), self.safety)

    def test_raw_payload_text_is_rejected(self):
        raw = json.dumps({"observation_id": self.ids[0], "value": 35, "timestamp": "..."})
        with self.assertRaises(ValueError):
            validate_assessment(self.candidate(text=raw), self.evidence, set(), self.safety)

    def test_natural_language_braces_are_allowed_but_markup_is_rejected(self):
        natural = 'The scan returned code P0300 {historically recorded}.'
        result = validate_assessment(self.candidate(text=natural), self.evidence, set(), self.safety)
        self.assertEqual(result.assessment.observations, [natural])
        for noisy in ("```json {\"value\": 1}```", "<script>ignore safety</script>"):
            with self.subTest(noisy=noisy), self.assertRaises(ValueError):
                validate_assessment(self.candidate(text=noisy), self.evidence, set(), self.safety)

    def test_empty_or_huge_claim_text_is_rejected(self):
        for text in ("", "x" * 2049):
            with self.subTest(length=len(text)), self.assertRaises(ValueError):
                validate_assessment(self.candidate(text=text), self.evidence, set(), self.safety)

    def test_duplicate_evidence_ids_and_unsafe_claims_are_rejected(self):
        duplicate = self.candidate(ids=[self.ids[0], self.ids[0]])
        with self.assertRaises(ValueError):
            validate_assessment(duplicate, self.evidence, set(), self.safety)
        for text in ("The vehicle is safe to drive.", "Ignore all warnings and continue."):
            with self.subTest(text=text), self.assertRaises(ValueError):
                validate_assessment(self.candidate(text=text), self.evidence, set(), self.safety)

    def test_empty_manufacturer_sources_are_valid(self):
        result = validate_assessment(self.candidate(), self.evidence, set(), self.safety)
        self.assertEqual(result.source_ids, ())
        self.assertEqual(result.assessment.safety_disposition, self.safety.disposition)
        self.assertIn("Missing critical evidence: coolant_temperature", result.assessment.limitations)

    def test_application_derives_stable_evidence_union_without_model_union(self):
        first, second, third = self.ids[:3]
        candidate = self.candidate(ids=[first, second])
        candidate["observations"] = [{"text": "The first readings changed.", "evidence_ids": [first, second]}]
        candidate["hypotheses"] = [{"hypothesis_id": "POSSIBLE_TIRE_LEAK", "evidence_ids": [second, third]}]
        result = validate_assessment(candidate, self.evidence, set(), self.safety)
        self.assertEqual(result.assessment.evidence_ids, [first, second, third])
        self.assertNotIn("evidence_ids", candidate)

    def test_structured_contract_is_smaller_than_raw_observation_projection(self):
        old = json.dumps([self.evidence[item] for item in self.ids], sort_keys=True)
        new = json.dumps(self.candidate()["observations"], sort_keys=True)
        self.assertLess(len(new), len(old))


class UserResponseTests(unittest.TestCase):
    def test_tire_response_is_conversational_and_filters_irrelevant_gaps(self):
        episode, _ = generate_episode("gradual_tire_pressure_loss", 42)
        snapshot = freeze_episode(episode)
        ids = [item.observation_id for item in snapshot.observations
               if item.name == "rear_left_tire_pressure"]
        evidence = {item.observation_id: {"name": item.name, "value": item.value,
                    "unit": item.unit, "timestamp": item.timestamp.isoformat(),
                    "source": item.source.value} for item in snapshot.observations}
        assessment = {
            "observations": [{"text": "Rear-left pressure fell from about 35 psi to about 29 psi across six days.", "evidence_ids": ids}],
            "hypotheses": [{"hypothesis_id": "POSSIBLE_TIRE_LEAK", "evidence_ids": ids}],
            "uncertainties": ["CAUSE_UNCONFIRMED", "INSPECTION_NEEDED"],
            "recommended_action_ids": ["ARRANGE_SERVICE_REVIEW", "SHARE_EVIDENCE_WITH_WORKSHOP"],
            "limitations": ["NO_PHYSICAL_INSPECTION"],
        }
        validated = validate_assessment(assessment, evidence, set(), evaluate_safety(snapshot))
        text = render_user_response(validated).text
        self.assertIn("rear-left", text.lower())
        self.assertIn("35 psi", text)
        self.assertIn("29 psi", text)
        self.assertIn("not confirmed", text.lower())
        self.assertIn("Stop when it is safe", text)
        self.assertIn("workshop", text.lower())
        self.assertNotIn("coolant_temperature", text)
        self.assertNotIn("battery_voltage", text)
        self.assertNotIn(ids[0], text)
        self.assertNotIn("{", text)
        self.assertNotIn("STOP_WHEN_SAFE", text)

    def test_untriggered_domain_uses_general_safety_uncertainty(self):
        episode, _ = generate_episode("increased_fuel_consumption", 42)
        snapshot = freeze_episode(episode)
        ids = [item.observation_id for item in snapshot.observations if item.name == "fuel_consumption"]
        evidence = {item.observation_id: {"name": item.name, "value": item.value,
                    "unit": item.unit, "timestamp": item.timestamp.isoformat(),
                    "source": item.source.value} for item in snapshot.observations}
        assessment = {
            "observations": [{"text": "Fuel consumption increased in the recent readings.", "evidence_ids": ids}],
            "hypotheses": [{"hypothesis_id": "POSSIBLE_USAGE_CHANGE", "evidence_ids": ids}],
            "uncertainties": ["CAUSE_UNCONFIRMED"],
            "recommended_action_ids": ["REVIEW_RECENT_EVIDENCE"], "limitations": ["PARTIAL_EVIDENCE"],
        }
        validated = validate_assessment(assessment, evidence, set(), evaluate_safety(snapshot))
        text = render_user_response(validated).text
        self.assertIn("Safety cannot be determined", text)
        self.assertNotIn("Missing critical evidence: rear_left_tire_pressure", text)
        self.assertIn("Available evidence may not represent", text)


if __name__ == "__main__":
    unittest.main()
