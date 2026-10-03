"""Paired development harness. Offline scripts test plumbing, not AI quality."""

import argparse
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from statistics import mean

from carmind.capabilities import CapabilityRegistry, load_all_capabilities
from carmind.contracts import VehicleProfile, UserMessage, VehicleContext, MaintenanceRecord, DiagnosticCodeRecord
from carmind.evidence import FrozenEvidenceSnapshot
from carmind.manufacturer_knowledge import ROOT, load_knowledge
from carmind.planner import _register
from carmind.planner_provider import FakePlannerProvider, XAIPlannerProvider, missing_configuration as missing_xai
from carmind.router_provider import FakeCapabilityRouter, JevCapabilityRouter, RouterResponse, missing_configuration as missing_jev
from carmind.routing import ExecutionMode, run_assessment, build_routing_state, RoutingTrace, RoutingPolicy, select_capabilities
from carmind.safety import evaluate_safety, catalog
from carmind.simulator import generate_episode, freeze_episode, list_scenarios
from carmind.tools import MaintenanceRequest, maintenance_results, execute_tool, _code_records

CASES_PATH = ROOT / "eval/development_cases.json"
EVALUATION_KNOWLEDGE = ROOT / "eval/fixtures/manufacturer_knowledge"


@dataclass(frozen=True)
class CaseInputs:
    snapshot: FrozenEvidenceSnapshot
    context: VehicleContext | None = None
    maintenance_request: MaintenanceRequest | None = None


def load_cases(path=CASES_PATH):
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = document["cases"]
    if document["version"] != 1 or len({c["case_id"] for c in cases}) != len(cases):
        raise ValueError("Invalid development case file")
    known = set(CapabilityRegistry().routing_descriptions())
    for case in cases:
        expected = case["expected"]["capabilities"]
        if not expected or len(set(expected)) != len(expected) or not set(expected) <= known:
            raise ValueError("Invalid evaluator capability labels")
    return cases


def make_inputs(case) -> CaseInputs:
    """Fixture metadata stays in the harness, never in the routing/planner API."""
    fixture = case["fixture"]
    if fixture in list_scenarios():
        return CaseInputs(freeze_episode(generate_episode(fixture, case.get("seed", 42))[0]))
    now = datetime(2026, 9, 28, tzinfo=timezone.utc)
    if fixture == "multi_domain":
        first = freeze_episode(generate_episode("weak_battery_start", 42)[0])
        second = freeze_episode(generate_episode("gradual_tire_pressure_loss", 42)[0])
        # Align both public histories to one cutoff; scenario labels are discarded.
        offset = second.assessment_at - first.assessment_at
        observations = tuple(sorted((*second.observations, *(replace(o, timestamp=o.timestamp + offset) for o in first.observations)), key=lambda o: (o.timestamp, o.observation_id)))
        return CaseInputs(replace(second, observations=observations, owner_message=replace(second.owner_message, text=case["message"])))
    profile = VehicleProfile("ownership-fixture", "Fictional", "Everyday", 2024, 49200, "Fictional gasoline engine")
    request = None
    if fixture == "official_maintenance":
        pack = load_knowledge(EVALUATION_KNOWLEDGE)
        # Keep this development fixture inapplicable regardless of the selected
        # source's year; it must never activate a manufacturer schedule.
        owner_year = pack.profile.model_year + 1 if pack.profile.model_year is not None else 2024
        identity = replace(pack.profile, model_year=owner_year)
        profile = VehicleProfile("owner-profile", identity.manufacturer, identity.model,
                                 owner_year, 49200, identity.engine_variant)
        request = MaintenanceRequest(pack, identity, "NORMAL")
    elif fixture != "ownership":
        raise ValueError("Unknown benchmark fixture")
    message = UserMessage("owner-message", case["message"], now)
    snapshot = FrozenEvidenceSnapshot("public-session", profile, message, now)
    records = [MaintenanceRecord("owner-battery-service", "battery", now - timedelta(days=90), 45000),
               MaintenanceRecord("owner-filter-service", "cabin_air_filter", datetime(2025, 4, 1, tzinfo=timezone.utc), 20000)]
    context = VehicleContext(profile, records, [DiagnosticCodeRecord("OWNER_REPORTED_CODE", now - timedelta(days=1), "USER", True)])
    return CaseInputs(snapshot, context, request)


