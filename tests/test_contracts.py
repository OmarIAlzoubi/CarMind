"""Tests for CarMind's initial data contracts."""

from dataclasses import fields
from datetime import datetime, timezone
import inspect
import unittest

from carmind.contracts import (
    Assessment,
    ConversationContext,
    DiagnosticCodeRecord,
    MaintenanceRecord,
    Observation,
    ObservationSource,
    SafetyDisposition,
    UserMessage,
    VehicleContext,
    VehicleProfile,
)


class ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.timestamp = datetime(2026, 9, 28, tzinfo=timezone.utc)
        self.profile = VehicleProfile("vehicle-1", "Example", "Compact", 2024)

    def test_valid_contracts(self) -> None:
        message = UserMessage("message-1", "My car is making a noise", self.timestamp)
        maintenance = MaintenanceRecord("record-1", "Oil service", self.timestamp, 12000.0)
        code = DiagnosticCodeRecord("P0001", self.timestamp, "owner", False)
        observation = Observation(
            "observation-1", "noise", "rattle", None, self.timestamp, ObservationSource.USER
        )
        context = VehicleContext(self.profile, [maintenance], [code], [observation])
        conversation = ConversationContext([message])
        assessment = Assessment(
            observations=["Owner reports a rattle"],
            hypotheses=["Cause is unknown"],
            evidence_ids=[observation.observation_id],
            uncertainties=["Location of noise is unknown"],
            safety_disposition=SafetyDisposition.UNDETERMINED,
            recommended_action_ids=[],
            limitations=["User-provided information only"],
        )
        self.assertIsNone(self.profile.vin)
        self.assertIsNone(self.profile.mileage_km)
        self.assertEqual(context.maintenance_records, [maintenance])
        self.assertEqual(context.diagnostic_codes, [code])
        self.assertEqual(context.observations, [observation])
        self.assertEqual(conversation.recent_messages, [message])
        self.assertEqual(assessment.evidence_ids, ["observation-1"])

    def test_optional_vehicle_and_maintenance_fields(self) -> None:
        profile = VehicleProfile("v", "Example", "Compact", 2024, 0.0, "Example engine", "VIN")
        record = MaintenanceRecord("r", "Inspection", self.timestamp, 0.0, "Completed")
        self.assertEqual(profile.mileage_km, 0.0)
        self.assertEqual(profile.engine, "Example engine")
        self.assertEqual(profile.vin, "VIN")
        self.assertEqual(record.odometer_km, 0.0)
        self.assertEqual(record.notes, "Completed")
        self.assertIsNone(MaintenanceRecord("r2", "Inspection", self.timestamp).odometer_km)

    def test_observation_value_types_and_sources(self) -> None:
        for source in ObservationSource:
            for value in ("rattle", 100, 12.5, True):
                with self.subTest(source=source, value=value):
                    observation = Observation("o", "signal", value, None, self.timestamp, source)
                    self.assertEqual(observation.value, value)
                    self.assertIs(observation.source, source)

    def test_collection_defaults_are_independent(self) -> None:
        pairs = [
            (VehicleContext(self.profile), VehicleContext(self.profile)),
            (ConversationContext(), ConversationContext()),
            (Assessment(), Assessment()),
        ]
        for first, second in pairs:
            for contract_field in fields(first):
                first_value = getattr(first, contract_field.name)
                if isinstance(first_value, list):
                    with self.subTest(contract=type(first).__name__, field=contract_field.name):
                        second_value = getattr(second, contract_field.name)
                        self.assertEqual(first_value, [])
                        self.assertIsNot(first_value, second_value)
                        first_value.append("test mutation")
                        self.assertEqual(second_value, [])

    def test_blank_user_message_rejected(self) -> None:
        for text in ("", " ", "\t\n"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                UserMessage("m", text, self.timestamp)

    def test_negative_vehicle_mileage_rejected(self) -> None:
        with self.assertRaises(ValueError):
            VehicleProfile("v", "Example", "Compact", 2024, mileage_km=-0.1)

    def test_negative_maintenance_odometer_rejected(self) -> None:
        with self.assertRaises(ValueError):
            MaintenanceRecord("r", "Oil service", self.timestamp, odometer_km=-0.1)

    def test_nonpositive_year_rejected(self) -> None:
        for year in (0, -1):
            with self.subTest(year=year), self.assertRaises(ValueError):
                VehicleProfile("v", "Example", "Compact", year)

    def test_blank_required_identifiers_rejected(self) -> None:
        constructors = [
            lambda identifier: VehicleProfile(identifier, "Example", "Compact", 2024),
            lambda identifier: UserMessage(identifier, "Hello", self.timestamp),
            lambda identifier: MaintenanceRecord(identifier, "Service", self.timestamp),
            lambda identifier: DiagnosticCodeRecord(identifier, self.timestamp, "owner", True),
            lambda identifier: Observation(
                identifier, "noise", "rattle", None, self.timestamp, ObservationSource.USER
            ),
        ]
        for index, constructor in enumerate(constructors):
            for identifier in ("", " \t"):
                with self.subTest(constructor=index, identifier=identifier), self.assertRaises(ValueError):
                    constructor(identifier)

    def test_assessment_defaults_to_undetermined(self) -> None:
        self.assertIs(Assessment().safety_disposition, SafetyDisposition.UNDETERMINED)

    def test_no_rule_triggered_is_not_driving_clearance(self) -> None:
        self.assertEqual(SafetyDisposition.NO_RULE_TRIGGERED.value, "NO_RULE_TRIGGERED")
        self.assertEqual(
            set(SafetyDisposition.__members__),
            {"NO_RULE_TRIGGERED", "SERVICE_REVIEW", "STOP_WHEN_SAFE", "UNDETERMINED"},
        )
        enum_source = inspect.getsource(SafetyDisposition).lower()
        self.assertNotIn("safe to drive", enum_source)
        self.assertIn("not a determination of driving safety", enum_source)


if __name__ == "__main__":
    unittest.main()
