"""Capability selection policy and one shared FULL/ROUTED assessment entry point."""

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from math import isfinite
from time import perf_counter

from carmind.capabilities import CapabilityRegistry
from carmind.contracts import VehicleContext
from carmind.evidence import FrozenEvidenceSnapshot
from carmind.planner import PlannerResult, run_planner
from carmind.router_provider import CapabilityRouter, RouterResponse, RouterFailure
from carmind.tools import _public_observations, _visible, maintenance_results


class ExecutionMode(str, Enum):
    FULL = "FULL"
    ROUTED = "ROUTED"


@dataclass(frozen=True)
class RoutingPolicy:
    include_threshold: float = 0.7
    uncertainty_floor: float = 0.3
    minimum_peak: float = 0.85
    coverage_required: bool = True

    def __post_init__(self):
        values = (self.uncertainty_floor, self.include_threshold, self.minimum_peak)
        if any(type(v) not in (int, float) or not isfinite(v) for v in values) or not 0 <= values[0] < values[1] <= values[2] <= 1:
            raise ValueError("Invalid routing thresholds")
        if type(self.coverage_required) is not bool:
            raise ValueError("Invalid coverage policy")


@dataclass
class RoutingTrace:
    router_mode: str
    raw_relevance: dict[str, float] = field(default_factory=dict)
    raw_selected_capabilities: tuple[str, ...] = ()
    initial_loaded_capabilities: tuple[str, ...] = ()
    effective_loaded_capabilities: tuple[str, ...] = ()
    routing_latency_seconds: float = 0.0
    routing_state_characters: int = 0
    fallback_used: bool = False
    fallback_reason: str | None = None
    expansion_requested: list[tuple[str, ...]] = field(default_factory=list)
    expansion_used: bool = False
    expanded_capabilities: tuple[str, ...] = ()
    expansion_failure_reason: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


@dataclass(frozen=True)
class AssessmentRun:
    mode: ExecutionMode
    planner: PlannerResult
    routing: RoutingTrace
    elapsed_seconds: float


