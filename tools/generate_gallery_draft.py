#!/usr/bin/env python3
"""Generate a draft GLB directly from the prepared style-gallery cache.

The normal legacy entry is intentionally comprehensive: it fetches landuse,
rebuilds preprocessing and renders a diagnostic PNG before exporting GLB.
After a user has already generated and selected a style, those stages are
duplicate work.  This entry reopens the same ``CityHarness`` cache, loads the
selected preprocess result, and renders only the lightweight draft GLB.  The
matching topdown already exists in the style gallery.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aesthetic.presets import CityPreset, register_preset
from aesthetic.rerun_harness import CityHarness
from _TEXTURE_STYLE_OF_DEEPSEEK.render_glb import render_glb_preview
from _TEXTURE_STYLE_OF_DEEPSEEK.design_spec import write_design_spec


def _args():
    parser = argparse.ArgumentParser(
        description="从风格画廊缓存快速生成 GLB 预览")
    parser.add_argument("--bbox", required=True,
                        help="south,west,north,east")
    parser.add_argument("--pbf", required=True)
    parser.add_argument("--city", required=True)
    parser.add_argument("--prototype", default="landscape")
    parser.add_argument("--scene-type", default="urban")
    parser.add_argument("--source-bbox", default=None,
                        help="selected full framing bbox; draft bbox stays 5 km")
    parser.add_argument("--params-json", required=True)
    parser.add_argument("--marker", action="append", default=[])
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--base-thickness-mm", type=float, default=0.4)
    return parser.parse_args()


def main() -> int:
    args = _args()
    try:
        bbox = tuple(float(value) for value in args.bbox.split(","))
    except ValueError:
        raise SystemExit("--bbox must be south,west,north,east")
    if len(bbox) != 4 or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        raise SystemExit("invalid --bbox")

    pbf = Path(args.pbf)
    if not pbf.is_absolute():
        pbf = ROOT / pbf
    params = json.loads(Path(args.params_json).read_text(encoding="utf-8"))
    params["base_thickness_mm"] = args.base_thickness_mm
    out_dir = (Path(args.output_dir) if args.output_dir
               else ROOT / "output" / args.city)
    out_dir.mkdir(parents=True, exist_ok=True)

    register_preset(CityPreset(
        name=args.city, bbox=bbox, pbf=str(pbf),
        prototype=args.prototype, reference_dir="杭州",
        description=args.city,
    ))

    started = time.time()
    print(f"[fast-draft] reopening gallery cache for {args.city}")
    harness = CityHarness(CityPreset(
        name=args.city, bbox=bbox, pbf=str(pbf),
        prototype=args.prototype, reference_dir="杭州",
        description=args.city,
    ), use_cache=True)
    harness.prepare()
    layers = harness.run_round(params)

    markers_local = []
    if args.marker:
        from pyproj import Transformer
        transform = Transformer.from_crs(
            "EPSG:4326", harness.ctx["utm_crs"], always_xy=True)
        ox, oy = harness.ctx["origin"]
        for raw in args.marker:
            try:
                lat, lon = (float(v) for v in raw.split(","))
            except ValueError:
                continue
            x, y = transform.transform(lon, lat)
            markers_local.append((x - ox, y - oy))

    glb_path = out_dir / f"{args.city}_draft.glb"
    render_glb_preview(
        layers, harness.ctx, str(glb_path),
        elevation_grid=harness.ctx["elevation_grid"],
        markers=markers_local or None,
        water_gdf=harness.ctx.get("water"),
        base_thickness_mm=args.base_thickness_mm,
        terrain_relief_mm=float(params.get(
            "terrain_thickness_mm", harness.base_params.terrain_thickness_mm)),
        preview_quality="fast",
    )

    design_spec = {
        "schema_version": "1.0",
        "city": args.city,
        "bbox_wgs84": list(bbox),
        "framing": {
            "role": "center_preview",
            "preview_bbox_wgs84": list(bbox),
            "source_bbox_wgs84": (
                [float(value) for value in args.source_bbox.split(",")]
                if args.source_bbox else list(bbox)),
            "preview_size_km": 5,
        },
        "prototype": args.prototype,
        "scene_type": args.scene_type,
        "params": params,
        "evidence": {
            "building_density_per_km2": harness.profile.building_density,
            "water_ratio": harness.profile.water_ratio,
            "elevation_range_m": harness.profile.elevation_range_m,
            "secondary_water_polygons": len(
                harness.ctx.get("amap_water_polys") or []),
            "roads": len(layers.roads_lines),
            "water_polygons": len(layers.WL) + len(layers.WO),
        },
    }
    write_design_spec(out_dir, design_spec)
    print(f"[fast-draft] done in {time.time() - started:.1f}s: {glb_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
