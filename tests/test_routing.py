import json
import os
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from carmind.capabilities import CapabilityRegistry, load_all_capabilities
from carmind.contracts import SafetyDisposition
from carmind.evidence import FrozenEvidenceSnapshot
from carmind.planner import run_planner
from carmind.planner_provider import FakePlannerProvider
from carmind.ownership_demo import empty_final
from carmind.router_provider import (
    FakeCapabilityRouter,
    JevCapabilityRouter,
    RouterFailure,
    RouterResponse,
)
from carmind.routing import (
    ExecutionMode,
    RoutingPolicy,
    RoutingTrace,
    build_routing_state,
    run_assessment,
    select_capabilities,
)
from carmind.safety import SafetyDecision, evaluate_safety
from carmind.simulator import freeze_episode, generate_episode
from tests.support import call, final, maintenance_context


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.episode, self.truth = generate_episode("gradual_tire_pressure_loss", 42)
        self.snapshot = freeze_episode(self.episode)
        self.registry = CapabilityRegistry()

    def _valid_provider(self, snapshot=None):
        snapshot = snapshot or self.snapshot
        ids = [o.observation_id for o in snapshot.observations if o.name == "rear_left_tire_pressure"]
        return FakePlannerProvider([call("get_tire_pressure_summary"), final(ids, "POSSIBLE_TIRE_LEAK", actions=("ARRANGE_SERVICE_REVIEW",))])

    def test_fake_router_supports_multi_label_and_deterministic_order(self):
        router = FakeCapabilityRouter(selected=["tires", "battery"])
        state = build_routing_state(self.snapshot)
        loaded = select_capabilities(router, state, self.registry, RoutingPolicy(), RoutingTrace("ROUTED"))
        self.assertEqual([p.id for p in loaded.packs], ["battery", "tires"])
        self.assertEqual(router.requests[0][1], self.registry.routing_descriptions())

    def test_routing_state_is_compact_and_public_only(self):
        state = build_routing_state(self.snapshot)
        serialized = json.dumps(state, sort_keys=True)
        self.assertIn(self.snapshot.owner_message.text, serialized)
        self.assertIn(self.snapshot.profile.model, serialized)
        self.assertNotIn(self.truth.scenario_id, serialized)
        self.assertNotIn("observation:015", serialized)
        descriptions = self.registry.routing_descriptions()
        self.assertTrue(descriptions)
        self.assertFalse(any("planner_instructions" in item for item in descriptions.values()))
        self.assertFalse(any("tool_ids" in item for item in descriptions.values()))

    def test_follow_up_routing_gets_recent_assessment_active_vehicle_and_stop(self):
        stop = SafetyDecision(SafetyDisposition.STOP_WHEN_SAFE, ("SIM_TIRE_LOSS",), (),
                              "prior", "Stop when safe.", ("Tire evidence remains unresolved.",))
        ownership = {
            "vehicle": {"vehicle_id": "car-1", "nickname": "Daily car"},
            "recent_turns": ({"text": "One tire keeps losing pressure.", "status": "complete"},),
            "previous_assessment": {
                "observations": ["Rear-left tire pressure declined across readings."],
                "unconfirmed_hypotheses": ["POSSIBLE_TIRE_LEAK"],
                "uncertainties": ["CAUSE_UNCONFIRMED"],
            },
        }
        follow_up = type(self.snapshot)(self.snapshot.episode_id, self.snapshot.profile,
                                        type(self.snapshot.owner_message)("follow-up", "Can I keep driving?",
                                                                         self.snapshot.assessment_at),
                                        self.snapshot.assessment_at)
        state = build_routing_state(follow_up, ownership_context=ownership, previous_stop=stop)
        router = FakeCapabilityRouter(selected=["tires", "trip_readiness"])
        trace = RoutingTrace("ROUTED")
        loaded = select_capabilities(router, state, self.registry, RoutingPolicy(), trace)
        sent, descriptions = router.requests[0]
        self.assertEqual(sent["owner_message"], "Can I keep driving?")
        self.assertEqual(sent["follow_up_context"]["active_vehicle"]["vehicle_id"], "car-1")
        self.assertIn("One tire keeps losing pressure.", sent["follow_up_context"]["recent_owner_messages"])
        self.assertIn("Rear-left tire pressure declined", sent["follow_up_context"]["prior_validated_assessment"]["observations"][0])
        self.assertEqual(sent["follow_up_context"]["unresolved_safety"]["disposition"], "STOP_WHEN_SAFE")
        self.assertFalse(trace.fallback_used)
        self.assertEqual(set(trace.raw_selected_capabilities), {"tires", "trip_readiness"})
        self.assertEqual({pack.id for pack in loaded.packs}, {"tires", "trip_readiness"})
        self.assertNotIn("SIM_TIRE_LOSS", json.dumps(sent))
        self.assertTrue(descriptions)

    def test_provider_call_budget_stops_before_phantom_attempt_and_preserves_stop(self):
        prior_stop = evaluate_safety(self.snapshot)
        tool_then_tool_then_final = FakePlannerProvider([
            call("get_tire_pressure_summary", wheel="rear_left"),
            call("get_tire_pressure_history", wheel="rear_left"),
            empty_final(),
        ])
        run = run_assessment(self.snapshot, tool_then_tool_then_final, mode=ExecutionMode.ROUTED,
                             router=FakeCapabilityRouter(selected=["tires"]), previous_stop=prior_stop,
                             max_model_calls=2)
        trace = run.planner.trace
        self.assertEqual(len(tool_then_tool_then_final.requests), 2)
        self.assertEqual(trace.planner_call_count, 2)
        self.assertEqual(len(trace.provider_calls), 2)
        self.assertTrue(all(item["success"] for item in trace.provider_calls))
        self.assertIsNone(trace.provider_error)
        self.assertEqual(trace.completion_status, "incomplete")
        self.assertEqual(trace.stop_reason, "call_budget_exhausted")
        self.assertEqual(run.planner.result.safety.disposition, SafetyDisposition.STOP_WHEN_SAFE)

        tool_then_final = FakePlannerProvider([
            call("get_tire_pressure_summary", wheel="rear_left"), empty_final(),
        ])
        completed = run_assessment(self.snapshot, tool_then_final, mode=ExecutionMode.ROUTED,
                                   router=FakeCapabilityRouter(selected=["tires"]),
                                   previous_stop=prior_stop, max_model_calls=2)
        self.assertEqual(len(tool_then_final.requests), 2)
        self.assertEqual(completed.planner.trace.planner_call_count, 2)
        self.assertEqual(completed.planner.trace.completion_status, "complete")
        self.assertEqual(completed.planner.result.safety.disposition, SafetyDisposition.STOP_WHEN_SAFE)

        no_calls = FakePlannerProvider([empty_final()])
        blocked = run_assessment(self.snapshot, no_calls, mode=ExecutionMode.ROUTED,
                                 router=FakeCapabilityRouter(selected=["tires"]),
                                 previous_stop=prior_stop, max_model_calls=0)
        self.assertEqual(no_calls.requests, [])
        self.assertEqual(blocked.planner.trace.stop_reason, "call_budget_exhausted")
        self.assertEqual(blocked.planner.trace.planner_call_count, 0)
        self.assertEqual(blocked.planner.result.safety.disposition, SafetyDisposition.STOP_WHEN_SAFE)

    def test_unknown_or_empty_router_output_falls_back_with_reason(self):
        scores = {key: 0.05 for key in self.registry.routing_descriptions()}
        scores["unknown"] = 0.95
        unknown = FakeCapabilityRouter(RouterResponse(scores))
        trace = RoutingTrace("ROUTED")
        loaded = select_capabilities(unknown, build_routing_state(self.snapshot), self.registry, RoutingPolicy(), trace)
        self.assertEqual(loaded.capability_count, 10)
        self.assertTrue(trace.fallback_used)
        self.assertEqual(trace.fallback_reason, "unknown_capability")

        empty = FakeCapabilityRouter(RouterResponse({key: 0.05 for key in self.registry.routing_descriptions()}))
        trace = RoutingTrace("ROUTED")
        select_capabilities(empty, build_routing_state(self.snapshot), self.registry, RoutingPolicy(), trace)
        self.assertEqual(trace.fallback_reason, "empty_selection")

    def test_routing_policy_boundaries_are_explicit(self):
        scores = {key: 0.05 for key in self.registry.routing_descriptions()}
        scores.update({"tires": 0.70, "battery": 0.30, "cooling": 0.85})
        trace = RoutingTrace("ROUTED")
        loaded = select_capabilities(
            FakeCapabilityRouter(RouterResponse(scores)), build_routing_state(self.snapshot),
            self.registry, RoutingPolicy(), trace,
        )
        self.assertFalse(trace.fallback_used)
        self.assertEqual([pack.id for pack in loaded.packs], ["cooling", "tires"])

        scores["engine"] = 0.69
        trace = RoutingTrace("ROUTED")
        select_capabilities(
            FakeCapabilityRouter(RouterResponse(scores)), build_routing_state(self.snapshot),
            self.registry, RoutingPolicy(), trace,
        )
        self.assertFalse(trace.fallback_used)
        self.assertEqual(trace.raw_selected_capabilities, ("cooling", "tires"))

        ambiguous = {key: 0.5 for key in self.registry.routing_descriptions()}
        trace = RoutingTrace("ROUTED")
        select_capabilities(
            FakeCapabilityRouter(RouterResponse(ambiguous)), build_routing_state(self.snapshot),
            self.registry, RoutingPolicy(), trace,
        )
        self.assertEqual(trace.fallback_reason, "borderline_relevance")

        broad = {key: 0.80 for key in self.registry.routing_descriptions()}
        trace = RoutingTrace("ROUTED")
        select_capabilities(
            FakeCapabilityRouter(RouterResponse(broad)), build_routing_state(self.snapshot),
            self.registry, RoutingPolicy(), trace,
        )
        self.assertEqual(trace.fallback_reason, "insufficient_coverage")

    def test_strong_selection_with_secondary_borderline_signal_is_kept(self):
        keys = tuple(self.registry.routing_descriptions())
        scores = {key: 0.05 for key in keys}
        scores[keys[0]] = 0.97
        scores[keys[1]] = 0.44
        trace = RoutingTrace("ROUTED")
        loaded = select_capabilities(
            FakeCapabilityRouter(RouterResponse(scores)), build_routing_state(self.snapshot),
            self.registry, RoutingPolicy(), trace,
        )
        self.assertEqual(trace.raw_selected_capabilities, (keys[0],))
        self.assertFalse(trace.fallback_used)
        self.assertEqual(trace.effective_loaded_capabilities, (keys[0],))
        self.assertEqual(tuple(pack.id for pack in loaded.packs), (keys[0],))

    def test_multiple_strong_capabilities_are_loaded_together(self):
        keys = tuple(self.registry.routing_descriptions())
        scores = {key: 0.05 for key in keys}
        scores[keys[0]] = 0.94
        scores[keys[1]] = 0.91
        scores[keys[2]] = 0.48
        trace = RoutingTrace("ROUTED")
        loaded = select_capabilities(
            FakeCapabilityRouter(RouterResponse(scores)), build_routing_state(self.snapshot),
            self.registry, RoutingPolicy(), trace,
        )
        self.assertFalse(trace.fallback_used)
        self.assertEqual(trace.raw_selected_capabilities, keys[:2])
        self.assertEqual(tuple(pack.id for pack in loaded.packs), keys[:2])

    def test_recorded_jev_scores_replay_without_full_fallback(self):
        # Fixed observation from the one live request; this test makes no API call.
        scores = {
            "battery": 0.03, "cooling": 0.04, "diagnostic_codes": 0.17,
            "electrical": 0.04, "engine": 0.04, "fuel_economy": 0.05,
            "maintenance": 0.24, "service_history": 0.13,
            "tires": 0.97, "trip_readiness": 0.44,
        }
        trace = RoutingTrace("ROUTED")
        select_capabilities(
            FakeCapabilityRouter(RouterResponse(scores)), build_routing_state(self.snapshot),
            self.registry, RoutingPolicy(), trace,
        )
        self.assertEqual(trace.raw_selected_capabilities, ("tires",))
        self.assertEqual(trace.effective_loaded_capabilities, ("tires",))
        self.assertFalse(trace.fallback_used)

    def test_router_exceptions_and_timeout_are_explicit_fallbacks(self):
        for error, reason in ((RouterFailure("api_failure"), "api_failure"), (TimeoutError(), "timeout")):
            trace = RoutingTrace("ROUTED")
            router = FakeCapabilityRouter(error)
            loaded = select_capabilities(router, build_routing_state(self.snapshot), self.registry, RoutingPolicy(), trace)
            self.assertEqual(loaded.capability_count, 10)
            self.assertTrue(trace.fallback_used)
            self.assertEqual(trace.fallback_reason, reason)

    def test_routed_mode_uses_same_planner_and_reduces_narrow_exposure(self):
        full_provider = self._valid_provider()
        full = run_assessment(self.snapshot, full_provider, mode=ExecutionMode.FULL)
        routed_provider = self._valid_provider()
        routed = run_assessment(
            self.snapshot, routed_provider, mode=ExecutionMode.ROUTED,
            router=FakeCapabilityRouter(selected=["tires"]),
        )
        self.assertEqual(full.planner.result.assessment.safety_disposition, routed.planner.result.assessment.safety_disposition)
        self.assertEqual(full.planner.result.safety, routed.planner.result.safety)
        self.assertEqual(full.planner.trace.tool_ids_called, routed.planner.trace.tool_ids_called)
        self.assertEqual(full.planner.trace.completion_status, routed.planner.trace.completion_status)
        self.assertEqual(full.planner.trace.exposed_capability_count, 10)
        self.assertEqual(routed.planner.trace.exposed_capability_count, 1)
        self.assertLess(routed.planner.trace.exposed_tool_count, full.planner.trace.exposed_tool_count)

    def test_compact_summary_remains_validated_and_safety_is_unchanged(self):
        ids = [item.observation_id for item in self.snapshot.observations if item.name == "rear_left_tire_pressure"]
        provider = FakePlannerProvider([call("get_tire_pressure_summary"),
                                        final(ids, "POSSIBLE_TIRE_LEAK", actions=("ARRANGE_SERVICE_REVIEW",))])
        result = run_assessment(self.snapshot, provider, mode=ExecutionMode.ROUTED,
                                router=FakeCapabilityRouter(selected=["tires"]))
        self.assertEqual(result.planner.trace.completion_status, "complete")
        self.assertEqual(result.planner.trace.validation_failures, [])
        self.assertEqual(result.planner.result.assessment.evidence_ids, ids)
        self.assertEqual(result.planner.result.safety, evaluate_safety(self.snapshot))
        self.assertEqual(result.planner.result.assessment.safety_disposition, SafetyDisposition.STOP_WHEN_SAFE)
        facts = result.planner.result.claims[0]["facts"]
        self.assertTrue(all(facts[identifier]["observation_id"] == identifier for identifier in ids))
        self.assertTrue(all(facts[identifier]["name"] == "rear_left_tire_pressure" for identifier in ids))
        self.assertNotIn("observations", json.loads(provider.requests[1][-1]["content"])["tool_result"]["data"])
        from carmind.benchmark import CaseInputs, quality_metrics
        quality = quality_metrics(result, CaseInputs(self.snapshot), {"evidence_names": ["rear_left_tire_pressure"]})
        self.assertTrue(quality["grounding_valid"])

    def test_routed_mode_rejects_tool_outside_allowlist(self):
        provider = FakePlannerProvider([call("get_cooling_summary")])
        result = run_assessment(
            self.snapshot, provider, mode=ExecutionMode.ROUTED,
            router=FakeCapabilityRouter(selected=["tires"]),
        )
        self.assertTrue(result.planner.trace.validation_failures)
        self.assertIn("Tool rejected", result.planner.trace.validation_failures[0])

    def test_targeted_expansion_is_recorded_and_adds_one_pack(self):
        ids = [o.observation_id for o in self.snapshot.observations if o.name == "rear_left_tire_pressure"]
        provider = FakePlannerProvider([
            {"type": "expand_capabilities", "capability_ids": ["tires"]},
            call("get_tire_pressure_summary"),
            final(ids, "POSSIBLE_TIRE_LEAK", actions=("ARRANGE_SERVICE_REVIEW",)),
        ])
        result = run_assessment(
            self.snapshot, provider, mode=ExecutionMode.ROUTED,
            router=FakeCapabilityRouter(selected=["fuel_economy"]),
        )
        self.assertTrue(result.routing.expansion_used)
        self.assertEqual(result.routing.expanded_capabilities, ("tires",))
        self.assertEqual(result.routing.expansion_requested, [("tires",)])
        self.assertEqual(result.planner.trace.completion_status, "complete")
        self.assertEqual(result.planner.trace.exposed_capability_count, 2)

    def test_second_expansion_returns_incomplete(self):
        provider = FakePlannerProvider([
            {"type": "expand_capabilities", "capability_ids": ["tires"]},
            {"type": "expand_capabilities", "capability_ids": ["battery"]},
        ])
        result = run_assessment(
            self.snapshot, provider, mode=ExecutionMode.ROUTED,
            router=FakeCapabilityRouter(selected=["fuel_economy"]),
        )
        self.assertEqual(result.planner.trace.completion_status, "incomplete")
        self.assertEqual(result.routing.expansion_failure_reason, "expansion_limit")

    def test_safety_and_maintenance_are_unchanged_by_routing(self):
        snapshot, context, request = maintenance_context()
        provider = FakePlannerProvider([final([], actions=("REVIEW_UPCOMING_MAINTENANCE",))])
        full = run_assessment(snapshot, provider, context, request, mode=ExecutionMode.FULL)
        routed = run_assessment(
            snapshot, FakePlannerProvider([final([], actions=("REVIEW_UPCOMING_MAINTENANCE",))]), context, request,
            mode=ExecutionMode.ROUTED, router=FakeCapabilityRouter(selected=["maintenance"]),
        )
        self.assertEqual(full.planner.result.safety, routed.planner.result.safety)
        self.assertEqual(full.planner.maintenance_state, routed.planner.maintenance_state)
        self.assertEqual(full.planner.result.assessment.safety_disposition, SafetyDisposition.UNDETERMINED)

    def test_jev_adapter_uses_batched_answers_without_network(self):
        import typesafe_sdk

        class FakeClient:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def system_one(self, **kwargs):
                return SimpleNamespace(
                    answers={"tires": SimpleNamespace(noul=0.82)},
                    model="typesafe-test",
                    usage=SimpleNamespace(input_tokens=12, output_tokens=3),
                )

        client = FakeClient()
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"}, clear=False), \
             patch.object(typesafe_sdk, "TypeSafeClient", return_value=client):
            response = JevCapabilityRouter().route({"owner_message": "test"}, {"tires": "pressure history"})
        self.assertEqual(response.relevance, {"tires": 0.82})
        self.assertEqual(response.model, "typesafe-test")
        self.assertEqual((response.input_tokens, response.output_tokens), (12, 3))

    def test_hidden_truth_and_future_samples_never_reach_planner(self):
        provider = self._valid_provider()
        run_assessment(self.snapshot, provider, mode=ExecutionMode.ROUTED, router=FakeCapabilityRouter(selected=["tires"]))
        serialized = json.dumps(provider.requests, default=str)
        self.assertNotIn(self.truth.scenario_id, serialized)
        future = [o.observation_id for o in self.episode.timeline if o.timestamp > self.snapshot.assessment_at]
        self.assertFalse(any(identifier in serialized for identifier in future))


if __name__ == "__main__":
    unittest.main()
