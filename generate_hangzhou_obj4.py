"""Generate Object 4 (Terrain with Water Holes) for Hangzhou West Lake."""

import os
import sys
import time
import numpy as np
import trimesh

_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm, project_geodataframe
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import set_pbf_file_path, fetch_water, fetch_roads
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.elevation import fetch_elevation_grid
from _TEXTURE_STYLE_OF_DEEPSEEK.object4_terrain_with_holes import build_terrain_with_water_holes_manifold
from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf
from _TEXTURE_STYLE_OF_DEEPSEEK.config import compute_scale, WATERWAY_WIDTHS

# Hangzhou West Lake area (25km × 25km)
LAT1, LON1 = 30.13, 120.01   # south-west
LAT2, LON2 = 30.36, 120.29   # north-east
CITY_NAME = "hangzhou_west_lake"
OUTPUT_DIR = "output/hangzhou_west_lake"

print("=" * 70)
print("  Hangzhou West Lake Object 4: Terrain + Water Holes")
print("=" * 70)

os.makedirs(OUTPUT_DIR, exist_ok=True)

# Set PBF data source
pbf_file = os.path.join(_project_root, "pbf_cache", "zhejiang-latest.osm.pbf")
if os.path.exists(pbf_file):
    print(f"\n✅ PBF file: {pbf_file}")
    print(f"   Size: {os.path.getsize(pbf_file) / 1024 / 1024:.1f} MB")
    set_pbf_file_path(pbf_file)
else:
    print(f"\n⚠️  PBF file not found")
    print(f"   Run: python3 manage_pbf.py download zhejiang")
    sys.exit(1)

# =====================================================================
# Stage 0: Bounding box
# =====================================================================
print(f"\n[Stage 0] Bounding box setup...")
t0 = time.time()

bbox = bbox_to_utm(LAT1, LON1, LAT2, LON2)
width_m = bbox["width_m"]
height_m = bbox["height_m"]
area_km2 = bbox["area_km2"]
scale = compute_scale(width_m, height_m)
south, west, north, east = bbox["wgs84_bbox"]

print(f"  Bounding box: ({LAT1}, {LON1}) -> ({LAT2}, {LON2})")
print(f"  Width: {width_m:.0f}m, Height: {height_m:.0f}m")
print(f"  Area: {area_km2:.1f} km2")
print(f"  Scale: {scale:.6f} mm/m")
print(f"  Time: {time.time() - t0:.1f}s")

# =====================================================================
# Stage 1: Elevation
# =====================================================================
print(f"\n[Stage 1] Fetching elevation data...")
t1 = time.time()

elevation_grid = fetch_elevation_grid(south, west, north, east, resolution=512)
if elevation_grid is None:
    print("  ERROR: Elevation data fetch failed!")
    sys.exit(1)

print(f"  Grid: {elevation_grid.shape}, Range: {elevation_grid.min():.1f} - {elevation_grid.max():.1f}m")
print(f"  Time: {time.time() - t1:.1f}s")

# =====================================================================
# Stage 2: Water (for holes)
# =====================================================================
print(f"\n[Stage 2] Fetching water data (for holes)...")
t2 = time.time()

water_gdf = fetch_water(south, west, north, east)
water_gdf = project_geodataframe(water_gdf, bbox["utm_crs"], bbox["origin"], clip_bbox=bbox["utm_bbox"])

def estimate_water_area(geom, row):
    if geom.geom_type in ['Polygon', 'MultiPolygon']:
        return geom.area
    elif geom.geom_type in ['LineString', 'MultiLineString']:
        waterway_type = row.get('waterway', 'river')
        width = WATERWAY_WIDTHS.get(waterway_type, 60)
        return geom.length * width
    return 0

water_gdf['est_area'] = water_gdf.apply(lambda r: estimate_water_area(r.geometry, r), axis=1)

# Keep top 500 water features
MAX_WATER = 500
if len(water_gdf) > MAX_WATER:
    water_gdf = water_gdf.nlargest(MAX_WATER, 'est_area')

print(f"  Water features: {len(water_gdf)}")
print(f"  Geometry types: {water_gdf.geometry.type.value_counts().to_dict()}")

