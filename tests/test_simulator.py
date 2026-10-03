"""Behavioral tests for development telemetry and its evidence boundary."""

from dataclasses import FrozenInstanceError, asdict, fields, replace
from datetime import timedelta
import json
import random
import unittest

from carmind.contracts import ObservationSource, UserMessage, VehicleProfile
from carmind.evidence import FrozenEvidenceSnapshot
from carmind.simulator import (
    ScenarioTruth,
    SimulationEpisode,
    freeze_episode,
    generate_episode,
    list_scenarios,
)


class SimulatorTests(unittest.TestCase):
    def snapshot(self, scenario: str, seed: int = 42) -> FrozenEvidenceSnapshot:
        episode, _ = generate_episode(scenario, seed)
        return freeze_episode(episode)

    def values(self, snapshot: FrozenEvidenceSnapshot, name: str) -> list[float]:
        return [item.value for item in snapshot.observations if item.name == name]

    def test_all_five_scenarios_generate(self) -> None:
        self.assertEqual(set(list_scenarios()), {
            "healthy_vehicle", "sustained_temperature_rise", "weak_battery_start",
            "gradual_tire_pressure_loss", "increased_fuel_consumption",
        })
        for scenario in list_scenarios():
            with self.subTest(scenario=scenario):
                episode, truth = generate_episode(scenario, 42)
                self.assertIsInstance(episode, SimulationEpisode)
                self.assertIsInstance(truth, ScenarioTruth)
                self.assertEqual(truth.scenario_id, scenario)
                self.assertTrue(episode.timeline)

    def test_same_seed_reproduces_entire_episode(self) -> None:
        for scenario in list_scenarios():
            with self.subTest(scenario=scenario):
                first, first_truth = generate_episode(scenario, 42)
                second, second_truth = generate_episode(scenario, 42)
                self.assertEqual(first, second)
                self.assertEqual(first_truth, second_truth)
                self.assertEqual(freeze_episode(first), freeze_episode(second))

    def test_different_seeds_change_telemetry_noise(self) -> None:
        for scenario in list_scenarios():
            with self.subTest(scenario=scenario):
                first = self.snapshot(scenario, 42)
                second = self.snapshot(scenario, 43)
                self.assertNotEqual(
                    [item.value for item in first.observations],
                    [item.value for item in second.observations],
                )

    def test_generation_does_not_change_global_random_state(self) -> None:
        before = random.getstate()
        for scenario in list_scenarios():
            generate_episode(scenario, 42)
        self.assertEqual(random.getstate(), before)

    def test_observation_ids_unique_and_timestamps_ordered(self) -> None:
        for scenario in list_scenarios():
            with self.subTest(scenario=scenario):
                episode, _ = generate_episode(scenario, 42)
                identifiers = [item.observation_id for item in episode.timeline]
                timestamps = [item.timestamp for item in episode.timeline]
                self.assertEqual(len(identifiers), len(set(identifiers)))
                self.assertEqual(timestamps, sorted(timestamps))
                self.assertEqual(len(set(timestamps)), 7)
                for observation in episode.timeline:
                    self.assertIs(observation.source, ObservationSource.SIMULATOR)
                    self.assertTrue(observation.name)
                    if isinstance(observation.value, (int, float)):
                        self.assertTrue(observation.unit)

    def test_cutoff_is_inclusive_and_future_samples_are_absent(self) -> None:
        for scenario in list_scenarios():
            with self.subTest(scenario=scenario):
                episode, _ = generate_episode(scenario, 42)
                snapshot = freeze_episode(episode)
                future = [item for item in episode.timeline if item.timestamp > episode.assessment_at]
                self.assertTrue(future)
                self.assertTrue(any(item.timestamp == snapshot.assessment_at for item in snapshot.observations))
                self.assertTrue(all(item.timestamp <= snapshot.assessment_at for item in snapshot.observations))
                visible_ids = {item.observation_id for item in snapshot.observations}
                self.assertTrue(visible_ids.isdisjoint(item.observation_id for item in future))
                serialized = json.dumps(asdict(snapshot), default=str)
                for item in future:
                    self.assertNotIn(item.observation_id, serialized)

    def test_snapshot_has_only_public_fields_and_no_truth_or_timeline(self) -> None:
        for scenario in list_scenarios():
            with self.subTest(scenario=scenario):
                snapshot = self.snapshot(scenario)
                self.assertEqual({item.name for item in fields(snapshot)}, {
                    "episode_id", "profile", "owner_message", "assessment_at", "observations",
                })
                self.assertFalse(hasattr(snapshot, "truth"))
                self.assertFalse(hasattr(snapshot, "timeline"))
                serialized = json.dumps(asdict(snapshot), default=str)
                for hidden_name in list_scenarios():
                    self.assertNotIn(hidden_name, serialized)

    def test_no_hidden_labels_in_complete_observations(self) -> None:
        for scenario in list_scenarios():
            episode, _ = generate_episode(scenario, 42)
            for observation in episode.timeline:
                for hidden_name in list_scenarios():
                    self.assertNotIn(hidden_name, observation.name)
                    self.assertNotIn(hidden_name, str(observation.value))

    def test_owner_message_and_profile_included(self) -> None:
        for scenario in list_scenarios():
            episode, _ = generate_episode(scenario, 42)
            snapshot = freeze_episode(episode)
            self.assertIsInstance(snapshot.owner_message, UserMessage)
            self.assertEqual(snapshot.owner_message, episode.owner_message)
            self.assertIsInstance(snapshot.profile, VehicleProfile)
            self.assertEqual(snapshot.profile, episode.profile)
            self.assertLessEqual(snapshot.owner_message.timestamp, snapshot.assessment_at)
            self.assertIsNone(snapshot.profile.vin)

    def test_snapshot_and_nested_contracts_are_immutable(self) -> None:
        snapshot = self.snapshot("healthy_vehicle")
        self.assertIsInstance(snapshot.observations, tuple)
        mutations = (
            (snapshot, "assessment_at", snapshot.assessment_at + timedelta(days=1)),
            (snapshot.profile, "mileage_km", 99999),
            (snapshot.owner_message, "text", "Changed"),
            (snapshot.observations[0], "value", 999),
        )
        for target, name, value in mutations:
            with self.subTest(field=name), self.assertRaises(FrozenInstanceError):
                setattr(target, name, value)

    def test_snapshot_copies_mutable_collection_input(self) -> None:
        snapshot = self.snapshot("healthy_vehicle")
        source = list(snapshot.observations)
        copied = replace(snapshot, observations=source)
        source.clear()
        self.assertEqual(copied.observations, snapshot.observations)

    def test_snapshot_rejects_future_observation_even_when_constructed_directly(self) -> None:
        episode, _ = generate_episode("healthy_vehicle", 42)
        with self.assertRaisesRegex(ValueError, "later than"):
            replace(freeze_episode(episode), observations=episode.timeline)

    def test_snapshot_rejects_future_owner_message(self) -> None:
        snapshot = self.snapshot("healthy_vehicle")
        message = replace(snapshot.owner_message, timestamp=snapshot.assessment_at + timedelta(seconds=1))
        with self.assertRaisesRegex(ValueError, "later than"):
            replace(snapshot, owner_message=message)

    def test_snapshot_rejects_naive_timestamps(self) -> None:
        snapshot = self.snapshot("healthy_vehicle")
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            replace(snapshot, assessment_at=snapshot.assessment_at.replace(tzinfo=None))

    def test_snapshot_rejects_duplicate_ids_and_unordered_samples(self) -> None:
        snapshot = self.snapshot("healthy_vehicle")
        with self.assertRaisesRegex(ValueError, "unique"):
            replace(snapshot, observations=(snapshot.observations[0],) * 2)
        with self.assertRaisesRegex(ValueError, "ordered"):
            replace(snapshot, observations=tuple(reversed(snapshot.observations)))

    def test_snapshot_rejects_blank_episode_id(self) -> None:
        with self.assertRaisesRegex(ValueError, "blank"):
            replace(self.snapshot("healthy_vehicle"), episode_id=" ")

    def test_no_telemetry_is_valid(self) -> None:
        snapshot = self.snapshot("healthy_vehicle")
        empty = FrozenEvidenceSnapshot(
            snapshot.episode_id, snapshot.profile, snapshot.owner_message, snapshot.assessment_at
        )
        self.assertEqual(empty.observations, ())

    def test_tire_loss_pattern_across_seeds(self) -> None:
        for seed in (0, 1, 42, 43, 999):
            with self.subTest(seed=seed):
                snapshot = self.snapshot("gradual_tire_pressure_loss", seed)
                loss = self.values(snapshot, "rear_left_tire_pressure")
                self.assertGreater(loss[0] - loss[-1], 5)
                self.assertTrue(all(a > b for a, b in zip(loss, loss[1:])))
                for wheel in ("front_left", "front_right", "rear_right"):
                    stable = self.values(snapshot, f"{wheel}_tire_pressure")
                    self.assertLess(max(stable) - min(stable), 0.3)

    def test_temperature_rise_pattern_across_seeds(self) -> None:
        for seed in (0, 1, 42, 43, 999):
            values = self.values(self.snapshot("sustained_temperature_rise", seed), "coolant_temperature")
            self.assertTrue(all(a < b for a, b in zip(values, values[1:])))
            self.assertTrue(all(value > values[0] + 20 for value in values[-3:]))

    def test_starting_voltage_drop_pattern_across_seeds(self) -> None:
        for seed in (0, 1, 42, 43, 999):
            snapshot = self.snapshot("weak_battery_start", seed)
            voltage = self.values(snapshot, "battery_voltage")
            states = [item.value for item in snapshot.observations if item.name == "operating_state"]
            self.assertGreater(voltage[0] - min(voltage), 3)
            self.assertTrue(all(value < voltage[0] - 2 for value, state in zip(voltage, states) if state == "starting"))
            self.assertGreater(voltage[-1] - min(voltage), 2)

    def test_fuel_consumption_worsens_across_seeds(self) -> None:
        for seed in (0, 1, 42, 43, 999):
            snapshot = self.snapshot("increased_fuel_consumption", seed)
            consumption = self.values(snapshot, "fuel_consumption")
            self.assertGreater(sum(consumption[-3:]) / 3, sum(consumption[:3]) / 3 + 1.5)
            duration = self.values(snapshot, "average_trip_duration")
            idle = self.values(snapshot, "idle_time_ratio")
            self.assertLess(duration[-1], duration[0])
            self.assertGreater(idle[-1], idle[0])

    def test_healthy_patterns_remain_stable_across_seeds(self) -> None:
        for seed in (0, 1, 42, 43, 999):
            snapshot = self.snapshot("healthy_vehicle", seed)
            for name, baseline, tolerance in (
                ("coolant_temperature", 90, 0.4),
                ("battery_voltage", 14.2, 0.1),
                ("fuel_consumption", 7, 0.2),
                ("average_trip_duration", 25, 0.4),
                ("idle_time_ratio", 0.08, 0.01),
                *((f"{wheel}_tire_pressure", 35, 0.2) for wheel in (
                    "front_left", "front_right", "rear_left", "rear_right"
                )),
            ):
                with self.subTest(seed=seed, name=name):
                    values = self.values(snapshot, name)
                    self.assertEqual(len(values), 6)
                    self.assertTrue(all(abs(value - baseline) < tolerance for value in values))

    def test_unknown_scenario_fails_cleanly(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown scenario"):
            generate_episode("not_a_scenario", 42)


if __name__ == "__main__":
    unittest.main()
