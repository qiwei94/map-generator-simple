#!/usr/bin/env python3
"""诊断 buildings v2 block_fill：为什么某区域的"楼"没被处理？

用法：
    # 瓜山立交桥附近 1km
    venv/bin/python tools/diagnose_block.py --lat 30.348 --lon 120.175 --radius-m 1000

    # 改半径
    venv/bin/python tools/diagnose_block.py --lat 30.25 --lon 120.15 --radius-m 500

输出：
  1. 控制台：OSM 建筑数 / tag / block 数 / 每个 block 的命运
  2. PNG：output/tune_buildings_v2/diagnose_<lat>_<lon>_<r>m.png
     图层：
       灰底 = 所有 OSM 建筑（含未被处理的）
       淡黄虚线 = 切出来的 city block 边界
       红色斜线 = 被 compactness 过滤掉的 block
       黄色斜线 = compact 通过但 count/density 阈值未过的 block
       浅蓝实色 = block_fill 输出（实际入模型）
       橙红 = ≥ PRINT_LIMIT 的个体大楼
       红圈 = 用户指定的查询圆
"""

from __future__ import annotations

import argparse
import pickle
import sys
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon as MplPoly, Circle as MplCircle
from matplotlib.collections import PatchCollection
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Point, Polygon, box
from shapely.ops import polygonize, unary_union

_PROJECT = Path(__file__).resolve().parent.parent
if str(_PROJECT) not in sys.path:
    sys.path.insert(0, str(_PROJECT))

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm
from _TEXTURE_STYLE_OF_DEEPSEEK.config import (
    BUILDING_PRINT_LIMIT_M2,
    BUILDING_SIMPLIFY_TOL_M,
    BUILDING_V2_DENSITY_THRESHOLD,
    BUILDING_V2_COUNT_THRESHOLD,
    BUILDING_V2_MIN_BLOCK_COMPACTNESS,
    BUILDING_V2_ROAD_TIER,
    BUILDING_V2_USE_WATER_BLOCKS,
)

LAT1, LON1, LAT2, LON2 = 30.13, 120.01, 30.36, 120.29
CACHE = _PROJECT / "tmp" / "tune_v2_cache.full.pkl"
OUT_DIR = _PROJECT / "output" / "tune_buildings_v2"

ROAD_TIERS = {
    1: ["motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link"],
    2: ["motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link",
        "secondary", "secondary_link", "tertiary", "tertiary_link"],
    3: ["motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link",
        "secondary", "secondary_link", "tertiary", "tertiary_link",
        "residential", "unclassified", "living_street"],
    4: ["motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link",
        "secondary", "secondary_link", "tertiary", "tertiary_link",
        "residential", "unclassified", "living_street",
        "service"],
    5: ["motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link",
        "secondary", "secondary_link", "tertiary", "tertiary_link",
        "residential", "unclassified", "living_street",
        "service",
        "pedestrian", "footway", "path", "steps", "track"],
}


def _compactness(poly: Polygon) -> float:
    L = poly.length
    return 0.0 if L <= 0 else 4.0 * np.pi * poly.area / (L * L)


