"""Offline evidence, context-accounting and unchanged policy regressions."""

from dataclasses import asdict, replace
from datetime import timedelta
import json
import unittest
from unittest.mock import patch

from carmind.benchmark import load_cases, make_inputs, run_benchmark
from carmind.contracts import Observation, ObservationSource, VehicleContext
from carmind.planner import run_full_planner, PROTOCOL
from carmind.planner_provider import FakePlannerProvider
from carmind.router_provider import FakeCapabilityRouter
from carmind.routing import run_assessment
from carmind.simulator import generate_episode, freeze_episode, list_scenarios
from carmind.safety import evaluate_safety
from carmind.tools import execute_tool, list_tools, ToolResult, MAINTENANCE_TOOLS
from support import call, final, maintenance_context


def snapshot(scenario="healthy_vehicle"):
    return freeze_episode(generate_episode(scenario, 42)[0])


def tool(identifier, s, context=None, request=None):
    return execute_tool(identifier, {}, s, context, allowed_tool_ids=(identifier,), maintenance_request=request)


class HardeningTests(unittest.TestCase):
    def test_compact_summaries_keep_numeric_provenance_and_raw_siblings(self):
        for identifier, names, histories in (
            ("get_cooling_summary", {"coolant_temperature"}, ["get_coolant_history"]),
            ("get_fuel_usage_summary", {"fuel_consumption", "average_trip_duration", "idle_time_ratio"},
             ["get_fuel_consumption_history", "get_trip_duration_history", "get_idle_time_history"]),
        ):
            with self.subTest(tool=identifier):
                s = snapshot()
                result = tool(identifier, s)
                self.assertEqual(set(result.data), {"summaries"})
                expected = {o.observation_id: o for o in s.observations if o.name in names}
                self.assertEqual(set(result.evidence_ids), set(expected))
                for group in result.data["summaries"]:
                    records = [expected[ref] for ref in group["evidence_ids"]]
                    self.assertEqual(group["count"], len(records))
                    self.assertEqual(group["first_at"], records[0].timestamp.isoformat())
                    self.assertEqual(group["last_at"], records[-1].timestamp.isoformat())
                    self.assertEqual(group["minimum"], min(o.value for o in records))
                    self.assertEqual(group["maximum"], max(o.value for o in records))
                    self.assertEqual(group["change"], records[-1].value - records[0].value)
                raw = [o for history in histories for o in tool(history, s).data["observations"]]
                self.assertEqual({o["observation_id"] for o in raw}, set(expected))

    def test_compact_summaries_filter_future_invalid_values_and_separate_units(self):
        for identifier, name in (("get_cooling_summary", "coolant_temperature"),
                                 ("get_fuel_usage_summary", "fuel_consumption")):
            with self.subTest(tool=identifier):
                s = snapshot()
                context = VehicleContext(s.profile, observations=[
                    Observation("future", name, 999, "alternate", s.assessment_at + timedelta(days=1), ObservationSource.USER),
                    Observation("invalid", name, "unknown", "alternate", s.assessment_at, ObservationSource.USER),
                    Observation("alternate", name, 10, "alternate", s.assessment_at, ObservationSource.USER),
                ])
                result = tool(identifier, s, context)
                self.assertNotIn("future", result.evidence_ids)
                self.assertNotIn("invalid", result.evidence_ids)
                self.assertIn("alternate", result.evidence_ids)
                self.assertEqual(next(g for g in result.data["summaries"] if g["unit"] == "alternate")["count"], 1)

    def test_every_tool_citation_resolves_to_an_exact_record(self):
        # Every catalog tool is exercised successfully, including fictional maintenance.
        s = snapshot()
        context = VehicleContext(s.profile, observations=[
            Observation("electrical", "accessory_voltage", 12, "V", s.assessment_at, ObservationSource.USER)])
        fixtures = [(s, context, None), (snapshot("weak_battery_start"), None, None), maintenance_context()]
        fixtures.extend((i.snapshot, i.context, i.maintenance_request) for i in map(make_inputs, load_cases()))
        for definition in list_tools():
            with self.subTest(tool=definition.tool_id):
                if definition.tool_id == "search_manufacturer_manual":
                    # These legacy fixtures intentionally have no registered manual.
                    continue
                for s, context, request in fixtures:
                    result = tool(definition.tool_id, s, context, request)
                    if result.success and result.evidence_ids:
                        break
                else:
                    self.fail("No successful fixture for " + definition.tool_id)
                sources = set()
                if request and definition.tool_id in MAINTENANCE_TOOLS:
                    sources = {source.source_id for source in request.pack.sources}
                provider = FakePlannerProvider([call(definition.tool_id), final(result.evidence_ids, sources=sorted(sources))])
                run = run_full_planner(s, provider, context, request)
                self.assertEqual(run.trace.completion_status, "complete", run.trace.validation_failures)
                facts = run.result.claims[0]["facts"]
                for ref, fact in facts.items():
                    identity = next((fact[key] for key in ("observation_id", "record_id", "reminder_id", "rule_id", "evidence_id", "vehicle_id", "source_id") if key in fact), None)
                    self.assertEqual(ref, identity)
                    self.assertNotEqual(fact, result.data)
                expected = {o.observation_id: {**asdict(o), "timestamp": o.timestamp.isoformat(), "source": o.source.value}
                            for o in (*s.observations, *(context.observations if context else ())) if o.timestamp <= s.assessment_at}
                for ref in facts.keys() & expected.keys():
                    self.assertEqual(facts[ref], expected[ref])

    def test_unresolved_tool_id_is_rejected_not_bound_to_entire_result(self):
        original = execute_tool
        def broken(identifier, *args, **kwargs):
            if identifier == "get_cooling_summary":
                return ToolResult(identifier, True, ("unresolved",), {"summaries": []})
            return original(identifier, *args, **kwargs)
        provider = FakePlannerProvider([call("get_cooling_summary"), final(["unresolved"])])
        with patch("carmind.planner.execute_tool", side_effect=broken):
            run = run_full_planner(snapshot(), provider)
        self.assertEqual(run.trace.completion_status, "incomplete")
        self.assertIn("Unresolved tool evidence identity", run.trace.validation_failures)

    def test_cross_domain_identity_collisions_fail_before_provider(self):
        s = snapshot()
        for identifier in (s.profile.vehicle_id, s.owner_message.message_id):
            context = VehicleContext(s.profile, observations=[replace(s.observations[0], observation_id=identifier)])
            provider = FakePlannerProvider([final()])
            run = run_full_planner(s, provider, context)
            self.assertEqual(run.trace.completion_status, "incomplete")
            self.assertEqual(provider.requests, [])
        provider = FakePlannerProvider([final()])
        run = run_full_planner(replace(s, owner_message=replace(s.owner_message, message_id=s.profile.vehicle_id)), provider)
        self.assertEqual(provider.requests, [])

    def test_evidence_index_is_complete_channel_availability_without_values(self):
        s = snapshot()
        context = VehicleContext(s.profile, observations=[replace(s.observations[0], observation_id="future", timestamp=s.assessment_at + timedelta(days=1))])
        provider = FakePlannerProvider([final()])
        run_full_planner(s, provider, context)
        index = json.loads(provider.requests[0][1]["content"])["evidence_index"]
        self.assertEqual(sum(g["count"] for g in index), len(s.observations))
        self.assertEqual({(g["name"], g["unit"], g["source"]) for g in index},
                         {(o.name, o.unit, o.source.value) for o in s.observations})
        for group in index:
            self.assertEqual(set(group), {"name", "unit", "source", "count", "first_at", "last_at"})
        text = json.dumps(index)
        for forbidden in ("future", "ScenarioTruth", "healthy_vehicle"):
            self.assertNotIn(forbidden, text)

    def test_context_accounting_matches_actual_requests(self):
        provider = FakePlannerProvider([call("get_tire_pressure_history"), call("get_tire_pressure_summary"), final()])
        run = run_full_planner(snapshot("gradual_tire_pressure_loss"), provider)
        for number, (request, metrics) in enumerate(zip(provider.requests, run.trace.call_exposures), 1):
            self.assertEqual(metrics["call_number"], number)
            self.assertEqual(metrics["request_characters"], sum(len(m["content"]) for m in request))
            self.assertEqual(metrics["request_characters"], metrics["protocol_characters"] + metrics["instruction_characters"] + metrics["initial_user_context_characters"] + metrics["prior_history_characters"] + metrics["accumulated_tool_result_characters"])
            payloads = [m["content"] for m in request[2:] if m["role"] == "user" and "tool_result" in json.loads(m["content"])]
            self.assertEqual(metrics["accumulated_tool_result_characters"], sum(map(len, payloads)))
            self.assertEqual(metrics["latest_tool_result_characters"], len(payloads[-1]) if payloads else 0)
        self.assertEqual(run.trace.cumulative_request_characters, sum(m["request_characters"] for m in run.trace.call_exposures))
        self.assertGreater(run.trace.call_exposures[1]["evidence_id_count"], run.trace.call_exposures[0]["evidence_id_count"])

    def test_tool_metrics_include_preparation_success_and_rejection_without_payloads(self):
        secret = "Authorization: Bearer synthetic-do-not-log"
        provider = FakePlannerProvider([call(secret), call("get_tire_pressure_summary")])
        s = snapshot("gradual_tire_pressure_loss")
        run = run_full_planner(replace(s, owner_message=replace(s.owner_message, text=secret)), provider, max_model_calls=2)
        self.assertEqual([t["phase"] for t in run.trace.tool_executions], ["preparation", "planner", "planner"])
        self.assertEqual(run.trace.tool_executions[1]["error"], "unknown_tool")
        self.assertEqual(run.trace.tool_executions[2]["evidence_id_count"], 24)
        text = json.dumps(asdict(run.trace))
        self.assertNotIn(secret, text)
        self.assertNotIn("summaries", text)
        for t in run.trace.tool_executions:
            self.assertGreater(t["payload_characters"], 0)
            self.assertGreaterEqual(t["elapsed_seconds"], 0)

    def test_safety_scenarios_and_missing_channels_remain_unchanged(self):
        expected = ["NO_RULE_TRIGGERED", "STOP_WHEN_SAFE", "UNDETERMINED", "STOP_WHEN_SAFE", "UNDETERMINED"]
        for scenario, disposition in zip(list_scenarios(), expected):
            s = snapshot(scenario)
            safety = evaluate_safety(s)
            self.assertEqual(safety.disposition.value, disposition)
            run = run_assessment(s, FakePlannerProvider([final()]), mode="ROUTED", router=FakeCapabilityRouter(selected=["tires"]))
            self.assertEqual(run.planner.result.safety, safety)
            self.assertTrue(set(safety.limitations) <= set(run.planner.result.assessment.limitations))
        limitations = evaluate_safety(snapshot("gradual_tire_pressure_loss")).limitations
        self.assertIn("Missing critical evidence: coolant_temperature", limitations)
        self.assertIn("Missing critical evidence: battery_voltage", limitations)

    def test_expansion_rejects_unknown_redundant_and_multiple_without_full_fallback(self):
        for ids, reason in ((["unknown"], "unknown_expansion_capability"), (["tires"], "no_additional_coverage"),
                            (["engine", "battery"], "expansion_requires_one_capability")):
            provider = FakePlannerProvider([{"type": "expand_capabilities", "capability_ids": ids}])
            run = run_assessment(snapshot(), provider, mode="ROUTED", router=FakeCapabilityRouter(selected=["tires"]))
            self.assertEqual(run.routing.expansion_failure_reason, reason)
            self.assertFalse(run.routing.fallback_used)
            self.assertEqual(run.routing.effective_loaded_capabilities, ("tires",))
            self.assertEqual(run.planner.trace.completion_status, "incomplete")

    def test_benchmark_reports_measured_payloads_and_separate_failures(self):
        report = run_benchmark()
        for mode in ("FULL", "ROUTED"):
            group = [r for r in report["rows"] if r["mode"] == mode]
            self.assertEqual(report["summary"][mode]["tool_result_characters"], sum(r["tool_result_characters"] for r in group) / len(group))
            self.assertEqual(report["summary"][mode]["validation_failures"], 0)
            self.assertEqual(report["summary"][mode]["grounding_failures"], 0)
            self.assertIsNone(report["summary"][mode]["total_tokens"])


if __name__ == "__main__":
    unittest.main()
