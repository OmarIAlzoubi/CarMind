"""Manually run one tire-pressure planner mode, with an explicit, bounded xAI request budget.

Run from the project root with the CarMind Python interpreter. This script never
calls Jev and never starts the other mode automatically.
"""

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import carmind.planner as planner_module
from carmind.capabilities import CapabilityRegistry
from carmind.planner import strict_json
from carmind.planner_provider import XAIPlannerProvider, missing_configuration, DEFAULT_TIMEOUT_SECONDS, validate_timeout_seconds
from carmind.response import render_user_response
from carmind.router_provider import FakeCapabilityRouter
from carmind.routing import ExecutionMode, run_assessment
from carmind.simulator import freeze_episode, generate_episode
from carmind.tools import TOOL_CATALOG

DEFAULT_MAX_CALLS = 2
MAX_REQUESTS = 4
OWNER_MESSAGE = "One tire keeps losing pressure. Can you check what might be happening?"


def load_local_env():
    """Read only the two xAI settings; never display or modify the .env file."""
    path = ROOT / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip().removeprefix("export ").strip()
        if name in ("XAI_API_KEY", "XAI_MODEL") and not os.environ.get(name):
            os.environ[name] = value.strip().strip('"\'')


def timeout_argument(value):
    try:
        return validate_timeout_seconds(float(value))
    except ValueError:
        raise argparse.ArgumentTypeError("timeout must be finite, greater than 0 and at most 180 seconds") from None


class RequestLimitReached(RuntimeError):
    pass


class MeasuredProvider:
    def __init__(self, backend, write=print, *, max_calls=DEFAULT_MAX_CALLS):
        if type(max_calls) is not int or not 1 <= max_calls <= MAX_REQUESTS:
            raise ValueError("max_calls must be an integer from 1 to 4")
        self.max_calls = max_calls
        self.backend = backend
        self.write = write
        self.attempts = 0
        self.turns = []

    def generate(self, messages):
        if self.attempts >= self.max_calls:
            raise RequestLimitReached("Request limit reached; no further provider call made.")
        self.attempts += 1  # A failed attempt also consumes the budget.
        number = self.attempts
        if number == 1:
            self.write("INITIAL CONTEXT CHARACTERS: " + str(sum(len(m["content"]) for m in messages)))
        self.write(f"CALL {number}: starting")
        started = perf_counter()
        try:
            response = self.backend.generate(messages)
        except Exception:
            self.turns.append({"call": number, "latency_seconds": perf_counter() - started,
                               "input_tokens": None, "output_tokens": None, "cached_tokens": None,
                               "action": "provider_failure", "requested_tool": None})
            self.write(f"CALL {number}: provider failure after {self.turns[-1]['latency_seconds']:.3f} s")
            raise
        provider_elapsed = perf_counter() - started
        try:
            action = strict_json(response.text)
            kind = action.get("type")
            kind = kind if kind in ("tool_call", "final", "expand_capabilities") else "invalid_action"
            requested = action.get("tool_id") if kind == "tool_call" else None
            tool = requested if isinstance(requested, str) and requested in TOOL_CATALOG else None
        except ValueError:
            kind, tool = "invalid_json", None
        turn = {"call": number, "latency_seconds": provider_elapsed,
                "input_tokens": response.input_tokens, "output_tokens": response.output_tokens,
                "cached_tokens": getattr(response, "cached_input_tokens", None),
                "action": kind, "requested_tool": tool}
        self.turns.append(turn)
        self.write("CALL " + str(number) + ": " + json.dumps(turn, default=str))
        return response