def build_routing_state(snapshot, context=None, maintenance_request=None, *,
                        ownership_context=None, previous_stop=None) -> dict:
    """Bounded availability summary: no measurement values, IDs, notes or documents."""
    if type(snapshot) is not FrozenEvidenceSnapshot:
        raise ValueError("Routing requires frozen public evidence")
    if context is not None and (type(context) is not VehicleContext or context.profile != snapshot.profile):
        raise ValueError("Invalid routing context")
    observations = _public_observations(snapshot, context)
    services = [r for r in context.maintenance_records if _visible(r.performed_at, snapshot.assessment_at)] if context else []
    codes = [r for r in context.diagnostic_codes if _visible(r.observed_at, snapshot.assessment_at)] if context else []
    state = maintenance_results(maintenance_request, snapshot, context) if maintenance_request else ()
    names = sorted(Counter(o.name for o in observations).items())
    service_types = sorted({r.service_type for r in services})
    profile = snapshot.profile
    state = {
        "owner_message": snapshot.owner_message.text[:2000],
        "vehicle": {"make": profile.make[:80], "model": profile.model[:80], "year": profile.year,
                    "engine": (profile.engine or "")[:120], "mileage_km": profile.mileage_km},
        "evidence_availability": [{"name": name[:100], "count": count} for name, count in names[:40]],
        "stored_context": {"service_record_count": len(services), "service_types": [s[:80] for s in service_types[:20]],
                           "diagnostic_code_count": len(codes), "recorded_active_code_count": sum(bool(c.active) for c in codes)},
        "maintenance_status_counts": dict(sorted(Counter(item.status for item in state).items())),
        "maintenance_state_available": bool(state),
        "truncated": len(snapshot.owner_message.text) > 2000 or len(names) > 40 or len(service_types) > 20
                     or any(len(n) > 100 for n, _ in names) or any(len(s) > 80 for s in service_types)
                     or len(profile.make) > 80 or len(profile.model) > 80 or len(profile.engine or "") > 120,
    }
    # Give semantic follow-up routing enough context to recognize a continuation.
    # This contains no old telemetry values or full ownership history, and never
    # selects/reuses a capability on Jev's behalf.
    if isinstance(ownership_context, dict):
        vehicle = ownership_context.get("vehicle")
        vehicle = vehicle if isinstance(vehicle, dict) else {}
        prior = ownership_context.get("previous_assessment")
        prior = prior if isinstance(prior, dict) else {}
        recent = ownership_context.get("recent_turns")
        recent = recent if isinstance(recent, (list, tuple)) else ()
        state["follow_up_context"] = {
            "active_vehicle": {
                key: str(vehicle[key])[:120]
                for key in ("vehicle_id", "nickname", "trim")
                if isinstance(vehicle.get(key), str) and vehicle[key]
            },
            "recent_owner_messages": [
                str(item.get("text", ""))[:400]
                for item in recent[-3:]
                if isinstance(item, dict) and isinstance(item.get("text"), str) and item.get("text")
            ],
            "prior_validated_assessment": {
                "observations": [str(x)[:200] for x in prior.get("observations", [])[:3]
                                 if isinstance(x, str)],
                "hypotheses": [str(x)[:120] for x in prior.get("unconfirmed_hypotheses", [])[:3]
                               if isinstance(x, str)],
                "uncertainties": [str(x)[:120] for x in prior.get("uncertainties", [])[:3]
                                  if isinstance(x, str)],
            } if prior else None,
        }
        aftersales = ownership_context.get("aftersales", {})
        requests = aftersales.get("service_requests", ()) if isinstance(aftersales, dict) else ()
        event = ownership_context.get("active_event")
        state["follow_up_context"]["service_request_count"] = len(requests) if isinstance(requests, (tuple, list)) else 0
        if isinstance(event, dict):
            state["follow_up_context"]["active_reminder"] = {
                key: str(event[key])[:80] for key in ("event_type", "maintenance_item")
                if isinstance(event.get(key), str)}
    if previous_stop is not None:
        disposition = getattr(previous_stop, "disposition", None)
        state.setdefault("follow_up_context", {})["unresolved_safety"] = {
            "disposition": getattr(disposition, "value", str(disposition)),
            "status": "unresolved",
        }
    return state


def select_capabilities(router, state, registry, policy, trace):
    """Validate every independent relevance value before applying Python policy."""
    import json
    started = perf_counter()
    descriptions = registry.routing_descriptions()
    trace.routing_state_characters = len(json.dumps(state, sort_keys=True))
    reason = None
    try:
        if state["truncated"]:
            raise RouterFailure("routing_state_truncated")
        if router is None:
            raise RouterFailure("router_unavailable")
        response = router.route(deepcopy(state), deepcopy(descriptions))
        if not isinstance(response, RouterResponse) or not isinstance(response.relevance, dict):
            raise RouterFailure("malformed_response")
        scores = response.relevance
        if any(not isinstance(key, str) or type(value) not in (int, float) or not isfinite(value) or not 0 <= value <= 1 for key, value in scores.items()):
            raise RouterFailure("invalid_relevance")
        trace.raw_relevance = dict(sorted(scores.items()))
        trace.raw_selected_capabilities = tuple(key for key, value in trace.raw_relevance.items() if value >= policy.include_threshold)
        trace.model = response.model if isinstance(response.model, str) else None
        trace.input_tokens = response.input_tokens if type(response.input_tokens) is int and response.input_tokens >= 0 else None
        trace.output_tokens = response.output_tokens if type(response.output_tokens) is int and response.output_tokens >= 0 else None
        trace.cost_usd = response.cost_usd if type(response.cost_usd) in (int, float) and isfinite(response.cost_usd) and response.cost_usd >= 0 else None
        if set(scores) - set(descriptions):
            reason = "unknown_capability"
        elif set(scores) != set(descriptions):
            reason = "incomplete_relevance"
        elif not trace.raw_selected_capabilities and any(
            policy.uncertainty_floor < p < policy.include_threshold for p in scores.values()
        ):
            reason = "borderline_relevance"
        elif policy.coverage_required and not trace.raw_selected_capabilities:
            reason = "empty_selection"
        elif trace.raw_selected_capabilities and max(scores.values()) < policy.minimum_peak:
            reason = "insufficient_coverage"
    except TimeoutError:
        reason = "timeout"
    except RouterFailure as error:
        # Only known error categories may enter logs; exception bodies may hold secrets.
        known = {"timeout", "authentication_failed", "missing_configuration", "sdk_unavailable", "api_failure",
                 "malformed_response", "invalid_relevance", "routing_state_truncated", "router_unavailable"}
        reason = str(error) if str(error) in known else "router_failure"
    except Exception:
        reason = "router_failure"
    finally:
        trace.routing_latency_seconds = perf_counter() - started
    if reason:
        trace.fallback_used, trace.fallback_reason = True, reason
        loaded = registry.load_all_capabilities()
    else:
        loaded = registry.load_capabilities(trace.raw_selected_capabilities)
    trace.initial_loaded_capabilities = trace.effective_loaded_capabilities = tuple(pack.id for pack in loaded.packs)
    return loaded


