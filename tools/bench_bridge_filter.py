#!/usr/bin/env python3
"""Benchmark bridge_filter: 对比跳过精确切割前后的耗时。

用法: venv/bin/python tools/bench_bridge_filter.py
"""
import pickle
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import geopandas as gpd
from shapely.geometry import LineString, MultiLineString
from shapely.ops import unary_union

# ---- 加载缓存数据 ----
cache = ROOT / "tmp" / "tune_v2_cache.westlake.full.pkl"
if not cache.exists():
    print(f"ERROR: {cache} not found"); sys.exit(1)

print("Loading cache...")
with open(cache, "rb") as f:
    data = pickle.load(f)

roads_gdf = data["roads"]
water_gdf = data["water"]
print(f"  roads: {len(roads_gdf)}, water: {len(water_gdf)}")

# ---- 旧方法：逐条 intersection 精确切割 ----
def old_filter(roads_gdf, water_gdf):
    water_union = unary_union(water_gdf.geometry)

    if 'bridge' in roads_gdf.columns:
        bridge_mask = (roads_gdf['bridge'] == 'yes').fillna(False)
    else:
        bridge_mask = False
    bridge_roads = roads_gdf[bridge_mask].copy()
    bridge_roads = bridge_roads[bridge_roads.geometry.intersects(water_union)].copy()

    segments = []
    for idx, row in bridge_roads.iterrows():
        intersection = row.geometry.intersection(water_union)
        if intersection.is_empty:
            continue
        if isinstance(intersection, (LineString, MultiLineString)):
            new_row = row.copy()
            new_row.geometry = intersection
            segments.append(new_row)

    if segments:
        return gpd.GeoDataFrame(segments, crs=roads_gdf.crs)
    return gpd.GeoDataFrame(columns=roads_gdf.columns, crs=roads_gdf.crs)


# ---- 新方法：直接返回完整桥梁道路 ----
def new_filter(roads_gdf, water_gdf):
    water_union = unary_union(water_gdf.geometry)

    if 'bridge' in roads_gdf.columns:
        bridge_mask = (roads_gdf['bridge'] == 'yes').fillna(False)
    else:
        bridge_mask = False
    bridge_roads = roads_gdf[bridge_mask].copy()
    bridge_roads = bridge_roads[bridge_roads.geometry.intersects(water_union)].copy()
    return bridge_roads


# ---- Benchmark ----
print("\n=== NEW (skip intersection) ===")
t0 = time.time()
result_new = new_filter(roads_gdf, water_gdf)
t_new = time.time() - t0
print(f"  结果: {len(result_new)} 条, 总长 {result_new.geometry.length.sum():.0f}m")
print(f"  耗时: {t_new:.2f}s")

print("\n=== OLD (per-road intersection) ===")
t0 = time.time()
result_old = old_filter(roads_gdf, water_gdf)
t_old = time.time() - t0
print(f"  结果: {len(result_old)} 条, 总长 {result_old.geometry.length.sum():.0f}m")
print(f"  耗时: {t_old:.2f}s")

print(f"\n=== 对比 ===")
print(f"  旧: {t_old:.2f}s → 新: {t_new:.2f}s  ({t_old/max(t_new,0.01):.0f}x 加速)")
print(f"  长度差: {result_new.geometry.length.sum() - result_old.geometry.length.sum():.0f}m "
      f"(桥段延伸量)")

# ---- 验证：旧桥段是否被新结果完全覆盖 ----
print(f"\n=== 覆盖验证 ===")
from shapely.ops import unary_union as _union

old_union = _union(result_old.geometry.buffer(1))  # 1m 容差
new_union = _union(result_new.geometry.buffer(1))

uncovered = old_union.difference(new_union)
coverage = 1 - uncovered.area / old_union.area if old_union.area > 0 else 1.0
print(f"  旧桥段被新结果覆盖: {coverage:.4%}")
if coverage > 0.999:
    print(f"  PASS: 所有旧桥段均被新结果包含")
else:
    print(f"  WARN: 有 {(1-coverage)*100:.2f}% 旧桥段未被覆盖")

# ---- 可视化：水体 + 新旧桥段对比 ----
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.collections import LineCollection

fig, ax = plt.subplots(1, 1, figsize=(14, 14))
ax.set_aspect('equal')
ax.set_facecolor('#f0f0f0')

# 水体（蓝色填充）
for geom in water_gdf.geometry:
    if geom is None or geom.is_empty:
        continue
    from shapely.geometry import Polygon, MultiPolygon
    polys = []
    if isinstance(geom, Polygon):
        polys = [geom]
    elif isinstance(geom, MultiPolygon):
        polys = list(geom.geoms)
    for p in polys:
        xs, ys = p.exterior.xy
        ax.fill(xs, ys, alpha=0.3, color='#4A90D9', zorder=1)

# 新桥段（绿色，粗，底层）
for geom in result_new.geometry:
    if geom is None or geom.is_empty:
        continue
    lines = geom.geoms if hasattr(geom, 'geoms') else [geom]
    for line in lines:
        xs, ys = line.xy
        ax.plot(xs, ys, color='#2ECC71', linewidth=2.0, alpha=0.6, zorder=2)

# 旧桥段（红色，细，顶层）
for geom in result_old.geometry:
    if geom is None or geom.is_empty:
        continue
    lines = geom.geoms if hasattr(geom, 'geoms') else [geom]
    for line in lines:
        xs, ys = line.xy
        ax.plot(xs, ys, color='#E74C3C', linewidth=1.0, alpha=0.8, zorder=3)

ax.legend(
    handles=[
        plt.Line2D([0],[0], color='#4A90D9', lw=6, alpha=0.3, label=f'水体 ({len(water_gdf)})'),
        plt.Line2D([0],[0], color='#2ECC71', lw=2, label=f'新：完整桥路 ({len(result_new)})'),
        plt.Line2D([0],[0], color='#E74C3C', lw=1, label=f'旧：精确切段 ({len(result_old)})'),
    ],
    loc='upper right', fontsize=10,
)
ax.set_title("Bridge filter: old (red=trimmed) vs new (green=full road)", fontsize=13)

out_path = ROOT / "output" / "bench_bridge_compare.png"
out_path.parent.mkdir(exist_ok=True)
fig.savefig(out_path, dpi=150, bbox_inches='tight')
plt.close()
print(f"\n  对比图: {out_path}")
