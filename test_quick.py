#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Quick test: Object4 terrain + bridges + water hollows
Small area (5km) for fast results

Tests:
  1. terrain + water hollows (no roads)
  2. terrain + bridges fusion + water hollows (bridges only)
"""

import os, sys, time, numpy as np

_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm, project_geodataframe
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import fetch_water, fetch_roads
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.elevation import fetch_elevation_grid
from _TEXTURE_STYLE_OF_DEEPSEEK.object4_terrain_with_holes import build_terrain_with_water_holes_manifold
from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf
from _TEXTURE_STYLE_OF_DEEPSEEK.config import compute_scale
from _TEXTURE_STYLE_OF_DEEPSEEK.bridge_filter import get_bridge_statistics

# West Lake core area (5km x 5km) - much faster
LAT1, LON1 = 30.22, 120.12
LAT2, LON2 = 30.26, 120.16

output_dir = "output/quick_test"
os.makedirs(output_dir, exist_ok=True)

print("=" * 60, flush=True)
print("  Quick Test: Object4 Terrain + Bridges + Water Hollows")
print("  Area: 5km x 5km (West Lake core)")
print("=" * 60, flush=True)

# ===== Step 0: Coordinate system
print("\n[Step 0] Coordinate system...", flush=True)
t0 = time.time()
bbox = bbox_to_utm(LAT1, LON1, LAT2, LON2)
width_m, height_m = bbox["width_m"], bbox["height_m"]
area_km2 = bbox["area_km2"]
utm_crs, origin, utm_bbox = bbox["utm_crs"], bbox["origin"], bbox["utm_bbox"]
scale = compute_scale(width_m, height_m)
south, west, north, east = bbox["wgs84_bbox"]
print(f"  Area: {area_km2:.1f} km2, Scale: {scale:.6f} mm/m, Time: {time.time()-t0:.1f}s", flush=True)

# ===== Step 1: Elevation data
print("\n[Step 1] Fetching elevation...", flush=True)
t1 = time.time()
elevation_grid = fetch_elevation_grid(south, west, north, east, 256)
print(f"  Shape: {elevation_grid.shape}, Elev: {elevation_grid.min():.1f}-{elevation_grid.max():.1f}m", flush=True)
print(f"  Time: {time.time()-t1:.1f}s", flush=True)

# ===== Step 2: OSM data (force fresh fetch, no proxy)
print("\n[Step 2] Fetching OSM data...", flush=True)
t2 = time.time()
water_gdf = fetch_water(south, west, north, east, use_cache=False)
roads_gdf = fetch_roads(south, west, north, east, use_cache=False)
water_count = len(water_gdf) if water_gdf is not None else 0
roads_count = len(roads_gdf) if roads_gdf is not None else 0
print(f"  Water: {water_count}", flush=True)
print(f"  Roads: {roads_count}", flush=True)
print(f"  Time: {time.time()-t2:.1f}s", flush=True)

# ===== Step 3: Project to local coordinates
print("\n[Step 3] Projecting to local coords...", flush=True)
t3 = time.time()
if water_gdf is not None and len(water_gdf) > 0:
    water_gdf = project_geodataframe(water_gdf, utm_crs, origin, clip_bbox=utm_bbox)
if roads_gdf is not None and len(roads_gdf) > 0:
    roads_gdf = project_geodataframe(roads_gdf, utm_crs, origin, clip_bbox=utm_bbox)
print(f"  Time: {time.time()-t3:.1f}s", flush=True)

# ===== Step 4: Bridge statistics (handle empty case)
print("\n[Step 4] Bridge statistics...", flush=True)
t4 = time.time()
stats = get_bridge_statistics(roads_gdf, water_gdf)
print(f"  Total roads: {stats.get('total_roads', 0)}", flush=True)
if stats.get('tagged_bridges') is not None:
    print(f"  Tagged bridges: {stats['tagged_bridges']}", flush=True)
    print(f"  Water-intersecting roads: {stats['water_intersecting']}", flush=True)
    print(f"  Bridge length: {stats.get('bridge_length_m', 0):.1f}m", flush=True)
print(f"  Time: {time.time()-t4:.2f}s", flush=True)

# ===== Step 5: Build & export
print("\n[Step 5] Building and exporting...", flush=True)

# Test 1: terrain + water hollows only
print("\n--- Test 1: terrain + water hollows only ---", flush=True)
t5a = time.time()
result1 = build_terrain_with_water_holes_manifold(
    elevation_grid, width_m, height_m, area_km2, scale,
    water_gdf=water_gdf, roads_gdf=None, enable_roads_fusion=False)
mesh1 = result1["mesh"]
t5a = time.time() - t5a
print(f"  Faces: {len(mesh1.faces)}, Watertight: {mesh1.is_watertight}", flush=True)
print(f"  Time: {t5a:.1f}s", flush=True)

out1 = os.path.join(output_dir, "terrain_water_holes.3mf")
export_deepseek_3mf({"terrain_surface": mesh1}, out1, extruders=4)
print(f"  Exported: {out1}", flush=True)
print(f"  Size: {os.path.getsize(out1)/1024:.1f} KB", flush=True)

# Test 2: terrain + bridges + water hollows
print("\n--- Test 2: terrain + bridges + water hollows ---", flush=True)
t5b = time.time()
result2 = build_terrain_with_water_holes_manifold(
    elevation_grid, width_m, height_m, area_km2, scale,
    water_gdf=water_gdf, roads_gdf=roads_gdf, enable_roads_fusion=True, bridges_only=True)
mesh2 = result2["mesh"]
t5b = time.time() - t5b
print(f"  Faces: {len(mesh2.faces)}, Watertight: {mesh2.is_watertight}", flush=True)
print(f"  Time: {t5b:.1f}s", flush=True)

out2 = os.path.join(output_dir, "terrain_bridges_water_holes.3mf")
export_deepseek_3mf({"terrain_surface": mesh2}, out2, extruders=4)
print(f"  Exported: {out2}", flush=True)
print(f"  Size: {os.path.getsize(out2)/1024:.1f} KB", flush=True)

# ===== Summary
print("\n" + "=" * 60, flush=True)
print("  Complete!", flush=True)
print("=" * 60, flush=True)
print(f"  Output: {os.path.abspath(output_dir)}", flush=True)
print(f"  1. terrain + water hollows: {out1}", flush=True)
print(f"  2. terrain + bridges + water hollows: {out2}", flush=True)
print(f"  Total time: {time.time()-t0:.1f}s", flush=True)