def build_blocks(roads_gdf, water_gdf, ctx, road_tier, use_water):
    allowed = set(ROAD_TIERS[road_tier])
    rfilt = roads_gdf[roads_gdf["highway"].isin(allowed)] if "highway" in roads_gdf.columns else roads_gdf

    bx = ctx["bbox_utm"]; ox, oy = ctx["origin"]
    bbox_local = (bx[0] - ox, bx[1] - oy, bx[2] - ox, bx[3] - oy)
    lines = [box(*bbox_local).boundary]
    for geom in rfilt.geometry:
        if geom is None or geom.is_empty: continue
        if isinstance(geom, MultiLineString): lines.extend(geom.geoms)
        elif isinstance(geom, LineString): lines.append(geom)
    if use_water and water_gdf is not None:
        for geom in water_gdf.geometry:
            if geom is None or geom.is_empty: continue
            if isinstance(geom, Polygon):
                lines.append(geom.exterior)
                for h in geom.interiors: lines.append(h)
            elif isinstance(geom, MultiPolygon):
                for g in geom.geoms:
                    if g.is_empty: continue
                    lines.append(g.exterior)
                    for h in g.interiors: lines.append(h)
            elif isinstance(geom, MultiLineString): lines.extend(geom.geoms)
            elif isinstance(geom, LineString): lines.append(geom)
    return list(polygonize(unary_union(lines)))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--radius-m", type=float, default=1000)
    args = ap.parse_args()

    if not CACHE.exists():
        sys.exit(f"❌ {CACHE} 不存在，先跑过 tune_buildings_v2.py 生成缓存")
    with open(CACHE, "rb") as f:
        data = pickle.load(f)
    polys, roads, water, ctx = data["polys"], data["roads"], data["water"], data["ctx"]
    # landmark_flags 与 polys 同序（同 v2 缓存约定）；用于诊断 OSM 标签命中地标
    landmark_flags = data.get("landmark_flags", [False] * len(polys))

    # Project (lat, lon) → UTM → local
    bbox = bbox_to_utm(LAT1, LON1, LAT2, LON2)
    from pyproj import Transformer
    t = Transformer.from_crs("EPSG:4326", bbox["utm_crs"], always_xy=True)
    cx_utm, cy_utm = t.transform(args.lon, args.lat)
    ox, oy = bbox["origin"]
    cx, cy = cx_utm - ox, cy_utm - oy
    print(f"\nFocus: ({args.lat}, {args.lon}) → local UTM ({cx:.0f}, {cy:.0f})")
    print(f"  {len(polys)} buildings, {len(roads)} roads, "
          f"{0 if water is None else len(water)} water")
    print(f"  当前 V2 配置:")
    print(f"    PRINT_LIMIT       = {BUILDING_PRINT_LIMIT_M2}")
    print(f"    ROAD_TIER         = {BUILDING_V2_ROAD_TIER}")
    print(f"    USE_WATER         = {BUILDING_V2_USE_WATER_BLOCKS}")
    print(f"    MIN_COMPACTNESS   = {BUILDING_V2_MIN_BLOCK_COMPACTNESS}")
    print(f"    COUNT_THRESHOLD   = {BUILDING_V2_COUNT_THRESHOLD}")
    print(f"    DENSITY_THRESHOLD = {BUILDING_V2_DENSITY_THRESHOLD}")

    disk = Point(cx, cy).buffer(args.radius_m)
    zoom_lo = (cx - args.radius_m * 1.2, cy - args.radius_m * 1.2)
    zoom_hi = (cx + args.radius_m * 1.2, cy + args.radius_m * 1.2)

    # ---- buildings inside disk
    bldg_in = [p for p in polys if p.intersects(disk)]
    print(f"\n[1] 圆内 OSM 建筑: {len(bldg_in)} 栋")
    if bldg_in:
        areas = sorted((p.area for p in bldg_in), reverse=True)
        big = sum(1 for a in areas if a >= BUILDING_PRINT_LIMIT_M2)
        small = sum(1 for a in areas if a < BUILDING_PRINT_LIMIT_M2)
        print(f"    最大: {areas[0]:.0f} m², 中位: {areas[len(areas)//2]:.0f} m², 最小: {areas[-1]:.0f} m²")
        print(f"    ≥ {BUILDING_PRINT_LIMIT_M2:.0f} m² (个体保留): {big}")
        print(f"    <  PRINT_LIMIT (进 block_fill):       {small}")
        # Top 5 大楼面积
        print(f"    Top 5 面积: {[f'{a:.0f}' for a in areas[:5]]} m²")

    # ---- 模拟全图 simplify + 大小分流（与 buildings.py 完全一致）
    indv_in = []
    smalls_in = []
    for p in polys:
        if p.area < 1.0: continue
        s = p.simplify(BUILDING_SIMPLIFY_TOL_M, preserve_topology=True)
        if s.is_empty or s.area < 1.0: continue
        if isinstance(s, MultiPolygon):
            s = max(s.geoms, key=lambda g: g.area)
        if not isinstance(s, Polygon): continue
        if s.area >= BUILDING_PRINT_LIMIT_M2:
            if s.intersects(disk): indv_in.append(s)
        else:
            if s.intersects(disk): smalls_in.append(s)
    print(f"\n[2] 经 simplify({BUILDING_SIMPLIFY_TOL_M}m) 后圆内")
    print(f"    individuals (≥{BUILDING_PRINT_LIMIT_M2:.0f}m²): {len(indv_in)}")
    print(f"    smalls (待 block_fill):                : {len(smalls_in)}")

    # ---- city blocks
    print(f"\n[3] 构建 city blocks（tier={BUILDING_V2_ROAD_TIER}, water={BUILDING_V2_USE_WATER_BLOCKS}）...")
    blocks = build_blocks(roads, water, ctx, BUILDING_V2_ROAD_TIER, BUILDING_V2_USE_WATER_BLOCKS)
    blocks_in = [b for b in blocks if b.intersects(disk)]
    print(f"    全图 {len(blocks)} 个 block，圆内相交 {len(blocks_in)} 个")

    # ---- 模拟 buildings.py 的 _aggregate_in_blocks（对所有 smalls + landmarks 全量运行）
    print(f"\n[4] 圆内每个 block 的命运（地标也参与 count/density 计算）:")
    # 准备 smalls + 地标全量 + STRtree
    all_smalls = []
    all_landmarks = []   # 地标只参与 count/density，不画几何
    for i, p in enumerate(polys):
        if p.area < 1.0: continue
        s = p.simplify(BUILDING_SIMPLIFY_TOL_M, preserve_topology=True)
        if s.is_empty or s.area < 1.0: continue
        if isinstance(s, MultiPolygon):
            s = max(s.geoms, key=lambda g: g.area)
        if not isinstance(s, Polygon): continue
        # 地标判定：OSM 标签命中 OR 面积 ≥ PRINT_LIMIT（与 v2 主流程一致）
        is_lm = (i < len(landmark_flags) and landmark_flags[i]) or s.area >= BUILDING_PRINT_LIMIT_M2
        if is_lm:
            all_landmarks.append(s)
        else:
            all_smalls.append(s)
    print(f"  全图 smalls={len(all_smalls)}, landmarks={len(all_landmarks)}")
    from shapely.strtree import STRtree
    btree = STRtree(blocks)
    bldg_in_block = {}
    for bi, c in enumerate([p.centroid for p in all_smalls]):
        for ci in btree.query(c):
            if blocks[ci].contains(c):
                bldg_in_block.setdefault(ci, []).append(bi)
                break
    landmark_in_block = {}
    for li, c in enumerate([p.centroid for p in all_landmarks]):
        for ci in btree.query(c):
            if blocks[ci].contains(c):
                landmark_in_block.setdefault(ci, []).append(li)
                break

    # 为每个圆内 block 报告命运
    fates = {"FILL": [], "SKIP_compact": [], "SKIP_count": [],
             "SKIP_density": [], "LANDMARK_ONLY": [], "EMPTY": []}
    for ci, b in enumerate(blocks):
        if not b.intersects(disk): continue
        bi_list = bldg_in_block.get(ci, [])
        lm_list = landmark_in_block.get(ci, [])
        comp = _compactness(b)
        if len(bi_list) == 0:
            # 主入口仍是"有小楼的 block"——纯地标 block 不进入 fill 判定（与运行时一致）
            if len(lm_list) > 0:
                fates["LANDMARK_ONLY"].append((ci, b, comp, len(lm_list), 0.0))
            else:
                fates["EMPTY"].append((ci, b, comp, 0, 0.0))
            continue
        # 含地标的 count + density
        block_polys = [all_smalls[bi] for bi in bi_list]
        lm_polys    = [all_landmarks[li] for li in lm_list]
        n_polys     = len(block_polys) + len(lm_polys)
        total_area  = sum(p.area for p in block_polys) + sum(p.area for p in lm_polys)
        density = total_area / max(b.area, 1.0)
        if comp < BUILDING_V2_MIN_BLOCK_COMPACTNESS:
            fates["SKIP_compact"].append((ci, b, comp, n_polys, density))
        elif n_polys < BUILDING_V2_COUNT_THRESHOLD:
            fates["SKIP_count"].append((ci, b, comp, n_polys, density))
        elif density < BUILDING_V2_DENSITY_THRESHOLD:
            fates["SKIP_density"].append((ci, b, comp, n_polys, density))
        else:
            fates["FILL"].append((ci, b, comp, n_polys, density))

    for fate, items in fates.items():
        print(f"  {fate:14s}: {len(items)} blocks", end="")
        if items and fate.startswith("SKIP"):
            sample = items[:3]
            extra = ", ".join(f"area={x[1].area:.0f}m² compact={x[2]:.2f} n={x[3]} d={x[4]*100:.1f}%"
                              for x in sample)
            print(f"  e.g. {extra}")
        else:
            print()

    # ---- 渲染 PNG
    out = OUT_DIR / f"diagnose_{args.lat}_{args.lon}_{int(args.radius_m)}m.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(14, 14), dpi=180)

    def _coll(geoms, **kw):
        patches = [MplPoly(np.array(g.exterior.coords))
                   for g in geoms if isinstance(g, Polygon) and not g.is_empty]
        return PatchCollection(patches, **kw)

    # 灰底：圆内所有 OSM 建筑（含未处理）
    raw_in = [p for p in polys if p.intersects(disk)]
    if raw_in:
        ax.add_collection(_coll(raw_in, facecolor="#b0b0b0", edgecolor="#666",
                                linewidths=0.3, alpha=0.7))

    # block 边界 — 全部 thin
    for b in blocks_in:
        xy = np.array(b.exterior.coords)
        ax.plot(xy[:, 0], xy[:, 1], color="#dabb6a", linewidth=0.4, linestyle="--", alpha=0.6)

    # SKIP_compact (red 斜线)
    for ci, b, *_ in fates["SKIP_compact"]:
        ax.add_collection(_coll([b], facecolor="#ff5555", edgecolor="#aa0000",
                                linewidths=0.6, alpha=0.35, hatch="///"))

    # SKIP_count + SKIP_density (yellow)
    for fate in ("SKIP_count", "SKIP_density"):
        for ci, b, *_ in fates[fate]:
            ax.add_collection(_coll([b], facecolor="#ffcc44", edgecolor="#cc8800",
                                    linewidths=0.6, alpha=0.45, hatch="///"))

    # FILL (浅蓝)
    for ci, b, *_ in fates["FILL"]:
        ax.add_collection(_coll([b], facecolor="#a8c8e8", edgecolor="#1a1a1a",
                                linewidths=0.6, alpha=0.85))

    # individuals (橙)
    if indv_in:
        ax.add_collection(_coll(indv_in, facecolor="#e85a2c", edgecolor="#1a1a1a",
                                linewidths=0.5, alpha=0.95))

    # 查询圆
    ax.add_patch(MplCircle((cx, cy), args.radius_m,
                            facecolor="none", edgecolor="#ff0000",
                            linewidth=2.0, linestyle="--"))
    ax.plot(cx, cy, marker="x", color="#ff0000", markersize=12, mew=2)

    ax.set_xlim(zoom_lo[0], zoom_hi[0])
    ax.set_ylim(zoom_lo[1], zoom_hi[1])
    ax.set_aspect("equal")
    ax.set_xticks([]); ax.set_yticks([])
    title = (f"Diagnose ({args.lat}, {args.lon}) r={args.radius_m:.0f}m\n"
             f"OSM={len(bldg_in)} | indv={len(indv_in)} | smalls={len(smalls_in)} | "
             f"blocks={len(blocks_in)}\n"
             f"FILL={len(fates['FILL'])}  "
             f"SKIP[compact]={len(fates['SKIP_compact'])}  "
             f"SKIP[count]={len(fates['SKIP_count'])}  "
             f"SKIP[density]={len(fates['SKIP_density'])}  "
             f"EMPTY={len(fates['EMPTY'])}")
    ax.set_title(title, fontsize=12, family="monospace")
    fig.tight_layout(pad=0.5)
    fig.savefig(out, dpi=180, bbox_inches="tight", facecolor="white", pad_inches=0.2)
    plt.close(fig)
    print(f"\n→ {out.name}")


if __name__ == "__main__":
    main()