def run_case(mode, backend, *, model, write=print, max_calls=DEFAULT_MAX_CALLS):
    """Run exactly one existing planner mode on the same reproducible evidence."""
    mode = ExecutionMode(mode.upper())
    snapshot = freeze_episode(generate_episode("gradual_tire_pressure_loss", 42)[0])
    assert snapshot.owner_message.text == OWNER_MESSAGE
    registry = CapabilityRegistry()
    loaded = registry.load_capabilities(["tires"]) if mode == ExecutionMode.ROUTED else registry.load_all_capabilities()
    write("MODE: " + mode.value)
    write("MODEL: " + model)
    write("EFFECTIVE SDK TIMEOUT SECONDS (connect/read/write/pool; not total): " + str(getattr(backend, "timeout_seconds", None)))
    write("REQUEST BUDGET: " + str(max_calls))
    write("CAPABILITY COUNT: " + str(loaded.capability_count))
    write("CAPABILITY IDs: " + json.dumps([pack.id for pack in loaded.packs]))
    write("TOOL COUNT: " + str(loaded.tool_count))
    write("TOOL IDs: " + json.dumps(loaded.tool_ids))
    write("INITIAL CONTEXT CHARACTERS: calculated immediately before call 1")
    measured = MeasuredProvider(backend, write, max_calls=max_calls)
    original_execute = planner_module.execute_tool

    def observed_tool(*args, **kwargs):
        started = perf_counter()
        result = original_execute(*args, **kwargs)
        write("LOCAL TOOL: " + json.dumps({"tool_id": result.tool_id if result.tool_id in TOOL_CATALOG else "<invalid>",
              "latency_seconds": perf_counter() - started,
              "evidence_ids": result.evidence_ids, "success": result.success,
              "error": result.error, "payload_characters": len(json.dumps({"tool_result": asdict(result)}, default=str)),
              "evidence_id_count": len(set(result.evidence_ids))}))
        return result

    planner_module.execute_tool = observed_tool
    try:
        run = run_assessment(snapshot, measured, mode=mode,
                             router=FakeCapabilityRouter(selected=["tires"]) if mode == ExecutionMode.ROUTED else None,
                             max_model_calls=max_calls)
    finally:
        planner_module.execute_tool = original_execute

    trace = run.planner.trace
    for exposure in trace.call_exposures:
        write("CONTEXT BREAKDOWN: " + json.dumps(exposure))
    write("TOOL METRICS: " + json.dumps(trace.tool_executions))
    write("TOOL SEQUENCE: " + json.dumps(trace.tool_ids_called))
    write("TARGETED EXPANSION RESULT: " + json.dumps({"added": run.routing.expanded_capabilities,
          "failure": run.routing.expansion_failure_reason}))
    write("TOTAL xAI REQUESTS: " + str(measured.attempts))
    write("USAGE: " + json.dumps({key: getattr(trace, key) for key in (
        "known_input_tokens", "known_output_tokens", "known_cached_input_tokens",
        "calls_with_unknown_usage", "calls_with_unknown_cached_usage", "usage_complete", "cached_usage_complete",
        "input_tokens", "output_tokens", "cached_input_tokens")}))
    write("PROVIDER CALL METRICS: " + json.dumps(trace.provider_calls))
    write(f"SUCCESSFUL PROVIDER LATENCY: {trace.successful_provider_latency_seconds:.3f} s")
    write(f"FAILED PROVIDER LATENCY: {trace.failed_provider_latency_seconds:.3f} s")
    write(f"LOCAL TOOL LATENCY (including preparation): {sum(t['elapsed_seconds'] for t in trace.tool_executions):.3f} s")
    write(f"TOTAL PLANNER ELAPSED (including failures): {trace.elapsed_seconds:.3f} s")
    write(f"TOTAL ORCHESTRATION LATENCY: {run.elapsed_seconds:.3f} s")
    write("COMPLETION STATUS: " + trace.completion_status)
    write("STOP REASON: " + str(trace.stop_reason))
    write("LAST MODEL ACTION: " + str(trace.last_model_action))
    write("FINAL STATUS: " + str(trace.final_status))
    write("REPAIR STATUS: " + trace.repair_status)
    for candidate in trace.rejected_model_candidates:
        write("REJECTED MODEL CANDIDATE (sanitized; not application assessment): " + json.dumps(candidate))
    write("APPLICATION ASSESSMENT: " + json.dumps(asdict(run.planner.result.assessment), default=str))
    write("ASSESSMENT JSON CHARACTERS: " + str(len(json.dumps(asdict(run.planner.result.assessment), default=str, sort_keys=True))))
    write("USER-FACING RESPONSE: " + render_user_response(run.planner.result).text)
    validation = "passed_after_repair" if trace.completion_status == "complete" and trace.validation_failures else (
        "passed" if trace.completion_status == "complete" else trace.final_status or trace.completion_status)
    write("VALIDATION RESULT: " + validation)
    write("VALIDATION FAILURES: " + json.dumps(trace.validation_failures))
    write("VALIDATION FAILURE CATEGORIES: " + json.dumps(trace.validation_failure_categories))
    write("SAFETY DISPOSITION: " + run.planner.result.safety.disposition.value)
    write("TARGETED EXPANSION REQUEST: " + json.dumps(run.routing.expansion_requested))
    if trace.provider_error is not None:
        write("SANITIZED PROVIDER DIAGNOSTIC: " + json.dumps(asdict(trace.provider_error)))
    if trace.stop_reason == "call_budget_exhausted":
        write("Call budget exhausted; no additional provider request was made.")
    return run, measured


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("routed", "full"), required=True)
    parser.add_argument("--max-calls", type=int, choices=range(1, MAX_REQUESTS + 1), default=DEFAULT_MAX_CALLS,
                        help="Provider attempt budget (default 2, absolute maximum 4; failures count)")
    parser.add_argument("--timeout-seconds", type=timeout_argument, default=DEFAULT_TIMEOUT_SECONDS,
                        help="SDK connect/read/write/pool timeout, not a total deadline (default 45, maximum 180)")
    args = parser.parse_args()
    load_local_env()
    missing = missing_configuration()
    if missing:
        parser.error("Missing configuration: " + ", ".join(missing))
    if os.environ["XAI_MODEL"] != "grok-4.6":
        parser.error("This experiment requires XAI_MODEL=grok-4.6")
    provider = XAIPlannerProvider(timeout_seconds=args.timeout_seconds)
    if provider._client.max_retries != 0:
        parser.error("SDK retries must be disabled")
    run, _ = run_case(args.mode, provider, model=os.environ["XAI_MODEL"], max_calls=args.max_calls)
    return 0 if run.planner.trace.completion_status == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
