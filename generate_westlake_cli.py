"""使用纯 osmium CLI 管线生成西湖 3MF 模型

完整管线演示：
PBF → osmium extract → osmium tags-filter → osmium export → GeoJSON → GeoDataFrame → 3MF

对比 generate_hangzhou_obj1.py：
- 原方案：Python pyosmium（~127秒）
- 本方案：纯 osmium CLI 管线（~5-10秒，需安装 osmium-tool）

前置要求：
conda install -c conda-forge osmium-tool
"""

import os
import sys
import time
import numpy as np

_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm, project_geodataframe
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osmium_cli_fetcher import fetch_from_cli, get_cli_fetcher
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.elevation import fetch_elevation_grid
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import build_deepseek_terrain
from _TEXTURE_STYLE_OF_DEEPSEEK.object4_terrain_with_holes import build_terrain_with_water_holes_manifold
from _TEXTURE_STYLE_OF_DEEPSEEK.buildings import build_deepseek_buildings
from _TEXTURE_STYLE_OF_DEEPSEEK.roads import build_deepseek_roads
from _TEXTURE_STYLE_OF_DEEPSEEK.water import build_deepseek_water
from _TEXTURE_STYLE_OF_DEEPSEEK.vegetation import build_deepseek_vegetation
from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf, split_terrain_mesh
from _TEXTURE_STYLE_OF_DEEPSEEK.config import compute_scale, WATERWAY_WIDTHS, TERRAIN_GRID, get_area_class

# PBF 文件
PBF_FILE = os.path.join(_project_root, 'pbf_cache', 'zhejiang-latest.osm.pbf')
if not os.path.exists(PBF_FILE):
    print(f"ERROR: PBF file not found: {PBF_FILE}")
    sys.exit(1)

# 西湖 25km 区域
LAT1, LON1 = 30.13, 120.01
LAT2, LON2 = 30.36, 120.29
CITY_NAME = "westlake_cli"
OUTPUT_DIR = "output/westlake_cli"

print("=" * 70)
print("  Pure Osmium CLI Pipeline: extract → tags-filter → export → 3MF")
print("=" * 70)

t_start = time.time()

os.makedirs(OUTPUT_DIR, exist_ok=True)

# =====================================================================
# Stage 0: 检查 CLI 工具可用性
# =====================================================================
print("\n[Stage 0] Checking CLI tools...")
fetcher = get_cli_fetcher()
print(f"  osmium available: {fetcher.osmium_available}")

if not fetcher.osmium_available:
    print("\n  ERROR: osmium CLI not installed!")
    print("  Install: conda install -c conda-forge osmium-tool")
    sys.exit(1)
else:
    USE_CLI = True
    print("  Using pure osmium CLI pipeline (extract → tags-filter → export)")

# =====================================================================
# Stage 1: Bounding box
# =====================================================================
print(f"\n[Stage 1] Bounding box setup...")
t1 = time.time()

bbox = bbox_to_utm(LAT1, LON1, LAT2, LON2)
width_m = bbox["width_m"]
height_m = bbox["height_m"]
area_km2 = bbox["area_km2"]
area_class = get_area_class(area_km2)
resolution = TERRAIN_GRID.get(area_class, 512)
scale = compute_scale(width_m, height_m)
south, west, north, east = bbox["wgs84_bbox"]
utm_crs = bbox["utm_crs"]
origin = bbox["origin"]
utm_bbox = bbox["utm_bbox"]

bbox_x_min = utm_bbox[0] - origin[0]
bbox_y_min = utm_bbox[1] - origin[1]
bbox_x_max = utm_bbox[2] - origin[0]
bbox_y_max = utm_bbox[3] - origin[1]

print(f"  Area: {width_m:.0f}m × {height_m:.0f}m = {area_km2:.1f} km² ({area_class})")
print(f"  Scale: {scale:.6f} mm/m")
print(f"  Resolution: {resolution}x{resolution}")
print(f"  Time: {time.time() - t1:.1f}s")

# =====================================================================
# Stage 1b: Fetch elevation data (SRTM HGT tiles)
# =====================================================================
print(f"\n[Stage 1b] Fetching elevation data...")
t1b = time.time()

try:
    elevation_grid = fetch_elevation_grid(south, west, north, east, resolution)
    print(f"  Grid shape: {elevation_grid.shape}")
    print(f"  Elevation range: {elevation_grid.min():.1f}m to {elevation_grid.max():.1f}m")
    print(f"  Time: {time.time() - t1b:.1f}s")
except Exception as e:
    print(f"  WARNING: Elevation fetch failed: {e}")
    print(f"  Using flat terrain (0m elevation)")
    elevation_grid = np.zeros((resolution, resolution), dtype=np.float64)
    print(f"  Time: {time.time() - t1b:.1f}s")

