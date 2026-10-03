"""Deterministic simulation policy over visible evidence, never evaluator truth."""

from dataclasses import dataclass, replace
import json
from math import isfinite

from carmind.contracts import ObservationSource, SafetyDisposition, VehicleContext
from carmind.evidence import FrozenEvidenceSnapshot
from carmind.manufacturer_knowledge import ROOT
from carmind.tools import _public_observations, TIRE_NAMES


def catalog(name: str) -> dict:
    return json.loads((ROOT / "data" / name).read_text(encoding="utf-8"))


@dataclass(frozen=True)
class SafetyDecision:
    disposition: SafetyDisposition
    rule_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    message_id: str
    approved_text: str
    limitations: tuple[str, ...]
    policy_version: int = 1
    user_limitations: tuple[str, ...] = ()


def retain_unresolved_stop(current: SafetyDecision, previous: SafetyDecision | None) -> SafetyDecision:
    """An unresolved prior stop is a constraint, never fresh telemetry or clearance.

    The application supplies only an earlier deterministic decision. No model or
    service-history write can clear it in this MVP. It remains active until a
    dedicated clearance mechanism or explicitly defined deterministic resolution
    event is added. Default callers are unchanged.
    """
    if previous is None:
        return current
    if type(previous) is not SafetyDecision or previous.disposition != SafetyDisposition.STOP_WHEN_SAFE:
        raise ValueError("Invalid prior safety constraint.")
    if current.disposition == SafetyDisposition.STOP_WHEN_SAFE:
        return current
    note = "The earlier stop warning remains unresolved; no new assessment has cleared it."
    return replace(previous, limitations=tuple(dict.fromkeys((*previous.limitations, *current.limitations, note))),
                   user_limitations=(note,))


def evaluate_safety(snapshot: FrozenEvidenceSnapshot, context: VehicleContext | None = None) -> SafetyDecision:
    if type(snapshot) is not FrozenEvidenceSnapshot:
        raise ValueError("Safety requires a frozen public snapshot")
    rules = catalog("safety_rules.json")
    hits, evidence, limitations = [], set(), []
    uncertain = False
    scope = rules["scope"]
    if any(getattr(snapshot.profile, name) != value for name, value in scope.items()):
        uncertain = True
        limitations.append("No safety threshold policy is configured for this vehicle.")
        observations = ()
    else:
        try:
            if context is not None and (type(context) is not VehicleContext or context.profile != snapshot.profile):
                raise ValueError("Mismatched context")
            observations = _public_observations(snapshot, context)
        except ValueError:
            observations = ()
            uncertain = True
            limitations.append("Conflicting or invalid evidence.")
    groups = {}
    for item in observations:
        groups.setdefault(item.name, []).append(item)
    critical = {"coolant_temperature": "degC", "battery_voltage": "V", **{n: "psi" for n in TIRE_NAMES}}
    valid = {}
    for name, unit in critical.items():
        samples = groups.get(name, [])
        if not samples:
            uncertain = True
            limitations.append("Missing critical evidence: " + name)
            continue
        conflicting = len({x.timestamp for x in samples}) != len(samples)
        malformed = any(x.source != ObservationSource.SIMULATOR or x.unit != unit or type(x.value) not in (int, float) or not isfinite(x.value) or x.value < 0 for x in samples)
        stale = (snapshot.assessment_at - samples[-1].timestamp).total_seconds() > rules["max_age_seconds"]
        if conflicting or malformed or stale:
            uncertain = True
            limitations.append("Unusable critical evidence: " + name)
        else:
            valid[name] = samples
            minimum_samples = rules["coolant"]["samples"] if name == "coolant_temperature" else 2
            if len(samples) < minimum_samples:
                uncertain = True
                limitations.append("Insufficient critical history: " + name)
    coolant = rules["coolant"]
    samples = valid.get("coolant_temperature", [])[-coolant["samples"]:]
    if len(samples) == coolant["samples"] and all(s.value >= coolant["minimum"] for s in samples) and (samples[-1].timestamp - samples[0].timestamp).total_seconds() >= coolant["duration_seconds"]:
        hits.append((coolant["disposition"], coolant["rule_id"]))
        evidence.update(s.observation_id for s in samples)
    tires = rules["tires"]
    for name in TIRE_NAMES:
        samples = valid.get(name, [])
        if samples:
            recent = [s for s in samples if (samples[-1].timestamp - s.timestamp).total_seconds() <= tires["window_seconds"]]
            if samples[-1].value <= tires["minimum"] or (len(recent) >= 2 and recent[0].value - recent[-1].value >= tires["drop"]):
                hits.append((tires["disposition"], tires["rule_id"]))
                evidence.update(s.observation_id for s in recent)
    states = groups.get("operating_state", [])
    if len({s.timestamp for s in states}) != len(states) or any(s.source != ObservationSource.SIMULATOR or s.value not in {"off", "running", "starting"} for s in states):
        states = []
        uncertain = True
    battery = rules["starting_voltage"]
    starting = {s.timestamp: s for s in states if s.value == "starting"}
    paired = [s for s in valid.get("battery_voltage", []) if s.timestamp in starting and s.value <= battery["maximum"]]
    if len(paired) >= battery["samples"]:
        hits.append((battery["disposition"], battery["rule_id"]))
        evidence.update(s.observation_id for s in paired)
        evidence.update(starting[s.timestamp].observation_id for s in paired)
    if "battery_voltage" in valid and not states:
        uncertain = True
        limitations.append("Starting-state evidence unavailable.")
    choices = {d for d, _ in hits}
    if uncertain:
        choices.add("UNDETERMINED")
    disposition = next((d for d in rules["precedence"] if d in choices), "NO_RULE_TRIGGERED")
    message = next(m for m in catalog("safety_messages.json")["messages"] if m["disposition"] == disposition)
    limitations.append("Simulation policy only; absence of a rule trigger is not driving clearance.")
    internal_limitations = tuple(limitations)
    rule_ids = tuple(sorted({r for _, r in hits}))
    # Keep the complete coverage audit internally, while presenting only gaps
    # tied to the active rule when a rule has a clear domain.  With no active
    # rule, retain the broader uncertainty because no narrower safety conclusion
    # is available.
    rule_channels = {rules[key]["rule_id"]: set(rules[key]["required_evidence"])
                     for key in ("coolant", "tires", "starting_voltage")}
    relevant_channels = set().union(*(rule_channels.get(rule, set()) for rule in rule_ids))
    def is_relevant_limitation(item):
        if item.startswith("Simulation policy only"):
            return True
        if item == "Starting-state evidence unavailable.":
            return "operating_state" in relevant_channels
        return any(item.endswith(": " + channel) for channel in relevant_channels)
    if not relevant_channels:
        # Without an active domain rule, the approved safety message explains
        # that the conclusion is unavailable; keep the detailed channel audit
        # internal rather than listing every unrelated missing signal.
        user_limitations = tuple(item for item in internal_limitations
                                 if item.startswith("Simulation policy only")
                                 or item in {"No safety threshold policy is configured for this vehicle.",
                                              "Conflicting or invalid evidence."})
    else:
        user_limitations = tuple(item for item in internal_limitations if is_relevant_limitation(item))
    return SafetyDecision(SafetyDisposition(disposition), rule_ids, tuple(sorted(evidence)),
                          message["message_id"], message["approved_text"], internal_limitations,
                          rules["version"], user_limitations)
