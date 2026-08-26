#!/usr/bin/env python3
"""Collect OSM buildings with Wikidata identities for showcase city frames."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import subprocess
import tempfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SHOWCASE = PROJECT_ROOT / "data" / "showcase_cities.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "height_cache" / "showcase_landmarks.json"
_QID_RE = re.compile(r"\bQ\d+\b", re.IGNORECASE)


def frame_bbox(lat: float, lon: float, size_km: float) -> list[float]:
    """Return a square-frame bbox as south, west, north, east."""
    half = float(size_km) / 2.0
    lat_delta = half / 110.574
    lon_scale = 111.320 * max(0.01, math.cos(math.radians(float(lat))))
    lon_delta = half / lon_scale
    return [lat - lat_delta, lon - lon_delta, lat + lat_delta, lon + lon_delta]


def parse_opl_landmarks(text: str) -> list[dict]:
    """Parse the small OPL stream emitted for objects tagged ``wikidata``."""
    records = []
    for line in text.splitlines():
        fields = line.split()
        if not fields or not re.fullmatch(r"[nwr]\d+", fields[0]):
            continue
        tag_field = next((field[1:] for field in fields if field.startswith("T")), "")
        tags = {}
        for item in tag_field.split(","):
            key, separator, value = item.partition("=")
            if separator:
                tags[key] = value
        if "building" not in tags and "building:part" not in tags:
            continue
        qids = sorted({match.group(0).upper()
                       for match in _QID_RE.finditer(tags.get("wikidata", ""))})
        for qid in qids:
            records.append({
                "qid": qid,
                "osm_id": fields[0],
                "building": tags.get("building") or tags.get("building:part"),
                "osm_height": tags.get("height"),
                "osm_levels": tags.get("building:levels"),
            })
    return records


def _records_from_tags(osm_id: str, tags: dict) -> list[dict]:
    if "building" not in tags and "building:part" not in tags:
        return []
    qids = sorted({match.group(0).upper()
                   for match in _QID_RE.finditer(tags.get("wikidata", ""))})
    return [{
        "qid": qid,
        "osm_id": osm_id,
        "building": tags.get("building") or tags.get("building:part"),
        "osm_height": tags.get("height"),
        "osm_levels": tags.get("building:levels"),
    } for qid in qids]


def collect_city(
    city: dict, pbf: Path, size_km: float, osmium: str, strategy: str,
) -> dict:
    lat, lon = map(float, city["center"])
    south, west, north, east = frame_bbox(lat, lon, size_km)
    bbox_arg = f"{west},{south},{east},{north}"
    with tempfile.TemporaryDirectory(prefix="showcase-landmarks-") as temporary:
        extract = Path(temporary) / "area.osm.pbf"
        subprocess.run(
            [osmium, "extract", "--bbox", bbox_arg,
             "--strategy", strategy, "--overwrite",
             "--output", str(extract), str(pbf)],
            check=True,
        )
        result = subprocess.run(
            [osmium, "tags-filter", "--omit-referenced",
             "--output-format", "opl", str(extract), "nwr/wikidata"],
            check=True, capture_output=True, text=True,
        )
    landmarks = parse_opl_landmarks(result.stdout)
    landmarks.sort(key=lambda row: (row["qid"], row["osm_id"]))
    stat = pbf.stat()
    return {
        "key": city["key"],
        "title": city.get("title") or city["key"],
        "center": [lat, lon],
        "size_km": size_km,
        "bbox": [south, west, north, east],
        "pbf": pbf.name,
        "pbf_size_bytes": stat.st_size,
        "pbf_mtime_ns": stat.st_mtime_ns,
        "landmark_objects": len(landmarks),
        "landmark_qids": len({item["qid"] for item in landmarks}),
        "landmarks": landmarks,
        "collection_backend": "osmium_cli",
    }


def collect_city_pyosmium(city: dict, pbf: Path, size_km: float) -> dict:
    """Single-pass tag collection for nodes/ways on pyosmium-only nodes."""
    import osmium

    lat, lon = map(float, city["center"])
    south, west, north, east = frame_bbox(lat, lon, size_km)
    landmarks = []

    def in_frame(node) -> bool:
        try:
            return west <= node.location.lon <= east and (
                south <= node.location.lat <= north)
        except (osmium.InvalidLocationError, RuntimeError):
            return False

    class LandmarkHandler(osmium.SimpleHandler):
        def node(self, node):
            if not in_frame(node):
                return
            tags = {tag.k: tag.v for tag in node.tags}
            landmarks.extend(_records_from_tags(f"n{node.id}", tags))

        def way(self, way):
            tags = {tag.k: tag.v for tag in way.tags}
            if ("wikidata" not in tags or
                    ("building" not in tags and "building:part" not in tags)):
                return
            if any(in_frame(node) for node in way.nodes):
                landmarks.extend(_records_from_tags(f"w{way.id}", tags))

    handler = LandmarkHandler()
    handler.apply_file(str(pbf), locations=True, idx="flex_mem")
    landmarks.sort(key=lambda row: (row["qid"], row["osm_id"]))
    stat = pbf.stat()
    return {
        "key": city["key"],
        "title": city.get("title") or city["key"],
        "center": [lat, lon],
        "size_km": size_km,
        "bbox": [south, west, north, east],
        "pbf": pbf.name,
        "pbf_size_bytes": stat.st_size,
        "pbf_mtime_ns": stat.st_mtime_ns,
        "landmark_objects": len(landmarks),
        "landmark_qids": len({item["qid"] for item in landmarks}),
        "landmarks": landmarks,
        "collection_backend": "pyosmium_single_pass_nodes_ways",
        "relation_landmarks_supported": False,
    }


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--showcase", type=Path, default=DEFAULT_SHOWCASE)
    parser.add_argument("--pbf-dir", type=Path, default=PROJECT_ROOT / "pbf_cache")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--size-km", type=float, default=25.0)
    parser.add_argument("--only", help="comma-separated city keys")
    parser.add_argument("--osmium", default="osmium")
    parser.add_argument(
        "--backend", choices=("cli", "pyosmium"), default="cli",
        help="pyosmium avoids a temporary extract on nodes without native CLI",
    )
    parser.add_argument(
        "--strategy", choices=("simple", "complete_ways"),
        default="complete_ways",
        help="simple is faster for tag inventory on low-memory pyosmium nodes",
    )
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
        "pbf_dir": str(args.pbf_dir.resolve()),
        "size_km": args.size_km,
        "cities": [],
        "skipped": [],
    }
    for index, city in enumerate(cities, 1):
        pbf = args.pbf_dir / city["pbf"]
        if not pbf.is_file():
            payload["skipped"].append({
                "key": city["key"], "pbf": city["pbf"],
                "reason": "pbf_not_found",
            })
            if args.strict:
                raise FileNotFoundError(pbf)
            continue
        print(f"[{index}/{len(cities)}] {city['key']}: {pbf.name}", flush=True)
        if args.backend == "pyosmium":
            result = collect_city_pyosmium(city, pbf, args.size_km)
        else:
            result = collect_city(
                city, pbf, args.size_km, args.osmium, args.strategy)
        payload["cities"].append(result)

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
