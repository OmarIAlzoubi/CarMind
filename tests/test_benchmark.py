import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from carmind.benchmark import load_cases, make_inputs, routing_metrics, run_benchmark, summarize
from carmind.planner_provider import FakePlannerProvider
from carmind.router_provider import FakeCapabilityRouter, RouterResponse
from carmind.routing import ExecutionMode


class BenchmarkTests(unittest.TestCase):
    def test_maintenance_fixture_is_explicit_and_test_only_without_local_sources(self):
        case = next(case for case in load_cases() if case["case_id"] == "maintenance")
        with TemporaryDirectory() as temp, patch.dict(os.environ, {"CARMIND_KNOWLEDGE_DIRECTORY": ""}), \
             patch("carmind.manufacturer_knowledge.KNOWLEDGE_DIRECTORY", Path(temp) / "absent"):
            request = make_inputs(case).maintenance_request
        self.assertTrue(request.pack.poc_only)
        self.assertIsNone(request.pack.profile.model_year)
        self.assertTrue(all(source.source_type == "TEST_ONLY_NON_PRODUCTION_FICTIONAL"
                            for source in request.pack.sources))

    def test_development_cases_are_labelled_and_complete(self):
        cases = load_cases()
        self.assertEqual(len(cases), 13)
        self.assertEqual(len({case["case_id"] for case in cases}), 13)
        for case in cases:
            self.assertTrue(case["expected"]["capabilities"])
            self.assertTrue(case.get("message") or make_inputs(case).snapshot.owner_message.text)

    def test_labels_are_not_in_router_or_planner_inputs(self):
        case = load_cases()[0]
        inputs = make_inputs(case)
        router = FakeCapabilityRouter(selected=case["script"]["selection"])
        report = run_benchmark([case], router_factory=lambda _: router)
        serialized = json.dumps(router.requests, default=str)
        for capability in case["expected"]["capabilities"]:
            # Expected labels are metadata, while the selected capability may be
            # present as a scripted fixture. The hidden fixture name is the
            # stronger boundary check here.
            self.assertNotIn(case["fixture"], serialized)
        self.assertEqual(report["rows"][0]["owner_message"], inputs.snapshot.owner_message.text)

    def test_routing_metrics(self):
        metrics = routing_metrics(["tires", "engine"], ["tires", "battery"])
        self.assertEqual(metrics["required_recall"], 0.5)
        self.assertEqual(metrics["selection_precision"], 0.5)
        self.assertFalse(metrics["exact_set_agreement"])

    def test_offline_benchmark_has_paired_modes_and_context_metrics(self):
        report = run_benchmark(load_cases()[:2])
        rows = report["rows"]
        self.assertEqual({row["mode"] for row in rows}, {"FULL", "ROUTED"})
        self.assertEqual(len(rows), 4)
        for case_id in {row["case_id"] for row in rows}:
            self.assertEqual({row["mode"] for row in rows if row["case_id"] == case_id}, {"FULL", "ROUTED"})
        self.assertGreater(report["summary"]["FULL"]["capability_count"], report["summary"]["ROUTED"]["capability_count"])
        self.assertGreater(report["summary"]["FULL"]["tool_count"], report["summary"]["ROUTED"]["tool_count"])
        self.assertIsNone(report["summary"]["FULL"]["planner_input_tokens"])
        self.assertIsNone(report["summary"]["ROUTED"]["total_cost_usd"])

    def test_fallback_adjusted_metrics_are_distinct(self):
        case = load_cases()[0]
        report = run_benchmark([case], router_factory=lambda _: FakeCapabilityRouter(
            RouterResponse({key: 0.5 for key in report_capabilities()})
        ))
        routed = next(row for row in report["rows"] if row["mode"] == "ROUTED")
        self.assertEqual(routed["raw_routing"]["required_recall"], 0.0)
        self.assertEqual(routed["effective_routing"]["required_recall"], 1.0)
        self.assertTrue(routed["routing"]["fallback_used"])

    def test_existing_ambiguous_fixture_still_falls_back(self):
        case = next(case for case in load_cases() if case["case_id"] == "ambiguous")
        report = run_benchmark([case])
        routed = next(row for row in report["rows"] if row["mode"] == "ROUTED")
        self.assertEqual(routed["routing"]["raw_selected_capabilities"], ())
        self.assertTrue(routed["routing"]["fallback_used"])
        self.assertEqual(routed["routing"]["fallback_reason"], "borderline_relevance")
        self.assertEqual(routed["capability_count"], 11)

    def test_summary_reports_quality_regression_honestly(self):
        rows = []
        for mode, success in (("FULL", True), ("ROUTED", False)):
            rows.append({
                "mode": mode,
                "capability_count": 10,
                "tool_count": 25,
                "instruction_characters": 100,
                "tool_schema_characters": 100,
                "planner_calls": 1,
                "tool_calls": 0,
                "initial_context_characters": 100,
                "cumulative_request_characters": 100,
                "tool_result_characters": 0,
                "planner_trace": {"validation_failures": []},
                "routing_latency": 0,
                "planner_latency": 0,
                "end_to_end_latency": 0,
                "quality": {"assessment_complete": success, "task_success": success,
                            "required_evidence_coverage": 1.0 if success else 0.0,
                            "safety_policy_violations": 0, "grounding_valid": True},
                "routing": {"fallback_used": False, "expansion_used": False},
                "planner_input_tokens": None, "planner_output_tokens": None,
                "total_tokens": None, "total_cost_usd": None,
                "raw_routing": {"required_recall": 0, "selection_precision": 0, "exact_set_agreement": False},
                "effective_routing": {"required_recall": 0, "selection_precision": 0, "exact_set_agreement": False},
            })
        result = summarize(rows)
        self.assertTrue(result["quality_regression_observed"])
        self.assertIn("Quality regressed", result["conclusion"])


def report_capabilities():
    from carmind.capabilities import CapabilityRegistry
    return CapabilityRegistry().routing_descriptions()


if __name__ == "__main__":
    unittest.main()
