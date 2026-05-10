"""Generate Object 1 (Water Plate) for Hangzhou West Lake - 25km × 25km."""

import os
import sys
import time

_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm, project_geodataframe
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import fetch_water
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.pbf_reader import fetch_from_pbf
from _TEXTURE_STYLE_OF_DEEPSEEK.water import build_deepseek_water
from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf
from _TEXTURE_STYLE_OF_DEEPSEEK.config import compute_scale, WATERWAY_WIDTHS

# Set PBF file path
PBF_FILE = os.path.join(_project_root, 'pbf_cache', 'zhejiang-latest.osm.pbf')
if not os.path.exists(PBF_FILE):
    print(f"ERROR: PBF file not found: {PBF_FILE}")
    print("Download it first: python tools/manage_pbf.py download zhejiang")
    sys.exit(1)
print(f"Using PBF file: {PBF_FILE}")

# Hangzhou West Lake area (25km × 25km)
LAT1, LON1 = 30.13, 120.01   # south-west
LAT2, LON2 = 30.36, 120.29   # north-east
CITY_NAME = "hangzhou_west_lake"
OUTPUT_DIR = "output/hangzhou_west_lake"

print("=" * 70)
print("  Hangzhou West Lake Object 1: Water Plate (25km × 25km)")
print("=" * 70)

os.makedirs(OUTPUT_DIR, exist_ok=True)

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
utm_crs = bbox["utm_crs"]
origin = bbox["origin"]
utm_bbox = bbox["utm_bbox"]

bbox_x_min = utm_bbox[0] - origin[0]
bbox_y_min = utm_bbox[1] - origin[1]
bbox_x_max = utm_bbox[2] - origin[0]
bbox_y_max = utm_bbox[3] - origin[1]

print(f"  Bounding box: ({LAT1}, {LON1}) -> ({LAT2}, {LON2})")
print(f"  Width: {width_m:.0f}m, Height: {height_m:.0f}m")
print(f"  Area: {area_km2:.1f} km2")
print(f"  Scale: {scale:.6f} mm/m")
print(f"  Time: {time.time() - t0:.1f}s")

# =====================================================================
# Stage 1: Water features (from PBF)
# =====================================================================
print(f"\n[Stage 1] Fetching water data (from PBF with relation support)...")
t1 = time.time()

water_gdf = fetch_from_pbf(PBF_FILE, 'water', south, west, north, east)

if water_gdf is None or len(water_gdf) == 0:
    print("  ERROR: No water features found!")
    sys.exit(1)

water_gdf = project_geodataframe(water_gdf, utm_crs, origin, clip_bbox=utm_bbox)

# Estimate area and keep top features
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
    print(f"  Named features ({len(named)}): {list(named[:15])}")

print(f"  Time: {time.time() - t1:.1f}s")

# =====================================================================
# Object 1: Water Plate
# =====================================================================
print(f"\n{'=' * 70}")
print("  Building Object 1: Water Plate")
print("=" * 70)
t_obj = time.time()

water_mesh = build_deepseek_water(
    water_gdf, bbox_x_min, bbox_y_min, bbox_x_max, bbox_y_max, scale
)

if water_mesh is not None:
    print(f"\n  Mesh stats:")
    print(f"    Vertices: {len(water_mesh.vertices)}")
    print(f"    Faces: {len(water_mesh.faces)}")
    print(f"    Watertight: {water_mesh.is_watertight}")
    print(f"    Volume: {water_mesh.volume:.2f} mm3")
    
    # Export 3MF
    output_path = os.path.join(OUTPUT_DIR, f"obj1_water_plate_{CITY_NAME}.3mf")
    meshes = {
        'terrain_surface': None,
        'terrain_walls': None,
        'buildings': None,
        'roads': None,
        'water': water_mesh,
        'vegetation': None,
    }
    export_deepseek_3mf(meshes, output_path, extruders=4)
    
    file_size = os.path.getsize(output_path) / (1024 * 1024)
    print(f"\n  Exported: {output_path}")
    print(f"  File size: {file_size:.2f} MB")
else:
    print("  ERROR: Water mesh generation failed!")
    sys.exit(1)

print(f"\n  Time: {time.time() - t_obj:.1f}s")

# =====================================================================
# Summary
# =====================================================================
print(f"\n{'=' * 70}")
print(f"  Hangzhou West Lake Object 1 Generation Complete")
print(f"{'=' * 70}")
print(f"\nOutput file: {output_path} ({file_size:.2f} MB)")
print(f"{'=' * 70}\n")