def evidence_selection(spec, inputs, prefix=""):
    """Resolve evaluator selectors or independent scripted citations to public IDs."""
    snapshot, context, request = inputs.snapshot, inputs.context, inputs.maintenance_request
    names_key = "cite_names" if prefix else "evidence_names"
    records_key = "cite_records" if prefix else "record_ids"
    maintenance_key = "cite_maintenance" if prefix else "maintenance"
    codes_key = "cite_codes" if prefix else "codes"
    ids = {o.observation_id for o in snapshot.observations if o.name in spec.get(names_key, [])}
    ids.update(spec.get(records_key, []))
    if spec.get(maintenance_key):
        ids.update(r.reminder_id for r in maintenance_results(request, snapshot, context))
    if spec.get(codes_key):
        ids.update(r["evidence_id"] for r in _code_records(context, snapshot.assessment_at, False))
    return sorted(ids)


def scripted_provider(script, inputs):
    """Author-specified tool trajectory, identical in both modes; no labels used."""
    ids = evidence_selection(script, inputs, prefix="script")
    sources = sorted({r.source_id for r in maintenance_results(inputs.maintenance_request, inputs.snapshot, inputs.context)}) if script.get("cite_maintenance") else []
    turns = [{"type": "tool_call", **deepcopy(tool)} for tool in script["tools"]]
    turns.append({"type": "final", "assessment": {
        "observations": [{"text": "Observed vehicle evidence supports this assessment.", "evidence_ids": ids}] if ids else [],
        "hypotheses": [{"hypothesis_id": script["hypothesis"], "evidence_ids": ids}] if script.get("hypothesis") else [],
        "uncertainties": ["APPLICABILITY_UNVERIFIED"] if sources else ["CAUSE_UNCONFIRMED", "MISSING_EVIDENCE"],
        "recommended_action_ids": ["REVIEW_UPCOMING_MAINTENANCE"] if sources else ["REVIEW_RECENT_EVIDENCE"],
        "limitations": ["POC_ONLY", "PARTIAL_EVIDENCE"],
    }})
    return FakePlannerProvider(turns)


def scripted_router(script):
    if script.get("borderline"):
        return FakeCapabilityRouter(RouterResponse({key: 0.5 for key in CapabilityRegistry().routing_descriptions()}))
    return FakeCapabilityRouter(selected=script["selection"])


def routing_metrics(selected, expected):
    actual, required = set(selected), set(expected)
    intersection = len(actual & required)
    return {"required_recall": intersection / len(required) if required else 1.0,
            "selection_precision": intersection / len(actual) if actual else (1.0 if not required else 0.0),
            "exact_set_agreement": actual == required}


def _reference_evidence(inputs):
    snapshot, context, request = inputs.snapshot, inputs.context, inputs.maintenance_request
    evidence = {snapshot.profile.vehicle_id: asdict(snapshot.profile), snapshot.owner_message.message_id: asdict(snapshot.owner_message)}
    sources = set()
    if request:
        _register(asdict(request.pack), evidence, sources)
        _register([asdict(r) for r in maintenance_results(request, snapshot, context)], evidence, sources)
    # Evaluator independently reconstructs valid public facts, not hidden causes.
    for tool in load_all_capabilities().tools:
        result = execute_tool(tool.tool_id, {}, snapshot, context, maintenance_request=request, allowed_tool_ids=(tool.tool_id,))
        if result.success:
            _register(result.data, evidence, sources)
    return json.loads(json.dumps(evidence, default=str)), sources


