"""One local manual, page-aware SQLite FTS retrieval, and explicit applicability.

The split PDFs are navigation copies of the canonical PDF. They are validated
against the manifest but never indexed as independent evidence sources.
"""

import argparse
from dataclasses import dataclass, field
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
from time import perf_counter
import unicodedata

from carmind.manufacturer_knowledge import ROOT


SOURCE_DIRECTORY = ROOT / "manufacturer_knowledge"
SOURCE_ENV = "CARMIND_MANUAL_SOURCE"
DEFAULT_INDEX = ROOT / ".local" / "manufacturer-index.sqlite3"
INDEX_VERSION = 3
MAX_QUERY = 240
MAX_TOP_K = 5
MAX_RETURNED_CHARS = 7000

# Retrieval vocabulary only. These words expand lexical search across an
# English PDF; they do not classify owner intent or choose automotive advice.
ARABIC_TERMS = {
    "زيت": "engine oil", "الزيت": "engine oil", "زيتها": "engine oil",
    "كفر": "tire", "الكفر": "tire", "كفرات": "tires", "الكفرات": "tires",
    "اطارات": "tires", "الإطارات": "tires", "ضغط": "pressure", "الضغط": "pressure",
    "لمبة": "indicator warning", "اللمبة": "indicator warning", "تحذير": "warning",
    "صيانة": "maintenance", "الصيانة": "maintenance", "بواجي": "spark plugs",
    "البواجي": "spark plugs", "تشغيل": "operation", "اشغل": "activate",
    "البطارية": "battery", "بطارية": "battery", "تبريد": "coolant",
    "الفيوز": "fuse", "سحب": "towing", "السحب": "towing", "اسحب": "towing",
    "أسحب": "towing", "لانش": "launch control", "متى": "schedule",
    "اغير": "replace", "أغير": "replace", "تغيير": "replace",
    "استهلاك": "consumption", "بنزين": "fuel", "قير": "transmission",
    "مناسب": "recommended specification", "المناسب": "recommended specification",
    "لتر": "capacity", "اللتر": "capacity",
}
STOPWORDS = {"the", "a", "an", "is", "are", "my", "car", "what", "how", "when",
             "which", "for", "to", "of", "do", "i", "can", "does",
             "وش", "كيف", "كم", "في", "عن", "سيارتي", "حق", "هذا", "هذه"}


@dataclass(frozen=True)
class ManualSource:
    source_id: str
    manufacturer: str
    model: str
    model_year: int | None
    market: str | None
    market_basis: str
    variant: str
    language: str
    document_title: str
    document_type: str
    revision: str | None
    pdf_metadata_modified: str | None
    source_path: str
    manifest_path: str
    page_count: int
    source_status: str
    applicability_note: str
    registry_path: Path | None = field(default=None, init=False, compare=False, repr=False)

    def __post_init__(self):
        if not all(isinstance(getattr(self, field), str) and getattr(self, field).strip()
                   for field in ("source_id", "manufacturer", "model", "document_title",
                                 "source_path", "manifest_path", "market_basis", "language",
                                 "document_type", "source_status", "applicability_note")):
            raise ValueError("Incomplete manual source")
        if self.market is not None and (not isinstance(self.market, str) or not self.market.strip()):
            raise ValueError("Invalid source market")
        if self.model_year is not None and (type(self.model_year) is not int or self.model_year < 1):
            raise ValueError("Invalid source model year")
        if type(self.page_count) is not int or self.page_count < 1:
            raise ValueError("Invalid source page count")

    def path(self, relative):
        candidate = (ROOT / relative).resolve()
        if not candidate.is_relative_to(ROOT.resolve()) or candidate.is_symlink():
            raise ValueError("Manual path escapes registered source root")
        return candidate

    def applicability(self, profile, market=None):
        if (profile.make.casefold(), profile.model.casefold()) != (self.manufacturer.casefold(), self.model.casefold()):
            return "inapplicable"
        if market is not None and self.market is not None and market.casefold() != self.market.casefold():
            return "inapplicable"
        if self.model_year is not None and profile.year != self.model_year:
            return "inapplicable"
        if self.model_year is None or market is None or self.market is None or self.source_status != "verified":
            return "unverified"
        return "verified_applicable"


