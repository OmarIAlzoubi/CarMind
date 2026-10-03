"""Scripted Arabic/English product journey through the real WhatsApp adapter."""

import argparse
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from carmind.composition import compose_app
from carmind.contracts import UserMessage
from carmind.evidence import FrozenEvidenceSnapshot
from carmind.ownership_demo import command_turn, empty_final
from carmind.manufacturer_manual import DEFAULT_INDEX, ManualIndex, open_optional_index
from carmind.planner_provider import FakePlannerProvider
from carmind.product import InboundMessage, ProductService
from carmind.provider_config import require_live_config
from carmind.simulator import freeze_episode, generate_episode
from carmind.storage import OwnershipStore
from carmind.whatsapp import handle_payload


SENDER = "demo-sender"


def run_demo(db_path: Path, output=print, *, manual_source=None):
    """No semantic quality claim: fake responses are scripted evaluation fixtures."""
    store = OwnershipStore(db_path)
    manual_index = None
    if DEFAULT_INDEX.is_file():
        try:
            candidate = ManualIndex(source_file=manual_source)
            if (candidate.source.manufacturer.casefold(), candidate.source.model.casefold()) == ("hyundai", "elantra n"):
                manual_index = candidate
            else:
                candidate.close()
        except (FileNotFoundError, ValueError):
            pass  # Scripted example remains usable without its matching manual.
    try:
        if store.binding("whatsapp", SENDER):
            raise ValueError("This demo sender already exists in that database; choose a fresh local database.")
        started = datetime.now(timezone.utc) - timedelta(days=2)
        app = compose_app(store, provider=FakePlannerProvider([]), manual_index=manual_index)
        service = ProductService(app)
        serial = 0

        def say(text, *, at, provider=None, snapshot=None, channel="whatsapp"):
            nonlocal serial
            serial += 1
            if provider is not None:
                app.provider = FakePlannerProvider(provider)
            if channel == "whatsapp":
                if snapshot is None:
                    reply = handle_payload(service, {"From": SENDER, "MessageSid": f"demo-{serial}",
                                                     "Body": text, "Timestamp": at.isoformat()}, received_at=at)
                else:
                    reply = service.handle(InboundMessage("whatsapp", SENDER, f"demo-{serial}",
                                                          text, at, snapshot=snapshot))
            else:
                reply = service.handle(InboundMessage("web", "local-web-user", f"demo-{serial}", text, at))
            output(f"{channel} · You: {text}")
            output("CarMind: " + reply.text)
            return reply

        first = "سيارتي Hyundai Elantra N موديل 2023 وممشاها 18500 كم"
        onboard = {"type": "vehicle_proposal", "owner_quote": first,
                   "vehicle": {"make": "Hyundai", "model": "Elantra N", "year": 2023,
                               "odometer": 18500, "unit": "km", "trim": None,
                               "engine": None, "market": None, "nickname": None}}
        pending = say(first, at=started, provider=[onboard])
        assert pending.status == "confirmation_required"
        say("أكد", at=started + timedelta(minutes=1))
        view = service.overview("whatsapp", SENDER, now=started + timedelta(minutes=2))
        vehicle_id = view["vehicle"]["id"]

        current = empty_final()
        current["assessment"]["observations"] = [{"text": "Recorded vehicle profile and mileage are available.",
                                                     "evidence_ids": [vehicle_id]}]
        say("كيف وضع سيارتي؟", at=started + timedelta(minutes=2), provider=[
            {"type": "tool_call", "tool_id": "get_vehicle_profile", "arguments": {}}, current])

        service_at = started + timedelta(days=1)
        oil_text = "غيرت الزيت اليوم على 20000 كم"
        oil_command = command_turn("record_service_event", {"service_type": "oil",
            "performed_at": service_at.isoformat(), "odometer": 20000, "unit": "km"}, oil_text)
        say(oil_text, at=service_at, provider=[oil_command])
        say("أكد", at=service_at + timedelta(minutes=1))
        oil_record = store.service_records(vehicle_id, service_at + timedelta(minutes=2))[0]
        history = empty_final()
        history["assessment"]["observations"] = [{"text": "The saved oil service is available.",
                                                     "evidence_ids": [oil_record.record_id]}]
        say("متى آخر مرة غيرت الزيت؟", at=service_at + timedelta(minutes=2), provider=[
            {"type": "tool_call", "tool_id": "get_latest_service_record",
             "arguments": {"service_type": "oil"}}, history])
        if manual_index is not None:
            profile = store.vehicle(service.store.binding("whatsapp", SENDER)["owner_id"],
                                    vehicle_id, service_at + timedelta(minutes=2)).profile
            manual_hit = manual_index.search("engine oil specification 0W-30", profile, top_k=1)[0]
            manual_final = empty_final()
            manual_final["assessment"].update(
                observations=[{"text": "الدليل المتاح يذكر زيت محرك SAE 0W-30.",
                               "evidence_ids": [manual_hit["evidence_id"]]}],
                uncertainties=["APPLICABILITY_UNVERIFIED"])
            manual_reply = say("وش الزيت المناسب لسيارتي؟", at=service_at + timedelta(minutes=2), provider=[
                {"type": "tool_call", "tool_id": "search_manufacturer_manual",
                 "arguments": {"query": "engine oil specification 0W-30", "top_k": 1}},
                manual_final])
            assert manual_reply.sources
            output("Manual source: " + manual_reply.sources[0]["section"] +
                   f" · PDF p. {manual_reply.sources[0]['physical_page']}")
        unknown = empty_final()
        unknown["assessment"]["uncertainties"] = ["APPLICABILITY_UNVERIFIED"]
        say("وش الصيانة الجاية؟", at=service_at + timedelta(minutes=3), provider=[unknown])

        tire = freeze_episode(generate_episode("gradual_tire_pressure_loss", 42)[0])
        tire_at = service_at + timedelta(minutes=4)
        offset = tire_at - tire.assessment_at
        profile = store.vehicle(service.store.binding("whatsapp", SENDER)["owner_id"], vehicle_id, tire_at).profile
        observations = tuple(replace(item, timestamp=item.timestamp + offset) for item in tire.observations)
        snapshot = FrozenEvidenceSnapshot("demo-tire", profile,
                       UserMessage("demo-tire", "الكفر الخلفي يسار ينقص هوا", tire_at), tire_at, observations)
        ids = [o.observation_id for o in observations if o.name == "rear_left_tire_pressure"]
        diagnostic = empty_final()
        diagnostic["assessment"].update(
            observations=[{"text": "ضغط الكفر الخلفي اليسار انخفض في القراءات التجريبية المتاحة.",
                           "evidence_ids": ids}],
            hypotheses=[{"hypothesis_id": "POSSIBLE_TIRE_LEAK", "evidence_ids": ids}],
            uncertainties=["CAUSE_UNCONFIRMED"], limitations=["NO_PHYSICAL_INSPECTION"])
        say("الكفر الخلفي يسار ينقص هوا", at=tire_at, provider=[
            {"type": "tool_call", "tool_id": "get_tire_pressure_history",
             "arguments": {"wheel": "rear_left"}}, diagnostic], snapshot=snapshot)
        say("أقدر أمشي؟", at=tire_at + timedelta(minutes=1), provider=[empty_final()])

        binding = store.binding("whatsapp", SENDER)
        service.bind_existing("web", "local-web-user", binding["owner_id"], binding["session_id"])
        web_view = service.overview("web", "local-web-user", now=tire_at + timedelta(minutes=2))
        assert web_view["vehicle"]["id"] == vehicle_id
        assert web_view["last_service"]["odometer_km"] == 20000
        output("Web · My Car: " + web_view["vehicle"]["label"] + f" · {web_view['odometer']['km']:,.0f} km")
        output("Web · Last service: " + web_view["last_service"]["type"])
        output("Web · Manufacturer guidance: " + web_view["manufacturer"]["status"])

        odometer_at = tire_at + timedelta(minutes=3)
        odo_text = "My odometer is now 20,200 km."
        odo_command = command_turn("update_odometer", {"reading": 20200, "unit": "km",
                                                     "occurred_at": odometer_at.isoformat()}, odo_text)
        say(odo_text, at=odometer_at, provider=[odo_command], channel="web")
        say("confirm", at=odometer_at + timedelta(minutes=1), channel="web")
        mileage = empty_final()
        mileage["assessment"]["observations"] = [{"text": "The current recorded odometer is available.",
                                                     "evidence_ids": [vehicle_id]}]
        say("كم ممشى السيارة عندك؟", at=odometer_at + timedelta(minutes=2), provider=[
            {"type": "tool_call", "tool_id": "get_vehicle_profile", "arguments": {}}, mileage])
        return service.overview("web", "local-web-user", now=odometer_at + timedelta(minutes=2))
    finally:
        store.close()
        if manual_index is not None:
            manual_index.close()


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Local WhatsApp-style simulator; no Twilio delivery")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--live", action="store_true", help="Interactive Jev/xAI conversation; uses real credits")
    parser.add_argument("--confirm-live-api-use", action="store_true")
    parser.add_argument("--max-calls-per-turn", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument("--manual-index", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--manual-source", type=Path, help="Registered local manual_source.json")
    parser.add_argument("--sender", default=SENDER, help="Local simulated sender identity")
    parser.add_argument("--link-web-user", action="store_true", help="Use the existing local Web owner in this database")
    args = parser.parse_args(argv)
    if args.live:
        try:
            require_live_config(confirmed=args.confirm_live_api_use,
                                max_calls_per_turn=args.max_calls_per_turn,
                                timeout_seconds=args.timeout_seconds)
            index = open_optional_index(args.manual_index, source_file=args.manual_source)
        except (ValueError, FileNotFoundError) as exc:
            parser.error(str(exc))
        store = OwnershipStore(args.db)
        try:
            service = ProductService(compose_app(store, live=True, manual_index=index,
                timeout_seconds=args.timeout_seconds, max_model_calls=args.max_calls_per_turn),
                activity_logger=print)
            if args.link_web_user:
                existing = store.binding("web", "local-web-user")
                if not existing:
                    parser.error("No local Web owner exists in this database")
                service.bind_existing("whatsapp", args.sender, existing["owner_id"], existing["session_id"])
            print("Local WhatsApp-style conversation (live; no Twilio). Type /quit to exit.")
            for line in sys.stdin:
                text = line.strip()
                if text == "/quit":
                    break
                if not text:
                    continue
                reply = handle_payload(service, {"From": args.sender,
                    "MessageSid": "local-" + str(uuid4()), "Body": text},
                    received_at=datetime.now(timezone.utc))
                print("CarMind: " + reply.text)
                for source in reply.sources:
                    print(f"  Source: {source['title']} · {source['section']} · PDF p. {source['physical_page']}")
        finally:
            store.close()
            if index is not None:
                index.close()
        return 0
    if args.confirm_live_api_use:
        parser.error("--confirm-live-api-use requires --live")
    result = run_demo(args.db, manual_source=args.manual_source)
    print("Offline journey complete: same vehicle and records in WhatsApp and Web.")
    print("Current odometer:", f"{result['odometer']['km']:,.0f} km")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
