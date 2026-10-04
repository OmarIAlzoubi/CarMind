"""Local, vehicle-scoped manufacturer PDF onboarding over the existing FTS index."""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
from time import perf_counter
from uuid import uuid4

from carmind.manufacturer_knowledge import ROOT
from carmind.manufacturer_manual import MAX_RETURNED_CHARS, ManualIndex, build_index


DEFAULT_DATA_ROOT = ROOT / ".local" / "manufacturer_knowledge"
MAX_PDF_BYTES = 25 * 1024 * 1024
MAX_PAGES = 2000


def registry_for_database(path):
    namespace = sha256(str(Path(path).resolve()).encode()).hexdigest()[:16]
    return VehicleManualRegistry(DEFAULT_DATA_ROOT / namespace)


class DocumentType(str, Enum):
    OWNER_MANUAL = "owner_manual"
    MAINTENANCE_SCHEDULE = "maintenance_schedule"
    WARRANTY_SERVICE_BOOKLET = "warranty_service_booklet"
    VEHICLE_SPECIFICATION = "vehicle_specification"
    OTHER_MANUFACTURER_DOCUMENT = "other_manufacturer_document"


class IngestionStatus(str, Enum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    PROCESSING = "PROCESSING"
    READY = "READY"
    FAILED = "FAILED"
    TEXT_EXTRACTION_UNAVAILABLE = "TEXT_EXTRACTION_UNAVAILABLE"


class ApplicabilityStatus(str, Enum):
    USER_PROVIDED = "USER_PROVIDED"
    DOCUMENT_VERIFIED = "DOCUMENT_VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    CONFLICTING = "CONFLICTING"


class DuplicateDocumentError(ValueError):
    pass


@dataclass(frozen=True)
class IngestionResult:
    vehicle_id: str
    source_id: str
    document_title: str
    document_type: DocumentType
    status: IngestionStatus
    applicability: ApplicabilityStatus
    page_count: int
    extraction_seconds: float
    index_seconds: float | None
    index_bytes: int | None
    source_path: str
    error: str | None = None


def _source_summary(source):
    return {key: getattr(source, key) for key in (
        "source_id", "document_title", "document_type", "manufacturer", "model",
        "model_year", "market", "source_status")}


class VehicleManualIndex:
    """A ready set for exactly one stored vehicle; each hit keeps its own source."""

    def __init__(self, vehicle_id, indexes):
        self.vehicle_id = vehicle_id
        self.indexes = tuple(indexes)
        self.sources = {index.source.source_id: index.source for index in self.indexes}

    def search(self, query, profile, *, market=None, top_k=3):
        if profile.vehicle_id != self.vehicle_id:
            return ()
        hits = [hit for index in self.indexes
                for hit in index.search(query, profile, market=market, top_k=top_k)]
        hits.sort(key=lambda hit: (-hit["score"], hit["source_id"], hit["physical_page"], hit["evidence_id"]))
        selected, remaining = [], MAX_RETURNED_CHARS
        for hit in hits[:top_k]:
            excerpt = hit["text"][:remaining]
            if not excerpt:
                break
            selected.append({**hit, "text": excerpt})
            remaining -= len(excerpt)
        return tuple(selected)

    def close(self):
        for index in self.indexes:
            index.close()


class VehicleManualRegistry:
    def __init__(self, root=DEFAULT_DATA_ROOT):
        self.root = Path(root).resolve()
        if not self.root.is_relative_to(ROOT.resolve()):
            raise ValueError("Manufacturer data must remain under the local project root")
        self._views = {}

    def _directory(self, vehicle_id):
        if not isinstance(vehicle_id, str) or not vehicle_id.strip() or len(vehicle_id) > 128:
            raise ValueError("Invalid vehicle ID")
        return self.root / sha256(vehicle_id.encode()).hexdigest()[:24]

    def _local_path(self, relative, vehicle_id):
        candidate = (ROOT / relative).resolve()
        if not candidate.is_relative_to(self._directory(vehicle_id)):
            raise ValueError("Manufacturer registry path escapes vehicle storage")
        return candidate

    def _read(self, vehicle_id):
        path = self._directory(vehicle_id) / "registry.json"
        if not path.is_file():
            return {"version": 1, "vehicle_id": vehicle_id, "sources": []}
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != 1 or data.get("vehicle_id") != vehicle_id or not isinstance(data.get("sources"), list):
            raise ValueError("Invalid vehicle manufacturer registry")
        return data

    def _write(self, vehicle_id, data):
        directory = self._directory(vehicle_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "registry.json"
        temporary = directory / "registry.json.tmp"
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
        previous = self._views.pop(vehicle_id, None)
        if previous is not None:
            previous[1].close()

    def list(self, vehicle_id):
        entries = []
        for item in self._read(vehicle_id)["sources"]:
            entry = dict(item)
            if entry["status"] == IngestionStatus.READY.value:
                try:
                    index = ManualIndex(self._local_path(entry["index_path"], vehicle_id),
                                        source_file=self._local_path(entry["source_file"], vehicle_id))
                    try:
                        if (index.source.source_id != entry["source_id"]
                                or index.source.ownership_vehicle_id not in (None, vehicle_id)):
                            raise ValueError("Manufacturer source identity mismatch")
                        self._local_path(index.source.source_path, vehicle_id)
                        self._local_path(index.source.manifest_path, vehicle_id)
                    finally:
                        index.close()
                except (FileNotFoundError, ValueError, sqlite3.Error):
                    entry["status"] = IngestionStatus.FAILED.value
            entries.append(entry)
        return tuple(entries)

    def for_vehicle(self, vehicle_id):
        registry_path = self._directory(vehicle_id) / "registry.json"
        version = registry_path.stat().st_mtime_ns if registry_path.is_file() else None
        previous = self._views.get(vehicle_id)
        if previous is not None:
            if previous[0] == version:
                return previous[1]
            previous[1].close()
            self._views.pop(vehicle_id)
        indexes = []
        for item in self.list(vehicle_id):
            if item["status"] != IngestionStatus.READY.value or item["applicability"] == ApplicabilityStatus.CONFLICTING.value:
                continue
            indexes.append(ManualIndex(self._local_path(item["index_path"], vehicle_id),
                                       source_file=self._local_path(item["source_file"], vehicle_id)))
        if not indexes:
            return None
        view = VehicleManualIndex(vehicle_id, indexes)
        self._views[vehicle_id] = (version, view)
        return view

    def close(self):
        for _, view in self._views.values():
            view.close()
        self._views.clear()


def _document_claims(pages):
    text = "\n".join(pages[:3])
    labels = {"manufacturer": r"Manufacturer", "model": r"Model", "model_year": r"Model year",
              "market": r"Market", "variant": r"(?:Variant|Trim)"}
    result = {}
    for key, label in labels.items():
        found = re.search(r"(?im)^" + label + r":\s*([^\r\n]{1,80})\s*$", text)
        if found:
            value = found.group(1).strip()
            result[key] = int(value) if key == "model_year" and re.fullmatch(r"\d{4}", value) else value
    return result


class ManufacturerIngestionService:
    def __init__(self, store, registry=None):
        self.store = store
        self.registry = registry or VehicleManualRegistry()

    def ingest_many(self, owner_id, vehicle_id, pdf_paths, *, metadata=None, now=None):
        paths = tuple(pdf_paths)
        if not paths:
            raise ValueError("At least one PDF is required")
        return tuple(self.ingest(owner_id, vehicle_id, path, metadata=metadata, now=now)
                     for path in paths)

    def ingest(self, owner_id, vehicle_id, pdf_path, *, metadata=None, now=None):
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError

        now = now or datetime.now(timezone.utc)
        vehicle = self.store.vehicle(owner_id, vehicle_id, now)
        metadata = dict(metadata or {})
        allowed = {"manufacturer", "model", "model_year", "market", "variant", "language",
                   "document_title", "document_type", "revision"}
        if set(metadata) - allowed:
            raise ValueError("Unsupported document metadata")
        document_type = DocumentType(metadata.get("document_type", DocumentType.OWNER_MANUAL.value))
        path = Path(pdf_path)
        if path.suffix.casefold() != ".pdf" or not path.is_file() or not 0 < path.stat().st_size <= MAX_PDF_BYTES:
            raise ValueError("A nonempty PDF within the upload size limit is required")
        with path.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                raise ValueError("Invalid PDF signature")
        digest = sha256(path.read_bytes()).hexdigest()
        registered = self.registry._read(vehicle_id)
        if any(item["sha256"] == digest for item in registered["sources"]):
            raise DuplicateDocumentError("This document is already registered for this vehicle")
        started = perf_counter()
        try:
            reader = PdfReader(path, strict=True)
            if reader.is_encrypted or not 0 < len(reader.pages) <= MAX_PAGES:
                raise ValueError("Encrypted, empty or oversized PDF is unsupported")
            pages = [(page.extract_text() or "").strip() for page in reader.pages]
        except (PdfReadError, OSError, TypeError, KeyError, IndexError) as exc:
            raise ValueError("PDF could not be read safely") from exc
        extraction_seconds = perf_counter() - started
        claimed = _document_claims(pages)
        profile = vehicle.profile
        make = metadata.get("manufacturer", profile.make)
        model = metadata.get("model", profile.model)
        year = metadata.get("model_year", claimed.get("model_year"))
        market = metadata.get("market", claimed.get("market"))
        for name, value in (("manufacturer", make), ("model", model), ("market", market)):
            if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 120):
                raise ValueError("Invalid document metadata: " + name)
        if year is not None and (type(year) is not int or year < 1):
            raise ValueError("Invalid document model year")
        for name in ("variant", "language", "document_title", "revision"):
            value = metadata.get(name)
            if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 180):
                raise ValueError("Invalid document metadata: " + name)
        conflicting = ((make.casefold(), model.casefold()) != (profile.make.casefold(), profile.model.casefold())
                       or (year is not None and year != profile.year)
                       or (market is not None and vehicle.market is not None and market.casefold() != vehicle.market.casefold())
                       or (metadata.get("variant") is not None and vehicle.trim is not None
                           and metadata["variant"].casefold() != vehicle.trim.casefold()))
        for name, value in claimed.items():
            expected = {"manufacturer": profile.make, "model": profile.model,
                        "model_year": profile.year, "market": vehicle.market, "variant": vehicle.trim}[name]
            if expected is not None and str(value).casefold() != str(expected).casefold():
                conflicting = True
            supplied = {"manufacturer": make, "model": model, "model_year": year,
                        "market": market, "variant": metadata.get("variant")}[name]
            if supplied is not None and str(value).casefold() != str(supplied).casefold():
                conflicting = True
        verified = (not conflicting and all(key in claimed for key in ("manufacturer", "model", "model_year", "market"))
                    and vehicle.market is not None
                    and ("variant" not in claimed if vehicle.trim is None else
                         claimed.get("variant", "").casefold() == vehicle.trim.casefold()))
        identity_claimed = bool(set(metadata) & {"manufacturer", "model", "model_year", "market", "variant"})
        applicability = (ApplicabilityStatus.CONFLICTING if conflicting else
                         ApplicabilityStatus.DOCUMENT_VERIFIED if verified else
                         ApplicabilityStatus.USER_PROVIDED if identity_claimed else ApplicabilityStatus.UNVERIFIED)
        source_id = "document-" + uuid4().hex
        directory = self.registry._directory(vehicle_id)
        source_dir = directory / "sources" / source_id
        source_dir.mkdir(parents=True, exist_ok=False)
        destination = source_dir / "document.pdf"
        with path.open("rb") as original, destination.open("xb") as output:
            while block := original.read(1024 * 1024):
                output.write(block)
        title = metadata.get("document_title") or (str(reader.metadata.title).strip() if reader.metadata and reader.metadata.title else None) or path.stem
        source_file = source_dir / "manual_source.json"
        manifest_file = source_dir / "sections_manifest.json"
        manifest = {"pdf_path": destination.name, "page_count": len(pages),
                    "sections": [{"index": 1, "title": "Document", "page_start": 1,
                                  "page_end": len(pages), "file": destination.name}]}
        manifest_file.write_text(json.dumps(manifest), encoding="utf-8")
        source = {"source_id": source_id, "manufacturer": make, "model": model, "model_year": year,
                  "market": market, "market_basis": "document text" if "market" in claimed else "user claim or unknown",
                  "variant": metadata.get("variant") or "unspecified", "language": metadata.get("language") or "unknown",
                  "document_title": title[:180], "document_type": document_type.value,
                  "revision": metadata.get("revision"), "pdf_metadata_modified": None,
                  "source_path": destination.relative_to(ROOT).as_posix(),
                  "manifest_path": manifest_file.relative_to(ROOT).as_posix(),
                  "page_count": len(pages), "source_status": "verified" if verified else applicability.value.casefold(),
                  "ownership_vehicle_id": vehicle_id, "applicability_status": applicability.value,
                  "applicability_note": "Document identity verified from labeled text" if verified else
                                        "User metadata or document applicability is not verified"}
        source_file.write_text(json.dumps(source, ensure_ascii=False, indent=2), encoding="utf-8")
        status = IngestionStatus.PROCESSING
        index_path = directory / "indexes" / f"{source_id}.sqlite3"
        entry = {"source_id": source_id, "document_title": title[:180], "document_type": document_type.value,
                 "source_file": source_file.relative_to(ROOT).as_posix(),
                 "index_path": index_path.relative_to(ROOT).as_posix(),
                 "source_path": source["source_path"], "sha256": digest,
                 "page_count": len(pages), "status": status.value, "applicability": applicability.value,
                 "claimed_metadata": metadata, "document_metadata": claimed,
                 "registered_at": now.isoformat()}
        registered["sources"].append(entry)
        self.registry._write(vehicle_id, registered)
        index_seconds = index_bytes = error = None
        if not any(pages):
            status = IngestionStatus.TEXT_EXTRACTION_UNAVAILABLE
        else:
            try:
                built = build_index(index_path, source_file=source_file)
                index_seconds, index_bytes = built["seconds"], built["bytes"]
                status = IngestionStatus.READY if built["chunks"] else IngestionStatus.TEXT_EXTRACTION_UNAVAILABLE
            except (ValueError, FileNotFoundError, OSError, sqlite3.Error) as exc:
                status, error = IngestionStatus.FAILED, type(exc).__name__
        entry["status"] = status.value
        self.registry._write(vehicle_id, registered)
        return IngestionResult(vehicle_id, source_id, title[:180], document_type, status, applicability,
                               len(pages), extraction_seconds, index_seconds, index_bytes,
                               source["source_path"], error)

    def rebuild(self, owner_id, vehicle_id, *, now=None):
        self.store.vehicle(owner_id, vehicle_id, now or datetime.now(timezone.utc))
        registry = self.registry._read(vehicle_id)
        for item in registry["sources"]:
            if item["status"] == IngestionStatus.TEXT_EXTRACTION_UNAVAILABLE.value:
                continue
            try:
                result = build_index(self.registry._local_path(item["index_path"], vehicle_id),
                                     source_file=self.registry._local_path(item["source_file"], vehicle_id), force=True)
                item["status"] = IngestionStatus.READY.value if result["chunks"] else IngestionStatus.TEXT_EXTRACTION_UNAVAILABLE.value
            except (ValueError, FileNotFoundError, OSError, sqlite3.Error):
                item["status"] = IngestionStatus.FAILED.value
        self.registry._write(vehicle_id, registry)
        return self.registry.list(vehicle_id)
