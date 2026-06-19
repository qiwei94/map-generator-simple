"""Generate PNG preview for Chicago using preprocess layers (fast 2D render).
Reuses the existing render_png pipeline which produces block_base + vegetation + water top-down view."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time

# Chicago bbox
LAT1, LON1 = 41.77, -87.77
LAT2, LON2 = 41.99, -87.47
PBF_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pbf_cache", "illinois-latest.osm.pbf")
CITY_NAME = "chicago"
OUTPUT_DIR = "output/chicago_cli"

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm, project_geodataframe
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osmium_cli_fetcher import fetch_from_cli, sample_building_density
from _TEXTURE_STYLE_OF_DEEPSEEK._layer_preprocess import preprocess_layers
from _TEXTURE_STYLE_OF_DEEPSEEK.render_png import render_from_layers
from _TEXTURE_STYLE_OF_DEEPSEEK.config import compute_scale, BUILDING_V2_HOTSPOT_RELAX

t0 = time.time()

print(f"[Stage 0] Chicago bbox: ({LAT1}, {LON1}) → ({LAT2}, {LON2})")
bbox = bbox_to_utm(LAT1, LON1, LAT2, LON2)
width_m = bbox["width_m"]
height_m = bbox["height_m"]
area_km2 = bbox["area_km2"]
scale = compute_scale(width_m, height_m)
south, west, north, east = bbox["wgs84_bbox"]
utm_crs = bbox["utm_crs"]
origin = bbox["origin"]
utm_bbox = (origin[0] - width_m/2, origin[1] - height_m/2,
            origin[0] + width_m/2, origin[1] + height_m/2)
bbox_local = (0, 0, width_m, height_m)
ctx = {"bbox_utm": bbox, "origin": origin, "width_m": width_m, "height_m": height_m,
       "utm_crs": utm_crs, "bbox_wgs84": (south, west, north, east),
       "area_km2": area_km2, "scale": scale}

print(f"  Area: {width_m:.0f}m x {height_m:.0f}m = {area_km2:.1f} km2")

# Fetch buildings
print(f"\n[Stage 1] Fetching buildings...")
buildings_gdf = fetch_from_cli('building', south, west, north, east, PBF_FILE)
if buildings_gdf is not None and len(buildings_gdf) > 0:
    print(f"  Buildings: {len(buildings_gdf)}")
    buildings_gdf = project_geodataframe(buildings_gdf, utm_crs, origin, clip_bbox=utm_bbox)
else:
    buildings_gdf = None

# Fetch water
print(f"\n[Stage 2] Fetching water...")
water_gdf = fetch_from_cli('water', south, west, north, east, PBF_FILE)
if water_gdf is not None and len(water_gdf) > 0:
    print(f"  Water: {len(water_gdf)}")
    water_gdf = project_geodataframe(water_gdf, utm_crs, origin, clip_bbox=utm_bbox)
else:
    water_gdf = None

# Fetch roads
print(f"\n[Stage 3] Fetching roads...")
roads_gdf = fetch_from_cli('road', south, west, north, east, PBF_FILE)
if roads_gdf is not None and len(roads_gdf) > 0:
    print(f"  Roads: {len(roads_gdf)}")
    roads_gdf = project_geodataframe(roads_gdf, utm_crs, origin, clip_bbox=utm_bbox)
else:
    roads_gdf = None

# Fetch landuse
print(f"\n[Stage 4] Fetching landuse...")
landuse_gdf = fetch_from_cli('landuse', south, west, north, east, PBF_FILE)
if landuse_gdf is not None and len(landuse_gdf) > 0:
    landuse_gdf = project_geodataframe(landuse_gdf, utm_crs, origin, clip_bbox=utm_bbox)
else:
    landuse_gdf = None

# Fetch vegetation
print(f"\n[Stage 5] Fetching vegetation...")
vegetation_gdf = fetch_from_cli('vegetation', south, west, north, east, PBF_FILE)
if vegetation_gdf is not None and len(vegetation_gdf) > 0:
    vegetation_gdf = project_geodataframe(vegetation_gdf, utm_crs, origin, clip_bbox=utm_bbox)
else:
    vegetation_gdf = None

# Preprocess layers
print(f"\n[Stage 6] Preprocessing layers...")
layers = preprocess_layers(
    buildings_gdf=buildings_gdf, water_gdf=water_gdf, roads_gdf=roads_gdf,
    vegetation_gdf=vegetation_gdf, landuse_gdf=landuse_gdf,
    bbox_wgs84=(south, west, north, east),
    utm_crs=utm_crs, origin=origin, bbox_local=bbox_local,
    area_km2=area_km2, scale=scale,
    enable_hotspot=True, hotspot_relax=BUILDING_V2_HOTSPOT_RELAX,
)

# Render PNG
print(f"\n[Stage 7] Rendering PNG...")
output_path = os.path.join(OUTPUT_DIR, "chicago_preview.png")
render_from_layers(
    layers, ctx, output_path,
    city_name=CITY_NAME, annotate=False,
    water_gdf=water_gdf, landuse_gdf=landuse_gdf,
)

print(f"\nTotal time: {time.time() - t0:.1f}s")
print(f"Output: {output_path}")