# =====================================================================
# Stage 2: Fetch water data (CLI only)
# =====================================================================
print(f"\n[Stage 2] Fetching water data...")
t2 = time.time()

if USE_CLI:
    # === 纯 osmium CLI 方式 ===
    print("  Method: osmium extract → tags-filter → export")
    water_gdf = fetch_from_cli(
        tag_type='water',
        south=south, west=west, north=north, east=east,
        pbf_file=PBF_FILE
    )
    METHOD = "CLI"
else:
    print("  ERROR: CLI tools not available!")
    print("  This script requires osmium CLI to be installed.")
    print("  Install: conda install -c conda-forge osmium-tool")
    sys.exit(1)

water_fetch_time = time.time() - t2

if water_gdf is None or len(water_gdf) == 0:
    print("  ERROR: No water features found!")
    sys.exit(1)

print(f"  Features: {len(water_gdf)}")
print(f"  Geometry types: {water_gdf.geometry.type.value_counts().to_dict()}")
print(f"  Time: {water_fetch_time:.1f}s ({METHOD})")

# 投影到 UTM
water_gdf = project_geodataframe(water_gdf, utm_crs, origin, clip_bbox=utm_bbox)

# 计算面积并筛选
def estimate_water_area(geom, row):
    if geom.geom_type in ['Polygon', 'MultiPolygon']:
        return geom.area
    elif geom.geom_type in ['LineString', 'MultiLineString']:
        waterway_type = row.get('waterway', 'river')
        width = WATERWAY_WIDTHS.get(waterway_type, 60)
        return geom.length * width
    return 0

water_gdf['est_area'] = water_gdf.apply(lambda r: estimate_water_area(r.geometry, r), axis=1)

MAX_WATER = 500
if len(water_gdf) > MAX_WATER:
    water_gdf = water_gdf.nlargest(MAX_WATER, 'est_area')
    print(f"  Filtered to top {MAX_WATER} features")

# 显示命名水体
if 'name' in water_gdf.columns:
    named = water_gdf['name'].dropna().unique()
    print(f"  Named features ({len(named)}): {list(named[:10])}")

# =====================================================================
# Stage 3: Fetch vegetation data (CLI only)
# =====================================================================
print(f"\n[Stage 3] Fetching vegetation data...")
t3 = time.time()

vegetation_gdf = fetch_from_cli(
    tag_type='vegetation',
    south=south, west=west, north=north, east=east,
    pbf_file=PBF_FILE
)

veg_fetch_time = time.time() - t3

if vegetation_gdf is not None and len(vegetation_gdf) > 0:
    print(f"  Features: {len(vegetation_gdf)}")
    print(f"  Geometry types: {vegetation_gdf.geometry.type.value_counts().to_dict()}")
    print(f"  Time: {veg_fetch_time:.1f}s")
    
    # 投影到 UTM
    vegetation_gdf = project_geodataframe(vegetation_gdf, utm_crs, origin, clip_bbox=utm_bbox)
    
    # 显示命名植被
    if 'name' in vegetation_gdf.columns:
        named_veg = vegetation_gdf['name'].dropna().unique()
        print(f"  Named features ({len(named_veg)}): {list(named_veg[:10])}")
else:
    print("  No vegetation features found")
    vegetation_gdf = None

# =====================================================================
# Stage 3b: Fetch buildings data (CLI only)
# =====================================================================
print(f"\n[Stage 3b] Fetching buildings data...")
t3b = time.time()

buildings_gdf = fetch_from_cli(
    tag_type='building',
    south=south, west=west, north=north, east=east,
    pbf_file=PBF_FILE
)

if buildings_gdf is not None and len(buildings_gdf) > 0:
    print(f"  Features: {len(buildings_gdf)}")
    print(f"  Geometry types: {buildings_gdf.geometry.type.value_counts().to_dict()}")
    print(f"  Time: {time.time() - t3b:.1f}s")
    
    # 投影到 UTM
    buildings_gdf = project_geodataframe(buildings_gdf, utm_crs, origin, clip_bbox=utm_bbox)
    
    # 显示命名建筑
    if 'name' in buildings_gdf.columns:
        named_bld = buildings_gdf['name'].dropna().unique()
        print(f"  Named features ({len(named_bld)}): {list(named_bld[:10])}")
else:
    print("  No building features found")
    buildings_gdf = None

# =====================================================================
# Stage 3c: Fetch roads data (CLI only)
# =====================================================================
print(f"\n[Stage 3c] Fetching roads data...")
t3c = time.time()

roads_gdf = fetch_from_cli(
    tag_type='road',
    south=south, west=west, north=north, east=east,
    pbf_file=PBF_FILE
)

