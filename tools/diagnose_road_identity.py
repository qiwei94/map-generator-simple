#!/usr/bin/env python3
"""Fast real-PBF audit for printable city-road identity selection.

This diagnostic intentionally stops before buildings, block base, meshes and
booleans.  It answers one narrow question quickly: does the road material
selector preserve the source frame's dominant ring, radial or grid structure?
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from aesthetic.city_signature import compare_visible_road_signature
from _TEXTURE_STYLE_OF_DEEPSEEK.config import compute_scale
from _TEXTURE_STYLE_OF_DEEPSEEK.road_roles import select_road_roles
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osmium_cli_fetcher import (
    fetch_from_cli,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import (
    bbox_to_utm,
    project_geodataframe,
)


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
        description="Audit printable road identity directly from a real PBF")
    parser.add_argument("--bbox", required=True, type=_parse_bbox)
    parser.add_argument("--pbf", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--road-width-multiplier", type=float, default=2.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    south, west, north, east = args.bbox
    if not args.pbf.is_file():
        raise SystemExit(f"PBF not found: {args.pbf}")

    bbox = bbox_to_utm(south, west, north, east)
    origin = bbox["origin"]
    utm_bbox = bbox["utm_bbox"]
    bbox_local = tuple(
        coordinate - origin[index % 2]
        for index, coordinate in enumerate(utm_bbox)
    )
    scale = compute_scale(bbox["width_m"], bbox["height_m"])
    nozzle_real_m = 0.4 / scale

    roads = fetch_from_cli(
        tag_type="road",
        south=south,
        west=west,
        north=north,
        east=east,
        pbf_file=str(args.pbf.resolve()),
    )
    if roads is None or len(roads) == 0:
        raise SystemExit("road extraction returned zero features")
    roads = project_geodataframe(
        roads, bbox["utm_crs"], origin, clip_bbox=utm_bbox)

    roles = select_road_roles(
        roads,
        topology_tier=4,
        nozzle_real_m=nozzle_real_m,
        bbox_local=bbox_local,
        scale_mm_per_m=scale,
        road_width_multiplier=args.road_width_multiplier,
        min_colored_strip_mm=0.63,
    )
    preservation = compare_visible_road_signature(
        roles.structural, roles.visible, bbox_local)
    report = {
        "bbox": list(args.bbox),
        "pbf": args.pbf.name,
        "frame_m": {
            "width": round(bbox["width_m"], 1),
            "height": round(bbox["height_m"], 1),
        },
        "scale_mm_per_m": round(scale, 8),
        "road_roles": roles.evidence,
        "signature_preservation": preservation,
    }
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0 if preservation["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
