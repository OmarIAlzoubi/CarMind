"""Explicit local proactive run-once commands; no daemon or live transport."""

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from carmind.composition import compose_app
from carmind.ownership import utc
from carmind.proactive import ConsoleNotificationSender
from carmind.proactive_runner import ProactiveCycleRunner
from carmind.storage import OwnershipStore


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run one local proactive evaluation or delivery cycle")
    parser.add_argument("--db", type=Path, default=Path("carmind-local.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "deliver", "cycle"):
        command = commands.add_parser(name)
        command.add_argument("--owner-id")
        command.add_argument("--now", help="Aware ISO timestamp; defaults to current UTC time")
        command.add_argument("--dry-run", action="store_true")
        command.add_argument("--json", action="store_true")
        if name in ("run", "cycle"):
            command.add_argument("--vehicle-id")
        if name in ("deliver", "cycle"):
            command.add_argument("--limit", type=int, default=50)
            senders_group = command.add_mutually_exclusive_group()
            senders_group.add_argument("--console", action="store_true",
                                       help="Explicitly deliver console-channel reminders locally")
            senders_group.add_argument("--twilio", action="store_true",
                                       help="Explicitly use configured Twilio WhatsApp sender")
            command.add_argument("--confirm-live-send", action="store_true",
                                 help="Required for real Twilio delivery")
    listing = commands.add_parser("list")
    listing.add_argument("--owner-id")
    pending = commands.add_parser("pending")
    pending.add_argument("--owner-id")
    args = parser.parse_args(argv)
    store = OwnershipStore(args.db)
    try:
        if args.command in ("run", "deliver", "cycle"):
            now = utc(datetime.fromisoformat(args.now)) if args.now else datetime.now(timezone.utc)
            if args.command != "run" and not (args.console or args.twilio):
                parser.error("Delivery needs an explicitly configured sender; use --console or --twilio")
            if getattr(args, "twilio", False) and not args.dry_run and not args.confirm_live_send:
                parser.error("Real Twilio delivery requires --confirm-live-send")
            if getattr(args, "confirm_live_send", False) and not getattr(args, "twilio", False):
                parser.error("--confirm-live-send requires --twilio")
            senders = ({"console": ConsoleNotificationSender(
                output=lambda text: print(text, file=sys.stderr if args.json else sys.stdout))}
                if getattr(args, "console", False) else {})
            if getattr(args, "twilio", False):
                from carmind.twilio_gateway import TwilioConfig, notification_sender
                try:
                    senders["whatsapp"] = notification_sender(store, TwilioConfig.from_environment())
                except (ValueError, ImportError) as exc:
                    parser.error(str(exc))
            runner = ProactiveCycleRunner(compose_app(store), senders)
            if args.command == "run":
                report = runner.evaluate(now=now, owner_id=args.owner_id,
                                         vehicle_id=args.vehicle_id, dry_run=args.dry_run)
            elif args.command == "deliver":
                report = runner.deliver(now=now, owner_id=args.owner_id,
                                        limit=args.limit, dry_run=args.dry_run)
            else:
                report = runner.cycle(now=now, owner_id=args.owner_id,
                    vehicle_id=args.vehicle_id, limit=args.limit, dry_run=args.dry_run)
            payload = asdict(report)
        elif args.command == "list":
            payload = [{key: row[key] for key in ("id", "owner_id", "vehicle_id", "event_type",
                        "status", "source_type", "maintenance_item", "due_km", "due_date")}
                       for row in store.proactive_events(owner_id=args.owner_id)]
        else:
            payload = [{key: row[key] for key in ("id", "event_id", "owner_id", "vehicle_id", "channel",
                        "scheduled_for", "status", "attempt_count")}
                       for row in store.notifications(owner_id=args.owner_id, statuses=("PENDING", "DEFERRED"))]
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=lambda value: value.isoformat()))
        return 0
    finally:
        store.close()
