"""Local runner checks: fake providers only, with no API configuration."""

import json
import os
import unittest
from unittest.mock import patch

from carmind.planner_provider import FakePlannerProvider, ProviderDiagnostic, ProviderFailure
from carmind.router_provider import JevCapabilityRouter
from scripts.live_xai_compare import RequestLimitReached, MeasuredProvider, run_case, main
from support import call, final


class LocalRunnerTests(unittest.TestCase):
    def test_explicit_three_call_budget_completes_scripted_sequence(self):
        backend = FakePlannerProvider([call("get_tire_pressure_history"), call("get_tire_pressure_summary"), final()])
        output = []
        run, measured = run_case("routed", backend, model="fake", max_calls=3, write=output.append)
        self.assertEqual(measured.attempts, 3)
        self.assertEqual(run.planner.trace.completion_status, "complete")
        with self.assertRaises(RequestLimitReached):
            measured.generate([])
        self.assertEqual(len(backend.requests), 3)
        self.assertEqual(sum(line.startswith("CONTEXT BREAKDOWN:") for line in output), 3)
        self.assertIn("payload_characters", "\n".join(output))

    def test_explicit_budget_rejects_invalid_values_and_caps_at_four(self):
        for budget in (0, 5, True, 2.5, "3"):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                MeasuredProvider(FakePlannerProvider([]), max_calls=budget)
        backend = FakePlannerProvider([call("get_vehicle_profile")] * 5)
        run, measured = run_case("routed", backend, model="fake", max_calls=4, write=lambda _: None)
        self.assertEqual(len(backend.requests), 4)
        self.assertEqual(run.planner.trace.completion_status, "incomplete")
        with self.assertRaises(RequestLimitReached):
            measured.generate([])

    def test_failed_attempt_consumes_explicit_budget_without_retry(self):
        class Failing:
            calls = 0
            def generate(self, messages):
                self.calls += 1
                raise RuntimeError("synthetic failure")
        backend = Failing()
        measured = MeasuredProvider(backend, write=lambda _: None, max_calls=1)
        with self.assertRaises(RuntimeError):
            measured.generate([])
        with self.assertRaises(RequestLimitReached):
            measured.generate([])
        self.assertEqual(backend.calls, 1)

    def test_manual_entry_point_rejects_sdk_retries_before_run(self):
        from types import SimpleNamespace
        with patch("sys.argv", ["live_xai_compare.py", "--mode", "routed", "--max-calls", "3"]), \
             patch("scripts.live_xai_compare.load_local_env"), \
             patch.dict(os.environ, {"XAI_API_KEY": "synthetic", "XAI_MODEL": "grok-4.6"}, clear=True), \
             patch("scripts.live_xai_compare.XAIPlannerProvider", return_value=SimpleNamespace(_client=SimpleNamespace(max_retries=1))), \
             patch("scripts.live_xai_compare.run_case") as run, patch("sys.stderr"):
            with self.assertRaises(SystemExit):
                main()
        run.assert_not_called()

    def test_modes_share_snapshot_but_expose_different_capabilities(self):
        routed = FakePlannerProvider([final()])
        full = FakePlannerProvider([final()])
        with patch.dict(os.environ, {"XAI_API_KEY": "", "TYPESAFE_API_KEY": ""}, clear=True), \
             patch.object(JevCapabilityRouter, "route", side_effect=AssertionError("Jev must not run")):
            routed_run, routed_meter = run_case("routed", routed, model="fake", write=lambda _: None)
            full_run, full_meter = run_case("full", full, model="fake", write=lambda _: None)
        self.assertEqual(routed_run.routing.initial_loaded_capabilities, ("tires",))
        self.assertEqual(routed_run.planner.trace.exposed_tool_count, 3)
        self.assertEqual(full_run.planner.trace.exposed_capability_count, 11)
        self.assertEqual(full_run.planner.trace.exposed_tool_count, 25)
        self.assertEqual((routed_meter.attempts, full_meter.attempts), (1, 1))
        routed_initial = json.loads(routed.requests[0][1]["content"])
        full_initial = json.loads(full.requests[0][1]["content"])
        self.assertEqual(len(routed_initial.pop("tools")), 3)
        self.assertEqual(len(full_initial.pop("tools")), 25)
        self.assertEqual(routed_initial, full_initial)
        self.assertEqual(routed_initial["owner_message"]["text"],
                         "One tire keeps losing pressure. Can you check what might be happening?")

    def test_two_call_budget_blocks_a_third_provider_request(self):
        backend = FakePlannerProvider([call("get_tire_pressure_history"),
                                       call("get_tire_pressure_summary"), final()])
        run, measured = run_case("routed", backend, model="fake", write=lambda _: None)
        self.assertEqual(measured.attempts, 2)
        self.assertEqual(len(backend.requests), 2)
        self.assertEqual(run.planner.trace.completion_status, "incomplete")
        self.assertEqual(run.planner.trace.tool_ids_called,
                         ["get_tire_pressure_history", "get_tire_pressure_summary"])
        with self.assertRaises(RequestLimitReached):
            measured.generate([])
        self.assertEqual(len(backend.requests), 2)

    def test_provider_failure_does_not_retry_and_reports_safe_diagnostic(self):
        diagnostic = ProviderDiagnostic("APIConnectionError", None, None, None,
                                        "Connection error.", None, "connection_network")
        class Failing:
            calls = 0
            def generate(self, messages):
                self.calls += 1
                raise ProviderFailure(diagnostic)
        backend = Failing()
        output = []
        run, measured = run_case("routed", backend, model="fake", write=output.append)
        self.assertEqual((backend.calls, measured.attempts), (1, 1))
        self.assertEqual(run.planner.trace.provider_error, diagnostic)
        self.assertIn("connection_network", "\n".join(output))

    def test_raw_provider_exception_and_secret_are_not_printed(self):
        secret = "UNIT_TEST_SECRET_DO_NOT_PRINT"
        class Failing:
            def generate(self, messages):
                raise RuntimeError("Authorization: Bearer " + secret)
        output = []
        with patch.dict(os.environ, {"XAI_API_KEY": "", "TYPESAFE_API_KEY": ""}, clear=True):
            run, measured = run_case("full", Failing(), model="fake", write=output.append)
        self.assertEqual(measured.attempts, 1)
        self.assertEqual(run.planner.trace.completion_status, "incomplete")
        self.assertNotIn(secret, "\n".join(output))
        self.assertNotIn("Authorization", "\n".join(output))


if __name__ == "__main__":
    unittest.main()
