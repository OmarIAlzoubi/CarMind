"""Grounded selection protocol: the model cannot author authoritative facts."""

from dataclasses import dataclass
import json
import os
import re
import unicodedata

from carmind.contracts import Assessment
from carmind.safety import SafetyDecision, catalog

# These are tentative interpretations, never diagnoses or repair instructions.
HYPOTHESES = {
    "POSSIBLE_TIRE_LEAK": "A tire leak is one possible explanation; the cause is not established.",
    "POSSIBLE_STARTING_SYSTEM_ISSUE": "A battery or starting-system issue is possible; testing is needed.",
    "POSSIBLE_COOLING_ISSUE": "A cooling-system issue is possible; the cause is not established.",
    "POSSIBLE_USAGE_CHANGE": "Changes in driving usage could contribute; causation is unconfirmed.",
    "POSSIBLE_ENGINE_ISSUE": "An engine-related issue is possible; the cause is not established.",
    "INSUFFICIENT_HISTORY": "The stored history may be incomplete.",
}
UNCERTAINTIES = {
    "CAUSE_UNCONFIRMED": "The cause has not been confirmed.",
    "MISSING_EVIDENCE": "Relevant evidence is missing.",
    "HISTORY_INCOMPLETE": "Stored service history may be incomplete.",
    "APPLICABILITY_UNVERIFIED": "Manufacturer schedule applicability has not been verified.",
    "INSPECTION_NEEDED": "A professional inspection may be needed to establish the cause.",
}
LIMITATIONS = {
    "NO_PHYSICAL_INSPECTION": "No physical inspection was performed.",
    "PARTIAL_EVIDENCE": "Available evidence may not represent the full vehicle condition.",
    "POC_ONLY": "This is a development proof of concept.",
}
MAX_OBSERVATION_CLAIM_CHARACTERS = 2048
_DRIVING_CLEARANCE_PATTERNS = (
    r"\b(?:safe|okay|ok|clear)\s+to\s+drive\b",
    r"\b(?:you|the\s+(?:car|vehicle)|this\s+(?:car|vehicle)|it)\s+(?:can|may|could)\s+(?:continue\s+)?(?:to\s+)?drive\b",
    r"\b(?:continue|keep)\s+driving\b",
    r"\bdriving\s+(?:is|seems|appears)\s+safe\b",
    r"\b(?:okay|ok)\s+(?:for\s+you\s+)?to\s+drive\b",
)


def _contains_driving_clearance(text: str) -> bool:
    """Reject direct model-authored driving clearance; only policy wording may advise."""
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in _DRIVING_CLEARANCE_PATTERNS)


@dataclass(frozen=True)
class ValidatedAssessment:
    assessment: Assessment
    claims: tuple[dict, ...]
    source_ids: tuple[str, ...]
    safety: SafetyDecision
    action_wording: tuple[str, ...]


class GroundingValidationError(ValueError):
    """Invalid evidence support; distinguished for tracing, with unchanged rejection."""


def sanitize_rejected_candidate(data, evidence_ids, source_ids):
    """Evaluation-only projection, never a repaired/validated assessment.

    Keep schema structure and exposed/catalog references. Arbitrary text, unknown
    IDs (including plausible future IDs), and extra field names cannot enter logs.
    Redaction preserves list positions; it does not silently remove invalid claims.
    """
    if not isinstance(data, dict):
        return {"assessment": "<invalid_type>"}
    secrets = tuple(os.environ.get(name) for name in ("XAI_API_KEY", "TYPESAFE_API_KEY"))

    def reference(value, allowed):
        if not isinstance(value, str):
            return "<invalid_type>"
        if value not in allowed:
            return "<unexposed_id>"
        if (not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value)
                or any(secret and secret in value for secret in secrets)
                or any(marker in value.lower() for marker in ("authorization", "bearer", "api_key", "apikey", "secret", "sk-", "xai-"))):
            return "<redacted_id>"
        return value

    def references(value, allowed):
        return [reference(item, allowed) for item in value] if isinstance(value, list) else "<invalid_type>"

    allowed = {"evidence_ids": set(evidence_ids), "source_ids": set(source_ids),
               "recommended_action_ids": {a["action_id"] for a in catalog("action_catalog.json")["actions"]},
               "uncertainties": set(UNCERTAINTIES), "limitations": set(LIMITATIONS)}
    result = {key: references(data[key], ids) if key in data else "<missing>" for key, ids in allowed.items()}
    for key in ("observations", "hypotheses"):
        if key not in data:
            result[key] = "<missing>"
        elif not isinstance(data[key], list):
            result[key] = "<invalid_type>"
        else:
            claims = []
            for item in data[key]:
                if not isinstance(item, dict):
                    claims.append("<invalid_type>")
                    continue
                claim = {"evidence_ids": references(item["evidence_ids"], allowed["evidence_ids"]) if "evidence_ids" in item else "<missing>"}
                if key == "hypotheses":
                    claim["hypothesis_id"] = reference(item["hypothesis_id"], HYPOTHESES) if "hypothesis_id" in item else "<missing>"
                claim["omitted_field_count"] = len(set(item) - set(claim))
                claims.append(claim)
            result[key] = claims
    result["omitted_field_count"] = len(set(data) - set(result))
    return result