def quality_metrics(run, inputs, expected):
    validated = run.planner.result
    assessment = validated.assessment
    correct_safety = evaluate_safety(inputs.snapshot, inputs.context)
    safety_ok = validated.safety == correct_safety and assessment.safety_disposition == correct_safety.disposition
    actions = {a["action_id"]: a for a in catalog("action_catalog.json")["actions"]}
    actions_ok = all(a in actions and correct_safety.disposition.value in actions[a]["allowed_dispositions"] for a in assessment.recommended_action_ids)
    actions_ok = actions_ok and validated.action_wording == tuple(actions[a]["approved_wording"] for a in assessment.recommended_action_ids if a in actions)
    evidence, sources = _reference_evidence(inputs)
    grounding = set(assessment.evidence_ids) <= evidence.keys() and set(validated.source_ids) <= sources
    used = set()
    for claim in validated.claims:
        used.update(claim["evidence_ids"])
        if "facts" in claim:
            grounding = grounding and all(evidence.get(ref) == fact for ref, fact in claim["facts"].items())
            grounding = grounding and set(claim["facts"]) == set(claim["evidence_ids"])
    grounding = grounding and used == set(assessment.evidence_ids)
    required = set(evidence_selection(expected, inputs))
    coverage = len(required & set(assessment.evidence_ids)) / len(required) if required else 1.0
    expected_state = maintenance_results(inputs.maintenance_request, inputs.snapshot, inputs.context) if inputs.maintenance_request else ()
    maintenance_ok = run.planner.maintenance_state == expected_state
    complete = run.planner.trace.completion_status == "complete"
    return {"assessment_complete": complete, "required_evidence_coverage": coverage,
            "valid_actions": actions_ok, "grounding_valid": grounding, "safety_correct": safety_ok,
            "maintenance_correct": maintenance_ok, "safety_policy_violations": int(not safety_ok),
            "task_success": complete and coverage == 1 and actions_ok and grounding and safety_ok and maintenance_ok}


def _sum_known(*values):
    return None if any(v is None for v in values) else sum(values)


def summarize(rows):
    result = {}
    for mode in ExecutionMode:
        group = [r for r in rows if r["mode"] == mode.value]
        if not group:
            continue
        result[mode.value] = {
            "cases": len(group),
            **{key: mean(r[key] for r in group) for key in (
                "capability_count", "tool_count", "instruction_characters", "tool_schema_characters",
                "planner_calls", "tool_calls", "initial_context_characters", "cumulative_request_characters",
                "routing_latency", "planner_latency", "end_to_end_latency")},
            "tool_result_characters": mean(r["tool_result_characters"] for r in group),
            "validation_failures": sum(len(r["planner_trace"]["validation_failures"]) for r in group),
            "grounding_failures": sum(not r["quality"]["grounding_valid"] for r in group),
            "completion_rate": mean(r["quality"]["assessment_complete"] for r in group),
            "task_success_rate": mean(r["quality"]["task_success"] for r in group),
            "required_evidence_coverage": mean(r["quality"]["required_evidence_coverage"] for r in group),
            "safety_policy_violations": sum(r["quality"]["safety_policy_violations"] for r in group),
            "fallback_rate": mean(r["routing"]["fallback_used"] for r in group),
            "expansion_rate": mean(r["routing"]["expansion_used"] for r in group),
        }
        for key in ("planner_input_tokens", "planner_output_tokens", "total_tokens", "total_cost_usd"):
            values = [r[key] for r in group]
            result[mode.value][key] = None if any(v is None for v in values) else mean(values)
        if mode == ExecutionMode.ROUTED:
            for selection in ("raw", "effective"):
                result[mode.value][selection + "_routing"] = {
                    key: mean(r[selection + "_routing"][key] for r in group)
                    for key in ("required_recall", "selection_precision", "exact_set_agreement")}
    if "FULL" in result and "ROUTED" in result:
        regression = (result["ROUTED"]["task_success_rate"] < result["FULL"]["task_success_rate"]
                      or result["ROUTED"]["required_evidence_coverage"] < result["FULL"]["required_evidence_coverage"]
                      or result["ROUTED"]["safety_policy_violations"] > result["FULL"]["safety_policy_violations"])
        result["quality_regression_observed"] = regression
        result["conclusion"] = ("Quality regressed; reduced exposure is not a successful tradeoff." if regression else
                                "No regression on these cases; this alone does not establish live routing benefit.")
    return result


