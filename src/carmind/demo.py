"""Milestone 4 demonstrations; optional live smoke, no delivery or persistence."""

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json

from carmind.contracts import MaintenanceRecord
from carmind.manufacturer_knowledge import ROOT, load_knowledge, VehicleKnowledgeProfile, ManufacturerSource, MaintenanceRule, KnowledgePack
from carmind.maintenance import evaluate_maintenance, reminder_events
from carmind.planner import run_full_planner
from carmind.planner_provider import FakePlannerProvider, XAIPlannerProvider, missing_configuration
from carmind.simulator import generate_episode, freeze_episode


def diagnostic_demo():
    episode, _ = generate_episode("gradual_tire_pressure_loss", 42)
    snapshot = freeze_episode(episode)
    ids = [o.observation_id for o in snapshot.observations if o.name == "rear_left_tire_pressure"]
    provider = FakePlannerProvider([
        {"type": "tool_call", "tool_id": "get_tire_pressure_history", "arguments": {"wheel": "rear_left"}},
        {"type": "final", "assessment": {
            "observations": [{"text": "Rear-left tire pressure declined across the visible readings.", "evidence_ids": ids}],
            "hypotheses": [{"hypothesis_id": "POSSIBLE_TIRE_LEAK", "evidence_ids": ids}],
            "uncertainties": ["CAUSE_UNCONFIRMED", "MISSING_EVIDENCE"],
            "recommended_action_ids": ["ARRANGE_SERVICE_REVIEW", "SHARE_EVIDENCE_WITH_WORKSHOP"],
            "limitations": ["POC_ONLY", "NO_PHYSICAL_INSPECTION"],
        }},
    ])
    result = run_full_planner(snapshot, provider)
    return {"owner_message": snapshot.owner_message.text, "planner": asdict(result)}


def maintenance_demo():
    now = datetime(2026, 9, 28, tzinfo=timezone.utc)
    pack = load_knowledge(ROOT / "eval/fixtures/manufacturer_knowledge")
    record = MaintenanceRecord("demo-filter-service", "cabin_air_filter", datetime(2025, 4, 1, tzinfo=timezone.utc), 20000)
    owner_year = pack.profile.model_year + 1 if pack.profile.model_year is not None else 2024
    owner_profile = replace(pack.profile, model_year=owner_year)
    official = evaluate_maintenance(pack, owner_profile, "source-demo", now, 49200, [record], "NORMAL")
    # A separate, unmistakably fictional fixture proves the calculation without
    # assigning a guessed model year or activating the quarantined official pack.
    profile = VehicleKnowledgeProfile("Fictional", "Maintenance Demo", 2024, "Simulation")
    source = ManufacturerSource("fictional-demo-source", "Fictional", "Maintenance Demo", 2024, "Simulation",
                                "Synthetic arithmetic fixture - not manufacturer guidance", "test_fixture",
                                "https://example.invalid/carmind-demo", "2026-09-28")
    rule = MaintenanceRule("fictional-filter-rule", source.source_id, "cabin_air_filter", "REPLACE", "NORMAL",
                           "Synthetic fixture", "Not a manufacturer page", interval_km=30000, interval_months=18)
    fixture = KnowledgePack(profile, (source,), (rule,))
    calculated = evaluate_maintenance(fixture, profile, "fictional-maintenance-demo", now, 49200, [record], "NORMAL")
    updated = evaluate_maintenance(fixture, profile, "fictional-maintenance-demo", now, 49200,
                                  [record, MaintenanceRecord("new-filter-service", "cabin_air_filter", now, 49200)], "NORMAL")
    item = calculated[0]
    return {
        "current_at": now, "current_odometer_km": 49200, "last_relevant_service": asdict(record),
        "official_pack": {"profile": asdict(pack.profile), "statuses": [asdict(r) for r in official],
                          "reminder_events": [asdict(r) for r in reminder_events(official)],
                          "limitation": "The demo owner year does not match this source, so no actionable manufacturer reminder is demonstrated."},
        "synthetic_calculation_only": {"status": asdict(item), "events": [asdict(r) for r in reminder_events(calculated)],
                                       "after_recorded_service": [asdict(r) for r in updated],
                                       "deterministic_explanation": f"Fictional demonstration only: {item.maintenance_item} is {item.status}; {item.remaining_km:g} km or {item.remaining_days} days remain, whichever comes first."},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Explicitly request the optional xAI smoke test")
    args = parser.parse_args()
    if args.live:
        missing = missing_configuration()
        if missing:
            print("Live smoke not run. Missing: " + ", ".join(missing))
            return 1
        try:
            snapshot = freeze_episode(generate_episode("healthy_vehicle", 42)[0])
            result = run_full_planner(snapshot, XAIPlannerProvider())
        except RuntimeError as error:
            print(str(error))
            return 1
        print(json.dumps({"success": result.trace.completion_status == "complete", "trace": asdict(result.trace)}, indent=2))
        return 0 if result.trace.completion_status == "complete" else 1
    print(json.dumps({"diagnostic": diagnostic_demo(), "maintenance": maintenance_demo()}, default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
