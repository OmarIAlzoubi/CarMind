"""Entirely fictional schedule fixtures; never manufacturer guidance."""
from dataclasses import replace
from datetime import datetime, timezone

from carmind.contracts import MaintenanceRecord, VehicleProfile, UserMessage, VehicleContext
from carmind.evidence import FrozenEvidenceSnapshot
from carmind.manufacturer_knowledge import VehicleKnowledgeProfile, ManufacturerSource, MaintenanceRule, KnowledgePack
from carmind.tools import MaintenanceRequest

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)
PROFILE = VehicleKnowledgeProfile("Fixture", "Example", 2024, "Test", "test engine")
SOURCE = ManufacturerSource("fixture-source", "Fixture", "Example", 2024, "Test", "Fictional test data", "test_fixture", "https://example.invalid/fixture", "2026-01-01")
RULE = MaintenanceRule("fixture-oil", SOURCE.source_id, "oil", "REPLACE", "NORMAL", "Test section", "Test page", interval_km=10000, interval_months=6)
PACK = KnowledgePack(PROFILE, (SOURCE,), (RULE,))


def record(item="oil", mileage=20000, when=None, record_id="record-1"):
    return MaintenanceRecord(record_id, item, when or datetime(2026, 1, 1, tzinfo=timezone.utc), mileage)


def maintenance_context(mileage=29200):
    profile = VehicleProfile("fixture-car", "Fixture", "Example", 2024, mileage, "test engine")
    snapshot = FrozenEvidenceSnapshot("fixture-episode", profile, UserMessage("message", "What maintenance is coming up?", NOW), NOW)
    return snapshot, VehicleContext(profile, [record()]), MaintenanceRequest(PACK, PROFILE, "NORMAL")


def final(ids=(), hypothesis=None, sources=(), actions=()):
    return {"type": "final", "assessment": {
        "observations": [{"text": "Observed vehicle evidence supports this assessment.", "evidence_ids": list(ids)}] if ids else [],
        "hypotheses": [{"hypothesis_id": hypothesis, "evidence_ids": list(ids)}] if hypothesis else [],
        **({"evidence_ids": list(ids)} if ids else {}),
        **({"source_ids": list(sources)} if sources else {}),
        "uncertainties": ["CAUSE_UNCONFIRMED"],
        "recommended_action_ids": list(actions), "limitations": ["POC_ONLY"],
    }}


def call(tool, **arguments):
    return {"type": "tool_call", "tool_id": tool, "arguments": arguments}
