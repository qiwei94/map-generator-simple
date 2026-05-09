"""Generate Object 4 (Terrain with Water Holes) for Chicago - with DETAILED logging."""

import os
import sys
import time
import logging
import numpy as np
import trimesh
import json

_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

# =====================================================================
# Logging Configuration
# =====================================================================
OUTPUT_DIR = "output/chicago"
os.makedirs(OUTPUT_DIR, exist_ok=True)

LOG_FILE = os.path.join(OUTPUT_DIR, "obj4_detailed_log.json")
OSM_LOG_FILE = os.path.join(OUTPUT_DIR, "osm_fetch.log")

# Configure logging for all modules (including osm.py, elevation.py, etc.)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(OSM_LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()  # Also output to console
    ]
)

# Set specific log levels for noisy modules
logging.getLogger("osmnx").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm, project_geodataframe
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import fetch_water, fetch_roads
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.elevation import fetch_elevation_grid
from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf
from _TEXTURE_STYLE_OF_DEEPSEEK.config import compute_scale, WATERWAY_WIDTHS

# Chicago full area
LAT1, LON1 = 41.80, -87.75
LAT2, LON2 = 42.00, -87.55
CITY_NAME = "chicago"

print("=" * 70)
print("  Chicago Object 4: Terrain + Water Holes (DETAILED LOG)")
print("=" * 70)

log_data = {"steps": [], "errors": []}

def log_step(step_name, details):
    """Log a step with details."""
    log_data["steps"].append({"step": step_name, "details": details})
    print(f"  [LOG] {step_name}: {json.dumps(details, ensure_ascii=False)[:200]}")

# =====================================================================
# Stage 0: Bounding box
# =====================================================================
print(f"\n[Stage 0] Bounding box setup...")
bbox = bbox_to_utm(LAT1, LON1, LAT2, LON2)
width_m = bbox["width_m"]
height_m = bbox["height_m"]
area_km2 = bbox["area_km2"]
scale = compute_scale(width_m, height_m)
south, west, north, east = bbox["wgs84_bbox"]

log_step("bbox", {
    "south": south, "west": west, "north": north, "east": east,
    "width_m": width_m, "height_m": height_m, "area_km2": area_km2, "scale": scale
})

print(f"  Area: {area_km2:.1f} km2, Scale: {scale:.6f} mm/m")

# =====================================================================
# Stage 1: Elevation
# =====================================================================
print(f"\n[Stage 1] Fetching elevation data...")
t1 = time.time()

elevation_grid = fetch_elevation_grid(south, west, north, east, resolution=512)
if elevation_grid is None:
    print("ERROR: Elevation data fetch failed!")
    sys.exit(1)

log_step("elevation", {
    "shape": list(elevation_grid.shape),
    "min": float(elevation_grid.min()),
    "max": float(elevation_grid.max()),
    "fetch_time_s": round(time.time() - t1, 2)
})
print(f"  Grid: {elevation_grid.shape}, Range: {elevation_grid.min():.1f} - {elevation_grid.max():.1f}m")

# =====================================================================
# Stage 2: Water
# =====================================================================
print(f"\n[Stage 2] Fetching water data...")
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
water_gdf = water_gdf.nlargest(500, 'est_area')

log_step("water", {
    "total_features": len(water_gdf),
    "geometry_types": water_gdf.geometry.type.value_counts().to_dict(),
    "fetch_time_s": round(time.time() - t2, 2)
})
print(f"  Water features: {len(water_gdf)}")

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

log_step("roads", {
    "total_fetched": len(roads_gdf) if roads_gdf is not None else 0,
    "filtered_to_main_roads": len(roads_gdf) if roads_gdf is not None else 0,
    "enable_roads_fusion": enable_roads,
    "bridges_only": enable_roads,
    "fetch_time_s": round(time.time() - t3, 2)
})
print(f"  Road features (main roads only): {len(roads_gdf) if roads_gdf is not None else 0}")
print(f"  Bridge fusion enabled: {enable_roads}")
if 'highway' in roads_gdf.columns and enable_roads:
    print(f"  Highway types: {roads_gdf['highway'].value_counts().head(5).to_dict()}")
print(f"  Time: {time.time() - t3:.1f}s")

# =====================================================================
# Step 1-4: Unified build with bridge fusion + water holes
# =====================================================================
print(f"\n{'=' * 70}")
print("  Steps 1-4: Unified Terrain Build (Bridge Fusion + Water Holes)")
print("=" * 70)
t_build = time.time()

from _TEXTURE_STYLE_OF_DEEPSEEK.object4_terrain_with_holes import build_terrain_with_water_holes_manifold

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

# Merge stats into log
log_step("build_result", {
    "terrain_faces": build_stats["terrain_faces"],
    "roads_faces": build_stats["roads_faces"],
    "water_columns": build_stats["water_columns"],
    "final_faces": build_stats["final_faces"],
    "boolean_ops": build_stats["boolean_ops"],
    "validation": validation,
    "time_s": round(time.time() - t_build, 2)
})

print(f"\n  Boolean operations performed: {build_stats['boolean_ops']}")
print(f"  Final mesh: {len(terrain_final.vertices)} vertices, {len(terrain_final.faces)} faces")
print(f"  Watertight: {terrain_final.is_watertight}")
print(f"  Volume: {validation['volume']:.2f} mm3")
print(f"  Z range: {validation['bounds'][0][2]:.2f} -> {validation['bounds'][1][2]:.2f} mm")
print(f"  Total build time: {time.time() - t_build:.1f}s")

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
print(f"  Exported: {output_path} ({file_size:.2f} MB)")

log_step("export", {"path": output_path, "size_mb": round(file_size, 2)})

# Save detailed log
with open(LOG_FILE, 'w', encoding='utf-8') as f:
    json.dump(log_data, f, indent=2, ensure_ascii=False, default=str)
print(f"\n  Detailed log saved to: {LOG_FILE}")

print(f"\n{'=' * 70}")
print(f"  Chicago Object 4 Generation Complete")
print(f"{'=' * 70}\n")
