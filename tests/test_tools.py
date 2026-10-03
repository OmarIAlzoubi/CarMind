"""Read-only tool permissions, provenance and cutoff behavior."""

import ast
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import timedelta
import inspect
import json
import unittest

import carmind.tools as tools_module
from carmind.capabilities import load_capabilities
from carmind.contracts import DiagnosticCodeRecord, MaintenanceRecord, Observation, ObservationSource, VehicleContext
from carmind.simulator import freeze_episode, generate_episode
from carmind.tools import TOOL_CATALOG, execute_tool, list_tools


class ToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.episode, _ = generate_episode("healthy_vehicle", 42)
        self.snapshot = freeze_episode(self.episode)
        self.cutoff = self.snapshot.assessment_at
        self.context = VehicleContext(
            self.snapshot.profile,
            maintenance_records=[
                MaintenanceRecord("service-old", "oil", self.cutoff - timedelta(days=10), 20000),
                MaintenanceRecord("service-new", "tires", self.cutoff - timedelta(days=1), 23000),
                MaintenanceRecord("service-future", "oil", self.cutoff + timedelta(days=1), 25000),
            ],
            diagnostic_codes=[
                DiagnosticCodeRecord("EXAMPLE-A", self.cutoff - timedelta(days=2), "owner", False),
                DiagnosticCodeRecord("EXAMPLE-B", self.cutoff, "owner", True),
                DiagnosticCodeRecord("FUTURE-CODE", self.cutoff + timedelta(days=1), "owner", True),
            ],
            observations=[
                Observation("engine-id", "engine_rpm", 800, "rpm", self.cutoff, ObservationSource.USER),
                Observation("electrical-id", "accessory_voltage", 12.1, "V", self.cutoff, ObservationSource.USER),
            ],
        )

    def run_tool(self, tool_id, arguments=None, snapshot=None, context=None):
        return execute_tool(
            tool_id, {} if arguments is None else arguments,
            self.snapshot if snapshot is None else snapshot,
            self.context if context is None else context,
            allowed_tool_ids=(tool_id,),
        )

    def test_catalog_is_unique_read_only_and_described(self) -> None:
        definitions = list_tools()
        self.assertEqual(len(definitions), 26)
        self.assertEqual(len({item.tool_id for item in definitions}), 26)
        for item in definitions:
            self.assertTrue(item.read_only)
            self.assertTrue(item.description)
            self.assertTrue(item.evidence_description)
            self.assertFalse(item.argument_schema["additionalProperties"])

    def test_schema_return_is_detached(self) -> None:
        schema = TOOL_CATALOG["get_tire_pressure_history"].argument_schema
        schema["properties"].clear()
        self.assertIn("wheel", TOOL_CATALOG["get_tire_pressure_history"].argument_schema["properties"])

    def test_all_twenty_tools_execute_with_available_evidence(self) -> None:
        for definition in list_tools():
            with self.subTest(tool=definition.tool_id):
                if definition.tool_id == "search_manufacturer_manual":
                    # A registered manual index and matching profile are exercised separately.
                    continue
                if definition.tool_id in tools_module.MAINTENANCE_TOOLS:
                    self.assertEqual(self.run_tool(definition.tool_id).error, "unavailable_manufacturer_knowledge")
                    continue
                if definition.tool_id == "get_starting_voltage_summary":
                    episode, _ = generate_episode("weak_battery_start", 42)
                    result = self.run_tool(definition.tool_id, snapshot=freeze_episode(episode))
                else:
                    result = self.run_tool(definition.tool_id)
                self.assertTrue(result.success, result.error)
                self.assertTrue(result.evidence_ids)
                json.dumps(asdict(result))

    def test_unknown_tool_rejected(self) -> None:
        result = self.run_tool("unknown")
        self.assertEqual(result.error, "unknown_tool")
        self.assertFalse(result.success)

    def test_unloaded_tool_rejected_and_default_is_deny(self) -> None:
        loaded = load_capabilities(["tires"])
        result = execute_tool("get_coolant_history", {}, self.snapshot, allowed_tool_ids=loaded.tool_ids)
        self.assertEqual(result.error, "tool_not_allowed")
        self.assertEqual(execute_tool("get_vehicle_profile", {}, self.snapshot).error, "tool_not_allowed")

    def test_malformed_arguments_rejected(self) -> None:
        for tool_id, arguments in (
            ("get_tire_pressure_history", {"wheel": "spare"}),
            ("get_tire_pressure_history", {"wheel": []}),
            ("get_tire_pressure_history", {"after": "tomorrow"}),
            ("get_tire_pressure_history", {"allowed_tool_ids": ["get_coolant_history"]}),
            ("get_engine_recent_history", {"limit": True}),
            ("get_engine_recent_history", {"limit": 0}),
            ("get_engine_recent_history", {"limit": 101}),
            ("get_engine_recent_history", {"limit": 1.5}),
            ("get_latest_service_record", {"service_type": " "}),
            ("get_vehicle_profile", []),
        ):
            with self.subTest(tool=tool_id, arguments=arguments):
                self.assertEqual(self.run_tool(tool_id, arguments).error, "invalid_arguments")

    def test_history_tools_return_only_relevant_visible_evidence(self) -> None:
        cases = (
            ("gradual_tire_pressure_loss", "get_tire_pressure_history", {f"{wheel}_tire_pressure" for wheel in ("front_left", "front_right", "rear_left", "rear_right")}),
            ("sustained_temperature_rise", "get_coolant_history", {"coolant_temperature"}),
            ("weak_battery_start", "get_battery_voltage_history", {"battery_voltage"}),
            ("increased_fuel_consumption", "get_fuel_consumption_history", {"fuel_consumption"}),
            ("increased_fuel_consumption", "get_trip_duration_history", {"average_trip_duration"}),
            ("increased_fuel_consumption", "get_idle_time_history", {"idle_time_ratio"}),
        )
        for scenario, tool_id, names in cases:
            with self.subTest(tool=tool_id):
                episode, _ = generate_episode(scenario, 42)
                snapshot = freeze_episode(episode)
                result = execute_tool(tool_id, {}, snapshot, allowed_tool_ids=(tool_id,))
                expected = {item.observation_id for item in snapshot.observations if item.name in names}
                self.assertTrue(result.success)
                self.assertEqual(set(result.evidence_ids), expected)
                self.assertEqual({item["name"] for item in result.data["observations"]}, names)
                future_ids = {item.observation_id for item in episode.timeline if item.timestamp > episode.assessment_at}
                self.assertTrue(future_ids.isdisjoint(result.evidence_ids))

    def test_tire_wheel_filter_and_summary_provenance(self) -> None:
        result = self.run_tool("get_tire_pressure_summary", {"wheel": "rear_left"})
        summary = result.data["summaries"][0]
        self.assertEqual(summary["name"], "rear_left_tire_pressure")
        self.assertEqual(summary["count"], 6)
        self.assertEqual(set(summary["evidence_ids"]), set(result.evidence_ids))
        self.assertAlmostEqual(summary["change"], summary["last"] - summary["first"])

    def test_tire_summary_is_compact_grounded_and_history_remains_raw(self) -> None:
        episode, _ = generate_episode("gradual_tire_pressure_loss", 42)
        snapshot = freeze_episode(episode)
        summary = self.run_tool("get_tire_pressure_summary", snapshot=snapshot)
        history = self.run_tool("get_tire_pressure_history", snapshot=snapshot)
        self.assertTrue(summary.success)
        self.assertEqual(set(summary.data), {"summaries"})
        self.assertEqual(len(summary.data["summaries"]), 4)
        self.assertEqual(len(history.data["observations"]), 24)
        self.assertEqual([item["timestamp"] for item in history.data["observations"]],
                         sorted(item["timestamp"] for item in history.data["observations"]))
        by_wheel = {item["name"]: item for item in summary.data["summaries"]}
        rear_left = by_wheel["rear_left_tire_pressure"]
        self.assertEqual(rear_left["count"], 6)
        self.assertAlmostEqual(rear_left["change"], rear_left["last"] - rear_left["first"])
        self.assertLess(rear_left["change"], -5)
        self.assertEqual(rear_left["first_at"], snapshot.observations[2].timestamp.isoformat())
        self.assertEqual(rear_left["last_at"], snapshot.observations[22].timestamp.isoformat())
        for name, item in by_wheel.items():
            if name != "rear_left_tire_pressure":
                self.assertLess(abs(item["change"]), 0.2)
        visible = {item.observation_id for item in snapshot.observations}
        future = {item.observation_id for item in episode.timeline if item.timestamp > snapshot.assessment_at}
        support = {identifier for item in summary.data["summaries"] for identifier in item["evidence_ids"]}
        self.assertEqual(support, set(summary.evidence_ids))
        self.assertEqual(support, visible)
        self.assertTrue(future.isdisjoint(support))
        self.assertEqual(execute_tool("get_tire_pressure_summary", {}, snapshot).error, "tool_not_allowed")

    def test_tire_summary_excludes_future_and_non_numeric_support(self) -> None:
        episode, _ = generate_episode("gradual_tire_pressure_loss", 42)
        snapshot = freeze_episode(episode)
        context = VehicleContext(snapshot.profile, observations=[
            Observation("future-tire", "rear_left_tire_pressure", 20, "psi", snapshot.assessment_at + timedelta(days=1), ObservationSource.USER),
            Observation("text-tire", "rear_left_tire_pressure", "unknown", "psi", snapshot.assessment_at, ObservationSource.USER),
        ])
        result = self.run_tool("get_tire_pressure_summary", snapshot=snapshot, context=context)
        self.assertTrue(result.success)
        self.assertNotIn("future-tire", result.evidence_ids)
        self.assertNotIn("text-tire", result.evidence_ids)
        self.assertNotIn("future-tire", json.dumps(asdict(result)))
        self.assertNotIn("text-tire", json.dumps(asdict(result)))

    def test_summary_does_not_mix_units(self) -> None:
        context = deepcopy(self.context)
        context.observations.append(Observation("other-unit", "coolant_temperature", 180, "degF", self.cutoff, ObservationSource.USER))
        result = self.run_tool("get_cooling_summary", context=context)
        self.assertEqual({item["unit"] for item in result.data["summaries"]}, {"degC", "degF"})

    def test_starting_summary_requires_paired_state_and_voltage(self) -> None:
        self.assertEqual(self.run_tool("get_starting_voltage_summary").error, "unavailable_evidence")
        episode, _ = generate_episode("weak_battery_start", 42)
        snapshot = freeze_episode(episode)
        result = self.run_tool("get_starting_voltage_summary", snapshot=snapshot)
        self.assertEqual(result.data["summaries"][0]["count"], 3)
        self.assertEqual(len(result.evidence_ids), 6)
        missing_state = replace(snapshot, observations=tuple(item for item in snapshot.observations if item.name == "battery_voltage"))
        self.assertEqual(self.run_tool("get_starting_voltage_summary", snapshot=missing_state).error, "unavailable_evidence")

    def test_engine_limit_is_bounded_and_ordered(self) -> None:
        result = self.run_tool("get_engine_recent_history", {"limit": 2})
        self.assertEqual(len(result.data["observations"]), 2)
        dates = [item["timestamp"] for item in result.data["observations"]]
        self.assertEqual(dates, sorted(dates))

    def test_context_future_observations_do_not_bypass_cutoff(self) -> None:
        context = deepcopy(self.context)
        context.observations.extend(self.episode.timeline)
        result = self.run_tool("get_coolant_history", context=context)
        self.assertEqual(len(result.evidence_ids), 6)
        self.assertEqual(set(result.evidence_ids), {item.observation_id for item in self.snapshot.observations if item.name == "coolant_temperature"})

    def test_future_service_and_code_records_excluded(self) -> None:
        for tool in ("get_service_history", "get_latest_service_record", "get_maintenance_odometer_context", "get_diagnostic_code_history", "get_active_diagnostic_codes", "get_trip_readiness_evidence"):
            with self.subTest(tool=tool):
                text = json.dumps(asdict(self.run_tool(tool)))
                self.assertNotIn("service-future", text)
                self.assertNotIn("FUTURE-CODE", text)

    def test_latest_service_filter_and_provenance(self) -> None:
        latest = self.run_tool("get_latest_service_record")
        self.assertEqual(latest.evidence_ids, ("service-new",))
        oil = self.run_tool("get_latest_service_record", {"service_type": "oil"})
        self.assertEqual(oil.evidence_ids, ("service-old",))
        self.assertEqual(self.run_tool("get_service_history", {"service_type": "missing"}).error, "unavailable_evidence")

    def test_active_codes_keep_recorded_flags_and_stable_references(self) -> None:
        result = self.run_tool("get_active_diagnostic_codes")
        self.assertEqual([item["code"] for item in result.data["records"]], ["EXAMPLE-B"])
        self.assertTrue(result.data["records"][0]["active"])
        self.assertEqual(result, self.run_tool("get_active_diagnostic_codes"))
        self.assertEqual(result.evidence_ids[0], result.data["records"][0]["evidence_id"])

    def test_no_inputs_mutated_and_result_changes_are_detached(self) -> None:
        before_snapshot, before_context = deepcopy(self.snapshot), deepcopy(self.context)
        for definition in list_tools():
            self.run_tool(definition.tool_id)
        self.assertEqual(before_snapshot, self.snapshot)
        self.assertEqual(before_context, self.context)
        result = self.run_tool("get_service_history")
        result.data["records"][0]["notes"] = "changed"
        observation_result = self.run_tool("get_coolant_history")
        observation_result.data["observations"][0]["value"] = -999
        self.assertEqual(before_snapshot, self.snapshot)
        self.assertEqual(before_context, self.context)

    def test_shared_tool_works_under_each_pack(self) -> None:
        results = []
        for pack in ("tires", "trip_readiness"):
            loaded = load_capabilities([pack])
            results.append(execute_tool("get_tire_pressure_history", {}, self.snapshot, allowed_tool_ids=loaded.tool_ids))
        self.assertTrue(results[0].success)
        self.assertEqual(*results)

    def test_trip_aggregation_is_partial_evidence_without_verdict(self) -> None:
        episode, _ = generate_episode("gradual_tire_pressure_loss", 42)
        result = execute_tool("get_trip_readiness_evidence", {}, freeze_episode(episode), allowed_tool_ids=("get_trip_readiness_evidence",))
        self.assertTrue(result.success)
        self.assertEqual(set(result.data["missing_categories"]), {"battery", "cooling", "service_history"})
        self.assertEqual(set(result.data), {"profile", "tires", "battery", "cooling", "service_records", "missing_categories"})
        for forbidden in ("safe to drive", "unsafe", "diagnosis", "repair", "safety_disposition", "ScenarioTruth", "gradual_tire_pressure_loss"):
            self.assertNotIn(forbidden, json.dumps(asdict(result)))

    def test_no_telemetry_still_allows_ownership_tools(self) -> None:
        empty = replace(self.snapshot, observations=())
        for tool in ("get_vehicle_profile", "get_service_history", "get_maintenance_odometer_context"):
            self.assertTrue(self.run_tool(tool, snapshot=empty).success)
        result = execute_tool("get_coolant_history", {}, empty, allowed_tool_ids=("get_coolant_history",))
        self.assertEqual(result.error, "unavailable_evidence")

    def test_missing_context_and_missing_requested_evidence_fail_explicitly(self) -> None:
        result = execute_tool("get_service_history", {}, self.snapshot, allowed_tool_ids=("get_service_history",))
        self.assertEqual(result.error, "unavailable_evidence")
        self.assertEqual(result.data, {})

    def test_mismatched_context_rejected(self) -> None:
        context = replace(self.context, profile=replace(self.context.profile, vehicle_id="other-vehicle"))
        self.assertEqual(self.run_tool("get_vehicle_profile", context=context).error, "context_profile_mismatch")

    def test_conflicting_ids_and_naive_context_timestamps_rejected(self) -> None:
        context = deepcopy(self.context)
        context.observations.append(replace(self.snapshot.observations[0], value=999))
        self.assertEqual(self.run_tool("get_coolant_history", context=context).error, "invalid_evidence")
        context.observations = [replace(self.snapshot.observations[0], timestamp=self.cutoff.replace(tzinfo=None))]
        self.assertEqual(self.run_tool("get_coolant_history", context=context).error, "invalid_evidence")

    def test_simulation_episode_and_truth_cannot_be_tool_input(self) -> None:
        _, truth = generate_episode("healthy_vehicle", 42)
        for invalid in (self.episode, truth):
            result = execute_tool("get_vehicle_profile", {}, invalid, allowed_tool_ids=("get_vehicle_profile",))
            self.assertEqual(result.error, "invalid_snapshot")

    def test_tool_module_has_no_simulator_import(self) -> None:
        tree = ast.parse(inspect.getsource(tools_module))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
            elif isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
        self.assertFalse(any("simulator" in name for name in imports))


if __name__ == "__main__":
    unittest.main()
