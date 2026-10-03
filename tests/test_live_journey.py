"""Offline checks of the explicit, credit-bounded stateful journey runner."""

from argparse import Namespace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from carmind.live_journey import (BudgetExceeded, CallBudget, CountedPlanner,
                                  CountedRouter, main)
from carmind.planner_provider import FakePlannerProvider
from carmind.router_provider import FakeCapabilityRouter
from carmind.routing import ExecutionMode, run_assessment
from carmind.simulator import freeze_episode, generate_episode


class LiveJourneyTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name) / "carmind-eval-test.sqlite3"
        self.state_path = self.db.with_suffix(".state.json")
        self.artifact_path = self.db.with_suffix(".jsonl")

    def test_dry_run_passes_all_checkpoints_without_real_providers(self):
        with patch("carmind.live_journey.XAIPlannerProvider", side_effect=AssertionError("xAI constructed")), \
             patch("carmind.live_journey.JevCapabilityRouter", side_effect=AssertionError("Jev constructed")), \
             patch("builtins.print"):
            self.assertEqual(main(["--db-path", str(self.db), "--retain-db"]), 0)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        records = [json.loads(line) for line in self.artifact_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(state["completed"], list(range(9)))
        self.assertEqual(state["failed"], [])
        self.assertEqual((state["counts"]["jev"], state["counts"]["xai"]), (0, 0))
        self.assertEqual([item["status"] for item in records], ["passed"] * 9)
        self.assertEqual(records[6]["safety"], "STOP_WHEN_SAFE")
        self.assertEqual(records[7]["safety"], "STOP_WHEN_SAFE")
        self.assertTrue(records[6]["simulated_evidence_supplied"])
        self.assertFalse(records[7]["simulated_evidence_supplied"])
        self.assertIn("tool_ids_used", records[7])
        self.assertNotIn("simulated_evidence", records[7])
        self.assertIn("15000 km", records[8]["response_text"])
        self.assertEqual(records[2]["planner"], None)
        self.assertEqual(records[4]["routing"], None)
        artifact = self.artifact_path.read_text(encoding="utf-8")
        self.assertNotIn("ScenarioTruth", artifact)
        self.assertNotIn("Authorization", artifact)
        self.assertTrue(self.db.is_file())

    def test_resume_requires_marker_and_refuses_replayed_checkpoint(self):
        with patch("builtins.print"):
            self.assertEqual(main(["--checkpoint", "1", "--db-path", str(self.db)]), 0)
            self.assertEqual(main(["--checkpoint", "2", "--db-path", str(self.db),
                                   "--reuse-evaluation-db"]), 0)
            self.assertEqual(main(["--checkpoint", "1", "--db-path", str(self.db),
                                   "--reuse-evaluation-db"]), 1)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["completed"], [0, 1, 2])
        self.assertEqual(state["counts"]["xai"], 0)
        self.assertEqual(len(self.artifact_path.read_text(encoding="utf-8").splitlines()), 3)

    def test_live_requires_ack_before_provider_or_database_creation(self):
        with patch("carmind.live_journey.XAIPlannerProvider", side_effect=AssertionError("xAI constructed")), \
             patch("carmind.live_journey.JevCapabilityRouter", side_effect=AssertionError("Jev constructed")), \
             patch("builtins.print"):
            self.assertEqual(main(["--live", "--checkpoint", "1", "--db-path", str(self.db)]), 1)
        self.assertFalse(self.db.exists())
        self.assertFalse(self.state_path.exists())

    def test_unmarked_existing_db_is_never_reused_or_cleaned(self):
        self.db.write_bytes(b"unrelated private data")
        with patch("builtins.print"):
            self.assertEqual(main(["--checkpoint", "1", "--db-path", str(self.db),
                                   "--reuse-evaluation-db"]), 1)
            self.assertEqual(main(["--cleanup", "--db-path", str(self.db)]), 1)
        self.assertEqual(self.db.read_bytes(), b"unrelated private data")

    def test_saved_dry_journey_cannot_become_live(self):
        with patch("builtins.print"):
            self.assertEqual(main(["--checkpoint", "1", "--db-path", str(self.db)]), 0)
            with patch("carmind.live_journey.missing_jev", return_value=[]), \
                 patch("carmind.live_journey.missing_xai", return_value=[]), \
                 patch("carmind.live_journey.XAIPlannerProvider", side_effect=AssertionError("xAI constructed")), \
                 patch("carmind.live_journey.JevCapabilityRouter", side_effect=AssertionError("Jev constructed")):
                self.assertEqual(main(["--live", "--confirm-live-api-use", "--checkpoint", "2",
                                       "--db-path", str(self.db), "--reuse-evaluation-db"]), 1)
        self.assertEqual(json.loads(self.state_path.read_text(encoding="utf-8"))["completed"], [0, 1])

    def test_failed_checkpoint_has_supported_retry_without_resetting_prior_state(self):
        with patch("carmind.live_journey._dry_script", return_value=[]), patch("builtins.print"):
            self.assertEqual(main(["--checkpoint", "1", "--db-path", str(self.db)]), 1)
        failed_state = json.loads(self.state_path.read_text(encoding="utf-8"))
        original_counts = dict(failed_state["counts"])
        with patch("builtins.print"):
            self.assertEqual(main(["--checkpoint", "1", "--db-path", str(self.db),
                                   "--reuse-evaluation-db", "--retry-failed-checkpoint"]), 0)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        records = [json.loads(line) for line in self.artifact_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(state["completed"], [0, 1])
        self.assertEqual(state["failed"], [])
        self.assertEqual(state["retry_counts"]["1"], 1)
        self.assertEqual(state["retry_history"][0]["checkpoint"], 1)
        self.assertEqual(state["counts"], original_counts)
        self.assertEqual([record["status"] for record in records], ["passed", "failed", "passed"])

    def test_checkpoint_seven_retry_reloads_stop_and_accepts_safe_incomplete_planner(self):
        with patch("builtins.print"):
            for checkpoint in range(1, 7):
                args = ["--checkpoint", str(checkpoint), "--db-path", str(self.db)]
                if checkpoint > 1:
                    args.append("--reuse-evaluation-db")
                self.assertEqual(main(args), 0)

            # Emulate an interrupted first attempt after the application has
            # produced its result. The saved result and database remain intact.
            with patch("carmind.live_journey._has_unresolved_stop_contract", return_value=False):
                self.assertEqual(main(["--checkpoint", "7", "--db-path", str(self.db),
                                       "--reuse-evaluation-db"]), 1)

            self.assertEqual(main(["--checkpoint", "7", "--db-path", str(self.db),
                                   "--reuse-evaluation-db", "--retry-failed-checkpoint"]), 0)

        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        records = [json.loads(line) for line in self.artifact_path.read_text(encoding="utf-8").splitlines()]
        retry = records[-1]
        self.assertEqual(state["completed"], list(range(8)))
        self.assertEqual(state["failed"], [])
        self.assertEqual(state["retry_counts"]["7"], 1)
        self.assertEqual(retry["safety"], "STOP_WHEN_SAFE")
        self.assertEqual(retry["safety_continuity"], {
            "current": "UNDETERMINED", "prior_unresolved_stop": "STOP_WHEN_SAFE",
            "effective": "STOP_WHEN_SAFE", "stop_guidance_applied": True,
        })
        self.assertEqual(retry["call_counts"]["jev"], 0)
        self.assertEqual(retry["call_counts"]["xai"], 0)

    def test_provider_budgets_reserve_before_call_and_block_second_call(self):
        state = {"counts": {"jev": 0, "xai": 0, "live_turns": 0,
                            "known_input_tokens": None, "known_output_tokens": None,
                            "known_cached_input_tokens": None, "unknown_usage_calls": 0}}
        limits = Namespace(max_live_turns=1, max_jev_calls=1, max_xai_calls=1,
                           max_calls_per_turn=1)
        budget = CallBudget(state, self.state_path, limits)
        planner = FakePlannerProvider([{"type": "final", "assessment": {}}])
        routed = FakeCapabilityRouter(selected=["tires"])
        budget.start_turn()
        CountedRouter(routed, budget).route({}, [])
        wrapped = CountedPlanner(planner, budget)
        wrapped.generate([])
        with self.assertRaises(BudgetExceeded):
            wrapped.generate([])
        self.assertEqual((state["counts"]["jev"], state["counts"]["xai"],
                          len(planner.requests), len(routed.requests)), (1, 1, 1, 1))
        self.assertEqual(json.loads(self.state_path.read_text(encoding="utf-8"))["counts"]["xai"], 1)

    def test_counted_provider_budget_integrates_without_phantom_planner_call(self):
        state = {"counts": {"jev": 0, "xai": 0, "live_turns": 0,
                            "known_input_tokens": None, "known_output_tokens": None,
                            "known_cached_input_tokens": None, "unknown_usage_calls": 0}}
        limits = Namespace(max_live_turns=1, max_jev_calls=1, max_xai_calls=2,
                           max_calls_per_turn=2)
        budget = CallBudget(state, self.state_path, limits)
        budget.start_turn()
        provider = FakePlannerProvider([
            {"type": "tool_call", "tool_id": "get_tire_pressure_summary",
             "arguments": {"wheel": "rear_left"}},
            {"type": "tool_call", "tool_id": "get_tire_pressure_history",
             "arguments": {"wheel": "rear_left"}},
            {"type": "final", "assessment": {}},
        ])
        episode = freeze_episode(generate_episode("gradual_tire_pressure_loss", 42)[0])
        run = run_assessment(episode, CountedPlanner(provider, budget), mode=ExecutionMode.ROUTED,
                             router=FakeCapabilityRouter(selected=["tires"]), max_model_calls=4)
        trace = run.planner.trace
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(state["counts"]["xai"], 2)
        self.assertEqual(trace.planner_call_count, 2)
        self.assertEqual(len(trace.provider_calls), 2)
        self.assertTrue(all(call["success"] for call in trace.provider_calls))
        self.assertEqual(trace.stop_reason, "call_budget_exhausted")
        self.assertIsNone(trace.provider_error)
        self.assertEqual(run.planner.result.safety.disposition.value, "STOP_WHEN_SAFE")

    def test_failed_checkpoint_keeps_safe_trace_and_never_reports_success(self):
        with patch("carmind.live_journey._dry_script", return_value=[]), patch("builtins.print"):
            self.assertEqual(main(["--checkpoint", "1", "--db-path", str(self.db)]), 1)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        records = [json.loads(line) for line in self.artifact_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(state["completed"], [0])
        self.assertEqual(state["failed"], [1])
        self.assertEqual(len(records), 2)
        self.assertEqual(records[1]["status"], "failed")
        self.assertEqual(records[1]["planner"]["completion_status"], "incomplete")
        self.assertEqual(state["counts"]["xai"], 0)


if __name__ == "__main__":
    unittest.main()
