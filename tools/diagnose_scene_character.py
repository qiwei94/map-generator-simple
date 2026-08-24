#!/usr/bin/env python3
"""Analyze local city character from roads, buildings and water in a PBF."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import pickle
import sys
import time

import geopandas as gpd
import numpy as np
from shapely.geometry import box


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from aesthetic.scene_character import (
    analyze_scene_character,
    render_scene_character,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osmium_cli_fetcher import (
    fetch_from_cli,
    fetch_tiled_from_cli,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm


def _parse_bbox(value: str) -> tuple[float, float, float, float]:
    try:
        parts = tuple(float(part.strip()) for part in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("bbox values must be numbers") from exc
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("bbox must be south,west,north,east")
    return parts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render an auditable local scene-character diagnostic")
    parser.add_argument("--bbox", required=True, type=_parse_bbox)
    parser.add_argument("--pbf", type=Path)
    parser.add_argument(
        "--pipeline-cache", type=Path,
        help="trusted local gdfs_v1 pickle containing roads/buildings/water")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--grid-size", type=int, default=8)
    parser.add_argument(
        "--tiled", action="store_true",
        help="reuse the project's 5 km feature tile cache")
    return parser.parse_args()


def _empty_gdf() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame({"geometry": []}, geometry="geometry",
                            crs="EPSG:4326")


def _load_sources(args) -> dict:
    if args.pipeline_cache:
        if not args.pipeline_cache.is_file():
            raise SystemExit(f"pipeline cache not found: {args.pipeline_cache}")
        # Pickle is intentionally restricted to an explicitly supplied,
        # trusted project cache.  Never auto-discover or download one.
        with args.pipeline_cache.open("rb") as handle:
            payload = pickle.load(handle)
        if not isinstance(payload, dict):
            raise SystemExit("pipeline cache must contain a feature dict")
        return {
            key: payload.get(key, _empty_gdf())
            for key in ("roads", "buildings", "water")
        }
    if not args.pbf or not args.pbf.is_file():
        raise SystemExit("provide an existing --pbf or --pipeline-cache")
    # Geometry-only diagnostics must never trigger an unrelated Overture
    # height download.  This process-scoped toggle does not change generation
    # defaults or persistent configuration.
    from _TEXTURE_STYLE_OF_DEEPSEEK import config as project_config
    project_config.OVERTURE_ENABLED = False
    south, west, north, east = args.bbox
    fetch = fetch_tiled_from_cli if args.tiled else fetch_from_cli
    result = {}
    for key, tag_type in (
        ("roads", "road"), ("buildings", "building"), ("water", "water")
    ):
        started = time.perf_counter()
        result[key] = fetch(
            tag_type=tag_type,
            south=south, west=west, north=north, east=east,
            pbf_file=str(args.pbf.resolve()),
        )
        print(f"[scene] fetched {key}: {len(result[key]):,} "
              f"in {time.perf_counter() - started:.1f}s")
    return result


def _project_and_clip(gdf, target_crs, projected_bbox):
    if gdf is None or len(gdf) == 0:
        return _empty_gdf().to_crs(target_crs)
    projected = gdf.to_crs(target_crs)
    frame = box(*projected_bbox)
    try:
        positions = projected.sindex.query(frame, predicate="intersects")
        projected = projected.iloc[np.unique(positions)].copy()
    except Exception:
        projected = projected.loc[projected.geometry.intersects(frame)].copy()
    if len(projected) == 0:
        return projected
    try:
        projected["geometry"] = projected.geometry.intersection(frame)
    except Exception:
        projected = gpd.clip(projected, frame)
    return projected.loc[
        projected.geometry.notnull() & ~projected.geometry.is_empty
    ].copy()


def main() -> int:
    args = parse_args()
    started = time.perf_counter()
    south, west, north, east = args.bbox
    projection = bbox_to_utm(south, west, north, east)
    sources = _load_sources(args)

    projected = {}
    for key, gdf in sources.items():
        stage = time.perf_counter()
        projected[key] = _project_and_clip(
            gdf, projection["utm_crs"], projection["utm_bbox"])
        print(f"[scene] projected {key}: {len(projected[key]):,} "
              f"in {time.perf_counter() - stage:.1f}s")

    report = analyze_scene_character(
        projected["roads"], projected["buildings"], projected["water"],
        projection["utm_bbox"], grid_size=args.grid_size)
    report["bbox_wgs84"] = list(args.bbox)
    report["source"] = {
        "pbf": args.pbf.name if args.pbf else None,
        "pipeline_cache": (args.pipeline_cache.name
                           if args.pipeline_cache else None),
    }
    report["elapsed_seconds"] = round(time.perf_counter() - started, 3)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / f"{args.tag}_scene_character.json"
    png_path = args.output_dir / f"{args.tag}_scene_character.png"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    render_scene_character(report, png_path)
    print(json.dumps({
        "json": str(json_path),
        "png": str(png_path),
        "summary": report["summary"],
        "elapsed_seconds": report["elapsed_seconds"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
