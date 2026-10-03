"""Synthetic final candidates only; never a reconstruction of the live response."""

from copy import deepcopy
from dataclasses import asdict
import json
import os
import unittest
from unittest.mock import patch

from carmind.assessment import sanitize_rejected_candidate
from carmind.capabilities import load_capabilities
from carmind.planner import PROTOCOL, run_planner
from carmind.planner_provider import FakePlannerProvider, ProviderResponse, ProviderFailure, ProviderDiagnostic
from carmind.simulator import generate_episode, freeze_episode
from scripts.live_xai_compare import run_case
from support import call, final, maintenance_context


class FinalAssessmentForensicsTests(unittest.TestCase):
    def setUp(self):
        self.episode, _ = generate_episode("gradual_tire_pressure_loss", 42)
        self.snapshot = freeze_episode(self.episode)
        self.ids = [o.observation_id for o in self.snapshot.observations if o.name == "rear_left_tire_pressure"]

    def run_fake(self, turns, *, max_calls=3):
        provider = FakePlannerProvider(turns)
        run = run_planner(self.snapshot, provider, loaded=load_capabilities(["tires"]), max_model_calls=max_calls)
        return run, provider

    def test_A_no_manufacturer_source_valid_with_explicit_empty_list(self):
        run, provider = self.run_fake([call("get_tire_pressure_summary"), final(self.ids)], max_calls=2)
        initial = json.loads(provider.requests[0][1]["content"])
        self.assertEqual(initial["allowed_manufacturer_source_ids"], [])
        self.assertIsNone(initial["manufacturer_knowledge"])
        self.assertEqual(initial["maintenance_state"], [])
        self.assertIn("source_ids MUST be []", PROTOCOL)
        self.assertEqual(run.trace.final_status, "final_valid")
        self.assertEqual(run.trace.stop_reason, "assessment_completed")
        self.assertEqual(run.result.source_ids, ())
        self.assertEqual(run.trace.rejected_model_candidates, [])

    def test_B_invented_source_is_rejected_not_stripped_or_published(self):
        candidate = final([self.snapshot.profile.vehicle_id], sources=["invented-source"])
        before = deepcopy(candidate)
        run, provider = self.run_fake([candidate], max_calls=1)
        self.assertEqual(candidate, before)
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(run.trace.final_status, "final_grounding_failed")
        self.assertEqual(run.trace.validation_failures, ["Unknown or unexposed manufacturer source"])
        event = run.trace.rejected_model_candidates[0]
        self.assertEqual(event["exposed_manufacturer_source_count"], 0)
        self.assertEqual(event["rejected_model_candidate"]["source_ids"], ["<unexposed_id>"])
        self.assertEqual(event["validation_failure"], run.trace.validation_failures[0])
        self.assertEqual(run.result.claims, ())
        self.assertEqual(run.result.assessment.evidence_ids, [])
        self.assertEqual(run.result.source_ids, ())

    def test_C_exposed_source_valid_for_supported_manufacturer_claim(self):
        snapshot, context, request = maintenance_context()
        provider = FakePlannerProvider([final(["fixture-oil"], sources=["fixture-source"])])
        run = run_planner(snapshot, provider, context, request, loaded=load_capabilities(["maintenance"]), max_model_calls=1)
        initial = json.loads(provider.requests[0][1]["content"])
        self.assertEqual(initial["allowed_manufacturer_source_ids"], ["fixture-source"])
        self.assertEqual(run.trace.final_status, "final_valid")
        self.assertEqual(run.result.source_ids, ("fixture-source",))

    def test_D_exposed_source_does_not_authorize_another_source(self):
        snapshot, context, request = maintenance_context()
        run = run_planner(snapshot, FakePlannerProvider([final(["fixture-oil"], sources=["other-source"])]),
                          context, request, loaded=load_capabilities(["maintenance"]), max_model_calls=1)
        self.assertEqual(run.trace.final_status, "final_grounding_failed")
        self.assertEqual(run.trace.rejected_model_candidates[0]["exposed_manufacturer_source_count"], 1)

    def test_E_valid_final_on_last_call_and_cached_accounting(self):
        # These are author-written fixtures with reported usage numbers, not live content.
        actions = [call("get_tire_pressure_summary"), call("get_tire_pressure_history", wheel="rear_left"), final(self.ids)]
        counts = [(3223, 25, 3200), (5409, 31, 5376), (6251, 1936, 6144)]
        turns = [ProviderResponse(json.dumps(action), i, o, cached_input_tokens=c) for action, (i, o, c) in zip(actions, counts)]
        run, _ = self.run_fake(turns)
        self.assertEqual(run.trace.stop_reason, "assessment_completed")
        self.assertEqual(run.trace.final_status, "final_valid")
        self.assertEqual(run.trace.repair_status, "not_requested")
        self.assertEqual(run.trace.planner_call_count, 3)
        self.assertEqual((run.trace.input_tokens, run.trace.cached_input_tokens, run.trace.output_tokens), (14883, 14720, 1992))
        self.assertTrue(run.trace.usage_complete)
        self.assertTrue(run.trace.cached_usage_complete)
        self.assertIsNone(run.trace.cost_usd)

    def test_F_invalid_final_on_last_call_reports_primary_failure_and_budget(self):
        run, provider = self.run_fake([call("get_tire_pressure_summary"),
                                       call("get_tire_pressure_history", wheel="rear_left"),
                                       final(self.ids, sources=["invented"])])
        self.assertEqual(run.trace.final_status, "final_grounding_failed")
        self.assertEqual(run.trace.stop_reason, "repair_needed_but_call_budget_exhausted")
        self.assertEqual(run.trace.repair_status, "repair_needed_but_call_budget_exhausted")
        self.assertEqual(run.trace.last_model_action, "final")
        self.assertEqual(len(provider.requests), 3)
        self.assertEqual(run.trace.rejected_model_candidates[0]["call_number"], 3)
        self.assertEqual(run.result.safety.disposition.value, "STOP_WHEN_SAFE")
        self.assertIn("Missing critical evidence: coolant_temperature", run.result.assessment.limitations)
        self.assertIn("Missing critical evidence: battery_voltage", run.result.assessment.limitations)

    def test_G_corrected_final_uses_normal_budget_and_preserves_context(self):
        run, provider = self.run_fake([call("get_tire_pressure_summary"), final(self.ids, sources=["invented"]), final(self.ids)])
        self.assertEqual(run.trace.final_status, "final_valid")
        self.assertEqual(run.trace.repair_status, "repair_succeeded")
        self.assertEqual(run.trace.completion_status, "complete")
        self.assertEqual(len(provider.requests), 3)
        self.assertEqual(provider.requests[2][:-1], provider.requests[1])
        error = json.loads(provider.requests[2][-1]["content"])
        self.assertEqual(error["validation_error"], "Unknown or unexposed manufacturer source")
        self.assertEqual(len(run.trace.rejected_model_candidates), 1)
        self.assertEqual(run.result.source_ids, ())

    def test_repair_can_request_tool_and_then_valid_final_within_existing_maximum(self):
        run, _ = self.run_fake([final(sources=["invented"]), call("get_tire_pressure_summary"), final(self.ids)], max_calls=4)
        self.assertEqual(run.trace.completion_status, "complete")
        self.assertEqual(run.trace.repair_status, "repair_succeeded")
        self.assertEqual(run.trace.tool_ids_called, ["get_tire_pressure_summary"])
        self.assertEqual(run.trace.planner_call_count, 3)

    def test_failed_repair_and_non_grounding_final_are_distinct(self):
        bad = final()
        bad["assessment"]["recommended_action_ids"] = ["invented-action"]
        run, _ = self.run_fake([bad, bad])
        self.assertEqual(run.trace.stop_reason, "repair_attempt_failed")
        self.assertEqual(run.trace.repair_status, "repair_attempt_failed")
        self.assertEqual(run.trace.final_status, "final_validation_failed")
        self.assertEqual(len(run.trace.rejected_model_candidates), 2)
        self.assertEqual(run.trace.planner_call_count, 2)

    def test_ordinary_tool_budget_stop_has_no_final_or_repair(self):
        run, _ = self.run_fake([call("get_tire_pressure_summary")], max_calls=1)
        self.assertEqual(run.trace.stop_reason, "call_budget_exhausted")
        self.assertIsNone(run.trace.final_status)
        self.assertEqual(run.trace.repair_status, "not_requested")

    def test_provider_failure_during_repair_preserves_final_rejection(self):
        class FailingRepair:
            count = 0
            def generate(inner, messages):
                inner.count += 1
                if inner.count == 1:
                    return ProviderResponse(json.dumps(final(sources=["invented"])))
                raise ProviderFailure(ProviderDiagnostic("APITimeoutError", None, None, None, "Request timed out.", None, "timeout"))
        run = run_planner(self.snapshot, FailingRepair(), loaded=load_capabilities(["tires"]))
        self.assertEqual(run.trace.stop_reason, "provider_timeout")
        self.assertEqual(run.trace.final_status, "final_grounding_failed")
        self.assertEqual(run.trace.repair_status, "repair_attempted")

    def test_sanitized_candidate_excludes_secrets_future_ids_truth_and_free_text(self):
        future = next(o.observation_id for o in self.episode.timeline if o.timestamp > self.snapshot.assessment_at)
        bad = final([self.ids[0]], sources=["invented"])
        bad["assessment"]["observations"].append({"text": "Authorization: Bearer synthetic-key", "evidence_ids": [future]})
        bad["assessment"]["hypotheses"] = [{"hypothesis_id": "gradual_tire_pressure_loss", "evidence_ids": ["synthetic-key"]}]
        bad["assessment"]["ScenarioTruth"] = {"scenario": "gradual_tire_pressure_loss"}
        bad["assessment"]["uncertainties"] = ["Authorization: Bearer synthetic-key"]
        bad["assessment"]["limitations"] = ["future timestamp or arbitrary secret data"]
        with patch.dict(os.environ, {"XAI_API_KEY": "synthetic-key"}, clear=True):
            run, _ = self.run_fake([call("get_tire_pressure_summary"), bad], max_calls=2)
            projection = sanitize_rejected_candidate(final(["synthetic-key"])["assessment"], {"synthetic-key"}, set())
        logged = json.dumps(asdict(run.trace)) + json.dumps(projection)
        for forbidden in ("synthetic-key", "Authorization", "ScenarioTruth", "gradual_tire_pressure_loss", future, "future timestamp"):
            self.assertNotIn(forbidden, logged)
        event = run.trace.rejected_model_candidates[0]
        self.assertIn(self.ids[0], json.dumps(event))
        self.assertEqual(event["rejected_model_candidate"]["omitted_field_count"], 1)
        self.assertIn("<redacted_id>", json.dumps(projection))
        self.assertNotIn("source_ids", asdict(run.result.assessment))

    def test_malformed_final_candidate_retains_safe_shape_only(self):
        for assessment in ("secret arbitrary text", {"source_ids": None}, {"observations": ["raw text"]}):
            run, _ = self.run_fake([{"type": "final", "assessment": assessment}], max_calls=1)
            self.assertEqual(run.trace.final_status, "final_validation_failed")
            self.assertNotIn("secret arbitrary text", json.dumps(asdict(run.trace)))
            self.assertNotIn("raw text", json.dumps(asdict(run.trace)))
            self.assertEqual(len(run.trace.rejected_model_candidates), 1)

    def test_runner_labels_rejected_candidate_and_successful_repair_separately(self):
        output = []
        run, _ = run_case("routed", FakePlannerProvider([final(sources=["invented"]), final()]),
                          model="fake", write=output.append, max_calls=2)
        self.assertEqual(run.planner.trace.final_status, "final_valid")
        self.assertIn("VALIDATION RESULT: passed_after_repair", output)
        self.assertIn("REPAIR STATUS: repair_succeeded", output)
        records = [line for line in output if line.startswith("REJECTED MODEL CANDIDATE")]
        self.assertEqual(len(records), 1)
        self.assertIn("rejected_model_candidate", records[0])
        self.assertIn("Unknown or unexposed manufacturer source", records[0])
        self.assertNotIn('"invented"', records[0])


if __name__ == "__main__":
    unittest.main()