def resolve_source_file(path=None):
    """Use an explicit CLI/env registry or exactly one local registry."""
    selected = path if path is not None else os.environ.get(SOURCE_ENV)
    if selected:
        candidate = Path(selected).expanduser().resolve()
    else:
        candidates = sorted(SOURCE_DIRECTORY.rglob("manual_source.json"))
        if not candidates:
            raise FileNotFoundError("No local manufacturer source is configured")
        if len(candidates) != 1:
            raise ValueError("Multiple manufacturer sources found; select one with --source")
        candidate = candidates[0].resolve()
    if not candidate.is_file():
        raise FileNotFoundError("Selected manufacturer source registry is unavailable")
    return candidate


def load_source(path=None):
    registry_path = resolve_source_file(path)
    data = json.loads(registry_path.read_text(encoding="utf-8"))
    required = {name for name, definition in ManualSource.__dataclass_fields__.items() if definition.init}
    if not isinstance(data, dict) or set(data) != required:
        raise ValueError("Invalid manual source registry")
    source = ManualSource(**data)
    object.__setattr__(source, "registry_path", registry_path)
    if not source.path(source.source_path).is_file() or not source.path(source.manifest_path).is_file():
        raise FileNotFoundError("Registered manual or manifest is unavailable")
    return source


def load_manual_facts(source=None):
    """Curated source facts; deliberately not an active maintenance schedule."""
    source = source or load_source()
    if source.registry_path is None:
        raise ValueError("Manual source has no registry path")
    data = json.loads(source.registry_path.with_name("manual_facts.json").read_text(encoding="utf-8"))
    facts = data.get("facts")
    if (data.get("source_id") != source.source_id or not isinstance(facts, list)
            or len({item.get("id") for item in facts if isinstance(item, dict)}) != len(facts)):
        raise ValueError("Invalid manual fact registry")
    for item in facts:
        pages = item.get("physical_pages")
        if (not isinstance(pages, list) or not pages or
                any(type(page) is not int or not 1 <= page <= source.page_count for page in pages)):
            raise ValueError("Manual fact lacks valid source pages")
    return data


def load_manifest(source, *, validate_splits=True):
    manifest = json.loads(source.path(source.manifest_path).read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("Invalid manual manifest")
    sections = manifest.get("sections")
    if (manifest.get("pdf_path") != Path(source.source_path).name or
            manifest.get("page_count") != source.page_count or not isinstance(sections, list) or not sections):
        raise ValueError("Invalid manual manifest")
    previous = 0
    for position, section in enumerate(sections, 1):
        if (not isinstance(section, dict) or set(section) != {"index", "title", "page_start", "page_end", "file"}
                or section["index"] != position or type(section["page_start"]) is not int
                or type(section["page_end"]) is not int or section["page_start"] != previous + 1
                or section["page_end"] < section["page_start"] or not isinstance(section["title"], str)):
            raise ValueError("Invalid or non-contiguous manual section")
        # The files are used only to check navigation integrity, never ingested.
        section_path = source.path(source.path(source.source_path).parent /
                                   section["file"].replace("\\", "/"))
        if validate_splits:
            if not section_path.is_file():
                raise FileNotFoundError("Manual section is missing")
            from pypdf import PdfReader
            if len(PdfReader(section_path).pages) != section["page_end"] - section["page_start"] + 1:
                raise ValueError("Manual section page count differs from manifest")
        previous = section["page_end"]
    if previous != source.page_count:
        raise ValueError("Manual manifest does not cover the source")
    return sections


def _sha(path):
    digest = sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _text(page):
    raw = page.extract_text() or ""
    return "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in raw.replace("\x00", " ").splitlines()
                     if line.strip())


def _printed_page(lines):
    for line in lines[:5]:
        found = re.fullmatch(r"\d{1,2}-\d{1,3}", line)
        if found:
            return line
    return None


def _heading(lines, section_title):
    normalized_section = re.sub(r"[^a-z0-9]", "", section_title.casefold())
    for line in lines[:28]:
        if re.sub(r"[^a-z0-9]", "", line.casefold()) == normalized_section:
            continue
        if line in {"CAUTION", "NOTICE", "WARNING", "Information"}:
            continue
        if re.fullmatch(r"\d{1,2}-\d{1,3}|\d{2}", line) or len(line) < 5 or len(line) > 78:
            continue
        if any(ord(c) < 32 for c in line) or re.match(r"[A-Z]{2,}\d", line):
            continue
        letters = [c for c in line if c.isalpha()]
        if letters and (sum(c.isupper() for c in letters) / len(letters) > .65 or
                        (line[0].isupper() and 2 <= len(line.split()) <= 8 and not line.endswith("."))):
            return line
    return section_title


