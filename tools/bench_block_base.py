#!/usr/bin/env python3
"""Benchmark _compute_block_base: 拆解各阶段耗时。

用法: venv/bin/python tools/bench_block_base.py
"""
import pickle
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import geopandas as gpd
from shapely.geometry import Polygon, MultiPolygon, LineString, MultiLineString
from shapely.ops import unary_union

from _TEXTURE_STYLE_OF_DEEPSEEK._block_filter import (
    _build_exclusion_mask,
    _subtract_exclusions,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.buildings import _build_city_blocks
from _TEXTURE_STYLE_OF_DEEPSEEK.config import BLOCK_BASE_MIN_AREA_M2

# ---- 加载缓存 ----
cache = ROOT / "tmp" / "tune_v2_cache.westlake.full.pkl"
print("Loading cache...")
with open(cache, "rb") as f:
    data = pickle.load(f)

roads_gdf = data["roads"]
water_gdf = data["water"]
ctx = data["ctx"]
veg_landmarks = data.get("veg_landmarks", [])
print(f"  roads: {len(roads_gdf)}, water: {len(water_gdf)}, "
      f"veg_landmarks: {len(veg_landmarks)}")

# ---- Step 0: city_blocks (先重建，后续需要) ----
print(f"\n=== Step 0: city_blocks ===")
bbox_local = ctx.get("bbox_local")
if bbox_local is None:
    bbox_utm = ctx.get("bbox_utm")
    origin = ctx.get("origin")
    if bbox_utm and origin:
        bbox_local = (
            bbox_utm[0] - origin[0],
            bbox_utm[1] - origin[1],
            bbox_utm[2] - origin[0],
            bbox_utm[3] - origin[1],
        )
        print(f"  bbox_local from ctx: {tuple(f'{v:.0f}' for v in bbox_local)}")
    else:
        print(f"  ctx keys: {list(ctx.keys())}"); sys.exit(1)

t0 = time.time()
city_blocks = _build_city_blocks(roads_gdf, water_gdf, road_tier=5, bbox_local=bbox_local)
print(f"  city_blocks: {len(city_blocks)} in {time.time()-t0:.1f}s")

# ---- Step 1: area 过滤 ----
print(f"\n=== Step 1: area filter (min={BLOCK_BASE_MIN_AREA_M2} m²) ===")
t1 = time.time()
blocks_filt = [b for b in city_blocks
               if isinstance(b, Polygon) and not b.is_empty
               and b.area >= BLOCK_BASE_MIN_AREA_M2]
print(f"  {len(city_blocks)} → {len(blocks_filt)} in {time.time()-t1:.2f}s")

# ---- Step 2: _build_exclusion_mask 拆解 ----
print(f"\n=== Step 2: _build_exclusion_mask (拆解) ===")

# 2a: veg
t2a = time.time()
veg_polys = [p for p in veg_landmarks if isinstance(p, Polygon) and not p.is_empty]
print(f"  2a veg polygons: {len(veg_polys)} in {time.time()-t2a:.3f}s")

# 2b: water buffer
t2b = time.time()
water_polys = []
for geom in water_gdf.geometry:
    if geom is None or geom.is_empty:
        continue
    if isinstance(geom, MultiPolygon):
        water_polys.extend(g for g in geom.geoms if not g.is_empty)
    elif isinstance(geom, Polygon):
        water_polys.append(geom)
water_buffered = [p.buffer(40.0) for p in water_polys]
print(f"  2b water buffer(40): {len(water_polys)} → {len(water_buffered)} in {time.time()-t2b:.2f}s")

# 2c: road buffer
roads_lines_only = roads_gdf[roads_gdf.geometry.type.isin(["LineString", "MultiLineString"])]
t2c = time.time()
road_buffered = []
for geom in roads_lines_only.geometry:
    if geom is None or geom.is_empty:
        continue
    road_buffered.append(geom.buffer(25.0))
print(f"  2c road buffer(25): {len(roads_lines_only)} roads → {len(road_buffered)} polys in {time.time()-t2c:.1f}s")

# 2d: unary_union
all_excl_polys = veg_polys + water_buffered + road_buffered
print(f"  2d unary_union input: {len(all_excl_polys)} polygons")
t2d = time.time()
excl_union = unary_union(all_excl_polys)
t2d_elapsed = time.time() - t2d
print(f"  2d unary_union: {t2d_elapsed:.1f}s")
if hasattr(excl_union, 'geoms'):
    print(f"  2d result: MultiPolygon with {len(excl_union.geoms)} parts")
else:
    print(f"  2d result: {excl_union.geom_type}")

# 2e: 对比：直接调 _build_exclusion_mask 总耗时
t2e = time.time()
excl_direct = _build_exclusion_mask(
    water_gdf, veg_landmarks,
    roads_gdf=roads_lines_only,
    road_inset=25.0, water_inset=40.0)
print(f"  2e _build_exclusion_mask 总耗时: {time.time()-t2e:.1f}s")

# ---- Step 3: _subtract_exclusions ----
print(f"\n=== Step 3: _subtract_exclusions ===")
print(f"  blocks: {len(blocks_filt)}, exclusion type: {excl_union.geom_type}")
t3 = time.time()
result = _subtract_exclusions(blocks_filt, excl_union, min_area=100.0)
t3_elapsed = time.time() - t3
print(f"  {len(blocks_filt)} → {len(result)} blocks in {t3_elapsed:.1f}s")

# ---- 总结 ----
print(f"\n=== 耗时分布 ===")
print(f"  road buffer:       {time.time()-t2c - t2d_elapsed:.1f}s (在 2c 阶段)")
print(f"  unary_union:       {t2d_elapsed:.1f}s")
print(f"  subtract:          {t3_elapsed:.1f}s")
print(f"  合计 (不含 city_blocks): {time.time()-t2a:.1f}s")
