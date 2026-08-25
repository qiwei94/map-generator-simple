#!/usr/bin/env python3
"""Render the AMap-guided OSM road skeleton without full city preprocessing.

This is an iteration/acceptance tool, not a second production selector.  It
uses the exact ``select_road_roles`` path used by formal generation, but skips
buildings, Block base, water subtraction, terrain and mesh work so corridor
continuity can be inspected in seconds rather than minutes.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
import time

from PIL import Image, ImageDraw
from shapely.geometry import box


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _TEXTURE_STYLE_OF_DEEPSEEK._tile_grid import snap_bbox
from _TEXTURE_STYLE_OF_DEEPSEEK.config import compute_scale
from _TEXTURE_STYLE_OF_DEEPSEEK.print_profile import DEFAULT_PRINTER_PROFILE
from _TEXTURE_STYLE_OF_DEEPSEEK.road_roles import (
    COMPOSITION_ROLE_COLUMN,
    select_road_roles,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osmium_cli_fetcher import (
    fetch_tiled_from_cli,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import (
    bbox_to_utm,
    project_geodataframe,
)
from aesthetic.amap_salience import build_amap_salience_guide


ROLE_STYLES = {
    "primary": ((45, 45, 45), 6),
    "secondary": ((88, 88, 88), 4),
    "context": ((135, 135, 135), 3),
    "connector": ((176, 176, 176), 2),
}


def _bbox(value: str) -> tuple[float, float, float, float]:
    try:
        result = tuple(float(part) for part in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("bbox values must be numeric") from exc
    if len(result) != 4 or result[2] <= result[0] or result[3] <= result[1]:
        raise argparse.ArgumentTypeError("bbox must be south,west,north,east")
    return result


def _iter_lines(geometry):
    if geometry is None or geometry.is_empty:
        return
    if geometry.geom_type == "LineString":
        yield geometry
        return
    if hasattr(geometry, "geoms"):
        for child in geometry.geoms:
            yield from _iter_lines(child)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fast real-data render of the selected road skeleton")
    parser.add_argument("--bbox", required=True, type=_bbox)
    parser.add_argument("--pbf", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--size", type=int, default=2048)
    parser.add_argument("--road-width-multiplier", type=float, default=5.0)
    args = parser.parse_args()
    if not args.pbf.is_file():
        parser.error(f"PBF not found: {args.pbf}")
    if args.size < 512:
        parser.error("--size must be at least 512")

    started = time.time()
    south, west, north, east = args.bbox
    fs, fw, fn, fe = snap_bbox(south, west, north, east)
    snap = bbox_to_utm(fs, fw, fn, fe)
    exact = bbox_to_utm(south, west, north, east)
    if exact["utm_crs"] != snap["utm_crs"]:
        raise SystemExit("exact and snapped frames crossed a UTM zone")
    origin = snap["origin"]
    exact_utm = exact["utm_bbox"]
    exact_local = (
        exact_utm[0] - origin[0], exact_utm[1] - origin[1],
        exact_utm[2] - origin[0], exact_utm[3] - origin[1],
    )
    snap_utm = snap["utm_bbox"]
    snap_local = (
        snap_utm[0] - origin[0], snap_utm[1] - origin[1],
        snap_utm[2] - origin[0], snap_utm[3] - origin[1],
    )

    roads = fetch_tiled_from_cli(
        "road", fs, fw, fn, fe, str(args.pbf.resolve()))
    roads = project_geodataframe(
        roads, snap["utm_crs"], origin, clip_bbox=snap_utm)
    roads = roads.clip(box(*exact_local), keep_geom_type=True).reset_index(
        drop=True)
    guide_bbox = (fs, fw, fn, fe)
    guide_frame_local = snap_local
    guide, guide_evidence = build_amap_salience_guide(
        guide_bbox, guide_frame_local, allow_network=False)
    if guide is None:
        # Historical city batches cached the reference against the exact
        # finished frame, while newer runs use the snapped fetch frame.  Both
        # are legitimate as long as raster and projected bounds stay paired.
        guide_bbox = (south, west, north, east)
        guide_frame_local = exact_local
        guide, guide_evidence = build_amap_salience_guide(
            guide_bbox, guide_frame_local, allow_network=False)
    if guide is None:
        raise SystemExit(guide_evidence.get(
            "reason", "AMap salience guide unavailable"))

    scale = compute_scale(exact["width_m"], exact["height_m"])
    nozzle_real_m = DEFAULT_PRINTER_PROFILE.nozzle_diameter_mm / scale
    selection = select_road_roles(
        roads,
        topology_tier=4,
        nozzle_real_m=nozzle_real_m,
        bbox_local=exact_local,
        scale_mm_per_m=scale,
        road_width_multiplier=args.road_width_multiplier,
        visual_salience_guide=guide,
    )

    image = Image.new("RGB", (args.size, args.size), (248, 248, 246))
    draw = ImageDraw.Draw(image)
    xmin, ymin, xmax, ymax = exact_local
    clip = box(*exact_local)

    def pixel(point):
        x = (point[0] - xmin) / (xmax - xmin) * (args.size - 1)
        y = (ymax - point[1]) / (ymax - ymin) * (args.size - 1)
        return (round(x), round(y))

    visible = selection.visible
    for role in ("connector", "context", "secondary", "primary"):
        color, width = ROLE_STYLES[role]
        subset = visible[
            visible[COMPOSITION_ROLE_COLUMN].astype(str) == role]
        for geometry in subset.geometry:
            try:
                geometry = geometry.intersection(clip)
            except Exception:
                continue
            for line in _iter_lines(geometry):
                coordinates = [pixel(value) for value in line.coords]
                if len(coordinates) >= 2:
                    draw.line(coordinates, fill=color, width=width,
                              joint="curve")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    image.save(args.output)
    evidence_path = args.evidence or args.output.with_suffix(".json")
    evidence = {
        "bbox_wgs84": list(args.bbox),
        "snap_bbox_wgs84": [fs, fw, fn, fe],
        "pbf": args.pbf.name,
        "elapsed_s": round(time.time() - started, 3),
        "guide": guide_evidence,
        "selection": selection.evidence,
        "render": str(args.output),
        "constraint": (
            "diagnostic render of production selector; source OSM geometry "
            "only; no invented paths"),
    }
    evidence_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    print(json.dumps({
        "render": str(args.output),
        "evidence": str(evidence_path),
        "elapsed_s": evidence["elapsed_s"],
        "source_lines": selection.evidence["source_line_features"],
        "visible": selection.evidence["visible_selected"],
        "corridor_matching": selection.evidence["ink_budget"].get(
            "corridor_matching"),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
