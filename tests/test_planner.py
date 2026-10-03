from dataclasses import asdict, replace
from datetime import timedelta
import json
import os
import unittest
from unittest.mock import patch

from carmind.capabilities import load_all_capabilities
from carmind.contracts import VehicleContext, MaintenanceRecord
from carmind.planner import run_full_planner, strict_json
from carmind.planner_provider import FakePlannerProvider, ProviderResponse, XAIPlannerProvider
from carmind.safety import evaluate_safety
from carmind.simulator import generate_episode
from carmind.tools import TOOL_CATALOG, MAINTENANCE_TOOLS, execute_tool
from support import final, call, maintenance_context, NOW
from test_safety import snapshot


class PlannerTests(unittest.TestCase):
    def run_fake(self, turns, s=None, **kwargs):
        provider = FakePlannerProvider(turns)
        return run_full_planner(s or snapshot(), provider, **kwargs), provider

    def test_full_exposure_owner_profile_and_compact_index(self):
        s = snapshot()
        result, provider = self.run_fake([final([s.profile.vehicle_id])], s)
        initial = json.loads(provider.requests[0][1]["content"])
        self.assertEqual(initial["owner_message"]["text"], s.owner_message.text)
        self.assertEqual(initial["vehicle_profile"]["vehicle_id"], s.profile.vehicle_id)
        self.assertEqual({t["tool_id"] for t in initial["tools"]},
                         set(TOOL_CATALOG) - {"search_manufacturer_manual"})
        self.assertTrue(all("value" not in i for i in initial["evidence_index"]))
        self.assertEqual(result.trace.exposed_capability_count, 10)
        self.assertEqual(result.trace.exposed_tool_count, 25)
        self.assertEqual(result.trace.planner_instruction_character_count, load_all_capabilities().instruction_character_count)

    def test_tool_round_trip_and_grounded_final(self):
        s = snapshot("gradual_tire_pressure_loss")
        ids = [o.observation_id for o in s.observations if o.name == "rear_left_tire_pressure"]
        result, provider = self.run_fake([call("get_tire_pressure_history", wheel="rear_left"), final(ids, "POSSIBLE_TIRE_LEAK", actions=["ARRANGE_SERVICE_REVIEW"])], s)
        self.assertEqual(result.trace.completion_status, "complete")
        self.assertEqual(result.trace.planner_call_count, 2)
        self.assertEqual(result.trace.tool_execution_count, 1)
        self.assertEqual(result.trace.tool_ids_called, ["get_tire_pressure_history"])
        self.assertIn("tool_result", provider.requests[1][-1]["content"])
        self.assertEqual(result.result.safety, evaluate_safety(s))
        self.assertIsNone(result.trace.input_tokens)
        self.assertIsNone(result.trace.output_tokens)

    def test_strict_json_rejects_nonobjects_duplicates_nonfinite_and_code(self):
        for value in ('[]', '{} {}', '{"type":1,"type":2}', '{"x":NaN}', '```json\n{}\n```', '__import__("os")'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                strict_json(value)

    def test_bad_output_gets_one_repair(self):
        result, provider = self.run_fake(['{', final()])
        self.assertEqual(result.trace.completion_status, "complete")
        self.assertEqual(len(result.trace.validation_failures), 1)
        self.assertIn("validation_error", provider.requests[1][-1]["content"])

    def test_two_validation_failures_stop(self):
        result, _ = self.run_fake(['{', '{', final()])
        self.assertEqual(result.trace.completion_status, "incomplete")
        self.assertEqual(result.trace.planner_call_count, 2)

    def test_unknown_tool_rejected(self):
        result, _ = self.run_fake([call("execute_code"), final()])
        self.assertIn("unknown_tool", result.trace.validation_failures[0])

    def test_invalid_arguments_rejected(self):
        result, _ = self.run_fake([call("get_vehicle_profile", code="print(1)"), final()])
        self.assertIn("invalid_arguments", result.trace.validation_failures[0])

    def test_invented_evidence_action_source_rejected(self):
        for bad in (final(["invented"]), final(actions=["invented"]), final(sources=["invented"])):
            result, _ = self.run_fake([bad, final()])
            self.assertEqual(len(result.trace.validation_failures), 1)

    def test_index_only_fact_requires_tool_exposure(self):
        s = snapshot()
        result, _ = self.run_fake([final([s.observations[0].observation_id]), final()], s)
        self.assertTrue(result.trace.validation_failures)

    def test_budget_enforced(self):
        result, _ = self.run_fake([call("get_vehicle_profile")] * 8)
        self.assertEqual(result.trace.planner_call_count, 4)
        self.assertEqual(result.trace.tool_execution_count, 4)
        self.assertEqual(result.trace.completion_status, "incomplete")

    def test_tool_budget_enforced_before_execution(self):
        result, _ = self.run_fake([call("get_vehicle_profile")] * 4, max_tool_executions=1)
        self.assertEqual(result.trace.tool_execution_count, 1)
        self.assertEqual(result.trace.planner_call_count, 2)
        self.assertEqual(result.trace.completion_status, "incomplete")

    def test_budget_limits_cannot_be_raised(self):
        for kwargs in ({"max_model_calls": 5}, {"max_tool_executions": 7}, {"max_model_calls": True}):
            with self.assertRaises(ValueError):
                self.run_fake([final()], **kwargs)

    def test_safety_override_rejected(self):
        s = snapshot("sustained_temperature_rise")
        bad = final()
        bad["assessment"]["safety_disposition"] = "NO_RULE_TRIGGERED"
        result, _ = self.run_fake([bad, final()], s)
        self.assertTrue(result.trace.validation_failures)
        self.assertEqual(result.result.assessment.safety_disposition.value, "STOP_WHEN_SAFE")

    def test_no_truth_or_future_evidence(self):
        s = snapshot()
        context = VehicleContext(s.profile, observations=[replace(s.observations[0], observation_id="FUTURE_ID", timestamp=s.assessment_at+timedelta(days=1))],
                                 maintenance_records=[MaintenanceRecord("FUTURE_SERVICE", "oil", s.assessment_at+timedelta(days=1), 30000)])
        result, provider = self.run_fake([call("get_coolant_history"), final()], s, context=context)
        text = json.dumps(provider.requests)
        for forbidden in ("FUTURE_ID", "FUTURE_SERVICE", "ScenarioTruth", "healthy_vehicle", "scenario_id"):
            self.assertNotIn(forbidden, text)
        for value in generate_episode("healthy_vehicle", 42):
            with self.assertRaises(ValueError):
                run_full_planner(value, FakePlannerProvider([]))

    def test_service_history_and_maintenance_present(self):
        s, context, request = maintenance_context()
        result, provider = self.run_fake([call("get_upcoming_maintenance_items"), final(["fixture-oil"], sources=["fixture-source"])], s, context=context, maintenance_request=request)
        initial = json.loads(provider.requests[0][1]["content"])
        self.assertEqual(initial["service_history"]["records"][0]["record_id"], "record-1")
        self.assertEqual(initial["maintenance_state"][0]["remaining_km"], 800)
        self.assertEqual(result.trace.completion_status, "complete")
        self.assertEqual(result.trace.validation_failures, [])

    def test_all_maintenance_tools_and_provenance(self):
        s, context, request = maintenance_context()
        for tool in MAINTENANCE_TOOLS:
            result = execute_tool(tool, {}, s, context, allowed_tool_ids=(tool,), maintenance_request=request)
            self.assertTrue(result.success, result.error)
            json.dumps(asdict(result))
        result = execute_tool("get_manufacturer_maintenance_rule", {"rule_id": "missing"}, s, context, allowed_tool_ids=("get_manufacturer_maintenance_rule",), maintenance_request=request)
        self.assertEqual(result.error, "unknown_manufacturer_reference")

    def test_maintenance_reminder_can_be_selected_without_recalculation(self):
        s, context, request = maintenance_context()
        from carmind.tools import maintenance_results
        reminder = maintenance_results(request, s, context)[0]
        result, _ = self.run_fake([call("get_upcoming_maintenance_items"), final([reminder.reminder_id], sources=["fixture-source"])], s, context=context, maintenance_request=request)
        self.assertEqual(result.trace.completion_status, "complete")
        self.assertEqual(result.trace.validation_failures, [])
        facts = result.result.claims[0]["facts"][reminder.reminder_id]
        self.assertEqual(facts["due_odometer_km"], 30000)
        self.assertEqual(facts["status"], "UPCOMING")

    def test_colliding_service_and_profile_ids_fail_closed(self):
        s = snapshot()
        context = VehicleContext(s.profile, maintenance_records=[MaintenanceRecord(s.profile.vehicle_id, "oil", s.assessment_at, 20000)])
        result, provider = self.run_fake([final()], s, context=context)
        self.assertEqual(result.trace.completion_status, "incomplete")
        self.assertEqual(provider.requests, [])

    def test_model_cannot_mutate_application_state(self):
        s = snapshot("gradual_tire_pressure_loss")
        class Mutating:
            def generate(self, messages):
                messages.clear()
                return ProviderResponse(json.dumps(final()))
        result = run_full_planner(s, Mutating())
        self.assertEqual(result.result.safety, evaluate_safety(s))

    def test_core_runs_with_network_forbidden(self):
        with patch('socket.socket', side_effect=AssertionError("Network prohibited")):
            result, _ = self.run_fake([call("get_vehicle_profile"), final()])
        self.assertEqual(result.trace.completion_status, "complete")

    def test_sdk_missing_dependency_has_clear_error(self):
        import builtins
        original = builtins.__import__
        def no_sdk(name, *args, **kwargs):
            if name == 'openai':
                raise ModuleNotFoundError('missing SDK runtime')
            return original(name, *args, **kwargs)
        with patch.dict(os.environ, {"XAI_API_KEY": "unit-test-placeholder", "XAI_MODEL": "configured-model"}, clear=True), patch('builtins.__import__', side_effect=no_sdk):
            with self.assertRaisesRegex(RuntimeError, "runtime dependencies"):
                XAIPlannerProvider()

    def test_wrong_vehicle_knowledge_request_rejected(self):
        s, context, request = maintenance_context()
        result = execute_tool("get_due_maintenance_items", {}, s, context, allowed_tool_ids=("get_due_maintenance_items",), maintenance_request=replace(request, profile=replace(request.profile, model_year=2025)))
        self.assertEqual(result.error, "invalid_evidence")

    def test_provider_failure_returns_incomplete_without_error_leak(self):
        class Broken:
            def generate(self, messages):
                raise RuntimeError("SECRET_SHOULD_NOT_APPEAR")
        result = run_full_planner(snapshot(), Broken())
        self.assertEqual(result.trace.completion_status, "incomplete")
        self.assertNotIn("SECRET_SHOULD_NOT_APPEAR", str(result))

    def test_tokens_aggregated_and_missing_is_none(self):
        result, _ = self.run_fake([ProviderResponse(json.dumps(call("get_vehicle_profile")), 10, 2), ProviderResponse(json.dumps(final()), 11, 3)])
        self.assertEqual((result.trace.input_tokens, result.trace.output_tokens), (21, 5))
        result, _ = self.run_fake([ProviderResponse(json.dumps(call("get_vehicle_profile")), 10, 2), final()])
        self.assertIsNone(result.trace.input_tokens)
        result, _ = self.run_fake([ProviderResponse(json.dumps(final()), 0, 0)])
        self.assertEqual(result.trace.input_tokens, 0)

    def test_live_configuration_required_without_import_or_network(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(RuntimeError, "XAI_API_KEY, XAI_MODEL"):
            XAIPlannerProvider()

    def test_live_adapter_uses_only_configured_model_and_key(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        client = MagicMock()
        client.chat.completions.create.return_value = SimpleNamespace(usage=None, choices=[SimpleNamespace(message=SimpleNamespace(content='{}'))])
        factory = MagicMock(return_value=client)
        with patch.dict(os.environ, {"XAI_API_KEY": "unit-test-placeholder", "XAI_MODEL": "configured-model"}, clear=True), patch.dict('sys.modules', {"openai": SimpleNamespace(OpenAI=factory)}):
            response = XAIPlannerProvider().generate([])
        self.assertEqual(factory.call_args.kwargs["base_url"], "https://api.x.ai/v1")
        self.assertEqual(factory.call_args.kwargs["max_retries"], 0)
        self.assertEqual(client.chat.completions.create.call_args.kwargs["model"], "configured-model")
        self.assertIsNone(response.input_tokens)