def string_list(value, label):
    if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value) or len(value) != len(set(value)):
        raise ValueError("Invalid " + label)
    return value


def _looks_like_raw_evidence(text: str) -> bool:
    """Reject obvious serialized evidence objects without banning punctuation."""
    stripped = text.strip()
    if not stripped:
        return False
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, RecursionError):
        parsed = None
    if isinstance(parsed, dict):
        return bool({"observation_id", "timestamp", "value"} & set(parsed))
    if isinstance(parsed, list) and parsed and all(isinstance(item, dict) for item in parsed):
        return any({"observation_id", "timestamp", "value"} & set(item) for item in parsed)
    return bool(re.search(r"(?:['\"])(?:observation_id|timestamp|value)(?:['\"])\s*:", stripped))


def _numeric_claims(text):
    normalized = "".join(str(unicodedata.digit(char)) if char.isdigit() else char for char in text)
    return set(re.findall(r"\d+(?:[.,]\d+)?", normalized))


def validate_assessment(data: dict, evidence: dict[str, dict], source_ids: set[str], safety: SafetyDecision) -> ValidatedAssessment:
    model_fields = {"observations", "hypotheses", "uncertainties", "recommended_action_ids", "limitations"}
    compatibility_fields = {"evidence_ids", "source_ids"}
    if not isinstance(data, dict) or not model_fields <= set(data) or not set(data) <= model_fields | compatibility_fields:
        raise ValueError("Invalid assessment fields; safety and derived fields are application-owned")
    if not isinstance(data["observations"], list) or not isinstance(data["hypotheses"], list):
        raise ValueError("Claims must be arrays")
    claims, observed, hypotheses, used = [], [], [], set()
    ordered_used = []
    for claim in data["observations"]:
        if not isinstance(claim, dict) or set(claim) != {"text", "evidence_ids"}:
            raise ValueError("Observations require concise text and evidence IDs")
        text = claim["text"]
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_OBSERVATION_CLAIM_CHARACTERS:
            raise ValueError("Observation claim text is invalid")
        if _looks_like_raw_evidence(text):
            raise ValueError("Observation claim must not copy raw evidence")
        if "```" in text or re.search(r"<\s*/?\s*[A-Za-z][^>]*>", text):
            raise ValueError("Observation claim contains markup noise")
        if _contains_driving_clearance(text) or "ignore all" in text.lower():
            raise ValueError("Observation claim contains disallowed safety wording")
        refs = string_list(claim["evidence_ids"], "claim evidence")
        if not refs or not set(refs) <= evidence.keys():
            raise GroundingValidationError("Each claim requires grounded evidence")
        used.update(refs)
        ordered_used.extend(refs)
        facts = {ref: evidence[ref] for ref in refs}
        manual_facts = [fact for fact in facts.values() if fact.get("manual_chunk")]
        if any(fact.get("applicability") == "inapplicable" for fact in manual_facts):
            raise GroundingValidationError("Inapplicable manual evidence cannot support a claim")
        brand_names = {fact["make"].strip() for fact in evidence.values()
                       if isinstance(fact, dict) and isinstance(fact.get("make"), str)
                       and fact["make"].strip()}
        generic_authority = re.search(
            r"\b(?:according to (?:the )?(?:manufacturer|owner'?s manual)|"
            r"(?:the manufacturer|the manual) (?:says|recommends|specifies))\b|"
            r"(?:حسب|وفق)\s+(?:دليل|الكتيب)|(?:يوصي|توصي)\s+(?:الشركة|الصانع)",
            text, re.IGNORECASE)
        brand_authority = any(re.search(r"\b" + re.escape(brand) +
                                  r"\s+(?:says|recommends|specifies)\b", text, re.IGNORECASE)
                              for brand in brand_names)
        if not manual_facts and (generic_authority or brand_authority):
            raise GroundingValidationError("Manufacturer authority requires cited manual evidence")
        if manual_facts:
            supported = set().union(*(_numeric_claims(fact["text"]) for fact in manual_facts))
            if not _numeric_claims(text) <= supported:
                raise GroundingValidationError("Manual claim contains a number absent from cited passages")
        claims.append({"text": text.strip(), "evidence_ids": refs, "facts": facts})
        observed.append(text.strip())
    for claim in data["hypotheses"]:
        if not isinstance(claim, dict) or set(claim) != {"hypothesis_id", "evidence_ids"} or claim["hypothesis_id"] not in HYPOTHESES:
            raise ValueError("Unknown hypothesis or free-form authoritative wording")
        refs = string_list(claim["evidence_ids"], "hypothesis evidence")
        if not refs or not set(refs) <= evidence.keys():
            raise GroundingValidationError("Hypothesis requires evidence references")
        used.update(refs)
        ordered_used.extend(refs)
        hypotheses.append(HYPOTHESES[claim["hypothesis_id"]])
        claims.append({"hypothesis_id": claim["hypothesis_id"], "evidence_ids": refs})
    ids = list(dict.fromkeys(ordered_used))
    if "evidence_ids" in data:
        legacy_ids = string_list(data["evidence_ids"], "evidence IDs")
        if legacy_ids != ids:
            raise GroundingValidationError("Evidence union does not match claim references")
    if not set(ids) <= evidence.keys():
        raise GroundingValidationError("Unknown or unexposed evidence ID")
    referenced_sources = {evidence[ref].get("source_id") for ref in ids}
    derived_sources = sorted(referenced_sources - {None})
    if not set(derived_sources) <= source_ids:
        raise GroundingValidationError("Unknown or unexposed manufacturer source")
    if "source_ids" in data:
        legacy_sources = string_list(data["source_ids"], "source IDs")
        if not set(legacy_sources) <= source_ids or set(legacy_sources) != set(derived_sources):
            raise GroundingValidationError("Unknown or unexposed manufacturer source")
    sources = derived_sources
    if not used == set(ids):
        raise GroundingValidationError("Evidence must support an explicit claim")
    if not (referenced_sources - {None}) <= set(sources):
        raise GroundingValidationError("Manufacturer-derived claims require their source IDs")
    if not set(sources) <= (referenced_sources | used):
        raise GroundingValidationError("Source reference must support a selected claim")
    def approved(field, entries):
        selected = string_list(data[field], field)
        if not set(selected) <= entries.keys():
            raise ValueError("Unknown " + field)
        return [entries[x] for x in selected]
    uncertainty = approved("uncertainties", UNCERTAINTIES)
    limitations = approved("limitations", LIMITATIONS)
    if data["hypotheses"] and not uncertainty:
        raise ValueError("Hypotheses must preserve uncertainty")
    if not used and not uncertainty:
        raise ValueError("Empty evidence requires explicit uncertainty")
    actions = {a["action_id"]: a for a in catalog("action_catalog.json")["actions"]}
    selected = string_list(data["recommended_action_ids"], "action IDs")
    for action in selected:
        if action not in actions or safety.disposition.value not in actions[action]["allowed_dispositions"]:
            raise ValueError("Unknown or disallowed action")
    result = Assessment(observed, hypotheses, ids, uncertainty, safety.disposition, selected, limitations + list(safety.limitations))
    return ValidatedAssessment(result, tuple(claims), tuple(sources), safety,
                               tuple(actions[a]["approved_wording"] for a in selected))
