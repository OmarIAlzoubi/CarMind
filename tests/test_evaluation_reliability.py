"""Offline timeout, usage and outcome accounting; no live SDK transport."""

from dataclasses import asdict
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from carmind.planner import PlannerTrace, run_full_planner
from carmind.planner_provider import (FakePlannerProvider, ProviderResponse, ProviderFailure,
                                      ProviderDiagnostic, XAIPlannerProvider)
from carmind.simulator import freeze_episode, generate_episode
from scripts.live_xai_compare import main, run_case, RequestLimitReached
from support import call, final


def snapshot():
    return freeze_episode(generate_episode("gradual_tire_pressure_loss", 42)[0])


def response(action, input_tokens=None, output_tokens=None, cached=None):
    return ProviderResponse(json.dumps(action, separators=(",", ":")), input_tokens, output_tokens,
                            cached_input_tokens=cached)


class ScriptedFailure:
    """Return the supplied successful turns, then one sanitized synthetic failure."""
    timeout_seconds = 120.0

    def __init__(self, responses=(), category="timeout"):
        self.responses = list(responses)
        self.calls = 0
        self.category = category

    def generate(self, messages):
        self.calls += 1
        if self.responses:
            return self.responses.pop(0)
        raise ProviderFailure(ProviderDiagnostic("APITimeoutError" if self.category == "timeout" else "APIConnectionError",
                              None, None, None, "Request timed out." if self.category == "timeout" else "Connection error.",
                              None, self.category))


class TimeoutTests(unittest.TestCase):
    def test_default_and_override_pass_same_request_settings_and_zero_retries(self):
        for timeout in (None, 120, 180):
            client = Mock()
            factory = Mock(return_value=client)
            client.chat.completions.create.return_value = SimpleNamespace(usage=None, choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))])
            with patch.dict(os.environ, {"XAI_API_KEY": "synthetic", "XAI_MODEL": "grok-4.6"}, clear=True), \
                 patch.dict("sys.modules", {"openai": SimpleNamespace(OpenAI=factory)}):
                provider = XAIPlannerProvider() if timeout is None else XAIPlannerProvider(timeout_seconds=timeout)
                provider.generate([])
            expected = 45 if timeout is None else timeout
            self.assertEqual(provider.timeout_seconds, expected)
            self.assertEqual(factory.call_args.kwargs["timeout"], expected)
            self.assertEqual(factory.call_args.kwargs["max_retries"], 0)
            self.assertEqual(factory.call_args.kwargs["base_url"], "https://api.x.ai/v1")
            self.assertEqual(client.chat.completions.create.call_args.kwargs,
                             {"model": "grok-4.6", "messages": [], "response_format": {"type": "json_object"}})

    def test_invalid_timeout_fails_before_configuration_or_sdk(self):
        factory = Mock()
        with patch("carmind.planner_provider.missing_configuration") as config, \
             patch.dict("sys.modules", {"openai": SimpleNamespace(OpenAI=factory)}):
            for value in (0, -1, 181, None, True, "120", float("inf"), float("nan"), 10**1000):
                with self.subTest(value=type(value).__name__), self.assertRaises(ValueError):
                    XAIPlannerProvider(timeout_seconds=value)
        config.assert_not_called()
        factory.assert_not_called()

    def test_invalid_cli_timeout_fails_before_loading_env_or_constructing_provider(self):
        for value in ("0", "-1", "181", "nan", "inf", "not-a-number"):
            with self.subTest(value=value), patch("sys.argv", ["runner", "--mode", "routed", "--timeout-seconds", value]), \
                 patch("scripts.live_xai_compare.load_local_env") as env, \
                 patch("scripts.live_xai_compare.XAIPlannerProvider") as factory, patch("sys.stderr"), self.assertRaises(SystemExit):
                main()
            env.assert_not_called()
            factory.assert_not_called()

    def test_cli_passes_identical_explicit_timeout_to_both_modes(self):
        for mode in ("full", "routed"):
            backend = SimpleNamespace(_client=SimpleNamespace(max_retries=0))
            completed = SimpleNamespace(planner=SimpleNamespace(trace=SimpleNamespace(completion_status="complete")))
            with patch("sys.argv", ["runner", "--mode", mode, "--max-calls", "3", "--timeout-seconds", "120"]), \
                 patch("scripts.live_xai_compare.load_local_env"), \
                 patch.dict(os.environ, {"XAI_API_KEY": "synthetic", "XAI_MODEL": "grok-4.6"}, clear=True), \
                 patch("scripts.live_xai_compare.XAIPlannerProvider", return_value=backend) as factory, \
                 patch("scripts.live_xai_compare.run_case", return_value=(completed, None)) as runner:
                self.assertEqual(main(), 0)
            factory.assert_called_once_with(timeout_seconds=120.0)
            self.assertEqual(runner.call_args.kwargs["max_calls"], 3)
            self.assertEqual(runner.call_args.args[0], mode)

    def test_installed_sdk_timeout_mapping_and_sanitized_failure_with_mock_transport(self):
        # The real SDK runs only against an in-memory transport; no sockets.
        import httpx2
        from openai import OpenAI
        seen = []
        def handler(request):
            seen.append(request.extensions["timeout"])
            raise httpx2.ReadTimeout("Authorization: Bearer synthetic-secret", request=request)
        with httpx2.Client(transport=httpx2.MockTransport(handler)) as http_client:
            with patch.dict(os.environ, {"XAI_API_KEY": "synthetic-secret", "XAI_MODEL": "grok-4.6"}, clear=True), \
                 patch("openai.OpenAI", side_effect=lambda **kwargs: OpenAI(http_client=http_client, **kwargs)):
                provider = XAIPlannerProvider(timeout_seconds=120)
                with self.assertRaises(ProviderFailure) as caught:
                    provider.generate([])
        self.assertEqual(seen, [{"connect": 120, "read": 120, "write": 120, "pool": 120}])
        diagnostic = asdict(caught.exception.diagnostic)
        self.assertEqual(diagnostic["category"], "timeout")
        self.assertEqual(diagnostic["message"], "Request timed out.")
        self.assertNotIn("synthetic-secret", str(diagnostic))
        self.assertNotIn("Authorization", str(diagnostic))

    def test_sdk_cached_usage_is_optional_and_preserved_when_reported(self):
        for cached in (None, 0, 256):
            provider = XAIPlannerProvider.__new__(XAIPlannerProvider)
            provider._model = "grok-4.6"
            usage = SimpleNamespace(prompt_tokens=1000, completion_tokens=20)
            if cached is not None:
                usage.prompt_tokens_details = SimpleNamespace(cached_tokens=cached)
            provider._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(
                return_value=SimpleNamespace(usage=usage, choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))])))))
            self.assertEqual(provider.generate([]).cached_input_tokens, cached)


