import ast
from dataclasses import replace
from datetime import timedelta
import inspect
import unittest

from carmind.contracts import SafetyDisposition as D, VehicleContext
from carmind.simulator import generate_episode, freeze_episode
from carmind.safety import evaluate_safety, catalog
import carmind.safety as safety_module


def snapshot(name="healthy_vehicle"):
    return freeze_episode(generate_episode(name, 42)[0])


class SafetyTests(unittest.TestCase):
    def test_healthy_is_not_clearance(self):
        decision = evaluate_safety(snapshot())
        self.assertEqual(decision.disposition, D.NO_RULE_TRIGGERED)
        self.assertIn("not a determination", decision.approved_text)

    def test_sustained_coolant(self):
        decision = evaluate_safety(snapshot("sustained_temperature_rise"))
        self.assertEqual(decision.disposition, D.STOP_WHEN_SAFE)
        self.assertIn("SIM_COOLANT_SUSTAINED", decision.rule_ids)

    def test_tire_loss(self):
        decision = evaluate_safety(snapshot("gradual_tire_pressure_loss"))
        self.assertEqual(decision.disposition, D.STOP_WHEN_SAFE)
        self.assertIn("SIM_TIRE_LOSS", decision.rule_ids)

    def test_starting_rule_and_uncertainty_precedence(self):
        weak = snapshot("weak_battery_start")
        decision = evaluate_safety(weak)
        self.assertIn("SIM_STARTING_VOLTAGE", decision.rule_ids)
        self.assertEqual(decision.disposition, D.UNDETERMINED)
        healthy = snapshot()
        all_evidence = tuple(sorted([o for o in healthy.observations if o.name not in {"battery_voltage", "operating_state"}] + list(weak.observations), key=lambda o: o.timestamp))
        decision = evaluate_safety(replace(healthy, observations=all_evidence))
        self.assertEqual(decision.disposition, D.SERVICE_REVIEW)

    def test_missing_critical_data(self):
        self.assertEqual(evaluate_safety(replace(snapshot(), observations=())).disposition, D.UNDETERMINED)

    def test_contradictory_timestamp(self):
        s = snapshot()
        context = VehicleContext(s.profile, observations=[replace(s.observations[0], observation_id="conflict", value=1000)])
        self.assertEqual(evaluate_safety(s, context).disposition, D.UNDETERMINED)

    def test_invalid_number_or_unit(self):
        s = snapshot()
        for change in ({"value": float("nan")}, {"value": True}, {"unit": "degF"}):
            with self.subTest(change=change):
                bad = replace(s, observations=(replace(s.observations[0], **change),) + s.observations[1:])
                self.assertEqual(evaluate_safety(bad).disposition, D.UNDETERMINED)

    def test_future_evidence_ignored(self):
        s = snapshot()
        context = VehicleContext(s.profile, observations=[replace(s.observations[0], observation_id="future", timestamp=s.assessment_at+timedelta(days=1), value=999)])
        self.assertEqual(evaluate_safety(s), evaluate_safety(s, context))

    def test_stale_evidence_uncertain(self):
        s = snapshot()
        self.assertEqual(evaluate_safety(replace(s, assessment_at=s.assessment_at+timedelta(days=8))).disposition, D.UNDETERMINED)

    def test_sparse_history_is_uncertain_even_with_all_channels(self):
        s = snapshot()
        latest = tuple(o for o in s.observations if o.timestamp == s.assessment_at)
        self.assertEqual(evaluate_safety(replace(s, observations=latest)).disposition, D.UNDETERMINED)

    def test_real_vehicle_does_not_use_simulation_thresholds(self):
        s = snapshot("sustained_temperature_rise")
        decision = evaluate_safety(replace(s, profile=replace(s.profile, make="Hyundai")))
        self.assertEqual(decision.disposition, D.UNDETERMINED)
        self.assertEqual(decision.rule_ids, ())

    def test_truth_and_episode_not_accepted(self):
        episode, truth = generate_episode("healthy_vehicle", 42)
        for value in (episode, truth):
            with self.assertRaises(ValueError):
                evaluate_safety(value)

    def test_no_simulator_or_provider_import(self):
        tree = ast.parse(inspect.getsource(safety_module))
        imports = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        self.assertFalse(any("simulator" in n or "provider" in n or "openai" in n for n in imports))

    def test_determinism_and_catalog_message(self):
        s = snapshot("gradual_tire_pressure_loss")
        decision = evaluate_safety(s)
        self.assertEqual(decision, evaluate_safety(s))
        message = next(m for m in catalog("safety_messages.json")["messages"] if m["message_id"] == decision.message_id)
        self.assertEqual(decision.approved_text, message["approved_text"])
        self.assertTrue(set(decision.evidence_ids) <= {o.observation_id for o in s.observations})