if roads_gdf is not None and len(roads_gdf) > 0:
    print(f"  Features: {len(roads_gdf)}")
    print(f"  Geometry types: {roads_gdf.geometry.type.value_counts().to_dict()}")
    print(f"  Time: {time.time() - t3c:.1f}s")
    
    # 投影到 UTM
    roads_gdf = project_geodataframe(roads_gdf, utm_crs, origin, clip_bbox=utm_bbox)
else:
    print("  No road features found")
    roads_gdf = None

# =====================================================================
# Stage 4: Build terrain mesh (obj_4: terrain + water hollow)
# =====================================================================
print(f"\n[Stage 4] Building terrain mesh (obj_4: terrain + water hollow)...")
t4 = time.time()

if water_gdf is not None and len(water_gdf) > 0:
    try:
        terrain_result = build_terrain_with_water_holes_manifold(
            elevation_grid, width_m, height_m, area_km2, scale,
            water_gdf,
            roads_gdf=roads_gdf if roads_gdf is not None and len(roads_gdf) > 0 else None,
            enable_roads_fusion=False,
        )
        terrain_solid = terrain_result["mesh"]
        print(f"  Terrain (with water holes) faces: {len(terrain_solid.faces):,}")
        print(f"  Watertight: {terrain_solid.is_watertight}")
    except Exception as e:
        print(f"  WARNING: Terrain with water holes failed: {e}")
        print(f"  Falling back to basic terrain...")
        terrain_solid = build_deepseek_terrain(
            elevation_grid, width_m, height_m, area_km2, scale, water_gdf
        )
else:
    terrain_solid = build_deepseek_terrain(
        elevation_grid, width_m, height_m, area_km2, scale, water_gdf
    )
    print(f"  Terrain (no water data) faces: {len(terrain_solid.faces):,}")

print(f"  Time: {time.time() - t4:.1f}s")

# =====================================================================
# Stage 5: Build buildings
# =====================================================================
print(f"\n[Stage 5] Building buildings...")
t5 = time.time()

buildings_mesh = None
if buildings_gdf is not None and len(buildings_gdf) > 0:
    try:
        buildings_mesh = build_deepseek_buildings(buildings_gdf, terrain_solid, area_km2, scale)
        if buildings_mesh is not None:
            print(f"  Building faces: {len(buildings_mesh.faces):,}")
        else:
            print(f"  No buildings generated (all filtered out)")
    except Exception as e:
        print(f"  Buildings processing failed (skipping): {e}")
        buildings_mesh = None
else:
    print(f"  No building data available")
print(f"  Time: {time.time() - t5:.1f}s")

# =====================================================================
# Stage 6: Build roads
# =====================================================================
print(f"\n[Stage 6] Building roads...")
t6 = time.time()

roads_mesh = None
if roads_gdf is not None and len(roads_gdf) > 0:
    try:
        roads_mesh = build_deepseek_roads(roads_gdf, terrain_solid, area_km2, scale)
        if roads_mesh is not None:
            print(f"  Road faces: {len(roads_mesh.faces):,}")
        else:
            print(f"  No roads generated")
    except Exception as e:
        print(f"  Roads processing failed (skipping): {e}")
        roads_mesh = None
else:
    print(f"  No road data available")
print(f"  Time: {time.time() - t6:.1f}s")

# =====================================================================
# Stage 7: Build water (base plate + water relief)
# =====================================================================
print(f"\n[Stage 7] Building water plate (base + water relief)...")
t7 = time.time()

water_mesh = None
if water_gdf is not None and len(water_gdf) > 0:
    try:
        water_mesh = build_deepseek_water(
            water_gdf, bbox_x_min, bbox_y_min, bbox_x_max, bbox_y_max, scale
        )
        if water_mesh is not None:
            print(f"  Water faces: {len(water_mesh.faces):,}")
        else:
            print(f"  No water features generated")
    except Exception as e:
        print(f"  Water processing failed (skipping): {e}")
else:
    print(f"  No water data available")
print(f"  Time: {time.time() - t7:.1f}s")

# =====================================================================
# Stage 8: Build vegetation
# =====================================================================
print(f"\n[Stage 8] Building vegetation features...")
t8 = time.time()

vegetation_mesh = None
if vegetation_gdf is not None and len(vegetation_gdf) > 0:
    try:
        vegetation_mesh = build_deepseek_vegetation(vegetation_gdf, terrain_solid, scale)
        if vegetation_mesh is not None:
            print(f"  Vegetation faces: {len(vegetation_mesh.faces):,}")
        else:
            print(f"  No vegetation features generated")
    except Exception as e:
        print(f"  Vegetation processing failed (skipping): {e}")
else:
    print(f"  No vegetation data available")
print(f"  Time: {time.time() - t8:.1f}s")

