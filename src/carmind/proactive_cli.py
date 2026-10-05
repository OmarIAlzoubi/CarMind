"""Explicit local proactive evaluation; no daemon or outbound delivery."""

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path

from carmind.composition import compose_app
from carmind.ownership import utc
from carmind.storage import OwnershipStore


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run local proactive checks without sending messages")
    parser.add_argument("--db", type=Path, default=Path("carmind-local.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--owner-id")
    run.add_argument("--vehicle-id")
    run.add_argument("--now", help="Aware ISO timestamp; defaults to current UTC time")
    listing = commands.add_parser("list")
    listing.add_argument("--owner-id")
    pending = commands.add_parser("pending")
    pending.add_argument("--owner-id")
    args = parser.parse_args(argv)
    store = OwnershipStore(args.db)
    try:
        if args.command == "run":
            now = utc(datetime.fromisoformat(args.now)) if args.now else datetime.now(timezone.utc)
            payload = asdict(compose_app(store).evaluate_proactive_state(
                now=now, owner_id=args.owner_id, vehicle_id=args.vehicle_id))
        elif args.command == "list":
            payload = [{key: row[key] for key in ("id", "owner_id", "vehicle_id", "event_type",
                        "status", "source_type", "maintenance_item", "due_km", "due_date")}
                       for row in store.proactive_events(owner_id=args.owner_id)]
        else:
            payload = [{key: row[key] for key in ("id", "event_id", "owner_id", "vehicle_id", "channel",
                        "scheduled_for", "status", "attempt_count")}
                       for row in store.notifications(owner_id=args.owner_id, statuses=("PENDING", "DEFERRED"))]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    finally:
        store.close()
