"""Resumable, credit-bounded stateful evaluation through the real CarMindApp.

Default execution is a scripted offline dry run. Live execution requires two
explicit flags and exactly one checkpoint per invocation.
"""

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
from enum import Enum
import json
import os
from pathlib import Path
import re
import subprocess
from tempfile import gettempdir
from uuid import uuid4

from carmind.composition import compose_app
from carmind.contracts import SafetyDisposition, UserMessage
from carmind.evidence import FrozenEvidenceSnapshot
from carmind.ownership_demo import command_turn, empty_final
from carmind.planner_provider import (FakePlannerProvider, XAIPlannerProvider,
                                      missing_configuration as missing_xai)
from carmind.router_provider import (FakeCapabilityRouter, JevCapabilityRouter,
                                     missing_configuration as missing_jev)
from carmind.simulator import freeze_episode, generate_episode
from carmind.storage import OwnershipStore
from carmind.provider_config import LIVE_ENV_NAMES, load_project_env


MARKER = "CARMIND_STATEFUL_EVALUATION_V1"
EVALUATION_DIRECTORY = Path(gettempdir()) / "carmind-evaluations"
def _load_project_env():
    """Load live-provider configuration from the project .env without overriding the shell."""
    load_project_env()
CHECKPOINT_MESSAGES = {
    1: "I changed the engine oil today at 15,000 km.",
    2: "I confirm the displayed engine-oil service.",
    3: "My odometer is now 18,500 km.",
    4: "I confirm the displayed odometer reading.",
    5: "When did I last change the engine oil?",
    6: "My rear-left tire keeps losing pressure.",
    7: "Can I keep driving?",
    8: "When did I last change the engine oil?",
}
ROUTED_PACKS = {1: ("service_history",), 3: ("maintenance",),
                5: ("service_history",), 6: ("tires",),
                7: ("tires",), 8: ("service_history",)}
PROVIDER_TURNS = frozenset(ROUTED_PACKS)


class JourneyFailure(RuntimeError):
    """A static, safe evaluation failure; no raw provider payloads."""


class BudgetExceeded(JourneyFailure):
    pass


def _json_default(value):
    if isinstance(value, (datetime, Path)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    if isinstance(value, Enum):
        return value.value
    raise TypeError("Unsupported evaluation artifact value")


def _redact(value, *, live=False):
    """Keep fixed evaluation artifacts free of configured credential values."""
    if isinstance(value, str):
        if live:
            for name in ("XAI_API_KEY", "TYPESAFE_API_KEY"):
                secret = os.environ.get(name)
                if secret:
                    value = value.replace(secret, "[REDACTED]")
        return re.sub(r"(?i)Bearer\s+[A-Za-z0-9._-]+", "[REDACTED]", value)
    if isinstance(value, dict):
        return {key: _redact(item, live=live) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item, live=live) for item in value]
    return value


def _paths(db_path):
    path = Path(db_path).resolve()
    if not re.fullmatch(r"carmind-eval-[A-Za-z0-9-]+\.sqlite3", path.name):
        raise JourneyFailure("Evaluation DB name must be carmind-eval-<id>.sqlite3.")
    if path.is_symlink():
        raise JourneyFailure("Evaluation DB symlinks are not accepted.")
    return path, path.with_suffix(".state.json"), path.with_suffix(".jsonl")