def run_benchmark(cases=None, *, live=False, provider_factory=None, router_factory=None):
    cases = load_cases() if cases is None else cases
    rows = []
    for index, case in enumerate(cases):
        inputs = make_inputs(case)
        # Alternate order to reduce fixed-order timing bias; identical fresh inputs.
        order = (ExecutionMode.FULL, ExecutionMode.ROUTED) if index % 2 == 0 else (ExecutionMode.ROUTED, ExecutionMode.FULL)
        for mode in order:
            provider = provider_factory(case["script"], deepcopy(inputs)) if provider_factory else (XAIPlannerProvider() if live else scripted_provider(case["script"], inputs))
            router = None if mode == ExecutionMode.FULL else (router_factory(case["script"]) if router_factory else (JevCapabilityRouter() if live else scripted_router(case["script"])))
            run_inputs = deepcopy(inputs)
            run = run_assessment(run_inputs.snapshot, provider, run_inputs.context, run_inputs.maintenance_request, mode=mode, router=router)
            t, r = run.planner.trace, run.routing
            router_tokens = 0 if mode == ExecutionMode.FULL else _sum_known(r.input_tokens, r.output_tokens)
            router_cost = 0 if mode == ExecutionMode.FULL else r.cost_usd
            row = {
                "case_id": case["case_id"], "mode": mode.value, "owner_message": inputs.snapshot.owner_message.text,
                "capability_count": t.exposed_capability_count, "tool_count": t.exposed_tool_count,
                "instruction_characters": t.planner_instruction_character_count, "tool_schema_characters": t.tool_schema_characters,
                "initial_context_characters": t.initial_context_characters, "cumulative_request_characters": t.cumulative_request_characters,
                "tool_result_characters": sum(item["payload_characters"] for item in t.tool_executions if item["phase"] == "planner"),
                "planner_calls": t.planner_call_count, "tool_calls": t.tool_execution_count,
                "tool_ids": t.tool_ids_called, "planner_trace": asdict(t), "routing": asdict(r),
                "planner_input_tokens": t.input_tokens, "planner_output_tokens": t.output_tokens,
                "total_tokens": _sum_known(t.input_tokens, t.output_tokens, router_tokens),
                "planner_cost_usd": t.cost_usd, "router_cost_usd": r.cost_usd,
                "total_cost_usd": _sum_known(t.cost_usd, router_cost),
                "routing_latency": r.routing_latency_seconds, "planner_latency": t.elapsed_seconds,
                "end_to_end_latency": run.elapsed_seconds,
                "quality": quality_metrics(run, inputs, case["expected"]),
                "safety": asdict(run.planner.result.safety),
                "evidence_ids": run.planner.result.assessment.evidence_ids,
                "maintenance_state": [asdict(item) for item in run.planner.maintenance_state],
            }
            row["raw_routing"] = routing_metrics(r.raw_selected_capabilities, case["expected"]["capabilities"]) if mode == ExecutionMode.ROUTED else None
            row["effective_routing"] = routing_metrics(r.effective_loaded_capabilities, case["expected"]["capabilities"])
            rows.append(row)
    rows.sort(key=lambda r: (r["case_id"], r["mode"]))
    return {"kind": "live_development" if live else "scripted_offline",
            "limitations": "Development fixtures, not held-out data. Scripted results establish plumbing, not Jev accuracy or live-model quality. Official knowledge remains quarantined for unknown model year.",
            "summary": summarize(rows), "rows": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--live-router", action="store_true")
    group.add_argument("--live-e2e", action="store_true")
    group.add_argument("--live-benchmark", action="store_true")
    parser.add_argument("--details", action="store_true")
    args = parser.parse_args()
    if args.live_router or args.live_e2e or args.live_benchmark:
        missing = missing_jev() + ([] if args.live_router else missing_xai())
        if missing:
            print("Live test not run. Missing: " + ", ".join(missing))
            return 1
    if args.live_router:
        inputs = make_inputs(load_cases()[0])
        registry, trace = CapabilityRegistry(), RoutingTrace("ROUTED")
        select_capabilities(JevCapabilityRouter(), build_routing_state(inputs.snapshot), registry, RoutingPolicy(), trace)
        print(json.dumps(asdict(trace), indent=2))
        return int(trace.fallback_used)
    cases = load_cases()[:1] if args.live_e2e else None
    report = run_benchmark(cases, live=args.live_e2e or args.live_benchmark)
    print(json.dumps(report if args.details else {k: v for k, v in report.items() if k != "rows"}, default=str, indent=2))
    if args.live_e2e:
        return int(any(not row["quality"]["task_success"] or row["routing"]["fallback_used"] for row in report["rows"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
