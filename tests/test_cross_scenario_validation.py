import json
import unittest

from carmind.assessment import MAX_OBSERVATION_CLAIM_CHARACTERS
from carmind.capabilities import load_capabilities
from carmind.planner import run_planner
from carmind.planner_provider import FakePlannerProvider, ProviderResponse
from carmind.response import render_user_response
from carmind.simulator import generate_episode, freeze_episode, list_scenarios
from support import maintenance_context


SCENARIO_FIXTURES = {
    "healthy_vehicle": ("trip_readiness", "get_trip_readiness_evidence",
                         ("coolant_temperature", "battery_voltage", "front_left_tire_pressure", "front_right_tire_pressure", "rear_left_tire_pressure", "rear_right_tire_pressure"),
                         "Available vehicle checks are present for the planned trip.", None,
                         ("PREPARE_TRIP_CHECKLIST",), ("MISSING_EVIDENCE",), ("POC_ONLY",)),
    "sustained_temperature_rise": ("cooling", "get_coolant_history", ("coolant_temperature",),
                                    "Coolant temperature rose and remained elevated near the assessment time.", "POSSIBLE_COOLING_ISSUE",
                                    ("ARRANGE_SERVICE_REVIEW", "SHARE_EVIDENCE_WITH_WORKSHOP"), ("CAUSE_UNCONFIRMED", "INSPECTION_NEEDED"), ("NO_PHYSICAL_INSPECTION",)),
    "weak_battery_start": ("battery", "get_battery_voltage_history", ("battery_voltage",),
                            "Battery voltage dropped during the simulated starting event.", "POSSIBLE_STARTING_SYSTEM_ISSUE",
                            ("CHECK_BATTERY_AT_SERVICE", "ARRANGE_SERVICE_REVIEW"), ("CAUSE_UNCONFIRMED", "INSPECTION_NEEDED"), ("NO_PHYSICAL_INSPECTION",)),
    "gradual_tire_pressure_loss": ("tires", "get_tire_pressure_history", ("rear_left_tire_pressure",),
                                    "Rear-left tire pressure fell from about 35 psi to about 29 psi across six days.", "POSSIBLE_TIRE_LEAK",
                                    ("ARRANGE_SERVICE_REVIEW", "SHARE_EVIDENCE_WITH_WORKSHOP"), ("CAUSE_UNCONFIRMED", "INSPECTION_NEEDED"), ("NO_PHYSICAL_INSPECTION",)),
    "increased_fuel_consumption": ("fuel_economy", "get_fuel_usage_summary", ("fuel_consumption", "average_trip_duration", "idle_time_ratio"),
                                    "Recent fuel consumption increased while trip duration and idle time also changed.", "POSSIBLE_USAGE_CHANGE",
                                    ("REVIEW_RECENT_EVIDENCE", "ARRANGE_SERVICE_REVIEW"), ("CAUSE_UNCONFIRMED", "MISSING_EVIDENCE"), ("PARTIAL_EVIDENCE",)),
}


def scenario_candidate(snapshot, spec, *, ids=None, action_override=None, source_ids=()):
    _, _, _, text, hypothesis, actions, uncertainties, limitations = spec
    ids = list(ids or [item.observation_id for item in snapshot.observations])
    return {"type": "final", "assessment": {
        "observations": [{"text": text, "evidence_ids": ids}],
        "hypotheses": ([{"hypothesis_id": hypothesis, "evidence_ids": ids}] if hypothesis else []),
        "uncertainties": list(uncertainties),
        "recommended_action_ids": list(action_override if action_override is not None else actions),
        "limitations": list(limitations),
    }}