def _chunks(text, limit=3200):
    lines = text.splitlines()
    current, length = [], 0
    for line in lines:
        if current and length + len(line) + 1 > limit:
            yield "\n".join(current)
            current, length = [], 0
        current.append(line)
        length += len(line) + 1
    if current:
        yield "\n".join(current)


def build_index(path=DEFAULT_INDEX, *, source_file=None, force=False):
    """Offline, deterministic rebuild; no PDF is parsed during runtime search."""
    started = perf_counter()
    source = load_source(source_file)
    sections = load_manifest(source)
    source_hash = _sha(source.path(source.source_path))
    manifest_hash = _sha(source.path(source.manifest_path))
    registry_hash = _sha(source.registry_path)
    target = Path(path).resolve()
    if target.exists() and not force:
        try:
            existing = ManualIndex(target, source_file=source.registry_path)
            existing.close()
            return {"status": "current", "chunks": None, "seconds": perf_counter() - started,
                    "bytes": target.stat().st_size, "path": str(target)}
        except (ValueError, OSError, sqlite3.Error):
            pass
    from pypdf import PdfReader
    reader = PdfReader(source.path(source.source_path))
    if len(reader.pages) != source.page_count:
        raise ValueError("Canonical manual page count changed")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".building")
    if temporary.exists():
        temporary.unlink()
    db = sqlite3.connect(temporary)
    count = 0
    try:
        db.executescript("""CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE chunks (id TEXT PRIMARY KEY, source_id TEXT NOT NULL, section TEXT NOT NULL,
          heading TEXT NOT NULL, physical_page INTEGER NOT NULL, printed_page TEXT, text TEXT NOT NULL);
        CREATE VIRTUAL TABLE chunks_fts USING fts5(id UNINDEXED, text, section, heading,
          tokenize='porter unicode61'); PRAGMA user_version=1;""")
        for key, value in {"source_hash": source_hash, "manifest_hash": manifest_hash,
                           "registry_hash": registry_hash, "source_id": source.source_id,
                           "index_version": str(INDEX_VERSION)}.items():
            db.execute("INSERT INTO meta VALUES (?,?)", (key, value))
        section_idx = 0
        for page_number, page in enumerate(reader.pages, 1):
            while page_number > sections[section_idx]["page_end"]:
                section_idx += 1
            section = sections[section_idx]["title"]
            text = _text(page)
            if not text:
                continue
            lines = text.splitlines()
            heading = _heading(lines, section)
            printed = _printed_page(lines)
            for part, content in enumerate(_chunks(text), 1):
                chunk_id = "manual:" + sha256(f"{source.source_id}|{page_number}|{part}|{content}".encode()).hexdigest()[:24]
                row = (chunk_id, source.source_id, section, heading, page_number, printed, content)
                db.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?,?)", row)
                db.execute("INSERT INTO chunks_fts VALUES (?,?,?,?)", (chunk_id, content, section, heading))
                count += 1
        db.commit()
    except BaseException:
        db.close()
        temporary.unlink(missing_ok=True)
        raise
    db.close()
    os.replace(temporary, target)
    return {"status": "built", "chunks": count, "seconds": perf_counter() - started,
            "bytes": target.stat().st_size, "path": str(target)}


def _terms(query):
    tokens = re.findall(r"[\w]+", unicodedata.normalize("NFKC", query).casefold())
    expanded = []
    for token in tokens:
        if token in STOPWORDS:
            continue
        if token.isascii():
            expanded.append(token)
        elif token in ARABIC_TERMS:
            expanded.extend(ARABIC_TERMS[token].split())
    # A quantity question about tire pressure needs the specification table,
    # not only a TPMS warning explanation. This is query expansion, not advice.
    if ("كم" in tokens or "much" in tokens) and {"tire", "pressure"} <= set(expanded):
        expanded.extend(("normal", "load"))
    return tuple(dict.fromkeys(term for term in expanded if len(term) > 1 and len(term) < 40))[:16]


