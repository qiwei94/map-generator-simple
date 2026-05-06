"""Standalone test for obj_4: terrain + water hollow (地形+水体镂空).

Fetches elevation and water data for a small area, builds terrain with
water holes via Manifold batch boolean, and exports to 3MF.

Usage:
    cd /path/to/map_generator_final
    python tools/test_object4.py
"""

import os
import sys
import time

# Ensure project root
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import numpy as np
import trimesh

# Proxy for OSM access
os.environ["HTTP_PROXY"] = "http://127.0.0.1:7897"
os.environ["HTTPS_PROXY"] = "http://127.0.0.1:7897"
os.environ["http_proxy"] = "http://127.0.0.1:7897"
os.environ["https_proxy"] = "http://127.0.0.1:7897"

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm, project_geodataframe
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.elevation import fetch_elevation_grid
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import fetch_water
from _TEXTURE_STYLE_OF_DEEPSEEK.config import compute_scale, get_area_class
from _TEXTURE_STYLE_OF_DEEPSEEK.object4_terrain_with_holes import build_terrain_with_water_holes_manifold
from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf, split_terrain_mesh

# =====================================================================
# Test area: smaller fraction of Hangzhou (~10km × 10km around West Lake)
# =====================================================================
# Center: ~30.26°N, 120.15°E
# Δlat ≈ 10/111 ≈ 0.09°, Δlon ≈ 10/96 ≈ 0.104°

LAT1, LON1 = 30.215, 120.098   # south-west
LAT2, LON2 = 30.305, 120.202   # north-east
CITY_NAME = "hangzhou_obj4_test"
OUTPUT_DIR = "output/obj4_test"

print("=" * 60)
print("  Test: Object 4 — Terrain + Water Hollow")
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

# =====================================================================
# Stage 1: Fetch elevation data
# =====================================================================
print(f"\n[Stage 1] Fetching elevation data...")
t1 = time.time()

south, west, north, east = bbox["wgs84_bbox"]
area_class = get_area_class(area_km2)
resolution = 256  # Smaller resolution for fast test

elevation_grid = fetch_elevation_grid(south, west, north, east, resolution)
print(f"  Grid shape: {elevation_grid.shape}")
print(f"  Elevation range: {elevation_grid.min():.1f}m to {elevation_grid.max():.1f}m")
print(f"  Time: {time.time() - t1:.1f}s")

# =====================================================================
# Stage 2: Fetch water data from OSM
# =====================================================================
print(f"\n[Stage 2] Fetching OSM water data...")
t2 = time.time()

water_gdf = fetch_water(south, west, north, east)
n_water = len(water_gdf) if water_gdf is not None else 0
print(f"  Water features: {n_water}")
print(f"  Time: {time.time() - t2:.1f}s")

if water_gdf is None or len(water_gdf) == 0:
    print("  WARNING: No water data fetched, using synthetic water polygon")
    from shapely.geometry import Polygon
    water_gdf = None  # Will skip water hollowing

# =====================================================================
# Stage 3: Project to local UTM
# =====================================================================
print(f"\n[Stage 3] Projecting to local coordinates...")
t3 = time.time()

utm_crs = bbox["utm_crs"]
origin = bbox["origin"]
utm_bbox = bbox["utm_bbox"]

if water_gdf is not None and len(water_gdf) > 0:
    water_gdf = project_geodataframe(water_gdf, utm_crs, origin, clip_bbox=utm_bbox)
    print(f"  Projected: {len(water_gdf)} features")
print(f"  Time: {time.time() - t3:.1f}s")

# =====================================================================
# Stage 4: Build terrain with water holes (obj_4)
# =====================================================================
print(f"\n[Stage 4] Building terrain with water holes (obj_4)...")
t4 = time.time()

result = build_terrain_with_water_holes_manifold(
    elevation_grid, width_m, height_m, area_km2, scale,
    water_gdf=water_gdf if water_gdf is not None and len(water_gdf) > 0 else None,
)

terrain_mesh = result["mesh"]
stats = result["stats"]
validation = result["validation"]

print(f"\n  Result mesh: {len(terrain_mesh.vertices)} verts, {len(terrain_mesh.faces)} faces")
print(f"  Watertight: {validation['watertight']}")
print(f"  Volume: {validation['volume']:.2f} mm³")
print(f"  Z range: {terrain_mesh.bounds[0][2]:.2f} → {terrain_mesh.bounds[1][2]:.2f} mm")
print(f"  Stage 4 time: {time.time() - t4:.1f}s")

# =====================================================================
# Stage 5: Split and export
# =====================================================================
print(f"\n[Stage 5] Exporting 3MF...")
t5 = time.time()

os.makedirs(OUTPUT_DIR, exist_ok=True)
output_path = os.path.join(OUTPUT_DIR, f"{CITY_NAME}_obj4.3mf")

# Split into surface + walls
terrain_parts = split_terrain_mesh(terrain_mesh)
print(f"  Surface: {len(terrain_parts['terrain_surface'].faces)} faces")
print(f"  Walls: {len(terrain_parts['terrain_walls'].faces)} faces")

meshes = {
    "terrain_surface": terrain_parts["terrain_surface"],
    "terrain_walls": terrain_parts["terrain_walls"],
    "buildings": None,
    "roads": None,
    "water": None,
    "vegetation": None,
}

export_deepseek_3mf(meshes, output_path, extruders=4)

file_size_mb = os.path.getsize(output_path) / (1024 * 1024)
print(f"  Output: {output_path}")
print(f"  File size: {file_size_mb:.2f} MB")
print(f"  Time: {time.time() - t5:.1f}s")

# =====================================================================
# Summary
# =====================================================================
print(f"\n{'=' * 60}")
print(f"  Done! Output: {output_path}")
print(f"{'=' * 60}")