class CrossScenarioValidationTests(unittest.TestCase):
    def test_every_simulator_scenario_accepts_a_concise_grounded_assessment(self):
        self.assertEqual(set(list_scenarios()), set(SCENARIO_FIXTURES))
        for scenario in list_scenarios():
            with self.subTest(scenario=scenario):
                episode, truth = generate_episode(scenario, 42)
                snapshot = freeze_episode(episode)
                spec = SCENARIO_FIXTURES[scenario]
                names = set(spec[2])
                ids = [item.observation_id for item in snapshot.observations if item.name in names]
                provider = FakePlannerProvider([
                    {"type": "tool_call", "tool_id": spec[1], "arguments": {}},
                    ProviderResponse(json.dumps(scenario_candidate(snapshot, spec, ids=ids))),
                ])
                run = run_planner(snapshot, provider, loaded=load_capabilities([spec[0]]), max_model_calls=2)
                self.assertEqual(run.trace.completion_status, "complete")
                self.assertEqual(run.trace.validation_failures, [])
                self.assertEqual(run.result.assessment.safety_disposition, run.result.safety.disposition)
                self.assertEqual(set(run.result.assessment.evidence_ids), set(ids))
                self.assertNotIn(scenario, json.dumps(provider.requests))
                response = render_user_response(run.result).text
                self.assertNotIn(ids[0], response)
                self.assertNotIn("{", response)
                self.assertNotIn("ScenarioTruth", response)
                self.assertNotIn("you may drive", response.lower())
                if run.result.safety.disposition.value == "STOP_WHEN_SAFE":
                    self.assertIn("do not treat", response.lower())
                else:
                    self.assertNotIn("permission to continue driving", response.lower())
                self.assertEqual(truth.scenario_id, scenario)  # evaluator-only fixture check

    def test_temperature_invalid_grounding_repairs_to_concise_claim(self):
        episode, _ = generate_episode("sustained_temperature_rise", 42)
        snapshot = freeze_episode(episode)
        ids = [item.observation_id for item in snapshot.observations if item.name == "coolant_temperature"]
        spec = SCENARIO_FIXTURES["sustained_temperature_rise"]
        invalid = scenario_candidate(snapshot, spec, ids=["future-evidence"])
        valid = scenario_candidate(snapshot, spec, ids=ids)
        run = run_planner(snapshot, FakePlannerProvider([
            {"type": "tool_call", "tool_id": "get_coolant_history", "arguments": {}}, invalid, valid
        ]), loaded=load_capabilities(["cooling"]), max_model_calls=3)
        self.assertEqual(run.trace.repair_status, "repair_succeeded")
        self.assertEqual(run.result.assessment.evidence_ids, ids)

    def test_battery_unsupported_action_repairs_to_allowlisted_action(self):
        episode, _ = generate_episode("weak_battery_start", 42)
        snapshot = freeze_episode(episode)
        ids = [item.observation_id for item in snapshot.observations if item.name == "battery_voltage"]
        spec = SCENARIO_FIXTURES["weak_battery_start"]
        invalid = scenario_candidate(snapshot, spec, ids=ids, action_override=("REPLACE_BATTERY_NOW",))
        valid = scenario_candidate(snapshot, spec, ids=ids)
        run = run_planner(snapshot, FakePlannerProvider([
            {"type": "tool_call", "tool_id": "get_battery_voltage_history", "arguments": {}}, invalid, valid
        ]), loaded=load_capabilities(["battery"]), max_model_calls=3)
        self.assertEqual(run.trace.repair_status, "repair_succeeded")
        self.assertEqual(run.result.assessment.recommended_action_ids, list(spec[5]))

    def test_wrong_model_safety_field_is_rejected_then_repaired(self):
        episode, _ = generate_episode("increased_fuel_consumption", 42)
        snapshot = freeze_episode(episode)
        spec = SCENARIO_FIXTURES["increased_fuel_consumption"]
        ids = [item.observation_id for item in snapshot.observations if item.name in set(spec[2])]
        invalid = scenario_candidate(snapshot, spec, ids=ids)
        invalid["assessment"]["safety_disposition"] = "NO_RULE_TRIGGERED"
        valid = scenario_candidate(snapshot, spec, ids=ids)
        run = run_planner(snapshot, FakePlannerProvider([
            {"type": "tool_call", "tool_id": "get_fuel_usage_summary", "arguments": {}}, invalid, valid
        ]), loaded=load_capabilities(["fuel_economy"]), max_model_calls=3)
        self.assertEqual(run.trace.repair_status, "repair_succeeded")
        self.assertEqual(run.result.assessment.safety_disposition, run.result.safety.disposition)

    def test_manufacturer_source_repair_keeps_explicit_provenance(self):
        snapshot, context, request = maintenance_context()
        bad = {"type": "final", "assessment": {
            "observations": [{"text": "The recorded maintenance item is in the available schedule.", "evidence_ids": ["fixture-oil"]}],
            "hypotheses": [], "evidence_ids": ["fixture-oil"], "source_ids": ["invented-source"],
            "uncertainties": ["APPLICABILITY_UNVERIFIED"], "recommended_action_ids": ["REVIEW_UPCOMING_MAINTENANCE"],
            "limitations": ["POC_ONLY"],
        }}
        good = dict(bad, assessment=dict(bad["assessment"], source_ids=["fixture-source"]))
        run = run_planner(snapshot, FakePlannerProvider([bad, good]), context, request,
                          loaded=load_capabilities(["maintenance"]), max_model_calls=2)
        self.assertEqual(run.trace.repair_status, "repair_succeeded")
        self.assertEqual(run.result.source_ids, ("fixture-source",))

    def test_claim_bound_is_explicit_and_defensive(self):
        self.assertEqual(MAX_OBSERVATION_CLAIM_CHARACTERS, 2048)


if __name__ == "__main__":
    unittest.main()
