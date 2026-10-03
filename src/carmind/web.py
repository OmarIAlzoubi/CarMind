"""Small localhost product UI and JSON API; no production authentication claim."""

import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
from time import perf_counter
from urllib.parse import urlsplit

from carmind.composition import compose_app
from carmind.manufacturer_manual import DEFAULT_INDEX, open_optional_index
from carmind.product import InboundMessage, ProductService
from carmind.provider_config import require_live_config
from carmind.storage import OwnershipStore


PAGE = Path(__file__).with_name("web_static") / "index.html"


def make_handler(service, *, web_identity="local-web-user", live=False):
    class Handler(BaseHTTPRequestHandler):
        def _json(self, status, data):
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/":
                body = PAGE.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path not in ("/api/overview", "/api/vehicles", "/api/services", "/api/reminders"):
                self._json(404, {"error": "Not found"})
                return
            try:
                overview = service.overview("web", web_identity, now=datetime.now(timezone.utc))
                if path == "/api/vehicles":
                    data = overview.get("vehicles", [])
                elif path == "/api/services":
                    data = overview.get("services", [])
                elif path == "/api/reminders":
                    data = overview.get("reminders", [])
                else:
                    data = {**overview, "ui_mode": "live" if live else "offline"}
                self._json(200, data)
            except ValueError:
                self._json(404, {"error": "No car has been set up yet."})

        def do_POST(self):
            path = urlsplit(self.path).path
            if path not in ("/api/chat", "/api/confirm", "/api/vehicle/select", "/api/proposal/cancel"):
                self._json(404, {"error": "Not found"})
                return
            try:
                started = perf_counter()
                size = int(self.headers.get("Content-Length", "0"))
                if size < 2 or size > 8192 or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    raise ValueError("Invalid request")
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise ValueError("Invalid request")
                now = datetime.now(timezone.utc)
                if path == "/api/proposal/cancel":
                    service.cancel_proposal("web", web_identity, data["proposal_id"])
                    self._json(200, {"status": "cancelled"})
                    return
                if path == "/api/vehicle/select":
                    vehicle_id = data["vehicle_id"]
                    if not isinstance(vehicle_id, str) or not vehicle_id.strip() or len(vehicle_id) > 128:
                        raise ValueError("Invalid vehicle selection")
                    service.select_vehicle("web", web_identity, vehicle_id, now=now)
                    self._json(200, service.overview("web", web_identity, now=now))
                    return
                text = data.get("text", "confirm" if path == "/api/confirm" else None)
                inbound = InboundMessage("web", web_identity, data.get("message_id"), text, now,
                                         locale=data.get("locale"),
                                         confirmation_id=data.get("proposal_id") if path == "/api/confirm" else None)
                reply = service.handle(inbound)
                self._json(200, {"text": reply.text, "status": reply.status,
                                 "active_vehicle_id": reply.active_vehicle_id,
                                 "active_vehicle_label": reply.active_vehicle_label,
                                 "proposal_id": reply.proposal_id, "proposal": reply.proposal,
                                 "safety_notice": reply.safety_notice, "sources": reply.sources})
                print(f"[CHAT] mode={'live' if live else 'offline'} status={reply.status} "
                      f"sources={len(reply.sources)} elapsed_ms={(perf_counter()-started)*1000:.0f}")
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                self._json(400, {"error": "Please check the request and selected car."})

        def log_message(self, format, *args):
            pass

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local CarMind conversation (offline by default)")
    parser.add_argument("--db", type=Path, default=Path("carmind-local.sqlite3"))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--live", action="store_true", help="Explicitly enable configured Jev/xAI providers; uses real credits")
    parser.add_argument("--confirm-live-api-use", action="store_true")
    parser.add_argument("--max-calls-per-turn", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument("--manual-index", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--manual-source", type=Path, help="Registered local manual_source.json")
    parser.add_argument("--link-whatsapp-sender", help="Trusted local demo link to an existing sender")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535")
    if args.live:
        try:
            require_live_config(confirmed=args.confirm_live_api_use,
                                max_calls_per_turn=args.max_calls_per_turn,
                                timeout_seconds=args.timeout_seconds)
        except ValueError as exc:
            parser.error(str(exc))
    elif args.confirm_live_api_use:
        parser.error("--confirm-live-api-use requires --live")
    try:
        manual_index = open_optional_index(args.manual_index, source_file=args.manual_source)
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))
    if manual_index is None:
        print("[RAG] local manufacturer documentation unavailable; other features remain available")
    store = OwnershipStore(args.db)
    try:
        service = ProductService(compose_app(store, live=args.live,
                    timeout_seconds=args.timeout_seconds,
                    max_model_calls=args.max_calls_per_turn, manual_index=manual_index),
                    activity_logger=print)
        if args.link_whatsapp_sender:
            existing = store.binding("whatsapp", args.link_whatsapp_sender)
            if not existing:
                parser.error("That sender is not present in this local database")
            service.bind_existing("web", "local-web-user", existing["owner_id"], existing["session_id"])
        else:
            service._identity("web", "local-web-user", datetime.now(timezone.utc))
        server = HTTPServer(("127.0.0.1", args.port), make_handler(service, live=args.live))
        print(f"CarMind local web ({'live' if args.live else 'offline'}): http://127.0.0.1:{args.port}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    finally:
        store.close()
        if manual_index is not None:
            manual_index.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
