"""Offline page-retrieval acceptance against inspected manual passages."""

import argparse
import json
from pathlib import Path
from statistics import mean
from time import perf_counter

from carmind.contracts import VehicleProfile
from carmind.manufacturer_knowledge import ROOT
from carmind.manufacturer_manual import DEFAULT_INDEX, ManualIndex


DEFAULT_CASES = ROOT / "eval" / "manual_retrieval_cases.json"


def evaluate(index, cases_path=DEFAULT_CASES):
    dataset = json.loads(Path(cases_path).read_text(encoding="utf-8"))
    if dataset["source_id"] != index.source.source_id:
        raise ValueError("Evaluation source does not match index")
    vehicle = dataset["vehicle"]
    if (not isinstance(vehicle, dict) or set(vehicle) != {"make", "model", "year"}
            or vehicle["make"].casefold() != index.source.manufacturer.casefold()
            or vehicle["model"].casefold() != index.source.model.casefold()):
        raise ValueError("Evaluation vehicle does not match selected source")
    profile = VehicleProfile("manual-eval", vehicle["make"], vehicle["model"], vehicle["year"])
    rows = []
    for case in dataset["cases"]:
        started = perf_counter()
        hits = index.search(case["query"], profile, top_k=5)
        elapsed_ms = (perf_counter() - started) * 1000
        expected = set(case["expected_pages"])
        pages = [hit["physical_page"] for hit in hits]
        rows.append({"topic": case["topic"], "query": case["query"],
                     "pages": pages, "top1": bool(set(pages[:1]) & expected),
                     "top3": bool(set(pages[:3]) & expected),
                     "top5": bool(set(pages[:5]) & expected),
                     "latency_ms": round(elapsed_ms, 3),
                     "retrieved_characters": sum(len(hit["text"]) for hit in hits)})
    return {"cases": len(rows), "top1": sum(row["top1"] for row in rows),
            "top3": sum(row["top3"] for row in rows),
            "top5": sum(row["top5"] for row in rows),
            "average_latency_ms": round(mean(row["latency_ms"] for row in rows), 3),
            "average_retrieved_characters": round(mean(row["retrieved_characters"] for row in rows)),
            "rows": rows}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Evaluate a selected local manufacturer manual offline")
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--source", type=Path, help="Registered local manual_source.json")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    args = parser.parse_args(argv)
    index = ManualIndex(args.index, source_file=args.source)
    try:
        result = evaluate(index, args.cases)
    finally:
        index.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
