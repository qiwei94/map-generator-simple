"""水体补全诊断工具 — 每步输出 PNG，快速发现问题。

逐步可视化：
1. OSM 原始数据（polygon + LineString）
2. 未覆盖段检测
3. 高德提取结果（仅中国城市）
4. Chamfer 匹配结果
5. 自适应 buffer 结果
6. 最终合并

用法:
    venv/bin/python tools/water_supplement_debug.py --city chongqing
    venv/bin/python tools/water_supplement_debug.py --city chicago
"""
import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
from shapely.geometry import shape, LineString, MultiLineString, Polygon, MultiPolygon
from shapely.ops import unary_union
from pyproj import Transformer, CRS
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _TEXTURE_STYLE_OF_DEEPSEEK._landmark import is_water_landmark
from _TEXTURE_STYLE_OF_DEEPSEEK.config import WATERWAY_HALF_WIDTH

PRESETS = {
    "chongqing": (29.4535, 106.4535, 29.6785, 106.7125, "chongqing-260508"),
    "chicago":   (41.7656, -87.8926, 41.9906, -87.5900, "chicago"),
}


def load_and_project(city_key):
    bbox = PRESETS[city_key]
    lat1, lon1, lat2, lon2 = bbox[:4]
    bbox_wgs84 = (lat1, lon1, lat2, lon2)

    # Find OSM water file — try exact match then glob
    osm_file = Path(f"tmp/osmium_water_{lat1}_{lon1}_{lat2}_{lon2}.geojson")
    if not osm_file.exists():
        # Try with 4-decimal formatting
        osm_file = Path(f"tmp/osmium_water_{lat1:.4f}_{lon1:.4f}_{lat2:.4f}_{lon2:.4f}.geojson")
    if not osm_file.exists():
        # Glob fallback
        pattern = f"osmium_water_{lat1}*{lon2}*.geojson"
        matches = list(Path("tmp").glob(pattern))
        if matches:
            osm_file = matches[0]
        else:
            print(f"ERROR: no water file found for {lat1},{lon1},{lat2},{lon2}")
            sys.exit(1)
    print(f"  Using: {osm_file}")

    # Determine UTM zone
    center_lon = (lon1 + lon2) / 2
    utm_zone = int((center_lon + 180) / 6) + 1
    hemisphere = "north" if (lat1 + lat2) / 2 >= 0 else "south"
    epsg = 32600 + utm_zone if hemisphere == "north" else 32700 + utm_zone
    utm_crs = CRS.from_epsg(epsg)
    tr = Transformer.from_crs("EPSG:4326", utm_crs, always_xy=True)
    ox, oy = tr.transform(lon1, lat1)
    origin = (ox, oy)

    with open(osm_file) as f:
        osm_data = json.load(f)

    polys_utm = []
    lines_utm = []
    all_lines_raw = []

    for feat in osm_data["features"]:
        g = shape(feat["geometry"])
        props = feat.get("properties", {})
        wtype = props.get("waterway", "river")

        if g.geom_type in ("Polygon", "MultiPolygon"):
            geoms = g.geoms if g.geom_type == "MultiPolygon" else [g]
            for poly in geoms:
                if poly.is_empty:
                    continue
                coords = [(x - ox, y - oy) for x, y in
                          [tr.transform(lon, lat) for lon, lat in poly.exterior.coords]]
                p = Polygon(coords)
                if p.is_valid and p.area > 1000:
                    # Check if landmark
                    row = {**props, "geometry": poly}
                    import pandas as pd
                    row_s = pd.Series(row)
                    if is_water_landmark(row_s, area_m2=p.area):
                        polys_utm.append(p)

        elif g.geom_type in ("LineString", "MultiLineString"):
            row = {**props, "geometry": g}
            import pandas as pd
            row_s = pd.Series(row)
            if not is_water_landmark(row_s):
                continue
            geoms = g.geoms if g.geom_type == "MultiLineString" else [g]
            for line in geoms:
                if line.is_empty or line.length < 10:
                    continue
                coords = [(x - ox, y - oy) for x, y in
                          [tr.transform(lon, lat) for lon, lat in line.coords]]
                ls = LineString(coords)
                lines_utm.append((ls, wtype))
                # Also buffer for WL polygon
                half_w = WATERWAY_HALF_WIDTH.get(wtype, 30)
                buf = ls.buffer(half_w, cap_style=2, join_style=2)
                if isinstance(buf, Polygon) and not buf.is_empty:
                    polys_utm.append(buf)
                elif isinstance(buf, MultiPolygon):
                    for part in buf.geoms:
                        if not part.is_empty:
                            polys_utm.append(part)

    print(f"  Loaded: {len(polys_utm)} WL polys, {len(lines_utm)} WL lines")
    return bbox_wgs84, utm_crs, origin, polys_utm, lines_utm


