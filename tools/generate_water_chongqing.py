"""Generate standalone water-only 3MF for Chongqing Sanjiangkou area.

三江口 — confluence of Yangtze River (长江) and Jialing River (嘉陵江).
Center: ~Chaotianmen (朝天门) 29.568°N, 106.586°E

Usage:
    python generate_water_chongqing.py

Output:
    output/water_only/chongqing_sanjiangkou_water.3mf
"""

import os
import sys
import time

_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import numpy as np
import trimesh

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm, project_geodataframe
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import fetch_water
from _TEXTURE_STYLE_OF_DEEPSEEK.water import build_deepseek_water
from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf
from _TEXTURE_STYLE_OF_DEEPSEEK.config import compute_scale, get_area_class

# =====================================================================
# Chongqing Sanjiangkou (三江口) — 25km × 25km
# =====================================================================
# Center ~29.56°N, 106.58°E (Chaotianmen)
# At 29.5°N: 1° lat ≈ 111km, 1° lon ≈ 96.5km
# Δlat ≈ 25/111 ≈ 0.225°,  Δlon ≈ 25/96.5 ≈ 0.259°

LAT1, LON1 = 29.455, 106.455   # south-west
LAT2, LON2 = 29.680, 106.715   # north-east
CITY_NAME = "chongqing_sanjiangkou"
OUTPUT_DIR = "output/water_only"

print("=" * 60)
print("  Water-only 3MF — Chongqing Sanjiangkou")
print("=" * 60)

# =====================================================================
# Stage 0: Coordinate setup
# =====================================================================
print(f"\n[Stage 0] Bounding box: ({LAT1}, {LON1}) → ({LAT2}, {LON2})")

bbox = bbox_to_utm(LAT1, LON1, LAT2, LON2)
width_m = bbox["width_m"]
height_m = bbox["height_m"]
area_km2 = bbox["area_km2"]
scale = compute_scale(width_m, height_m)

print(f"  Width: {width_m:.0f}m, Height: {height_m:.0f}m")
print(f"  Area: {area_km2:.1f} km²")
print(f"  Scale: {scale:.6f} mm/m")
print(f"  Model size: {scale * max(width_m, height_m):.1f}mm × {scale * max(width_m, height_m):.1f}mm")

# =====================================================================
# Stage 1: Fetch water data from OSM
# =====================================================================
print(f"\n[Stage 1] Fetching OSM water data...")
t1 = time.time()

south, west, north, east = bbox["wgs84_bbox"]
water_gdf = fetch_water(south, west, north, east)
n_water = len(water_gdf) if water_gdf is not None else 0
print(f"  Water features: {n_water}")
print(f"  Time: {time.time() - t1:.1f}s")

if water_gdf is None or len(water_gdf) == 0:
    print("  ERROR: No water data fetched!")
    sys.exit(1)

# =====================================================================
# Stage 2: Project to local UTM coordinates
# =====================================================================
print(f"\n[Stage 2] Projecting to local coordinates...")
t2 = time.time()

utm_crs = bbox["utm_crs"]
origin = bbox["origin"]
utm_bbox = bbox["utm_bbox"]

water_gdf = project_geodataframe(water_gdf, utm_crs, origin, clip_bbox=utm_bbox)
print(f"  Projected: {len(water_gdf)} features")
print(f"  Time: {time.time() - t2:.1f}s")

# =====================================================================
# Stage 3: Build water mesh (Manifold boolean union)
# =====================================================================
print(f"\n[Stage 3] Building water mesh...")
t3 = time.time()

origin_x, origin_y = origin
water_bbox_x_min = utm_bbox[0] - origin_x
water_bbox_y_min = utm_bbox[1] - origin_y
water_bbox_x_max = utm_bbox[2] - origin_x
water_bbox_y_max = utm_bbox[3] - origin_y

water_mesh = build_deepseek_water(
    water_gdf,
    water_bbox_x_min, water_bbox_y_min,
    water_bbox_x_max, water_bbox_y_max,
    scale,
)

if water_mesh is None or len(water_mesh.faces) == 0:
    print("  ERROR: Water mesh generation failed!")
    sys.exit(1)

print(f"  Water mesh: {len(water_mesh.vertices)} verts, {len(water_mesh.faces)} faces")
print(f"  Z range: {water_mesh.vertices[:,2].min():.4f} → {water_mesh.vertices[:,2].max():.4f} mm")
print(f"  Watertight: {water_mesh.is_watertight}")
print(f"  Time: {time.time() - t3:.1f}s")

# =====================================================================
# Stage 4: Export as standalone water 3MF
# =====================================================================
print(f"\n[Stage 4] Exporting 3MF...")
t4 = time.time()

output_path = os.path.join(OUTPUT_DIR, f"{CITY_NAME}_water.3mf")
os.makedirs(OUTPUT_DIR, exist_ok=True)

meshes = {
    "terrain_surface": None,
    "terrain_walls": None,
    "buildings": None,
    "roads": None,
    "water": water_mesh,
    "vegetation": None,
}
export_deepseek_3mf(meshes, output_path, extruders=4)

file_size_mb = os.path.getsize(output_path) / (1024 * 1024)
print(f"  Output: {output_path}")
print(f"  File size: {file_size_mb:.2f} MB")
print(f"  Time: {time.time() - t4:.1f}s")

# =====================================================================
# Summary
# =====================================================================
print(f"\n{'=' * 60}")
print(f"  Done! Output: {output_path}")
print(f"{'=' * 60}")
