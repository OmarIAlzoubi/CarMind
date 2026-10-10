"""Offline acceptance of the shared semantic planner inside persistent CarMindApp."""

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from carmind.capabilities import CapabilityRegistry
from carmind.benchmark import routing_metrics
from carmind.composition import compose_app
from carmind.contracts import SafetyDisposition, UserMessage
from carmind.evidence import FrozenEvidenceSnapshot
from carmind.live_journey import _has_unresolved_stop_contract
from carmind.ownership import parse_command
from carmind.ownership_demo import command_turn, empty_final
from carmind.planner_provider import FakePlannerProvider
from carmind.router_provider import FakeCapabilityRouter
from carmind.routing import ExecutionMode
from carmind.simulator import freeze_episode, generate_episode
from carmind.storage import OwnershipStore


NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)


class StatefulIntelligenceTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = OwnershipStore(Path(temporary.name) / "state.sqlite3")
        self.addCleanup(self.store.close)
        self.provider = FakePlannerProvider([])
        self.router = FakeCapabilityRouter(selected=["service_history"])
        self.app = compose_app(self.store, provider=self.provider, router=self.router)
        self.owner = self.app.create_owner(now=NOW, owner_id="owner")
        profile = replace(freeze_episode(generate_episode("healthy_vehicle", 42)[0]).profile, mileage_km=None)
        self.vehicle_id = profile.vehicle_id
        self.app.add_vehicle(self.owner, profile, now=NOW)
        self.session = self.app.start_session(self.owner, now=NOW, vehicle_id=self.vehicle_id)
        self.serial = 0

    def turn(self, text, script, *, snapshot=None, router=None):
        self.serial += 1
        self.provider = FakePlannerProvider(script)
        self.app.provider = self.provider
        if router is not None:
            self.app.router = router
        message = UserMessage(f"turn-{self.serial}", text, NOW)
        return self.app.handle_message(self.owner, self.session, message, now=NOW, snapshot=snapshot)

    def confirm(self, proposal):
        self.serial += 1
        message = UserMessage(f"turn-{self.serial}", "I confirm the displayed change.", NOW)
        return self.app.handle_message(self.owner, self.session, message, now=NOW,
                                       confirmation_id=proposal.proposal_id)

    def service(self, text="I changed the engine oil today at 15,000 km."):
        result = self.turn(text, [command_turn("record_service_event", {
            "service_type": "oil", "performed_at": NOW.isoformat(), "odometer": 15000,
            "unit": "km"}, text)])
        self.assertEqual(result.status, "confirmation_required")
        return result.proposed_commands[0]

    def test_real_composition_is_explicit_and_uses_same_app(self):
        offline = compose_app(self.store)
        self.assertEqual(offline.mode, ExecutionMode.FULL)
        self.assertIsInstance(offline.provider, FakePlannerProvider)
        with patch("carmind.composition.XAIPlannerProvider") as xai, patch("carmind.composition.JevCapabilityRouter") as jev:
            live = compose_app(self.store, live=True, timeout_seconds=9)
        self.assertEqual(live.mode, ExecutionMode.ROUTED)
        self.assertIs(type(live), type(offline))
        xai.assert_called_once_with(timeout_seconds=9)
        jev.assert_called_once_with(timeout_seconds=9)

    def test_service_proposal_confirmation_and_replay(self):
        proposal = self.service()
        self.assertEqual(self.store.service_records(self.vehicle_id, NOW), [])
        self.assertEqual(self.confirm(proposal).status, "applied")
        again = self.confirm(proposal)
        self.assertTrue(again.trace.replayed)
        self.assertEqual(len(self.store.service_records(self.vehicle_id, NOW)), 1)
        self.assertEqual(len(self.router.requests), 1)
        self.assertEqual(len(self.provider.requests), 1)

    def test_unrelated_diagnostic_turn_does_not_change_pending_service_proposal(self):
        proposal = self.service()
        tire = freeze_episode(generate_episode("gradual_tire_pressure_loss", 42)[0])
        offset = NOW - tire.assessment_at
        snapshot = FrozenEvidenceSnapshot("intervening-tire", self.store.vehicle(self.owner, self.vehicle_id, NOW).profile,
                                          UserMessage("tire", "A tire is losing pressure.", NOW), NOW,
                                          tuple(replace(item, timestamp=item.timestamp + offset)
                                                for item in tire.observations))
        self.turn("A tire is losing pressure.", [empty_final()], snapshot=snapshot,
                  router=FakeCapabilityRouter(selected=["tires"]))
        self.assertFalse(self.store.service_records(self.vehicle_id, NOW))
        self.assertEqual(self.confirm(proposal).status, "applied")
        self.assertEqual(len(self.store.service_records(self.vehicle_id, NOW)), 1)

    def test_other_session_cannot_confirm_proposal(self):
        proposal = self.service()
        second = self.app.start_session(self.owner, now=NOW, vehicle_id=self.vehicle_id)
        message = UserMessage("other-session-confirm", "I confirm that change.", NOW)
        result = self.app.handle_message(self.owner, second, message, now=NOW,
                                         confirmation_id=proposal.proposal_id)
        self.assertEqual(result.status, "error")
        self.assertFalse(self.store.service_records(self.vehicle_id, NOW))
        self.assertEqual(self.confirm(proposal).status, "applied")

    def test_ambiguous_statement_asks_one_question_without_proposal(self):
        result = self.turn("I think they may have changed the oil.", [
            {"type": "request_clarification", "question": "Was the engine oil actually changed, and when?"}])
        self.assertEqual(result.status, "clarification_required")
        self.assertIn("actually changed", result.response)
        self.assertFalse(result.proposed_commands)
        self.assertFalse(self.store.service_records(self.vehicle_id, NOW))
        self.assertEqual(result.trace.planner.planner_call_count, 1)

    def test_invalid_clarification_repairs_and_does_not_write(self):
        result = self.turn("Maybe my mileage is around 18k.", [
            {"type": "request_clarification", "question": "Record exactly 18,000 km."},
            {"type": "request_clarification", "question": "What exact odometer reading and unit do you see?"}])
        self.assertEqual(result.status, "clarification_required")
        self.assertEqual(result.trace.planner.repair_status, "repair_succeeded")
        self.assertFalse(self.store.odometer_events(self.vehicle_id))

    def test_unsupported_hidden_and_sql_actions_never_mutate(self):
        text = "My mileage changed."
        hidden = command_turn("update_odometer", {"reading": 100, "unit": "km",
                              "occurred_at": NOW.isoformat()}, text)
        hidden["command"]["arguments"]["sql"] = "DROP TABLE services"
        for bad in (hidden, {"type": "propose_command", "command": {"kind": "complete_reminder"}},
                    "DELETE FROM services", {"type": "tool_call", "tool_id": "write_service", "arguments": {}}):
            with self.subTest(bad=bad):
                result = self.turn(text, [bad, empty_final()])
                self.assertFalse(result.proposed_commands)
                self.assertFalse(self.store.service_records(self.vehicle_id, NOW))
                self.assertFalse(self.store.odometer_events(self.vehicle_id))

    def test_router_failure_falls_back_without_losing_write_confirmation(self):
        text = "I changed the engine oil today at 15,000 km."
        failed_router = FakeCapabilityRouter(RuntimeError("private router details"))
        result = self.turn(text, [command_turn("record_service_event", {
            "service_type": "oil", "performed_at": NOW.isoformat(), "odometer": 15000,
            "unit": "km"}, text)], router=failed_router)
        self.assertEqual(result.status, "confirmation_required")
        self.assertTrue(result.trace.routing.fallback_used)
        self.assertEqual(result.trace.routing.fallback_reason, "router_failure")
        self.assertEqual(result.trace.planner.exposed_capability_count, 11)
        self.assertFalse(self.store.service_records(self.vehicle_id, NOW))
        self.assertEqual(self.confirm(result.proposed_commands[0]).status, "applied")

    def test_provider_failure_before_or_after_tool_does_not_write(self):
        for script in ([RuntimeError("private details")],
                       [{"type": "tool_call", "tool_id": "get_vehicle_profile", "arguments": {}},
                        RuntimeError("private details")]):
            with self.subTest(script=script):
                result = self.turn("Tell me about the car.", script)
                self.assertEqual(result.status, "incomplete")
                self.assertFalse(self.store.service_records(self.vehicle_id, NOW))
                self.assertFalse(self.store.odometer_events(self.vehicle_id))
                self.assertNotIn("private", result.response)

    def test_service_read_uses_persisted_record_and_excludes_model_embellishment(self):
        proposal = self.service()
        self.confirm(proposal)
        record = self.store.service_records(self.vehicle_id, NOW)[0]
        final = empty_final()
        final["assessment"]["observations"] = [{
            "text": "Workshop Alpha used Brand X 0W-20 oil during your recorded oil service at 15000 km.",
            "evidence_ids": [record.record_id]}]
        result = self.turn("When did I last change the oil?", [
            {"type": "tool_call", "tool_id": "get_latest_service_record", "arguments": {"service_type": "oil"}}, final])
        self.assertEqual(result.status, "complete")
        self.assertIn("oil service on", result.response)
        self.assertIn("15000 km", result.response)
        self.assertNotIn("Workshop Alpha", result.response)
        self.assertNotIn("Brand X", result.response)
        self.assertEqual(result.assessment.assessment.evidence_ids, [record.record_id])

    def test_live_maintenance_unknown_does_not_invent_interval(self):
        final = empty_final()
        final["assessment"]["uncertainties"] = ["APPLICABILITY_UNVERIFIED"]
        result = self.turn("When is my next manufacturer service?", [final],
                           router=FakeCapabilityRouter(selected=["maintenance"]))
        self.assertEqual(result.maintenance_applicability, "UNKNOWN")
        self.assertIn("No verified maintenance schedule", result.response)
        self.assertFalse(self.store.reminders(self.vehicle_id))

    def test_arabic_owner_turn_uses_same_command_and_read_paths(self):
        text = "غيرت زيت المكينة اليوم على ١٥ ألف"
        proposal = self.service(text)
        before = self.turn("كم كان العداد وقت آخر تغيير زيت؟", [empty_final()])
        self.assertNotIn("15000", before.response)
        # The queried service is only a proposal at this point. Confirmation
        # then enables a citable read.
        self.assertFalse(self.store.service_records(self.vehicle_id, NOW))
        self.confirm(proposal)
        record = self.store.service_records(self.vehicle_id, NOW)[0]
        final = empty_final()
        final["assessment"]["observations"] = [{"text": "Recorded oil service at 15000 km.",
                                                 "evidence_ids": [record.record_id]}]
        answer = self.turn("كم كان العداد وقت آخر تغيير زيت؟", [
            {"type": "tool_call", "tool_id": "get_latest_service_record", "arguments": {"service_type": "oil"}}, final])
        self.assertEqual(answer.status, "complete")
        self.assertIn("15000 km", answer.response)

    def test_prior_stop_suppresses_three_model_clearance_variants(self):
        original = freeze_episode(generate_episode("gradual_tire_pressure_loss", 42)[0])
        offset = NOW - original.assessment_at
        observations = tuple(replace(o, timestamp=o.timestamp + offset) for o in original.observations)
        profile = self.store.vehicle(self.owner, self.vehicle_id, NOW).profile
        snapshot = FrozenEvidenceSnapshot("test-tire", profile,
                                          UserMessage("source", "One tire keeps losing pressure.", NOW), NOW,
                                          observations)
        self.turn("One tire keeps losing pressure.", [empty_final()], snapshot=snapshot,
                  router=FakeCapabilityRouter(selected=["tires"]))
        for phrase in ("You can probably keep driving.", "It sounds fine now.", "No need to stop."):
            candidate = empty_final()
            candidate["assessment"]["observations"] = [{"text": phrase, "evidence_ids": [self.vehicle_id]}]
            with self.subTest(phrase=phrase):
                result = self.turn("Can I keep driving?", [candidate, empty_final()],
                                   router=FakeCapabilityRouter(selected=["tires"]))
                self.assertEqual(result.safety.disposition, SafetyDisposition.STOP_WHEN_SAFE)
                self.assertEqual(result.trace.prior_stop_disposition, "STOP_WHEN_SAFE")
                self.assertEqual(result.trace.effective_safety_disposition, "STOP_WHEN_SAFE")
                self.assertTrue(result.trace.stop_guidance_applied)
                self.assertNotIn(phrase, result.response)
                self.assertIn("earlier stop warning remains unresolved", result.response)

    def test_current_prior_and_effective_safety_are_distinct_and_unrelated_query_does_not_clear(self):
        tire = freeze_episode(generate_episode("gradual_tire_pressure_loss", 42)[0])
        offset = NOW - tire.assessment_at
        tire = replace(tire, assessment_at=NOW,
                       observations=tuple(replace(item, timestamp=item.timestamp + offset)
                                          for item in tire.observations))
        self.turn("One tire keeps losing pressure.", [empty_final()], snapshot=tire,
                  router=FakeCapabilityRouter(selected=["tires"]))
        result = self.turn("When did I last change the oil?", [empty_final()],
                           router=FakeCapabilityRouter(selected=["service_history"]))
        self.assertEqual(result.trace.current_safety_disposition, "UNDETERMINED")
        self.assertEqual(result.trace.prior_stop_disposition, "STOP_WHEN_SAFE")
        self.assertEqual(result.trace.effective_safety_disposition, "STOP_WHEN_SAFE")
        self.assertEqual(result.safety.disposition, SafetyDisposition.STOP_WHEN_SAFE)
        self.assertTrue(result.trace.stop_guidance_applied)
        self.assertNotIn("you can keep driving", result.response.lower())

    def test_no_prior_stop_does_not_create_inherited_stop(self):
        result = self.turn("Tell me about my car.", [empty_final()])
        self.assertIsNone(result.trace.prior_stop_disposition)
        self.assertEqual(result.trace.effective_safety_disposition, "UNDETERMINED")
        self.assertEqual(result.safety.disposition, SafetyDisposition.UNDETERMINED)
        self.assertFalse(result.trace.stop_guidance_applied)

    def test_follow_up_router_sees_prior_tire_assessment_and_failed_planner_keeps_stop(self):
        original = freeze_episode(generate_episode("gradual_tire_pressure_loss", 42)[0])
        offset = NOW - original.assessment_at
        observations = tuple(replace(item, timestamp=item.timestamp + offset)
                             for item in original.observations)
        profile = self.store.vehicle(self.owner, self.vehicle_id, NOW).profile
        snapshot = FrozenEvidenceSnapshot("prior-tire", profile,
                                          UserMessage("prior-tire-message", "One tire keeps losing pressure.", NOW),
                                          NOW, observations)
        ids = [item.observation_id for item in observations if item.name == "rear_left_tire_pressure"]
        assessment = empty_final()
        assessment["assessment"]["observations"] = [{
            "text": "Rear-left tire pressure declined across the available readings.",
            "evidence_ids": ids,
        }]
        self.turn("One tire keeps losing pressure.", [
            {"type": "tool_call", "tool_id": "get_tire_pressure_summary",
             "arguments": {"wheel": "rear_left"}}, assessment,
        ], snapshot=snapshot, router=FakeCapabilityRouter(selected=["tires"]))

        follow_up_router = FakeCapabilityRouter(selected=["tires", "trip_readiness"])
        result = self.turn("Can I keep driving?", [RuntimeError("offline synthetic planner failure")],
                          router=follow_up_router)
        sent, _ = follow_up_router.requests[0]
        follow_context = sent["follow_up_context"]
        self.assertIn("One tire keeps losing pressure.", follow_context["recent_owner_messages"])
        self.assertIn("Rear-left tire pressure declined", follow_context["prior_validated_assessment"]["observations"][0])
        self.assertEqual(follow_context["unresolved_safety"]["disposition"], "STOP_WHEN_SAFE")
        self.assertEqual(set(result.trace.routing.raw_selected_capabilities), {"tires", "trip_readiness"})
        self.assertFalse(result.trace.routing.fallback_used)
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(result.safety.disposition, SafetyDisposition.STOP_WHEN_SAFE)
        self.assertTrue(_has_unresolved_stop_contract(result))
        self.assertIn("earlier stop warning remains unresolved", result.response)

    def test_proposal_display_contains_every_semantic_field(self):
        raw = command_turn("record_service_event", {"service_type": "oil", "performed_at": NOW.isoformat(),
                            "odometer": 15000, "unit": "km", "notes": "owner note",
                            "supersedes_id": "old-service"}, "I corrected my service.")
        command = parse_command(raw["command"], "I corrected my service.")
        description = self.app.describe_proposal(command)
        for expected in ("oil", NOW.isoformat(), "15000", "km", "owner note", "old-service"):
            self.assertIn(expected, description)
        for kind, argument, value in (("select_vehicle", "vehicle_id", "other-car"),
                                      ("acknowledge_reminder", "reminder_id", "reminder-123")):
            raw = command_turn(kind, {argument: value}, "Please update this.")
            self.assertIn(value, self.app.describe_proposal(parse_command(raw["command"], "Please update this.")))

    def test_ownership_routing_fixtures_are_separate_and_narrow(self):
        document = json.loads((Path(__file__).resolve().parents[1] / "eval" / "ownership_routing_cases.json").read_text(encoding="utf-8"))
        self.assertEqual(document["version"], 1)
        known = set(CapabilityRegistry().routing_descriptions())
        counts = []
        for case in document["cases"]:
            self.assertTrue(set(case["required"]) <= known)
            router = FakeCapabilityRouter(selected=case["required"])
            result = self.turn(case["message"], [empty_final()], router=router)
            loaded = set(result.trace.routing.effective_loaded_capabilities)
            required = set(case["required"])
            self.assertEqual(loaded, required)
            self.assertFalse(result.trace.routing.fallback_used)
            metrics = routing_metrics(result.trace.routing.raw_selected_capabilities, case["required"])
            self.assertEqual((metrics["required_recall"], metrics["selection_precision"],
                              metrics["exact_set_agreement"]), (1.0, 1.0, True))
            counts.append(len(loaded))
        self.assertLess(max(counts), 10)


if __name__ == "__main__":
    unittest.main()