def plot_step(ax, polys, lines, title, xlim=None, ylim=None,
              extra_polys=None, extra_label=None, highlight_lines=None):
    """Helper to plot a step."""
    for p in polys:
        if not p.is_valid:
            continue
        try:
            x, y = p.exterior.xy
            ax.fill(x, y, color="#4488cc", alpha=0.5)
            ax.plot(x, y, "b-", linewidth=0.3)
        except:
            pass

    if extra_polys:
        for p in extra_polys:
            if not p.is_valid:
                continue
            try:
                x, y = p.exterior.xy
                ax.fill(x, y, color="#44cc88", alpha=0.5)
                ax.plot(x, y, "g-", linewidth=0.5)
            except:
                pass

    for line, _ in lines:
        x, y = line.xy
        ax.plot(x, y, "orange", linewidth=1, linestyle="--", alpha=0.7)

    if highlight_lines:
        for seg in highlight_lines:
            x, y = seg.xy
            ax.plot(x, y, "red", linewidth=2.5)

    ax.set_aspect("equal")
    if xlim:
        ax.set_xlim(xlim)
    if ylim:
        ax.set_ylim(ylim)
    ax.set_title(title, fontsize=10)
    ax.grid(True, alpha=0.2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", required=True, choices=list(PRESETS.keys()))
    args = parser.parse_args()

    out_dir = Path(f"output/water_debug_{args.city}")
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"=== Water Supplement Debug: {args.city} ===")
    bbox_wgs84, utm_crs, origin, wl_polys, wl_lines = load_and_project(args.city)

    # Compute extent
    all_geoms = wl_polys + [line for line, _ in wl_lines]
    if all_geoms:
        bounds = [g.bounds for g in all_geoms if not g.is_empty]
        x_min = min(b[0] for b in bounds) - 500
        y_min = min(b[1] for b in bounds) - 500
        x_max = max(b[2] for b in bounds) + 500
        y_max = max(b[3] for b in bounds) + 500
    else:
        x_min, y_min, x_max, y_max = 0, 0, 25000, 25000

    xlim = (x_min, x_max)
    ylim = (y_min, y_max)

    # === Step 1: Original data ===
    print("\nStep 1: Original OSM data")
    fig, ax = plt.subplots(1, 1, figsize=(12, 12))
    plot_step(ax, wl_polys, wl_lines,
              f"Step 1: OSM original — {len(wl_polys)} polys (blue), "
              f"{len(wl_lines)} lines (orange)",
              xlim=xlim, ylim=ylim)
    fig.savefig(out_dir / "step1_osm_original.png", dpi=120, bbox_inches="tight")
    plt.close()
    print(f"  → {out_dir}/step1_osm_original.png")

    # === Step 2: Uncovered detection ===
    print("\nStep 2: Uncovered segment detection")
    wl_union = unary_union(wl_polys) if wl_polys else Polygon()
    poly_coverage = wl_union.buffer(30) if not wl_union.is_empty else Polygon()

    uncovered = []
    for line, wtype in wl_lines:
        diff = line.difference(poly_coverage)
        if diff.is_empty:
            continue
        if isinstance(diff, LineString):
            if diff.length >= 200:
                uncovered.append(diff)
        elif hasattr(diff, "geoms"):
            for g in diff.geoms:
                if isinstance(g, LineString) and g.length >= 200:
                    uncovered.append(g)

    total_km = sum(s.length for s in uncovered) / 1000
    print(f"  Uncovered: {len(uncovered)} segments, {total_km:.1f} km")

    fig, ax = plt.subplots(1, 1, figsize=(12, 12))
    plot_step(ax, wl_polys, wl_lines,
              f"Step 2: Uncovered segments (RED) — {len(uncovered)} segs, {total_km:.1f}km",
              xlim=xlim, ylim=ylim, highlight_lines=uncovered)
    fig.savefig(out_dir / "step2_uncovered.png", dpi=120, bbox_inches="tight")
    plt.close()
    print(f"  → {out_dir}/step2_uncovered.png")

    # === Step 3: Gaode supplement ===
    print("\nStep 3: Gaode water extraction")
    from _TEXTURE_STYLE_OF_DEEPSEEK._water_supplement import (
        _fetch_amap_water, _project_to_utm, _chamfer_match,
        _apply_chamfer_transform, _adaptive_buffer_segments,
        _MIN_POLYGON_AREA_M2,
    )

    t0 = time.time()
    amap_polys_wgs = _fetch_amap_water(bbox_wgs84)
    print(f"  Fetch time: {time.time() - t0:.1f}s")

    amap_polys_utm = []
    if amap_polys_wgs:
        amap_polys_utm = _project_to_utm(amap_polys_wgs, utm_crs, origin)
        amap_polys_utm = [p for p in amap_polys_utm if p.is_valid and p.area > 20000]
        print(f"  Gaode UTM: {len(amap_polys_utm)} polygons")

    fig, ax = plt.subplots(1, 1, figsize=(12, 12))
    plot_step(ax, wl_polys, wl_lines,
              f"Step 3: Gaode extraction (GREEN) — {len(amap_polys_utm)} polys",
              xlim=xlim, ylim=ylim, extra_polys=amap_polys_utm)
    fig.savefig(out_dir / "step3_gaode.png", dpi=120, bbox_inches="tight")
    plt.close()
    print(f"  → {out_dir}/step3_gaode.png")

    # === Step 4: Chamfer matching ===
    print("\nStep 4: Chamfer matching")
    amap_matched = amap_polys_utm
    if amap_polys_utm and wl_polys:
        osm_valid = [p for p in wl_polys if p.is_valid and p.area > 20000]
        amap_valid = [p for p in amap_polys_utm if p.is_valid and p.area > 20000]
        scale, angle, score = _chamfer_match(osm_valid, amap_valid)
        print(f"  Result: scale={scale:.3f}, angle={angle:.1f}°, score={score:.1f}m")

        if abs(scale - 1.0) > 0.02 or abs(angle) > 0.5:
            amap_matched = _apply_chamfer_transform(amap_polys_utm, scale, angle)
            print(f"  → Applied transform")
        else:
            print(f"  → Identity (no correction needed)")
    else:
        scale, angle, score = 1.0, 0.0, 0.0
        print(f"  → Skipped (no Gaode data or no OSM polygons)")

    fig, ax = plt.subplots(1, 1, figsize=(12, 12))
    plot_step(ax, wl_polys, wl_lines,
              f"Step 4: After Chamfer (scale={scale:.3f}, angle={angle:.1f}°, "
              f"score={score:.1f}m)",
              xlim=xlim, ylim=ylim, extra_polys=amap_matched)
    fig.savefig(out_dir / "step4_chamfer.png", dpi=120, bbox_inches="tight")
    plt.close()
    print(f"  → {out_dir}/step4_chamfer.png")

    # === Step 5: Subtract existing + add supplement ===
    print("\nStep 5: Subtract existing coverage → supplement")
    supplement_polys = []
    for ap in amap_matched:
        if not ap.is_valid or ap.area < _MIN_POLYGON_AREA_M2:
            continue
        diff = ap.difference(poly_coverage)
        if diff.is_empty:
            continue
        if isinstance(diff, Polygon) and diff.area > _MIN_POLYGON_AREA_M2:
            supplement_polys.append(diff)
        elif isinstance(diff, MultiPolygon):
            for part in diff.geoms:
                if part.area > _MIN_POLYGON_AREA_M2:
                    supplement_polys.append(part)
    print(f"  Supplement from Gaode: {len(supplement_polys)} polygons")

    combined_polys = wl_polys + supplement_polys
    fig, ax = plt.subplots(1, 1, figsize=(12, 12))
    plot_step(ax, wl_polys, wl_lines,
              f"Step 5: OSM + Gaode supplement (GREEN) — +{len(supplement_polys)}",
              xlim=xlim, ylim=ylim, extra_polys=supplement_polys)
    fig.savefig(out_dir / "step5_supplement.png", dpi=120, bbox_inches="tight")
    plt.close()
    print(f"  → {out_dir}/step5_supplement.png")

    # === Step 6: Adaptive buffer for remaining uncovered ===
    print("\nStep 6: Adaptive buffer for remaining uncovered")
    updated_union = unary_union(combined_polys) if combined_polys else Polygon()
    updated_coverage = (updated_union.buffer(30)
                        if not updated_union.is_empty else Polygon())

    still_uncovered = []
    for seg in uncovered:
        diff = seg.difference(updated_coverage)
        if diff.is_empty:
            continue
        if isinstance(diff, LineString):
            if diff.length >= 200:
                still_uncovered.append(diff)
        elif hasattr(diff, "geoms"):
            for g in diff.geoms:
                if isinstance(g, LineString) and g.length >= 200:
                    still_uncovered.append(g)

    still_km = sum(s.length for s in still_uncovered) / 1000
    print(f"  Still uncovered: {len(still_uncovered)} segments, {still_km:.1f} km")

    adaptive_polys = []
    if still_uncovered:
        adaptive_polys = _adaptive_buffer_segments(
            still_uncovered, updated_union, combined_polys)
        print(f"  Adaptive buffer: {len(adaptive_polys)} polygons")

    fig, ax = plt.subplots(1, 1, figsize=(12, 12))
    plot_step(ax, combined_polys, [],
              f"Step 6: + Adaptive buffer (GREEN) — {len(adaptive_polys)} polys "
              f"({still_km:.1f}km uncovered)",
              xlim=xlim, ylim=ylim, extra_polys=adaptive_polys,
              highlight_lines=still_uncovered)
    # Also show lines
    for line, _ in wl_lines:
        x, y = line.xy
        ax.plot(x, y, "orange", linewidth=0.5, linestyle="--", alpha=0.4)
    fig.savefig(out_dir / "step6_adaptive.png", dpi=120, bbox_inches="tight")
    plt.close()
    print(f"  → {out_dir}/step6_adaptive.png")

    # === Step 7: Final result ===
    print("\nStep 7: Final result")
    final_polys = combined_polys + adaptive_polys
    print(f"  Total: {len(wl_polys)} → {len(final_polys)} "
          f"(+{len(supplement_polys)} gaode, +{len(adaptive_polys)} adaptive)")

    fig, axes = plt.subplots(1, 2, figsize=(24, 12))

    # Before
    plot_step(axes[0], wl_polys, wl_lines,
              f"BEFORE: {len(wl_polys)} polys",
              xlim=xlim, ylim=ylim)

    # After
    plot_step(axes[1], wl_polys, wl_lines,
              f"AFTER: {len(final_polys)} polys "
              f"(+{len(supplement_polys)} gaode, +{len(adaptive_polys)} adaptive)",
              xlim=xlim, ylim=ylim,
              extra_polys=supplement_polys + adaptive_polys)

    fig.savefig(out_dir / "step7_final_comparison.png", dpi=120, bbox_inches="tight")
    plt.close()
    print(f"  → {out_dir}/step7_final_comparison.png")

    # === Summary panel ===
    print("\n" + "=" * 60)
    print("Summary:")
    print(f"  City: {args.city}")
    print(f"  OSM polygons: {len(wl_polys)}")
    print(f"  OSM lines: {len(wl_lines)}")
    print(f"  Uncovered: {len(uncovered)} segments ({total_km:.1f} km)")
    print(f"  Gaode supplement: {len(supplement_polys)} polygons")
    print(f"  Adaptive buffer: {len(adaptive_polys)} polygons")
    print(f"  Final: {len(final_polys)} polygons")
    print(f"  Output: {out_dir}/")
    print("=" * 60)


if __name__ == "__main__":
    main()