class AccountingTests(unittest.TestCase):
    def test_later_timeout_preserves_observed_usage_and_payload_contracts(self):
        backend = ScriptedFailure([
            response(call("get_tire_pressure_summary"), 3223, 25),
            response(call("get_tire_pressure_history", wheel="rear_left"), 5409, 31),
        ])
        output = []
        run, meter = run_case("routed", backend, model="fake", max_calls=3, write=output.append)
        trace = run.planner.trace
        self.assertEqual((trace.known_input_tokens, trace.known_output_tokens), (8632, 56))
        self.assertIsNone(trace.known_cached_input_tokens)
        self.assertIsNone(trace.input_tokens)
        self.assertIsNone(trace.output_tokens)
        self.assertEqual(trace.calls_with_unknown_usage, 1)
        self.assertEqual(trace.calls_with_unknown_cached_usage, 3)
        self.assertFalse(trace.usage_complete)
        self.assertEqual(trace.stop_reason, "provider_timeout")
        self.assertEqual(trace.last_model_action, "tool_call")
        self.assertEqual(trace.provider_calls[-1]["input_tokens"], None)
        initial = trace.call_exposures[0]["request_characters"]
        self.assertEqual([x["request_characters"] for x in trace.call_exposures], [initial, initial + 3959, initial + 5769])
        requested = [t for t in trace.tool_executions if t["phase"] == "planner"]
        self.assertEqual([t["payload_characters"] for t in requested], [3886, 1718])
        self.assertEqual([t["evidence_id_count"] for t in requested], [24, 6])
        self.assertEqual(run.planner.result.safety.disposition.value, "STOP_WHEN_SAFE")
        self.assertEqual(backend.calls, 3)
        with self.assertRaises(RequestLimitReached):
            meter.generate([])
        self.assertEqual(backend.calls, 3)
        timeout_line = next(i for i, line in enumerate(output) if line.startswith("EFFECTIVE SDK TIMEOUT"))
        first_call = next(i for i, line in enumerate(output) if line.startswith("CALL 1"))
        self.assertLess(timeout_line, first_call)
        self.assertIn("120.0", output[timeout_line])
        usage = json.loads(next(line.removeprefix("USAGE: ") for line in output if line.startswith("USAGE:")))
        self.assertEqual(usage["known_input_tokens"], 8632)
        self.assertIn("STOP REASON: provider_timeout", output)
        self.assertTrue(any(line.startswith("FAILED PROVIDER LATENCY:") for line in output))

    def test_trace_separates_latency_and_partial_cache_usage(self):
        trace = PlannerTrace()
        for number, (result, latency) in enumerate(((ProviderResponse("{}", 3223, 25, cached_input_tokens=100), 6.437),
                                                  (ProviderResponse("{}", 5409, 31, cached_input_tokens=200), 6.422),
                                                  (None, 45.010)), 1):
            trace.planner_call_count = number
            trace.record_provider_call(result, latency, "timeout" if result is None else None)
        self.assertAlmostEqual(trace.successful_provider_latency_seconds, 12.859)
        self.assertAlmostEqual(trace.failed_provider_latency_seconds, 45.010)
        self.assertEqual(trace.known_cached_input_tokens, 300)
        self.assertIsNone(trace.cached_input_tokens)
        self.assertEqual(trace.calls_with_unknown_cached_usage, 1)
        self.assertFalse(trace.cached_usage_complete)

    def test_known_zero_complete_usage_and_no_reported_usage_are_distinct(self):
        complete = run_full_planner(snapshot(), FakePlannerProvider([
            response(call("get_vehicle_profile"), 0, 0, 0), response(final(), 10, 2, 0)])).trace
        self.assertEqual((complete.input_tokens, complete.known_input_tokens), (10, 10))
        self.assertEqual(complete.known_cached_input_tokens, 0)
        self.assertTrue(complete.usage_complete)
        self.assertTrue(complete.cached_usage_complete)
        unknown = run_full_planner(snapshot(), ScriptedFailure()).trace
        self.assertIsNone(unknown.known_input_tokens)
        self.assertIsNone(unknown.known_output_tokens)
        self.assertIsNone(unknown.known_cached_input_tokens)
        self.assertFalse(unknown.usage_complete)

    def test_successful_response_missing_usage_keeps_partial_subtotals(self):
        trace = run_full_planner(snapshot(), FakePlannerProvider([
            response(call("get_vehicle_profile"), 12, 3), response(final(), None, 4)])).trace
        self.assertEqual(trace.known_input_tokens, 12)
        self.assertEqual(trace.known_output_tokens, 7)
        self.assertIsNone(trace.input_tokens)
        self.assertEqual(trace.output_tokens, 7)
        self.assertEqual(trace.calls_with_unknown_usage, 1)
        self.assertEqual(trace.stop_reason, "assessment_completed")
        self.assertFalse(trace.usage_complete)

    def test_provider_latency_integration_counts_failures_separately(self):
        clock = [0.0]
        class Timed:
            calls = 0
            def generate(self, messages):
                self.calls += 1
                if self.calls == 1:
                    clock[0] += 6.437
                    return response(call("get_vehicle_profile"), 3223, 25)
                clock[0] += 45.010
                return ScriptedFailure().generate(messages)
        with patch("carmind.planner.perf_counter", side_effect=lambda: clock[0]):
            trace = run_full_planner(snapshot(), Timed()).trace
        self.assertAlmostEqual(trace.successful_provider_latency_seconds, 6.437)
        self.assertAlmostEqual(trace.failed_provider_latency_seconds, 45.010)
        self.assertAlmostEqual(trace.elapsed_seconds, 51.447)
        self.assertEqual(sum(t["elapsed_seconds"] for t in trace.tool_executions), 0)

    def test_stop_reasons_preserve_application_incomplete_and_last_action(self):
        cases = (
            (FakePlannerProvider([final()]), {}, "assessment_completed", "final"),
            (FakePlannerProvider([call("get_vehicle_profile")]), {"max_model_calls": 1}, "call_budget_exhausted", "tool_call"),
            (FakePlannerProvider([call("get_vehicle_profile")]), {"max_tool_executions": 0}, "tool_budget_exhausted", "tool_call"),
            (ScriptedFailure(category="connection_network"), {}, "provider_connection_failure", None),
            (FakePlannerProvider(["{", "{"]), {}, "repair_attempt_failed", "invalid_response"),
            (FakePlannerProvider([final(["invented"]), final(["invented"])]), {}, "repair_attempt_failed", "final"),
        )
        for provider, kwargs, reason, action in cases:
            with self.subTest(reason=reason):
                run = run_full_planner(snapshot(), provider, **kwargs)
                self.assertEqual(run.trace.stop_reason, reason)
                self.assertEqual(run.trace.last_model_action, action)
                self.assertEqual(run.trace.completion_status, "complete" if reason == "assessment_completed" else "incomplete")
        repaired = run_full_planner(snapshot(), FakePlannerProvider([final(["invented"]), final()])).trace
        self.assertEqual(repaired.stop_reason, "assessment_completed")
        self.assertEqual(repaired.validation_failure_categories, ["grounding_failure"])

    def test_summary_and_filtered_history_payloads_remain_separate(self):
        provider = FakePlannerProvider([call("get_tire_pressure_summary"), call("get_tire_pressure_history", wheel="rear_left"), final()])
        run_case("routed", provider, model="fake", max_calls=3, write=lambda _: None)
        summary = json.loads(provider.requests[1][-1]["content"])["tool_result"]
        history = json.loads(provider.requests[2][-1]["content"])["tool_result"]
        self.assertEqual(set(summary["data"]), {"summaries"})
        self.assertEqual(len(summary["evidence_ids"]), 24)
        self.assertEqual(len(history["data"]["observations"]), 6)
        self.assertTrue(set(history["evidence_ids"]) <= set(summary["evidence_ids"]))


if __name__ == "__main__":
    unittest.main()
