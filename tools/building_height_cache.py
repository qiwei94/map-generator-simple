#!/usr/bin/env python3
"""Inspect, verify, back up, or export the persistent building-height cache."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.building_height_store import (  # noqa: E402
    BuildingHeightStore,
)


DEFAULT_STORE = PROJECT_ROOT / "data" / "height_cache" / "building_heights.sqlite3"


def status(store: BuildingHeightStore, verify_raw: bool = False) -> dict:
    with store.connect() as conn:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        schema_row = conn.execute(
            "SELECT value FROM store_meta WHERE key='schema_version'"
        ).fetchone()
        observations = [dict(row) for row in conn.execute(
            """SELECT source, source_release, COUNT(*) AS count,
                      ROUND(MIN(height_m), 2) AS min_height_m,
                      ROUND(MAX(height_m), 2) AS max_height_m
               FROM height_observations
               GROUP BY source, source_release
               ORDER BY source, source_release""")]
        coverage = [dict(row) for row in conn.execute(
            """SELECT source, source_release, COUNT(*) AS regions,
                      SUM(observation_count) AS imported_observations
               FROM source_coverage
               GROUP BY source, source_release
               ORDER BY source, source_release""")]
        landmarks = [dict(row) for row in conn.execute(
            """SELECT status, COUNT(*) AS count FROM landmark_heights
               GROUP BY status ORDER BY status""")]
        requests = [dict(row) for row in conn.execute(
            """SELECT source, status, COUNT(*) AS count FROM request_cache
               GROUP BY source, status ORDER BY source, status""")]
        missing_rtree = conn.execute(
            """SELECT COUNT(*) FROM height_observations h
               LEFT JOIN height_observations_rtree r ON r.id=h.id
               WHERE r.id IS NULL""").fetchone()[0]
        raw_rows = [dict(row) for row in conn.execute(
            """SELECT raw_path, raw_sha256 FROM source_coverage
               WHERE raw_path IS NOT NULL""")]

    raw_files = {"recorded": len(raw_rows), "missing": 0, "checksum_failures": 0}
    for row in raw_rows:
        path = row["raw_path"]
        if not path or not os.path.isfile(path):
            raw_files["missing"] += 1
        elif verify_raw and row["raw_sha256"]:
            if store.file_sha256(path) != row["raw_sha256"]:
                raw_files["checksum_failures"] += 1

    return {
        "path": store.path,
        "size_bytes": os.path.getsize(store.path),
        "schema_version": schema_row[0] if schema_row else None,
        "integrity": integrity,
        "missing_rtree_rows": missing_rtree,
        "observations": observations,
        "coverage": coverage,
        "landmarks": landmarks,
        "requests": requests,
        "raw_files": raw_files,
    }


def backup(store: BuildingHeightStore, destination: Path) -> None:
    destination = destination.resolve()
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite existing backup: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    source_conn = sqlite3.connect(store.path)
    target_conn = sqlite3.connect(str(temporary))
    try:
        source_conn.backup(target_conn)
    finally:
        target_conn.close()
        source_conn.close()
    os.replace(temporary, destination)


def export(store: BuildingHeightStore, destination: Path) -> None:
    import geopandas as gpd
    from shapely import wkb

    with store.connect() as conn:
        rows = conn.execute(
            """SELECT source, source_feature_id, source_release, height_m,
                      num_floors, height_kind, confidence, name, source_url,
                      retrieved_at, geom_wkb
               FROM height_observations ORDER BY source, source_feature_id"""
        ).fetchall()
    records = []
    for row in rows:
        item = dict(row)
        item["geometry"] = wkb.loads(item.pop("geom_wkb"))
        records.append(item)
    if records:
        frame = gpd.GeoDataFrame(records, geometry="geometry", crs="EPSG:4326")
    else:
        frame = gpd.GeoDataFrame(
            columns=["source", "source_feature_id", "source_release",
                     "height_m", "num_floors", "height_kind", "confidence",
                     "name", "source_url", "retrieved_at", "geometry"],
            geometry="geometry", crs="EPSG:4326",
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    suffix = destination.suffix.lower()
    if suffix in {".parquet", ".geoparquet"}:
        frame.to_parquet(destination, index=False)
    elif suffix in {".geojson", ".json"}:
        frame.to_file(destination, driver="GeoJSON")
    else:
        raise ValueError("export output must end in .parquet or .geojson")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", default=str(DEFAULT_STORE))
    sub = parser.add_subparsers(dest="command", required=True)
    status_parser = sub.add_parser("status")
    status_parser.add_argument("--verify-raw", action="store_true")
    backup_parser = sub.add_parser("backup")
    backup_parser.add_argument("output", type=Path)
    export_parser = sub.add_parser("export")
    export_parser.add_argument("output", type=Path)
    args = parser.parse_args()

    store = BuildingHeightStore(args.store)
    if args.command == "status":
        print(json.dumps(status(store, args.verify_raw), ensure_ascii=False, indent=2))
    elif args.command == "backup":
        backup(store, args.output)
        print(args.output.resolve())
    elif args.command == "export":
        export(store, args.output)
        print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