def _save_state(path, state):
    temporary = path.with_suffix(".state.tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True, default=_json_default), encoding="utf-8")
    temporary.replace(path)


def _git_identity():
    root = Path(__file__).resolve().parents[2]
    def read(*args):
        result = subprocess.run(("git", *args), cwd=root, capture_output=True, text=True, check=False)
        return result.stdout.strip() if result.returncode == 0 else None
    return read("rev-parse", "HEAD"), bool(read("status", "--porcelain"))


def _new_state(db_path):
    commit, dirty = _git_identity()
    run_id = str(uuid4())
    return {"marker": MARKER, "run_id": run_id, "db_path": str(db_path),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "code_commit": commit, "working_tree_dirty": dirty,
            "owner_id": "evaluation-owner-" + run_id[:8],
            "vehicle_id": "evaluation-vehicle-" + run_id[:8],
            "session_id": "evaluation-session-" + run_id[:8],
            "completed": [], "failed": [], "proposals": {}, "service_entity_id": None,
            "retry_counts": {}, "retry_history": [],
            "counts": {"jev": 0, "xai": 0, "live_turns": 0,
                       "known_input_tokens": None, "known_output_tokens": None,
                       "known_cached_input_tokens": None, "unknown_usage_calls": 0}}


def _load_state(db_path, state_path):
    if not state_path.is_file():
        raise JourneyFailure("Existing DB lacks evaluation metadata; it cannot be reused.")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("marker") != MARKER or state.get("db_path") != str(db_path):
        raise JourneyFailure("Evaluation metadata does not match this DB.")
    return state


class CallBudget:
    """Reserve and persist a credit before every SDK operation, including failures."""

    def __init__(self, state, state_path, args):
        self.state, self.state_path, self.args = state, state_path, args
        self.xai_this_turn = 0
        self.blocked_reason = None

    def start_turn(self):
        if self.state["counts"]["live_turns"] >= self.args.max_live_turns:
            self.blocked_reason = "live_turn_budget_exhausted"
            raise BudgetExceeded(self.blocked_reason)
        self.state["counts"]["live_turns"] += 1
        _save_state(self.state_path, self.state)

    @property
    def remaining_xai_calls(self):
        return max(0, min(self.args.max_calls_per_turn - self.xai_this_turn,
                          self.args.max_xai_calls - self.state["counts"]["xai"]))

    def reserve(self, kind):
        counts = self.state["counts"]
        limit = self.args.max_jev_calls if kind == "jev" else self.args.max_xai_calls
        if self.blocked_reason or counts[kind] >= limit or (kind == "xai" and self.xai_this_turn >= self.args.max_calls_per_turn):
            self.blocked_reason = self.blocked_reason or f"{kind}_budget_exhausted"
            raise BudgetExceeded(self.blocked_reason)
        counts[kind] += 1
        if kind == "xai":
            self.xai_this_turn += 1
        _save_state(self.state_path, self.state)

    def usage(self, response):
        counts = self.state["counts"]
        input_tokens = getattr(response, "input_tokens", None)
        output_tokens = getattr(response, "output_tokens", None)
        cached = getattr(response, "cached_input_tokens", None)
        if input_tokens is None or output_tokens is None:
            counts["unknown_usage_calls"] += 1
        for key, value in (("known_input_tokens", input_tokens),
                           ("known_output_tokens", output_tokens),
                           ("known_cached_input_tokens", cached)):
            if type(value) is int and value >= 0:
                counts[key] = (counts[key] or 0) + value
        _save_state(self.state_path, self.state)


class CountedPlanner:
    def __init__(self, provider, budget):
        self.provider, self.budget = provider, budget

    @property
    def request_budget_remaining(self):
        return self.budget.remaining_xai_calls

    def generate(self, messages):
        self.budget.reserve("xai")
        try:
            response = self.provider.generate(messages)
        except Exception:
            self.budget.usage(None)
            raise
        self.budget.usage(response)
        return response


class CountedRouter:
    def __init__(self, router, budget):
        self.router, self.budget = router, budget

    def route(self, state, capabilities):
        self.budget.reserve("jev")
        try:
            response = self.router.route(state, capabilities)
        except Exception:
            self.budget.usage(None)
            raise
        self.budget.usage(response)
        return response


def _dry_script(checkpoint, state, store, now, message, snapshot):
    if checkpoint == 1:
        return [command_turn("record_service_event", {"service_type": "oil", "performed_at": now.isoformat(),
                   "odometer": 15000, "unit": "km"}, message.text)]
    if checkpoint == 3:
        return [command_turn("update_odometer", {"reading": 18500, "unit": "km",
                   "occurred_at": now.isoformat()}, message.text)]
    if checkpoint in (5, 8):
        record = store.service_records(state["vehicle_id"], now)[0]
        final = empty_final()
        final["assessment"]["observations"] = [{"text": f"Your recorded oil service was on {record.performed_at.date()} at 15000 km.",
                                                 "evidence_ids": [record.record_id]}]
        return [{"type": "tool_call", "tool_id": "get_latest_service_record", "arguments": {"service_type": "oil"}}, final]
    if checkpoint == 6:
        ids = [o.observation_id for o in snapshot.observations if o.name == "rear_left_tire_pressure"]
        final = empty_final()
        final["assessment"].update(
            observations=[{"text": "Rear-left tire pressure fell across the available readings.", "evidence_ids": ids}],
            hypotheses=[{"hypothesis_id": "POSSIBLE_TIRE_LEAK", "evidence_ids": ids}],
            uncertainties=["CAUSE_UNCONFIRMED"], recommended_action_ids=["ARRANGE_SERVICE_REVIEW"],
            limitations=["NO_PHYSICAL_INSPECTION"])
        return [{"type": "tool_call", "tool_id": "get_tire_pressure_summary", "arguments": {"wheel": "rear_left"}},
                {"type": "tool_call", "tool_id": "get_tire_pressure_history", "arguments": {"wheel": "rear_left"}}, final]
    if checkpoint == 7:
        final = empty_final()
        final["assessment"]["recommended_action_ids"] = ["ARRANGE_SERVICE_REVIEW"]
        return [final]
    return []


def _tire_snapshot(state, store, message, now):
    original = freeze_episode(generate_episode("gradual_tire_pressure_loss", 42)[0])
    offset = now - original.assessment_at
    observations = tuple(replace(o, timestamp=o.timestamp + offset) for o in original.observations)
    profile = store.vehicle(state["owner_id"], state["vehicle_id"], now).profile
    return FrozenEvidenceSnapshot("evaluation-tire-" + state["run_id"], profile, message, now, observations)


def _make_app(store, checkpoint, state, state_path, args, now, message, snapshot):
    if checkpoint not in PROVIDER_TURNS:
        # A confirmation must never reach a real provider or router.
        return compose_app(store, provider=FakePlannerProvider([]),
                           router=FakeCapabilityRouter(selected=["service_history"]))
    if not args.live:
        return compose_app(store, provider=FakePlannerProvider(_dry_script(checkpoint, state, store, now, message, snapshot)),
                           router=FakeCapabilityRouter(selected=ROUTED_PACKS[checkpoint]))
    budget = CallBudget(state, state_path, args)
    budget.start_turn()
    provider = CountedPlanner(XAIPlannerProvider(timeout_seconds=args.timeout_seconds), budget)
    router = CountedRouter(JevCapabilityRouter(timeout_seconds=args.timeout_seconds), budget)
    return compose_app(store, live=True, provider=provider, router=router,
                       max_model_calls=min(4, budget.remaining_xai_calls)), budget


def _check(condition, reason):
    if not condition:
        raise JourneyFailure(reason)


def _has_unresolved_stop_contract(result):
    """Checkpoint 7 verifies the application safety contract, independent of planner completion."""
    trace = result.trace
    return bool(
        result.safety is not None
        and result.safety.disposition == SafetyDisposition.STOP_WHEN_SAFE
        and trace is not None
        and trace.prior_stop_disposition == SafetyDisposition.STOP_WHEN_SAFE.value
        and trace.effective_safety_disposition == SafetyDisposition.STOP_WHEN_SAFE.value
        and trace.stop_guidance_applied
    )


def _safe_artifact(checkpoint, message, result, state, *, simulated=False):
    trace = result.trace
    route = trace.routing if trace else None
    planner = trace.planner if trace else None
    proposal = result.proposed_commands[0] if result.proposed_commands else None
    return {"checkpoint": checkpoint, "run_id": state["run_id"], "timestamp": message.timestamp.isoformat(),
            "user_message": message.text, "response_text": result.response, "status": result.status,
            "vehicle_id": trace.vehicle_id if trace else state["vehicle_id"],
            "simulated_evidence_supplied": simulated,
            "tool_ids_used": list(planner.tool_ids_called) if planner else [],
            "safety": result.safety.disposition.value if result.safety else None,
            "safety_continuity": {
                "current": trace.current_safety_disposition,
                "prior_unresolved_stop": trace.prior_stop_disposition,
                "effective": trace.effective_safety_disposition,
                "stop_guidance_applied": trace.stop_guidance_applied,
            } if trace else None,
            "run_configuration": state["configuration"],
            "ownership_context_characters": trace.ownership_context_characters if trace else None,
            "proposal": {"id": proposal.proposal_id, "command": asdict(proposal.command),
                         "expires_at": proposal.expires_at.isoformat()} if proposal else None,
            "applied": [asdict(item) for item in result.applied_commands],
            "routing": {"raw_relevance": route.raw_relevance,
                        "raw_selected": route.raw_selected_capabilities,
                        "effective_loaded": route.effective_loaded_capabilities,
                        "fallback_used": route.fallback_used, "fallback_reason": route.fallback_reason,
                        "expanded": route.expanded_capabilities, "model": route.model,
                        "input_tokens": route.input_tokens, "output_tokens": route.output_tokens}
                       if route else None,
            "planner": {"action": planner.last_model_action, "calls": planner.planner_call_count,
                        "tools": planner.tool_ids_called, "capability_count": planner.exposed_capability_count,
                        "tool_count": planner.exposed_tool_count,
                        "instruction_characters": planner.planner_instruction_character_count,
                        "initial_context_characters": planner.initial_context_characters,
                        "input_tokens": planner.input_tokens, "output_tokens": planner.output_tokens,
                        "cached_input_tokens": planner.cached_input_tokens,
                        "provider_calls": planner.provider_calls,
                        "provider_error": asdict(planner.provider_error) if planner.provider_error else None,
                        "validation_failures": planner.validation_failures,
                        "repair_status": planner.repair_status,
                        "completion_status": planner.completion_status} if planner else None,
            "call_counts": dict(state["counts"])}


def _run_checkpoint(checkpoint, store, state, state_path, artifact_path, args, *, allow_retry=False):
    now = datetime.now(timezone.utc)
    if checkpoint == 0:
        if 0 in state["completed"]:
            raise JourneyFailure("Checkpoint 0 already completed; use the saved evaluation DB.")
        profile = replace(freeze_episode(generate_episode("healthy_vehicle", 42)[0]).profile,
                          vehicle_id=state["vehicle_id"], mileage_km=None)
        app = compose_app(store)
        app.create_owner(now=now, owner_id=state["owner_id"])
        app.add_vehicle(state["owner_id"], profile, now=now)
        app.start_session(state["owner_id"], now=now, vehicle_id=state["vehicle_id"],
                          session_id=state["session_id"])
        record = {"checkpoint": 0, "run_id": state["run_id"], "timestamp": now.isoformat(),
                  "status": "passed", "owner_id": state["owner_id"], "vehicle_id": state["vehicle_id"],
                  "provider_calls": {"jev": 0, "xai": 0}}
    else:
        _check(checkpoint - 1 in state["completed"], "Previous checkpoint has not passed.")
        _check(checkpoint not in state["completed"] and (checkpoint not in state["failed"] or allow_retry),
               "Checkpoint already attempted; it will not be repeated automatically.")
        retry_number = state.get("retry_counts", {}).get(str(checkpoint), 0)
        suffix = f"-retry-{retry_number}" if retry_number else ""
        message = UserMessage(f"evaluation-{state['run_id']}-{checkpoint}{suffix}",
                              CHECKPOINT_MESSAGES[checkpoint], now)
        before_services = len(store.service_records(state["vehicle_id"], now))
        before_odometer = store.vehicle(state["owner_id"], state["vehicle_id"], now).profile.mileage_km
        snapshot = _tire_snapshot(state, store, message, now) if checkpoint == 6 else None
        confirmation_id = state["proposals"].get(str(checkpoint - 1)) if checkpoint in (2, 4) else None
        if checkpoint == 8:
            # A new connection and app must read the committed ownership state.
            with_store = OwnershipStore(state["db_path"])
            store_for_turn = with_store
        else:
            with_store = None
            store_for_turn = store
        result = None
        try:
            built = _make_app(store_for_turn, checkpoint, state, state_path, args, now, message, snapshot)
            app, budget = built if isinstance(built, tuple) else (built, None)
            result = app.handle_message(state["owner_id"], state["session_id"], message, now=now,
                                        snapshot=snapshot, confirmation_id=confirmation_id)
            if budget and budget.blocked_reason:
                raise JourneyFailure("Provider budget exhausted; checkpoint stopped.")
            _check(result.trace is not None and result.trace.vehicle_id == state["vehicle_id"],
                   "Checkpoint returned an incorrect vehicle binding.")
            if checkpoint in (1, 3):
                expected = "record_service_event" if checkpoint == 1 else "update_odometer"
                _check(result.status == "confirmation_required" and len(result.proposed_commands) == 1,
                       "Expected one validated ownership proposal.")
                proposal = result.proposed_commands[0]
                _check(proposal.command.kind.value == expected, "Unexpected ownership command type.")
                args_ = proposal.command.arguments
                if checkpoint == 1:
                    _check(args_.get("service_type") == "oil" and args_.get("odometer") == 15000 and args_.get("unit") == "km",
                           "Service proposal does not match the owner's stated values.")
                    _check(datetime.fromisoformat(args_["performed_at"]).date() == now.date(),
                           "Service proposal date is not today.")
                else:
                    _check(args_.get("reading") == 18500 and args_.get("unit") == "km",
                           "Odometer proposal does not match the owner's stated values.")
                _check(all(str(value) in result.response for value in args_.values() if isinstance(value, (str, int, float))),
                       "The proposal display omits a semantic field.")
                _check(len(store_for_turn.service_records(state["vehicle_id"], now)) == before_services
                       and store_for_turn.vehicle(state["owner_id"], state["vehicle_id"], now).profile.mileage_km == before_odometer,
                       "An unconfirmed proposal changed ownership state.")
                state["proposals"][str(checkpoint)] = proposal.proposal_id
            elif checkpoint in (2, 4):
                _check(result.status == "applied" and len(result.applied_commands) == 1,
                       "Confirmation did not apply exactly one command.")
                _check(result.trace.planner is None and result.trace.routing is None,
                       "Confirmation unexpectedly entered a provider path.")
                if checkpoint == 2:
                    records = store_for_turn.service_records(state["vehicle_id"], now)
                    _check(len(records) == before_services + 1 and records[0].service_type == "oil"
                           and records[0].odometer_km == 15000, "Confirmed service was not persisted exactly.")
                    state["service_entity_id"] = records[0].record_id
                else:
                    _check(store_for_turn.vehicle(state["owner_id"], state["vehicle_id"], now).profile.mileage_km == 18500,
                           "Confirmed mileage was not persisted.")
            elif checkpoint in (5, 8):
                _check(result.status == "complete" and result.trace.planner is not None,
                       "Ownership query was not completed.")
                _check(any(tool in result.trace.planner.tool_ids_called for tool in
                           ("get_latest_service_record", "get_service_history")),
                       "Ownership query did not use a persistent read tool.")
                _check(state["service_entity_id"] in result.assessment.assessment.evidence_ids,
                       "Ownership answer did not cite the stored service.")
                _check("15000" in result.response and "oil" in result.response.lower(),
                       "Ownership answer omitted the stored service facts.")
                _check(len(store_for_turn.service_records(state["vehicle_id"], now)) == before_services,
                       "Ownership read mutated service history.")
            elif checkpoint == 6:
                _check(result.status == "complete" and result.safety.disposition == SafetyDisposition.STOP_WHEN_SAFE,
                       "Tire assessment did not complete with deterministic stop.")
                _check(any(tool.startswith("get_tire_pressure") for tool in result.trace.planner.tool_ids_called),
                       "Tire assessment did not use tire evidence tools.")
                _check(len(store_for_turn.service_records(state["vehicle_id"], now)) == before_services,
                       "Tire assessment mutated ownership history.")
            else:
                _check(_has_unresolved_stop_contract(result),
                       "Follow-up did not retain the structured unresolved-stop response contract.")
            record = _safe_artifact(checkpoint, message, result, state, simulated=checkpoint == 6)
            record["status"] = "passed"
        except Exception:
            if result is not None:
                state["_pending_failure_artifact"] = _safe_artifact(
                    checkpoint, message, result, state, simulated=checkpoint == 6)
            raise
        finally:
            if with_store is not None:
                with_store.close()
    state["completed"].append(checkpoint)
    if checkpoint in state["failed"]:
        state["failed"].remove(checkpoint)
    _save_state(state_path, state)
    with artifact_path.open("a", encoding="utf-8") as artifact:
        artifact.write(json.dumps(_redact(record, live=args.live), default=_json_default, sort_keys=True) + "\n")
    print(f"CHECKPOINT {checkpoint}: passed")
    if checkpoint:
        print("User:", message.text)
        print("CarMind:", _redact(result.response, live=args.live))
        if result.proposed_commands:
            print("Proposal ID:", result.proposed_commands[0].proposal_id)
        if result.trace.routing:
            route = result.trace.routing
            print("Routing:", json.dumps({"raw_relevance": route.raw_relevance,
                  "raw_selected": route.raw_selected_capabilities,
                  "effective_loaded": route.effective_loaded_capabilities,
                  "fallback_reason": route.fallback_reason, "expanded": route.expanded_capabilities}, sort_keys=True))
        if result.trace.planner:
            print("Planner:", json.dumps({"calls": result.trace.planner.planner_call_count,
                  "action": result.trace.planner.last_model_action,
                  "tools": result.trace.planner.tool_ids_called,
                  "capabilities": result.trace.planner.exposed_capability_count,
                  "exposed_tools": result.trace.planner.exposed_tool_count}, sort_keys=True))
    return record


def _cleanup(db_path, state_path, artifact_path):
    state = _load_state(db_path, state_path)
    for path in (db_path, state_path, artifact_path):
        if path.exists():
            path.unlink()
    print("Removed evaluation artifacts for run", state["run_id"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Use real Jev and xAI for one checkpoint")
    parser.add_argument("--confirm-live-api-use", action="store_true", help="Acknowledge real provider credits")
    parser.add_argument("--checkpoint", type=int, choices=range(9), help="Run exactly one checkpoint")
    parser.add_argument("--db-path", type=Path, help="Isolated carmind-eval-*.sqlite3 path")
    parser.add_argument("--reuse-evaluation-db", action="store_true", help="Resume a marked evaluation database")
    parser.add_argument("--retry-failed-checkpoint", action="store_true",
                        help="Retry only the named failed checkpoint with a fresh message ID and saved cumulative budgets")
    parser.add_argument("--retain-db", action="store_true", help="Keep dry-run database for inspection")
    parser.add_argument("--cleanup", action="store_true", help="Remove only a marked evaluation DB and its artifacts")
    parser.add_argument("--max-live-turns", type=int, default=1)
    parser.add_argument("--max-xai-calls", type=int, default=1)
    parser.add_argument("--max-jev-calls", type=int, default=1)
    parser.add_argument("--max-calls-per-turn", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    args = parser.parse_args(argv)
    try:
        if args.live and not args.confirm_live_api_use:
            raise JourneyFailure("Live mode requires --confirm-live-api-use; no provider was constructed.")
        if args.live and args.checkpoint is None:
            raise JourneyFailure("Live mode requires one --checkpoint; no journey is run automatically.")
        if args.retry_failed_checkpoint and (args.checkpoint is None or args.db_path is None
                                             or not args.reuse_evaluation_db):
            raise JourneyFailure("Retry requires one --checkpoint, --db-path and --reuse-evaluation-db.")
        if args.cleanup and (args.live or args.db_path is None):
            raise JourneyFailure("Cleanup requires --db-path and cannot be combined with --live.")
        if any(type(value) is not int or value < 1 for value in
               (args.max_live_turns, args.max_xai_calls, args.max_jev_calls, args.max_calls_per_turn)):
            raise JourneyFailure("Provider budgets must be positive integers.")
        if not 0 < args.timeout_seconds <= 180:
            raise JourneyFailure("Timeout must be greater than zero and at most 180 seconds.")
        if args.live:
            _load_project_env()

            missing = missing_jev() + missing_xai()
            if missing:
                raise JourneyFailure("Missing configuration names: " + ", ".join(missing))
        db_path, state_path, artifact_path = _paths(args.db_path or
            EVALUATION_DIRECTORY / f"carmind-eval-{uuid4()}.sqlite3")
        if args.cleanup:
            _cleanup(db_path, state_path, artifact_path)
            return 0
        existing = db_path.exists() or state_path.exists()
        if args.retry_failed_checkpoint and not existing:
            raise JourneyFailure("Retry requires an existing failed evaluation checkpoint.")
        if existing and not args.reuse_evaluation_db:
            raise JourneyFailure("Evaluation DB exists; pass --reuse-evaluation-db to resume it.")
        if not existing and args.reuse_evaluation_db:
            raise JourneyFailure("Requested evaluation DB does not exist.")
        db_path.parent.mkdir(parents=True, exist_ok=True)
        state = _load_state(db_path, state_path) if existing else _new_state(db_path)
        if existing and not db_path.is_file():
            raise JourneyFailure("Evaluation metadata exists but the DB is missing.")
        mode = "live" if args.live else "dry_run"
        if existing and state.get("configuration", {}).get("mode") != mode:
            raise JourneyFailure("Evaluation mode cannot change during a saved journey.")
        if args.retry_failed_checkpoint and args.checkpoint not in state.get("failed", ()):
            raise JourneyFailure("Only a failed checkpoint can be retried; saved ownership data was not reset.")
        configuration = {
            "mode": mode,
            "planner_model": os.environ.get("XAI_MODEL") if args.live else None,
            "jev_model_override": os.environ.get("TYPESAFE_DEFAULT_MODEL") if args.live else None,
            "timeout_seconds": args.timeout_seconds,
            "budgets": {"max_live_turns": args.max_live_turns,
                        "max_xai_calls": args.max_xai_calls,
                        "max_jev_calls": args.max_jev_calls,
                        "max_calls_per_turn": args.max_calls_per_turn},
        }
        state["configuration"] = configuration
        state.setdefault("invocations", []).append({"at": datetime.now(timezone.utc).isoformat(),
                                                     **configuration})
        _save_state(state_path, state)
        print("EVALUATION DB:", db_path)
        print("RUN ID:", state["run_id"])
        print("VEHICLE ID:", state["vehicle_id"])
        print("CODE COMMIT:", state["code_commit"])
        print("WORKING TREE DIRTY:", state["working_tree_dirty"])
        print("MODE:", "LIVE ONE CHECKPOINT" if args.live else "DRY RUN / OFFLINE")
        store = OwnershipStore(db_path)
        try:
            steps = (args.checkpoint,) if args.checkpoint is not None else tuple(range(9))
            if steps[0] == 1 and 0 not in state["completed"]:
                _run_checkpoint(0, store, state, state_path, artifact_path, args)
            for step in steps:
                retrying = args.retry_failed_checkpoint and step == args.checkpoint and step in state["failed"]
                if step in state["completed"] or (step in state["failed"] and not retrying):
                    raise JourneyFailure("Checkpoint was already attempted; no automatic retry.")
                if retrying:
                    retry_key = str(step)
                    retry_number = state.setdefault("retry_counts", {}).get(retry_key, 0) + 1
                    state["retry_counts"][retry_key] = retry_number
                    commit, dirty = _git_identity()
                    state.setdefault("retry_history", []).append({
                        "checkpoint": step, "retry_number": retry_number,
                        "at": datetime.now(timezone.utc).isoformat(),
                        "code_commit": commit, "working_tree_dirty": dirty,
                    })
                    _save_state(state_path, state)
                try:
                    _run_checkpoint(step, store, state, state_path, artifact_path, args,
                                    allow_retry=retrying)
                except Exception:
                    if step not in state["failed"]:
                        state["failed"].append(step)
                    failure = state.pop("_pending_failure_artifact", None) or {
                        "checkpoint": step, "run_id": state["run_id"]}
                    failure.update(status="failed",
                                   error_category="checkpoint_invariant_or_provider_failure",
                                   call_counts=dict(state["counts"]))
                    _save_state(state_path, state)
                    with artifact_path.open("a", encoding="utf-8") as artifact:
                        artifact.write(json.dumps(_redact(failure, live=args.live),
                                                  default=_json_default, sort_keys=True) + "\n")
                    raise
        finally:
            store.close()
        print("PROVIDER COUNTS:", json.dumps(state["counts"], sort_keys=True))
        print("ARTIFACT:", artifact_path)
        if not args.live and args.checkpoint is None and not args.retain_db and args.db_path is None:
            db_path.unlink()
            state["dry_run_db_removed"] = True
            _save_state(state_path, state)
            print("Dry-run DB removed; use --retain-db next time to inspect it.")
        return 0
    except (JourneyFailure, ValueError, OSError) as error:
        # Only static runner errors are printed; provider exceptions stay in safe traces.
        print("EVALUATION STOPPED:", str(error) if isinstance(error, JourneyFailure) else "Local evaluation setup failed.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
