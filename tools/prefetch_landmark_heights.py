#!/usr/bin/env python3
"""Merge city landmark inventories and persist bounded Wikidata lookups."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.wikidata_height_enrichment import (  # noqa: E402
    prefetch_wikidata_landmarks,
    prefetch_wikidata_landmarks_sparql,
)


DEFAULT_CACHE = PROJECT_ROOT / "data" / "height_cache"


def merge_inventories(paths: list[Path]) -> dict:
    cities = {}
    skipped = {}
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        for city in payload.get("cities", []):
            current = cities.get(city["key"])
            if current is None or city.get("landmark_objects", 0) > current.get(
                    "landmark_objects", 0):
                cities[city["key"]] = city
        for item in payload.get("skipped", []):
            skipped[item["key"]] = item
    for key in cities:
        skipped.pop(key, None)
    return {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "inventory_sources": [str(path.resolve()) for path in paths],
        "cities": [cities[key] for key in sorted(cities)],
        "skipped": [skipped[key] for key in sorted(skipped)],
    }


def enrich(payload: dict, records: dict, query_qids: set[str]) -> None:
    for city in payload["cities"]:
        for landmark in city["landmarks"]:
            record = records.get(landmark["qid"], {})
            fallback = ("unresolved" if landmark["qid"] in query_qids
                        else "osm_height_available")
            landmark["wikidata_status"] = record.get("status", fallback)
            landmark["label"] = record.get("label")
            landmark["wikidata_height_m"] = record.get("height_m")
            landmark["source_url"] = record.get("source_url")
            landmark["retrieved_at"] = record.get("retrieved_at")
        city["wikidata_height_hits"] = len({
            item["qid"] for item in city["landmarks"]
            if item["wikidata_status"] == "ok"
        })


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def write_csv(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    fields = [
        "city", "city_title", "qid", "label", "wikidata_status",
        "wikidata_height_m", "osm_id", "building", "osm_height",
        "osm_levels", "source_url", "retrieved_at",
    ]
    with temporary.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for city in payload["cities"]:
            for landmark in city["landmarks"]:
                writer.writerow({
                    "city": city["key"],
                    "city_title": city["title"],
                    **{field: landmark.get(field) for field in fields
                       if field not in {"city", "city_title"}},
                })
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("inventory", nargs="+", type=Path)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument(
        "--output", type=Path,
        default=DEFAULT_CACHE / "showcase_landmarks_enriched.json")
    parser.add_argument(
        "--csv-output", type=Path,
        default=DEFAULT_CACHE / "showcase_landmark_heights.csv")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument(
        "--query-policy", choices=("missing_osm_height", "all"),
        default="missing_osm_height",
        help="skip remote lookups already superseded by OSM height/levels",
    )
    parser.add_argument(
        "--discovery", choices=("sparql", "entities"), default="sparql",
        help="sparql finds P2048 first and hydrates only positive entities",
    )
    parser.add_argument("--no-fetch", action="store_true")
    parser.add_argument("--only", help="comma-separated city keys")
    args = parser.parse_args()

    payload = merge_inventories(args.inventory)
    if args.only:
        selected = {
            item.strip() for item in args.only.split(",") if item.strip()
        }
        payload["cities"] = [
            city for city in payload["cities"] if city["key"] in selected
        ]
    all_qids = sorted({
        item["qid"] for city in payload["cities"] for item in city["landmarks"]
    })
    if args.query_policy == "all":
        qids = all_qids
    else:
        qids = sorted({
            item["qid"] for city in payload["cities"]
            for item in city["landmarks"]
            if item.get("osm_height") is None and item.get("osm_levels") is None
        })
    if args.no_fetch:
        from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.building_height_store import (  # noqa: E501
            BuildingHeightStore,
        )
        store = BuildingHeightStore(
            str(args.cache_dir / "building_heights.sqlite3"))
        records = store.get_landmarks(qids)
        summary = {
            "qid_count": len(qids), "cached_before": len(records),
            "api_requested": 0, "api_batches": 0, "api_errors": 0,
            "height_hits": sum(
                row.get("status") == "ok" for row in records.values()),
            "negative_cached": sum(
                row.get("status") == "missing" for row in records.values()),
            "unresolved": len(qids) - len(records), "errors": [],
        }
    else:
        fetcher = (prefetch_wikidata_landmarks_sparql
                   if args.discovery == "sparql"
                   else prefetch_wikidata_landmarks)
        summary, records = fetcher(
            qids, cache_dir=str(args.cache_dir), timeout=args.timeout)
    enrich(payload, records, set(qids))
    payload["summary"] = {
        "cities": len(payload["cities"]),
        "landmark_objects": sum(
            city["landmark_objects"] for city in payload["cities"]),
        "all_qid_count": len(all_qids),
        "query_policy": args.query_policy,
        "skipped_due_osm_height": len(set(all_qids) - set(qids)),
        **summary,
    }
    atomic_json(args.output, payload)
    write_csv(args.csv_output, payload)
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    print(args.output.resolve())
    print(args.csv_output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