class ManualIndex:
    def __init__(self, path=DEFAULT_INDEX, *, source_file=None):
        self.path = Path(path).resolve()
        self.source = load_source(source_file)
        if not self.path.is_file():
            raise FileNotFoundError("Local manual index is missing; build it offline first")
        self.db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        self.db.row_factory = sqlite3.Row
        try:
            metadata = dict(self.db.execute("SELECT key,value FROM meta"))
            expected = {"source_hash": _sha(self.source.path(self.source.source_path)),
                        "manifest_hash": _sha(self.source.path(self.source.manifest_path)),
                        "registry_hash": _sha(self.source.registry_path), "source_id": self.source.source_id,
                        "index_version": str(INDEX_VERSION)}
            if metadata != expected:
                raise ValueError("Local manual index is stale")
        except (sqlite3.Error, ValueError):
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def search(self, query, profile, *, market=None, top_k=3):
        if (not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY
                or type(top_k) is not int or not 1 <= top_k <= MAX_TOP_K):
            raise ValueError("Invalid manual search request")
        applicability = self.source.applicability(profile, market)
        if applicability == "inapplicable":
            return ()
        terms = _terms(query)
        if not terms:
            return ()
        expression = " OR ".join('"' + term.replace('"', '') + '"' for term in terms)
        rows = self.db.execute("""SELECT c.*, bm25(chunks_fts,0,1,2,3) AS rank
          FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.id
          WHERE chunks_fts MATCH ? ORDER BY rank LIMIT 100""", (expression,)).fetchall()
        scored = []
        for row in rows:
            searchable = (row["heading"] + " " + row["section"] + " " + row["text"]).casefold()
            heading = row["heading"].casefold().replace("capacities", "capacity")
            overlap = sum(term in searchable for term in terms)
            if overlap < max(1, (len(terms) + 1) // 2):
                continue
            phrase = " ".join(terms)
            title_hits = sum(term in heading for term in terms)
            score = overlap * 4 + title_hits * 8 + (4 if phrase in searchable else 0) + (-row["rank"])
            if "capacity" in terms and "capacity" in heading:
                score += 8
            # Favor source tables for quantitative questions without encoding
            # any automotive threshold or answer in the retrieval policy.
            if set(terms) & {"pressure", "capacity", "specification"}:
                score += min(6, len(re.findall(r"\b\d+(?:[.,]\d+)?\b", row["text"])) / 8)
                if "pressure" in terms and "psi" in searchable and "kpa" in searchable:
                    score += 4
            scored.append((score, row))
        scored.sort(key=lambda pair: (-pair[0], pair[1]["physical_page"], pair[1]["id"]))
        result, remaining = [], MAX_RETURNED_CHARS
        for score, row in scored[:top_k]:
            excerpt = row["text"][:min(2400, remaining)]
            if not excerpt:
                break
            remaining -= len(excerpt)
            result.append({"evidence_id": row["id"], "manual_chunk": True,
                           "source_id": self.source.source_id,
                           "document_title": self.source.document_title,
                           "manufacturer": self.source.manufacturer,
                           "source_market": self.source.market,
                           "section": row["section"],
                           "heading": row["heading"], "physical_page": row["physical_page"],
                           "printed_page": row["printed_page"], "text": excerpt,
                           "applicability": applicability, "score": round(score, 5)})
        return tuple(result)


def open_optional_index(path=DEFAULT_INDEX, *, source_file=None):
    """Product startup may omit RAG; explicit misconfiguration still fails clearly."""
    explicit = source_file is not None or bool(os.environ.get(SOURCE_ENV))
    try:
        selected = resolve_source_file(source_file)
    except FileNotFoundError:
        if explicit:
            raise
        return None
    # Multiple discovered sources are ambiguous and require explicit selection.
    try:
        return ManualIndex(path, source_file=selected)
    except (FileNotFoundError, ValueError):
        if explicit:
            raise
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline local manufacturer-manual index")
    parser.add_argument("command", choices=("build", "status"))
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--source", type=Path, help="Registered local manual_source.json")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "build":
        result = build_index(args.index, source_file=args.source, force=args.force)
    else:
        try:
            index = ManualIndex(args.index, source_file=args.source)
            result = {"status": "current", "bytes": index.path.stat().st_size}
            index.close()
        except (FileNotFoundError, ValueError):
            result = {"status": "missing_or_stale"}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