if 'name' in water_gdf.columns:
    named = water_gdf['name'].dropna().unique()
    print(f"  Named features ({len(named)}): {list(named[:10])}")

print(f"  Time: {time.time() - t2:.1f}s")

# =====================================================================
# Stage 3: Roads (for bridge fusion)
# =====================================================================
print(f"\n[Stage 3] Fetching road data (for bridge fusion)...")
t3 = time.time()

roads_gdf = fetch_roads(south, west, north, east)
roads_gdf = project_geodataframe(roads_gdf, bbox["utm_crs"], bbox["origin"], clip_bbox=bbox["utm_bbox"])

# Keep main road types only
main_roads = ['motorway', 'trunk', 'primary', 'secondary', 'tertiary',
              'motorway_link', 'trunk_link', 'primary_link', 'secondary_link', 'tertiary_link']
if 'highway' in roads_gdf.columns:
    roads_gdf = roads_gdf[roads_gdf['highway'].isin(main_roads)]

# Limit to 5000 features
if len(roads_gdf) > 5000:
    roads_gdf = roads_gdf.sample(5000, random_state=42)

enable_roads = roads_gdf is not None and len(roads_gdf) > 0

print(f"  Road features (main roads only): {len(roads_gdf) if roads_gdf is not None else 0}")
print(f"  Bridge fusion enabled: {enable_roads}")

if 'highway' in roads_gdf.columns and enable_roads:
    print(f"  Highway types: {roads_gdf['highway'].value_counts().head(5).to_dict()}")

print(f"  Time: {time.time() - t3:.1f}s")

# =====================================================================
# Stage 4: Build terrain with water holes
# =====================================================================
print(f"\n{'=' * 70}")
print("  Stage 4: Building Terrain with Water Holes")
print("=" * 70)
t_build = time.time()

result = build_terrain_with_water_holes_manifold(
    elevation_grid=elevation_grid,
    width_m=width_m,
    height_m=height_m,
    area_km2=area_km2,
    scale=scale,
    water_gdf=water_gdf,
    roads_gdf=roads_gdf,
    enable_roads_fusion=enable_roads,
    bridges_only=True,  # Only bridge segments over water
)

terrain_final = result["mesh"]
build_stats = result["stats"]
validation = result["validation"]

print(f"\n  Build statistics:")
print(f"    Terrain faces: {build_stats['terrain_faces']}")
print(f"    Roads faces: {build_stats['roads_faces']}")
print(f"    Water columns: {build_stats['water_columns']}")
print(f"    Boolean operations: {build_stats['boolean_ops']}")
print(f"    Final faces: {build_stats['final_faces']}")

print(f"\n  Mesh validation:")
print(f"    Vertices: {len(terrain_final.vertices)}")
print(f"    Faces: {len(terrain_final.faces)}")
print(f"    Watertight: {terrain_final.is_watertight}")
print(f"    Volume: {validation['volume']:.2f} mm3")
print(f"    Z range: {validation['bounds'][0][2]:.2f} -> {validation['bounds'][1][2]:.2f} mm")

print(f"\n  Build time: {time.time() - t_build:.1f}s")

# =====================================================================
# Export
# =====================================================================
print(f"\n{'=' * 70}")
print("  Export 3MF")
print("=" * 70)

output_path = os.path.join(OUTPUT_DIR, f"obj4_terrain_{CITY_NAME}.3mf")
meshes = {
    "terrain_surface": terrain_final,
    "terrain_walls": None,
    "buildings": None,
    "roads": None,
    "water": None,
    "vegetation": None,
}
export_deepseek_3mf(meshes, output_path, extruders=4)

file_size = os.path.getsize(output_path) / (1024 * 1024)
print(f"  Exported: {output_path}")
print(f"  File size: {file_size:.2f} MB")

# =====================================================================
# Summary
# =====================================================================
print(f"\n{'=' * 70}")
print(f"  Hangzhou West Lake Object 4 Generation Complete")
print(f"{'=' * 70}")
print(f"\nOutput file: {output_path} ({file_size:.2f} MB)")
print(f"{'=' * 70}\n")
