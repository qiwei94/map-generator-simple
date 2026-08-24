#!/usr/bin/env python3
"""Audit optional AMap-guided OSM road/water selection without building mesh."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np
from PIL import Image, ImageDraw


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from aesthetic.amap_salience import (
    AmapSalienceGuide,
    compare_salience_masks,
    extract_amap_salience_masks,
    fetch_amap_salience_reference,
    render_salience_comparison,
)
from _TEXTURE_STYLE_OF_DEEPSEEK._layer_preprocess import _extract_WL_WO
from _TEXTURE_STYLE_OF_DEEPSEEK.road_roles import (
    resolve_printable_road_width_m,
    select_road_roles,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osmium_cli_fetcher import (
    fetch_from_cli,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import (
    bbox_to_utm,
    project_geodataframe,
)


def _parse_bbox(value: str) -> tuple[float, float, float, float]:
    parts = tuple(float(part.strip()) for part in value.split(","))
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("bbox must be south,west,north,east")
    return parts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare baseline and AMap-guided OSM selection")
    parser.add_argument("--bbox", required=True, type=_parse_bbox)
    parser.add_argument("--pbf", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--model-width-mm", type=float, default=200.0)
    parser.add_argument("--road-tier", type=int, default=4)
    parser.add_argument("--road-width-multiplier", type=float, default=2.0)
    parser.add_argument("--reference-size", type=int, default=1024)
    parser.add_argument("--allow-network", action="store_true")
    return parser.parse_args()


def _local_projection(bbox):
    south, west, north, east = bbox
    projection = bbox_to_utm(south, west, north, east)
    origin_x, origin_y = projection["origin"]
    xmin, ymin, xmax, ymax = projection["utm_bbox"]
    projection["bbox_local"] = (
        xmin - origin_x, ymin - origin_y,
        xmax - origin_x, ymax - origin_y,
    )
    return projection


def _load_source(pbf: Path, bbox, projection):
    if not pbf.is_file():
        raise SystemExit(f"PBF not found: {pbf}")
    from _TEXTURE_STYLE_OF_DEEPSEEK import config as project_config
    project_config.OVERTURE_ENABLED = False
    south, west, north, east = bbox
    output = {}
    for key, tag_type in (("roads", "road"), ("water", "water")):
        source = fetch_from_cli(
            tag_type=tag_type,
            south=south, west=west, north=north, east=east,
            pbf_file=str(pbf.resolve()),
        )
        output[key] = project_geodataframe(
            source, projection["utm_crs"], projection["origin"],
            projection["utm_bbox"])
    return output


def _iter_lines(geometry):
    if geometry is None or geometry.is_empty:
        return []
    if geometry.geom_type == "LineString":
        return [geometry]
    if hasattr(geometry, "geoms"):
        return [part for part in geometry.geoms
                if part.geom_type == "LineString" and not part.is_empty]
    return []


def _line_mask(roads, bbox_local, shape, scale, multiplier):
    height, width = shape
    xmin, ymin, xmax, ymax = bbox_local
    sx = (width - 1) / max(xmax - xmin, 1.0)
    sy = (height - 1) / max(ymax - ymin, 1.0)
    image = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(image)
    for _, row in roads.iterrows():
        highway = str(row.get("highway") or "")
        width_m = resolve_printable_road_width_m(
            highway,
            scale_mm_per_m=scale,
            road_width_multiplier=multiplier,
            min_colored_strip_mm=0.63,
        )
        width_px = max(1, int(round(width_m * (sx + sy) * 0.5)))
        for line in _iter_lines(row.geometry):
            points = [((x - xmin) * sx, (ymax - y) * sy)
                      for x, y in line.coords]
            if len(points) >= 2:
                draw.line(points, fill=255, width=width_px)
    return np.asarray(image) > 0


def _iter_polygons(geometry):
    if geometry is None or geometry.is_empty:
        return []
    if geometry.geom_type == "Polygon":
        return [geometry]
    if hasattr(geometry, "geoms"):
        polygons = []
        for child in geometry.geoms:
            polygons.extend(_iter_polygons(child))
        return polygons
    return []


def _water_mask(polygons, bbox_local, shape):
    height, width = shape
    xmin, ymin, xmax, ymax = bbox_local
    sx = (width - 1) / max(xmax - xmin, 1.0)
    sy = (height - 1) / max(ymax - ymin, 1.0)
    image = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(image)
    for geometry in polygons:
        for polygon in _iter_polygons(geometry):
            exterior = [((x - xmin) * sx, (ymax - y) * sy)
                        for x, y in polygon.exterior.coords]
            draw.polygon(exterior, fill=255)
            for ring in polygon.interiors:
                hole = [((x - xmin) * sx, (ymax - y) * sy)
                        for x, y in ring.coords]
                draw.polygon(hole, fill=0)
    return np.asarray(image) > 0


def _selection(sources, projection, scale, args, guide=None):
    roads = select_road_roles(
        sources["roads"],
        topology_tier=args.road_tier,
        nozzle_real_m=0.4 / scale,
        bbox_local=projection["bbox_local"],
        scale_mm_per_m=scale,
        road_width_multiplier=args.road_width_multiplier,
        min_colored_strip_mm=0.63,
        visual_salience_guide=guide,
    )
    wl, wo, _, water_evidence = _extract_WL_WO(
        sources["water"], 0.4 / scale,
        bbox_local=projection["bbox_local"],
        visual_salience_guide=guide,
    )
    shape = (args.reference_size, args.reference_size)
    return {
        "road_mask": _line_mask(
            roads.visible, projection["bbox_local"], shape, scale,
            args.road_width_multiplier),
        "water_mask": _water_mask([*wl, *wo], projection["bbox_local"], shape),
        "road_evidence": roads.evidence,
        "water_evidence": water_evidence,
    }


def main() -> int:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    projection = _local_projection(args.bbox)
    scale = args.model_width_mm / projection["width_m"]
    sources = _load_source(args.pbf, args.bbox, projection)
    reference_array, source_evidence = fetch_amap_salience_reference(
        args.bbox, zoom=13, output_size=args.reference_size,
        allow_network=args.allow_network)
    if reference_array is None:
        raise SystemExit(source_evidence.get("reason", "reference unavailable"))
    reference = extract_amap_salience_masks(reference_array)
    guide = AmapSalienceGuide(
        reference, projection["bbox_local"], tolerance_px=3)

    baseline = _selection(sources, projection, scale, args)
    guided = _selection(sources, projection, scale, args, guide)
    baseline_report = compare_salience_masks(
        reference, baseline["road_mask"], baseline["water_mask"])
    guided_report = compare_salience_masks(
        reference, guided["road_mask"], guided["water_mask"])

    payload = {
        "status": "selection_only_no_mesh",
        "bbox_wgs84": list(args.bbox),
        "scale_mm_per_m": scale,
        "nozzle_real_m": 0.4 / scale,
        "source": source_evidence,
        "baseline": {
            "comparison": baseline_report,
            "road_evidence": baseline["road_evidence"],
            "water_evidence": baseline["water_evidence"],
        },
        "guided": {
            "comparison": guided_report,
            "road_evidence": guided["road_evidence"],
            "water_evidence": guided["water_evidence"],
        },
        "warning": (
            "This audit changes selection only. It does not validate mesh, "
            "Z, booleans, manifoldness, or 3MF printability."
        ),
    }
    json_path = args.output_dir / f"{args.tag}_guided_selection.json"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    baseline_png = args.output_dir / f"{args.tag}_baseline_comparison.png"
    guided_png = args.output_dir / f"{args.tag}_guided_comparison.png"
    render_salience_comparison(
        reference, baseline["road_mask"], baseline["water_mask"],
        baseline_report, baseline_png)
    render_salience_comparison(
        reference, guided["road_mask"], guided["water_mask"],
        guided_report, guided_png)
    print(json.dumps({
        "json": str(json_path),
        "baseline_png": str(baseline_png),
        "guided_png": str(guided_png),
        "baseline_road_recall": {
            key: value["recall_with_tolerance"]
            for key, value in baseline_report["roads"].items()},
        "guided_road_recall": {
            key: value["recall_with_tolerance"]
            for key, value in guided_report["roads"].items()},
        "baseline_water_recall": baseline_report["water"][
            "recall_with_tolerance"],
        "guided_water_recall": guided_report["water"][
            "recall_with_tolerance"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
