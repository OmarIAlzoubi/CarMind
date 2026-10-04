"""Local manufacturer-document commands; no provider or network calls."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from carmind.manufacturer_ingestion import ManufacturerIngestionService, registry_for_database
from carmind.storage import OwnershipStore


def main(argv=None):
    parser = argparse.ArgumentParser(description="Manage local manufacturer documents")
    parser.add_argument("--db", type=Path, default=Path("carmind-local.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="Register and index one or more local PDFs")
    add.add_argument("--vehicle-id", required=True)
    add.add_argument("--file", type=Path, action="append", required=True)
    add.add_argument("--manufacturer")
    add.add_argument("--model")
    add.add_argument("--year", type=int)
    add.add_argument("--market")
    add.add_argument("--variant")
    add.add_argument("--language")
    add.add_argument("--document-title")
    add.add_argument("--document-type", default="owner_manual")
    for name in ("list", "status", "rebuild"):
        command = commands.add_parser(name)
        command.add_argument("--vehicle-id", required=name != "list")
    args = parser.parse_args(argv)
    store = OwnershipStore(args.db)
    registry = registry_for_database(args.db)
    try:
        service = ManufacturerIngestionService(store, registry)
        now = datetime.now(timezone.utc)
        if args.command == "add":
            owner_id = store.owner_for_vehicle(args.vehicle_id)
            metadata = {"manufacturer": args.manufacturer, "model": args.model,
                        "model_year": args.year, "market": args.market, "variant": args.variant,
                        "language": args.language, "document_title": args.document_title,
                        "document_type": args.document_type}
            metadata = {key: value for key, value in metadata.items() if value is not None}
            results = [service.ingest(owner_id, args.vehicle_id, path, metadata=metadata, now=now)
                       for path in args.file]
            payload = [{"source_id": item.source_id, "title": item.document_title,
                        "status": item.status.value, "applicability": item.applicability.value,
                        "pages": item.page_count} for item in results]
        elif args.command == "rebuild":
            owner_id = store.owner_for_vehicle(args.vehicle_id)
            payload = service.rebuild(owner_id, args.vehicle_id, now=now)
        else:
            vehicle_ids = (args.vehicle_id,) if args.vehicle_id else store.all_vehicle_ids()
            payload = {vehicle_id: registry.list(vehicle_id) for vehicle_id in vehicle_ids}
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0
    finally:
        registry.close()
        store.close()
