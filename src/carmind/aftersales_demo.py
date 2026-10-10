"""Reproducible public aftersales journey; no live model, Twilio or dealer API."""

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from carmind.aftersales import demo_slots
from carmind.app import CarMindApp
from carmind.contracts import MaintenanceRecord, VehicleProfile
from carmind.ownership_demo import command_turn, empty_final, fictional_schedule
from carmind.planner_provider import FakePlannerProvider
from carmind.product import InboundMessage, ProductService
from carmind.storage import OwnershipStore


NOW = datetime(2026, 1, 6, 12, tzinfo=timezone.utc)


def run_demo(path: Path) -> dict:
    """Use an isolated database path; all manufacturer facts are TEST_ONLY fiction."""
    store = OwnershipStore(path)
    try:
        app = CarMindApp(store, FakePlannerProvider([]),
                         approved_schedules=(fictional_schedule(),), allow_test_schedules=True)
        owner = app.create_owner(now=NOW, owner_id="fictional-owner")
        profile = VehicleProfile("fictional-car", "Fictional", "Everyday", 2024, 25000,
                                 "Fictional gasoline engine")
        app.add_vehicle(owner, profile, now=NOW, market="TEST_ONLY", nickname="My demo car")
        session = app.start_session(owner, now=NOW, vehicle_id=profile.vehicle_id,
                                    session_id="fictional-session")
        service = ProductService(app)
        contact = "whatsapp:+15550000000"  # synthetic; omitted from output
        service.bind_existing("whatsapp", contact, owner, session)
        with store.transaction():
            store.record_service("synthetic-prior-service", profile.vehicle_id,
                MaintenanceRecord("synthetic-prior-service", "oil", NOW - timedelta(days=180), 15000),
                NOW, "synthetic-fixture")
        _, maintenance = app.read_maintenance(owner, profile.vehicle_id, now=NOW)
        due = next((item for item in maintenance if item.status in ("DUE", "OVERDUE")), None)
        conversation = []
        turn = 0

        def say(text, responses=None, *, confirm=None, related_event_id=None):
            nonlocal turn
            turn += 1
            if responses is not None:
                app.provider = FakePlannerProvider(responses)
            reply = service.handle(InboundMessage("whatsapp", contact, f"demo-{turn}", text,
                NOW + timedelta(minutes=turn), confirmation_id=confirm, related_event_id=related_event_id))
            conversation.append({"customer": text, "carmind": reply.text,
                                 "status": reply.status, "proposal_id": reply.proposal_id})
            return reply

        maintenance_final = empty_final()
        if due:
            maintenance_final["assessment"]["observations"] = [{
                "text": "The TEST_ONLY fictional oil schedule has a due item for this vehicle.",
                "evidence_ids": [due.reminder_id]}]
        say("What maintenance is due for my car?", [
            {"type": "tool_call", "tool_id": "get_due_maintenance_items", "arguments": {}},
            maintenance_final])
        event_id = next(event["id"] for event in store.proactive_events(owner_id=owner)
                        if event["event_type"] in ("MAINTENANCE_DUE", "MAINTENANCE_OVERDUE"))
        text = "Okay, I want to get it done"
        request = say(text, [command_turn("create_service_request",
            {"intent_type": "routine_maintenance", "requested_services": ["oil"],
             "symptoms": []}, text)], related_event_id=event_id)
        say("confirm", confirm=request.proposal_id)
        request_id = store.service_requests(owner, profile.vehicle_id)[0]["id"]
        slot = demo_slots(NOW + timedelta(minutes=4))[0]
        say("Can I book a demo appointment?", [{"type": "request_clarification",
            "question": "These are SIMULATION slots only: tomorrow at 10:00 or 14:00 UTC. Which do you prefer?"}])
        text = "Choose the 10:00 UTC demo appointment"
        booking = say(text, [command_turn("book_demo_slot",
            {"service_request_id": request_id, "slot_id": slot.slot_id}, text)])
        say("confirm", confirm=booking.proposal_id)
        text = "I would also like a person to follow up"
        handoff = say(text, [command_turn("request_human_handoff",
            {"reason": "customer requests follow-up", "service_request_id": request_id}, text)])
        say("confirm", confirm=handoff.proposal_id)
        overview = service.overview("whatsapp", contact, now=NOW + timedelta(minutes=10))
        return {"mode": "OFFLINE_SYNTHETIC", "manufacturer_evidence": "TEST_ONLY_NON_PRODUCTION_FICTIONAL",
                "conversation": conversation, "vehicle": overview["vehicle"],
                "maintenance": overview["maintenance"], "service_requests": overview["service_requests"],
                "demo_bookings": overview["demo_bookings"], "handoffs": overview["handoffs"],
                "business_events": overview["business_events"],
                "event_counts": service.aftersales_analytics("whatsapp", contact),
                "notification_state": overview["needs_attention"]}
    finally:
        store.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline synthetic CarMind aftersales demo")
    parser.add_argument("--db", type=Path, help="New isolated local demo database path")
    args = parser.parse_args(argv)
    if args.db:
        if args.db.exists():
            parser.error("Choose a new isolated database path")
        report = run_demo(args.db)
    else:
        with TemporaryDirectory(prefix="carmind-aftersales-") as directory:
            report = run_demo(Path(directory) / "demo.sqlite3")
    print(json.dumps(report, ensure_ascii=True, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
