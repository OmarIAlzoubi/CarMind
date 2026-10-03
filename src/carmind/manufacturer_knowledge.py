"""Versioned facts and exact applicability; no manufacturer logic in Core."""

from dataclasses import dataclass
from datetime import date
import json
import os
from math import isfinite
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE_DIRECTORY = ROOT / "manufacturer_knowledge"
KNOWLEDGE_ENV = "CARMIND_KNOWLEDGE_DIRECTORY"


@dataclass(frozen=True)
class VehicleKnowledgeProfile:
    manufacturer: str
    model: str
    model_year: int | None
    market: str
    engine_variant: str | None = None
    transmission: str | None = None
    knowledge_version: str = "1"

    def __post_init__(self):
        if any(not isinstance(v, str) or not v.strip() for v in (
            self.manufacturer, self.model, self.market, self.knowledge_version
        )):
            raise ValueError("Incomplete knowledge identity")
        if self.model_year is not None and (type(self.model_year) is not int or self.model_year <= 0):
            raise ValueError("Invalid model year")


@dataclass(frozen=True)
class ManufacturerSource:
    source_id: str
    manufacturer: str
    model: str
    model_year: int | None
    market: str
    document_title: str
    source_type: str
    official_source_reference: str
    retrieved_at: str
    document_version: str | None = None

    def __post_init__(self):
        for name in ("source_id", "manufacturer", "model", "market", "document_title", "source_type", "official_source_reference", "retrieved_at"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError("Source metadata is required: " + name)
        if not self.official_source_reference.startswith("https://"):
            raise ValueError("Source must have HTTPS reference")
        date.fromisoformat(self.retrieved_at)


@dataclass(frozen=True)
class MaintenanceRule:
    rule_id: str
    source_id: str
    maintenance_item: str
    action_type: str
    operating_condition: str
    section: str
    page: str
    trigger: str = "WHICHEVER_FIRST"
    interval_km: float | None = None
    interval_months: int | None = None
    first_due_km: float | None = None
    first_due_months: int | None = None
    repeat_km: float | None = None
    repeat_months: int | None = None

    def __post_init__(self):
        for name in ("rule_id", "source_id", "maintenance_item", "section", "page"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError("Rule provenance and identity required")
        if self.action_type not in {"INSPECT", "REPLACE", "CHECK"}:
            raise ValueError("Invalid action")
        if self.operating_condition not in {"NORMAL", "SEVERE", "UNKNOWN"}:
            raise ValueError("Invalid operating condition")
        if self.trigger not in {"MILEAGE", "TIME", "AND", "WHICHEVER_FIRST"}:
            raise ValueError("Invalid trigger")
        for name in ("interval_km", "interval_months", "first_due_km", "first_due_months", "repeat_km", "repeat_months"):
            value = getattr(self, name)
            if value is not None and (type(value) not in (int, float) or not isfinite(value) or value <= 0 or ("months" in name and type(value) is not int)):
                raise ValueError("Intervals must be positive finite numbers; months must be integers")
        km = self.interval_km is not None or self.first_due_km is not None
        months = self.interval_months is not None or self.first_due_months is not None
        if (self.trigger == "MILEAGE" and (not km or months)) or (self.trigger == "TIME" and (not months or km)) or (self.trigger in {"AND", "WHICHEVER_FIRST"} and not (km and months)):
            raise ValueError("Trigger dimensions do not match")


@dataclass(frozen=True)
class KnowledgePack:
    profile: VehicleKnowledgeProfile
    sources: tuple[ManufacturerSource, ...]
    rules: tuple[MaintenanceRule, ...]
    poc_only: bool = True

    def __post_init__(self):
        object.__setattr__(self, "sources", tuple(self.sources))
        object.__setattr__(self, "rules", tuple(self.rules))
        ids = {s.source_id for s in self.sources}
        if not ids or len(ids) != len(self.sources):
            raise ValueError("Unique source metadata required")
        if len({r.rule_id for r in self.rules}) != len(self.rules):
            raise ValueError("Duplicate rule")
        for source in self.sources:
            if (source.manufacturer, source.model, source.model_year, source.market) != (self.profile.manufacturer, self.profile.model, self.profile.model_year, self.profile.market):
                raise ValueError("Source identity mismatch")
        if any(r.source_id not in ids for r in self.rules):
            raise ValueError("Unknown source provenance")

    def matches(self, profile: VehicleKnowledgeProfile) -> bool:
        # Unknown year is not a wildcard, even against another unknown year.
        return self.profile.model_year is not None and self.profile == profile


def resolve_knowledge_directory(directory=None) -> Path:
    selected = directory if directory is not None else os.environ.get(KNOWLEDGE_ENV)
    if selected:
        candidate = Path(selected).expanduser().resolve()
    else:
        candidates = sorted(path.parent for path in KNOWLEDGE_DIRECTORY.rglob("maintenance_schedule.json")
                            if (path.parent / "sources.json").is_file())
        if not candidates:
            raise FileNotFoundError("No local manufacturer maintenance source is configured")
        if len(candidates) != 1:
            raise ValueError("Multiple manufacturer maintenance sources found; select one explicitly")
        candidate = candidates[0].resolve()
    if not (candidate / "sources.json").is_file() or not (candidate / "maintenance_schedule.json").is_file():
        raise FileNotFoundError("Selected manufacturer maintenance source is unavailable")
    return candidate


def load_knowledge(directory=None) -> KnowledgePack:
    directory = resolve_knowledge_directory(directory)
    sources = json.loads((directory / "sources.json").read_text(encoding="utf-8"))
    schedule = json.loads((directory / "maintenance_schedule.json").read_text(encoding="utf-8"))
    return KnowledgePack(VehicleKnowledgeProfile(**schedule["profile"]),
                         tuple(ManufacturerSource(**s) for s in sources["sources"]),
                         tuple(MaintenanceRule(**r) for r in schedule["rules"]), schedule["poc_only"])
