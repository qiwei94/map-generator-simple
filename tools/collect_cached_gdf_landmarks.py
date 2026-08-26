#!/usr/bin/env python3
"""Collect landmark identities from the exact GDF caches used for renders."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import pickle
import re
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from tools.collect_showcase_landmarks import atomic_json, frame_bbox  # noqa: E402


DEFAULT_SHOWCASE = PROJECT_ROOT / "data" / "showcase_cities.json"
DEFAULT_OUTPUT = (
    PROJECT_ROOT / "data" / "height_cache" / "showcase_landmarks_gdf.json")
_QID_RE = re.compile(r"\bQ\d+\b", re.IGNORECASE)


def _text(value):
    try:
        if value is None or value != value:
            return None
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return text or None


def records_from_buildings(buildings) -> list[dict]:
    if buildings is None or buildings.empty or "wikidata" not in buildings:
        return []
    records = []
    for index, row in buildings[buildings["wikidata"].notna()].iterrows():
        qids = sorted({match.group(0).upper()
                       for match in _QID_RE.finditer(str(row.get("wikidata")))})
        for qid in qids:
            records.append({
                "qid": qid,
                "osm_id": None,
                "cache_row": str(index),
                "building": _text(row.get("building")) or _text(
                    row.get("building:part")),
                "osm_height": _text(row.get("height")),
                "osm_levels": _text(row.get("building:levels")),
                "osm_name": _text(row.get("name")),
                "osm_name_en": _text(row.get("name:en")),
                "historic": _text(row.get("historic")),
                "heritage": _text(row.get("heritage")),
                "tourism": _text(row.get("tourism")),
                "amenity": _text(row.get("amenity")),
                "man_made": _text(row.get("man_made")),
            })
    records.sort(key=lambda item: (item["qid"], item["cache_row"]))
    return records


def find_cache(pipeline_dir: Path, city_key: str) -> Path | None:
    candidates = list(pipeline_dir.glob(
        f"showcase_{city_key}_25km*_aesthetic/gdfs_v1_*.pkl"))
    if not candidates:
        return None
    return max(candidates, key=lambda path: (path.stat().st_mtime_ns,
                                             path.stat().st_size))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--showcase", type=Path, default=DEFAULT_SHOWCASE)
    parser.add_argument(
        "--pipeline-dir", type=Path,
        default=PROJECT_ROOT / "cache" / "pipeline")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--size-km", type=float, default=25.0)
    parser.add_argument("--only", help="comma-separated city keys")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    plan = json.loads(args.showcase.read_text(encoding="utf-8"))
    selected = None
    if args.only:
        selected = {item.strip() for item in args.only.split(",") if item.strip()}
    cities = [city for city in plan["cities"]
              if selected is None or city["key"] in selected]
    payload = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "showcase": str(args.showcase.resolve()),
        "pipeline_dir": str(args.pipeline_dir.resolve()),
        "size_km": args.size_km,
        "cities": [],
        "skipped": [],
    }
    for index, city in enumerate(cities, 1):
        cache = find_cache(args.pipeline_dir, city["key"])
        if cache is None:
            payload["skipped"].append({
                "key": city["key"], "reason": "gdf_cache_not_found"})
            if args.strict:
                raise FileNotFoundError(city["key"])
            continue
        print(f"[{index}/{len(cities)}] {city['key']}: {cache.name}", flush=True)
        with cache.open("rb") as stream:
            cached = pickle.load(stream)
        buildings = cached.get("buildings") if isinstance(cached, dict) else None
        landmarks = records_from_buildings(buildings)
        lat, lon = map(float, city["center"])
        stat = cache.stat()
        payload["cities"].append({
            "key": city["key"],
            "title": city.get("title") or city["key"],
            "center": [lat, lon],
            "size_km": args.size_km,
            "bbox": frame_bbox(lat, lon, args.size_km),
            "pbf": city.get("pbf"),
            "gdf_cache": str(cache.resolve()),
            "gdf_cache_size_bytes": stat.st_size,
            "gdf_cache_mtime_ns": stat.st_mtime_ns,
            "building_objects": 0 if buildings is None else len(buildings),
            "landmark_objects": len(landmarks),
            "landmark_qids": len({item["qid"] for item in landmarks}),
            "landmarks": landmarks,
            "collection_backend": "render_gdf_cache",
        })
        del cached, buildings

    unique_qids = {
        item["qid"] for city in payload["cities"] for item in city["landmarks"]
    }
    payload["summary"] = {
        "requested_cities": len(cities),
        "collected_cities": len(payload["cities"]),
        "skipped_cities": len(payload["skipped"]),
        "landmark_objects": sum(
            city["landmark_objects"] for city in payload["cities"]),
        "unique_qids": len(unique_qids),
    }
    atomic_json(args.output, payload)
    print(json.dumps(payload["summary"], ensure_ascii=False))
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
