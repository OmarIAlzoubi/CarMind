"""Manifest validation and dynamic exposure tests."""

from dataclasses import asdict
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from carmind.capabilities import (
    CapabilityRegistry, DEFAULT_CAPABILITIES_DIRECTORY,
    list_capabilities, load_all_capabilities, load_capabilities,
)
from carmind.tools import TOOL_CATALOG


class CapabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = CapabilityRegistry()

    def test_exact_ten_packs_validate(self) -> None:
        packs = list_capabilities()
        self.assertEqual({pack.id for pack in packs}, {
            "engine", "cooling", "battery", "electrical", "tires", "fuel_economy",
            "maintenance", "service_history", "diagnostic_codes", "trip_readiness",
        })
        self.assertEqual(len(packs), 10)
        self.assertEqual(len({pack.id for pack in packs}), 10)
        for pack in packs:
            self.assertEqual(pack.version, 1)
            self.assertTrue(pack.routing_description.strip())
            self.assertTrue(pack.planner_instructions.strip())
            self.assertTrue(pack.required_inputs)
            self.assertTrue(set(pack.tool_ids) <= TOOL_CATALOG.keys())

    def test_narrow_selection_exposes_exact_pack_tools(self) -> None:
        loaded = load_capabilities(["tires"])
        self.assertEqual(tuple(pack.id for pack in loaded.packs), ("tires",))
        self.assertEqual(set(loaded.tool_ids), {
            "get_vehicle_profile", "get_tire_pressure_history", "get_tire_pressure_summary",
        })

    def test_multiple_packs_deduplicate_shared_tools(self) -> None:
        loaded = load_capabilities(["tires", "trip_readiness"])
        self.assertEqual(len(loaded.tool_ids), len(set(loaded.tool_ids)))
        self.assertEqual(loaded.tool_ids.count("get_tire_pressure_history"), 1)
        self.assertEqual(set(loaded.tool_ids), {tool for pack in loaded.packs for tool in pack.tool_ids})

    def test_full_context_has_more_tools_and_instructions(self) -> None:
        full, narrow = load_all_capabilities(), load_capabilities(["tires"])
        self.assertEqual(full.capability_count, 10)
        self.assertEqual(full.tool_count, 25)
        self.assertGreater(full.tool_count, narrow.tool_count)
        self.assertGreater(full.instruction_character_count, narrow.instruction_character_count)
        self.assertEqual(full.instruction_character_count, len(full.planner_instructions))

    def test_ordering_deterministic_with_reordered_and_repeated_ids(self) -> None:
        packs = self.registry.list_capabilities()
        self.assertEqual([pack.id for pack in packs], sorted(pack.id for pack in packs))
        first = load_capabilities(["maintenance", "fuel_economy", "maintenance"])
        second = load_capabilities(["fuel_economy", "maintenance"])
        self.assertEqual(first, second)
        self.assertEqual(first.tool_ids, tuple(sorted(first.tool_ids)))

    def test_all_exposed_tools_read_only(self) -> None:
        self.assertTrue(all(tool.read_only for tool in load_all_capabilities().tools))

    def test_unknown_capability_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown capabilities"):
            load_capabilities(["tires", "not_a_pack"])

    def test_invalid_selection_type_fails(self) -> None:
        for value in ("tires", [None], 42):
            with self.subTest(value=value), self.assertRaises(ValueError):
                load_capabilities(value)

    def test_empty_selection_has_no_exposure(self) -> None:
        loaded = load_capabilities([])
        self.assertEqual((loaded.capability_count, loaded.tool_count, loaded.instruction_character_count), (0, 0, 0))

    def test_additional_loading_preserves_original_and_deduplicates(self) -> None:
        initial = self.registry.load_capabilities(["engine", "fuel_economy"])
        expanded = self.registry.load_additional(initial, ["electrical", "engine"])
        self.assertEqual(initial.capability_count, 2)
        self.assertEqual(expanded, load_capabilities(["engine", "fuel_economy", "electrical"]))

    def test_router_view_contains_descriptions_only(self) -> None:
        view = self.registry.routing_descriptions()
        self.assertEqual(view, {pack.id: pack.routing_description for pack in list_capabilities()})
        self.assertNotIn("planner_instructions", json.dumps(view))
        view.clear()
        self.assertEqual(len(self.registry.routing_descriptions()), 10)

    def test_loaded_data_has_no_scenario_truth(self) -> None:
        text = json.dumps(asdict(load_all_capabilities()))
        for label in ("ScenarioTruth", "scenario_id", "healthy_vehicle", "sustained_temperature_rise", "weak_battery_start", "gradual_tire_pressure_loss", "increased_fuel_consumption"):
            self.assertNotIn(label, text)

    def test_invalid_manifests_rejected(self) -> None:
        valid = json.loads((DEFAULT_CAPABILITIES_DIRECTORY / "tires" / "manifest.json").read_text())
        mutations = (
            {"version": 2}, {"version": True}, {"id": " "},
            {"routing_description": ""}, {"planner_instructions": " \n"},
            {"tool_ids": ["unknown"]}, {"tool_ids": "get_vehicle_profile"},
            {"tool_ids": ["get_vehicle_profile", "get_vehicle_profile"]},
            {"required_inputs": [None]},
        )
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "pack"
            path.mkdir()
            manifest = path / "manifest.json"
            for mutation in mutations:
                with self.subTest(mutation=mutation):
                    manifest.write_text(json.dumps(valid | mutation))
                    with self.assertRaises(ValueError):
                        CapabilityRegistry(Path(temporary))
            for field in valid:
                with self.subTest(missing=field):
                    manifest.write_text(json.dumps({key: value for key, value in valid.items() if key != field}))
                    with self.assertRaises(ValueError):
                        CapabilityRegistry(Path(temporary))
            for malformed in ("{", "[]", "null"):
                manifest.write_text(malformed)
                with self.assertRaises(ValueError):
                    CapabilityRegistry(Path(temporary))

    def test_duplicate_capability_ids_rejected(self) -> None:
        contents = (DEFAULT_CAPABILITIES_DIRECTORY / "tires" / "manifest.json").read_text()
        with TemporaryDirectory() as temporary:
            for name in ("one", "two"):
                directory = Path(temporary) / name
                directory.mkdir()
                (directory / "manifest.json").write_text(contents)
            with self.assertRaisesRegex(ValueError, "Duplicate capability ID"):
                CapabilityRegistry(Path(temporary))

    def test_missing_registry_directory_fails(self) -> None:
        with TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "No capability manifests"):
                CapabilityRegistry(Path(temporary))


if __name__ == "__main__":
    unittest.main()