def run_assessment(snapshot, provider, context=None, maintenance_request=None, *, mode=ExecutionMode.FULL,
                   router: CapabilityRouter | None = None, policy=RoutingPolicy(),
                   max_model_calls=4, max_tool_executions=6, ownership_context=None, previous_stop=None,
                   manual_index=None, manual_market=None) -> AssessmentRun:
    """Selection differs; planner, context preparation, safety and budgets do not.

    A failed or second targeted expansion returns an incomplete assessment rather
    than silently broadening to every capability. This deterministic policy keeps
    the benchmark honest about what the routed planner actually saw.
    """
    mode = ExecutionMode(mode)
    started = perf_counter()
    context, maintenance_request = deepcopy(context), deepcopy(maintenance_request)
    registry = CapabilityRegistry(include_manual=manual_index is not None)
    trace = RoutingTrace(mode.value)
    if mode == ExecutionMode.FULL:
        loaded = registry.load_all_capabilities()
        trace.initial_loaded_capabilities = trace.effective_loaded_capabilities = tuple(p.id for p in loaded.packs)
    else:
        try:
            state = build_routing_state(snapshot, context, maintenance_request,
                                        ownership_context=ownership_context, previous_stop=previous_stop)
        except ValueError:
            # Planner retains its normal fail-closed application evidence validation.
            loaded = registry.load_all_capabilities()
            trace.fallback_used, trace.fallback_reason = True, "invalid_routing_state"
            trace.initial_loaded_capabilities = trace.effective_loaded_capabilities = tuple(p.id for p in loaded.packs)
        else:
            loaded = select_capabilities(router, state, registry, policy, trace)

    def expand(ids, current):
        trace.expansion_requested.append(tuple(ids))
        if trace.expansion_used:
            trace.expansion_failure_reason = "expansion_limit"
            return None
        # Count one attempt even if the ID is wrong or adds no coverage.
        trace.expansion_used = True
        if len(ids) != 1:
            trace.expansion_failure_reason = "expansion_requires_one_capability"
            return None
        try:
            expanded = registry.load_additional(current, ids)
        except ValueError:
            trace.expansion_failure_reason = "unknown_expansion_capability"
            return None
        added = tuple(p.id for p in expanded.packs if p.id not in trace.effective_loaded_capabilities)
        if not added:
            trace.expansion_failure_reason = "no_additional_coverage"
            return None
        trace.expanded_capabilities = added
        trace.effective_loaded_capabilities = tuple(p.id for p in expanded.packs)
        return expanded

    result = run_planner(snapshot, provider, context, maintenance_request, loaded=loaded,
                         expansion_handler=expand, max_model_calls=max_model_calls,
                         max_tool_executions=max_tool_executions, ownership_context=ownership_context,
                         previous_stop=previous_stop, manual_index=manual_index,
                         manual_market=manual_market)
    return AssessmentRun(mode, result, trace, perf_counter() - started)