# =====================================================================
# Stage 9: Split terrain into surface + walls and export 3MF
# =====================================================================
print(f"\n[Stage 9] Preparing and exporting 3MF...")
t9 = time.time()

terrain_parts = split_terrain_mesh(terrain_solid)
print(f"  Terrain surface faces: {len(terrain_parts['terrain_surface'].faces):,}")
print(f"  Terrain walls faces: {len(terrain_parts['terrain_walls'].faces):,}")

meshes = {
    'terrain_surface': terrain_parts['terrain_surface'],
    'terrain_walls': terrain_parts['terrain_walls'],
    'buildings': buildings_mesh,
    'roads': roads_mesh,
    'water': water_mesh,
    'vegetation': vegetation_mesh,
}

print(f"\n  Mesh stats:")
if terrain_parts['terrain_surface'] is not None:
    ts = terrain_parts['terrain_surface']
    print(f"    Terrain Surface - Vertices: {len(ts.vertices)}, Faces: {len(ts.faces)}, Watertight: {ts.is_watertight}")
    tb = ts.bounds
    print(f"    Terrain Surface - Bounds: X[{tb[0][0]:.1f}, {tb[1][0]:.1f}] Y[{tb[0][1]:.1f}, {tb[1][1]:.1f}] Z[{tb[0][2]:.1f}, {tb[1][2]:.1f}] mm")
if terrain_parts['terrain_walls'] is not None:
    tw = terrain_parts['terrain_walls']
    print(f"    Terrain Walls - Vertices: {len(tw.vertices)}, Faces: {len(tw.faces)}, Watertight: {tw.is_watertight}")
if buildings_mesh is not None:
    print(f"    Buildings - Vertices: {len(buildings_mesh.vertices)}, Faces: {len(buildings_mesh.faces)}")
if roads_mesh is not None:
    print(f"    Roads - Vertices: {len(roads_mesh.vertices)}, Faces: {len(roads_mesh.faces)}")
if water_mesh is not None:
    wb = water_mesh.bounds
    print(f"    Water - Vertices: {len(water_mesh.vertices)}, Faces: {len(water_mesh.faces)}, Watertight: {water_mesh.is_watertight}")
    print(f"    Water - Bounds: X[{wb[0][0]:.1f}, {wb[1][0]:.1f}] Y[{wb[0][1]:.1f}, {wb[1][1]:.1f}] Z[{wb[0][2]:.1f}, {wb[1][2]:.1f}] mm")
if vegetation_mesh is not None:
    vb = vegetation_mesh.bounds
    print(f"    Vegetation - Vertices: {len(vegetation_mesh.vertices)}, Faces: {len(vegetation_mesh.faces)}")
    print(f"    Vegetation - is_watertight: {vegetation_mesh.is_watertight}")
    print(f"    Vegetation - Bounds: X[{vb[0][0]:.1f}, {vb[1][0]:.1f}] Y[{vb[0][1]:.1f}, {vb[1][1]:.1f}] Z[{vb[0][2]:.1f}, {vb[1][2]:.1f}] mm")
    print(f"    Vegetation - Size: {vb[1][0]-vb[0][0]:.1f} x {vb[1][1]-vb[0][1]:.1f} x {vb[1][2]-vb[0][2]:.1f} mm")

# Export 3MF
output_path = os.path.join(OUTPUT_DIR, f"full_{CITY_NAME}.3mf")
export_deepseek_3mf(meshes, output_path, extruders=4)

file_size = os.path.getsize(output_path) / (1024 * 1024)
print(f"\n  Exported: {output_path}")
print(f"  File size: {file_size:.2f} MB")

build_time = time.time() - t4
print(f"  Build time: {build_time:.1f}s")

# =====================================================================
# Summary
# =====================================================================
total_time = time.time() - t_start
print(f"\n{'=' * 70}")
print(f"  Pipeline Summary")
print(f"{'=' * 70}")
print(f"  Method: {METHOD}")
print(f"  Area: {area_km2:.1f} km², Scale: {scale:.6f} mm/m")
print(f"  Terrain faces: {len(terrain_solid.faces):,}")
print(f"  Water faces: {len(water_mesh.faces):,}" if water_mesh is not None else "  Water: None")
print(f"  Vegetation faces: {len(vegetation_mesh.faces):,}" if vegetation_mesh is not None else "  Vegetation: None")
print(f"  Buildings faces: {len(buildings_mesh.faces):,}" if buildings_mesh is not None else "  Buildings: None")
print(f"  Roads faces: {len(roads_mesh.faces):,}" if roads_mesh is not None else "  Roads: None")
print(f"  Output: {output_path} ({file_size:.2f} MB)")
print(f"  Total time: {total_time:.1f}s")
print(f"{'=' * 70}\n")