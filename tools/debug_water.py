#!/usr/bin/env python3
"""Debug: 可视化 water_landmark_polys 中 OSM 原始 vs supplement 部分。"""
import pickle
import sys
import time
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from shapely.geometry import Polygon, MultiPolygon, LineString, MultiLineString

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from _TEXTURE_STYLE_OF_DEEPSEEK._water_supplement import supplement_wl_coverage
from tools.tune_buildings_v2 import is_water_landmark

# ---- 加载缓存数据 ----
cache = ROOT / "tmp" / "tune_v2_cache.westlake.full.pkl"
with open(cache, "rb") as f:
    data = pickle.load(f)
water_gdf = data["water"]
ctx = data["ctx"]
print(f"Loaded: {len(water_gdf)} water features")
print(f"  ctx keys: {sorted(ctx.keys())}")
print(f"  bbox_wgs84: {ctx.get('bbox_wgs84')}")
print(f"  utm_crs: {ctx.get('utm_crs')}")

# ---- 收集 OSM 原始水体 polygon ----
osm_water_polys = []
wl_lines_raw = []

WATERWAY_HALF_WIDTH = {
    "river": 90, "riverbank": 200, "canal": 25,
    "stream": 10, "drain": 6, "ditch": 4,
}

for idx, row in water_gdf.iterrows():
    geom = row.geometry
    if geom is None or geom.is_empty:
        continue
    if isinstance(geom, (Polygon, MultiPolygon)):
        polys_iter = geom.geoms if isinstance(geom, MultiPolygon) else [geom]
        for g in polys_iter:
            if g.is_empty:
                continue
            if is_water_landmark(row, area_m2=g.area):
                osm_water_polys.append(g)
    elif isinstance(geom, (LineString, MultiLineString)):
        if not is_water_landmark(row, area_m2=0.0):
            continue
        wway = row.get("waterway", "river")
        lines_iter = geom.geoms if isinstance(geom, MultiLineString) else [geom]
        for L in lines_iter:
            if L.is_empty or L.length < 10.0:
                continue
            wl_lines_raw.append((L, wway))

print(f"OSM polygons: {len(osm_water_polys)}")
print(f"WL lines: {len(wl_lines_raw)}")

# ---- 调 supplement（会 print 过程日志）----
bbox_wgs84 = ctx.get("bbox_wgs84")
utm_crs = ctx.get("utm_crs")
origin_xy = ctx.get("origin")

all_polys = supplement_wl_coverage(
    osm_water_polys, wl_lines_raw, bbox_wgs84,
    utm_crs=utm_crs, origin=origin_xy,
)

supplement_polys = all_polys[len(osm_water_polys):]
print(f"\nOSM: {len(osm_water_polys)}, supplement: {len(supplement_polys)}, total: {len(all_polys)}")

# ---- 获取高德原始 polygon（用于对比 OSM 独有水体）----
from _TEXTURE_STYLE_OF_DEEPSEEK._water_supplement import (
    _fetch_amap_water, _project_to_utm, _chamfer_match, _apply_chamfer_transform
)
from shapely.ops import unary_union

amap_polys_utm = []
try:
    amap_polys_wgs84 = _fetch_amap_water(bbox_wgs84)
    if amap_polys_wgs84:
        amap_raw = _project_to_utm(amap_polys_wgs84, utm_crs, origin_xy)
        if amap_raw:
            osm_valid = [p for p in osm_water_polys if p.is_valid and p.area > 20000]
            amap_valid = [p for p in amap_raw if p.is_valid and p.area > 20000]
            scale, angle, score = _chamfer_match(osm_valid, amap_valid)
            if abs(scale - 1.0) > 0.02 or abs(angle) > 0.5:
                amap_polys_utm = _apply_chamfer_transform(amap_raw, scale, angle)
            else:
                amap_polys_utm = amap_raw
except Exception as e:
    print(f"  Gaode fetch for comparison failed: {e}")

# 找出 OSM 有但高德没有的水体
osm_only_polys = []
if amap_polys_utm:
    amap_union = unary_union([p for p in amap_polys_utm if p.is_valid and not p.is_empty])
    amap_coverage = amap_union.buffer(30)
    for p in osm_water_polys:
        if not isinstance(p, Polygon) or p.is_empty:
            continue
        overlap = p.intersection(amap_coverage).area
        if overlap / p.area < 0.3:
            osm_only_polys.append(p)
    print(f"\nOSM-only (高德无覆盖): {len(osm_only_polys)} polygons")
else:
    print("\n无高德数据，跳过 OSM-only 对比")

# ---- 画 debug 图 ----
fig, ax = plt.subplots(figsize=(16, 16))
ax.set_aspect('equal')
ax.set_facecolor('#f5f5f5')

# OSM 原始：深蓝
for p in osm_water_polys:
    if not isinstance(p, Polygon) or p.is_empty:
        continue
    xs, ys = p.exterior.xy
    ax.fill(xs, ys, alpha=0.7, facecolor='#0e74a8', edgecolor='#072e44', linewidth=0.5)

# supplement：红色半透（问题区域）
for i, p in enumerate(supplement_polys):
    if not isinstance(p, Polygon) or p.is_empty:
        continue
    xs, ys = p.exterior.xy
    ax.fill(xs, ys, alpha=0.5, facecolor='#e74c3c', edgecolor='#8b0000', linewidth=0.8)
    cx, cy = p.centroid.x, p.centroid.y
    ax.text(cx, cy, f"S{i}", fontsize=6, ha='center', va='center', color='white',
            fontweight='bold')

# OSM-only（高德无覆盖）：绿色
for p in osm_only_polys:
    if not isinstance(p, Polygon) or p.is_empty:
        continue
    xs, ys = p.exterior.xy
    ax.fill(xs, ys, alpha=0.6, facecolor='#27ae60', edgecolor='#145a32', linewidth=0.8)

# WL lines：黄色虚线
for L, wtype in wl_lines_raw:
    xs, ys = L.xy
    ax.plot(xs, ys, color='#f39c12', linewidth=0.8, linestyle='--', alpha=0.8)

ax.legend(handles=[
    plt.Rectangle((0,0),1,1, fc='#0e74a8', alpha=0.7, label=f'OSM polygon ({len(osm_water_polys)})'),
    plt.Rectangle((0,0),1,1, fc='#e74c3c', alpha=0.5, label=f'Supplement ({len(supplement_polys)})'),
    plt.Rectangle((0,0),1,1, fc='#27ae60', alpha=0.6, label=f'OSM-only (Gaode missing: {len(osm_only_polys)})'),
    plt.Line2D([0],[0], color='#f39c12', ls='--', label=f'WL lines ({len(wl_lines_raw)})'),
], loc='upper right', fontsize=10)
ax.set_title("Water debug: OSM (blue) vs Supplement (red) vs OSM-only (GREEN = Gaode missing)", fontsize=14)

out = ROOT / "output" / "debug_water_supplement.png"
fig.savefig(out, dpi=150, bbox_inches='tight')
plt.close()
print(f"\n→ {out}")

# 输出 supplement 每个的面积
print("\nSupplement polygon areas:")
for i, p in enumerate(supplement_polys):
    print(f"  S{i}: area={p.area:.0f} m², bounds={tuple(f'{v:.0f}' for v in p.bounds)}")

# 输出 OSM-only 水体
if osm_only_polys:
    print(f"\nOSM-only polygons (Gaode missing, {len(osm_only_polys)} total):")
    for i, p in enumerate(osm_only_polys):
        print(f"  G{i}: area={p.area:.0f} m², bounds={tuple(f'{v:.0f}' for v in p.bounds)}")
