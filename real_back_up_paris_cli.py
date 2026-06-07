"""使用纯 osmium CLI 管线生成巴黎 3MF 模型

完整管线演示：
PBF → osmium extract → osmium tags-filter → osmium export → GeoJSON → GeoDataFrame → 3MF

对比 generate_hangzhou_obj1.py：
- 原方案：Python pyosmium（~127秒）
- 本方案：纯 osmium CLI 管线（~5-10秒，需安装 osmium-tool）

前置要求：
conda install -c conda-forge osmium-tool
"""

import argparse
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
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osmium_cli_fetcher import fetch_from_cli, get_cli_fetcher, sample_building_density
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.elevation import fetch_elevation_grid
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import build_deepseek_terrain
from _TEXTURE_STYLE_OF_DEEPSEEK.object4_terrain_with_holes import build_terrain_with_water_holes_manifold
from _TEXTURE_STYLE_OF_DEEPSEEK.buildings import build_deepseek_buildings, build_deepseek_buildings_v3
from _TEXTURE_STYLE_OF_DEEPSEEK.roads import build_deepseek_roads, build_deepseek_roads_v3
from _TEXTURE_STYLE_OF_DEEPSEEK.water import build_deepseek_water, build_deepseek_water_v3
from _TEXTURE_STYLE_OF_DEEPSEEK.vegetation_exclusion import build_deepseek_vegetation_v3
from _TEXTURE_STYLE_OF_DEEPSEEK.block_base import build_deepseek_block_base_v3
from _TEXTURE_STYLE_OF_DEEPSEEK._layer_preprocess import preprocess_layers
from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf, split_terrain_mesh
from _TEXTURE_STYLE_OF_DEEPSEEK.config import compute_scale, WATERWAY_WIDTHS, TERRAIN_GRID, get_area_class, BUILDING_V2_HOTSPOT_RELAX

# ---------------------------------------------------------------------------
# CLI 参数
# ---------------------------------------------------------------------------
parser = argparse.ArgumentParser(
    description="使用 osmium CLI 管线生成巴黎 3MF 模型"
)
parser.add_argument(
    '--elevation-file',
    type=str,
    default=None,
    metavar='PATH',
    help='本地 DEM GeoTIFF 文件路径（可从 gscloud.cn 下载区域 DEM），跳过网络下载'
)
parser.add_argument(
    '--use-ndsm',
    action='store_true',
    default=False,
    help='使用 nDSM (Copernicus DSM - SRTM) 估算建筑高度，替代默认 10m fallback'
)
parser.add_argument(
    '--narrow-threshold',
    type=float,
    default=6.0,
    help='细长建筑 aspect ratio 阈值，超过此值触发高度惩罚（默认 6.0）'
)
parser.add_argument(
    '--narrow-penalty',
    type=float,
    default=0.5,
    help='细长建筑高度缩放系数（默认 0.5，即减半）'
)
cli_args = parser.parse_args()

# PBF 文件
PBF_FILE = os.path.join(_project_root, 'pbf_cache', 'ile-de-france-latest.osm.pbf')
if not os.path.exists(PBF_FILE):
    print(f"ERROR: PBF file not found: {PBF_FILE}")
    sys.exit(1)

# 巴黎 25km 区域
LAT1, LON1 = 48.76, 2.18
LAT2, LON2 = 48.98, 2.52
CITY_NAME = "paris_cli"
OUTPUT_DIR = "output/paris_cli"

# Sub-mesh on/off (一次性调参开关，改完跑即可)
ENABLE_VEGETATION = False
ENABLE_BLOCK_BASE = True
# MERGE_BLOCK_LAYERS 和 _bldg_tag 由 Stage 0c 自动检测决定
# 低密度城市（<150K 建筑）→ 完整三层：block_base + BO + BL
# 高密度城市（≥150K 建筑）→ MERGE 两层：block_base + BL（跳过 BO）

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
# Stage 0c: 自动检测建筑密度 — 决定过滤器和管线模式
# =====================================================================
print(f"\n[Stage 0c] Auto-detecting building density...")
t0c = time.time()
_density_info = sample_building_density(PBF_FILE, LAT1, LON1, LAT2, LON2)
_bldg_tag = 'building_landmarks' if _density_info['should_filter'] else 'building'
MERGE_BLOCK_LAYERS = _density_info['should_filter']  # 高密度才用 MERGE 模式
if MERGE_BLOCK_LAYERS:
    print(f"  高密度城市: building_landmarks + MERGE 两层模式")
else:
    print(f"  低密度城市: 全量 building + 完整三层模式 (block_base + BO + BL)")
print(f"  Time: {time.time() - t0c:.1f}s")

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
if cli_args.elevation_file:
    print(f"  Using local DEM: {cli_args.elevation_file}")
t1b = time.time()

try:
    elevation_grid = fetch_elevation_grid(
        south, west, north, east, resolution,
        elevation_file=cli_args.elevation_file,
    )
    print(f"  Grid shape: {elevation_grid.shape}")
    print(f"  Elevation range: {elevation_grid.min():.1f}m to {elevation_grid.max():.1f}m")
    print(f"  Time: {time.time() - t1b:.1f}s")
except Exception as e:
    print(f"  WARNING: Elevation fetch failed: {e}")
    print(f"  Using flat terrain (0m elevation)")
    elevation_grid = np.zeros((resolution, resolution), dtype=np.float64)
    print(f"  Time: {time.time() - t1b:.1f}s")

# =====================================================================
# Stage 1c: Compute nDSM grid (optional, for building height estimation)
# =====================================================================
if cli_args.use_ndsm:
    print(f"\n[Stage 1c] Computing nDSM grid (Copernicus DSM - SRTM DEM)...")
    t1c = time.time()
    from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.ndsm import compute_ndsm_grid
    ndsm_grid = compute_ndsm_grid(south, west, north, east, resolution, resolution)
    if ndsm_grid is not None:
        print(f"  nDSM shape: {ndsm_grid.shape}")
        print(f"  nDSM range: {ndsm_grid.min():.1f}m to {ndsm_grid.max():.1f}m")
        print(f"  Non-zero pixels: {(ndsm_grid > 0).sum()}/{ndsm_grid.size} "
              f"({(ndsm_grid > 0).sum()/ndsm_grid.size*100:.1f}%)")
        # Set module-level cache so building fetchers can use it
        from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import set_ndsm_grid
        set_ndsm_grid(ndsm_grid, south, west, north, east)
        print(f"  Time: {time.time() - t1c:.1f}s")
    else:
        print(f"  WARNING: nDSM computation failed (missing DEM tiles?)")
        print(f"  Building heights will use default fallback (10m)")
        print(f"  Time: {time.time() - t1c:.1f}s")

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

# _bldg_tag 已在 Stage 0c 自动设置
buildings_gdf = fetch_from_cli(
    tag_type=_bldg_tag,
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
# Stage 3d: Fetch landuse data (for block_base Z-texture classification)
# =====================================================================
print(f"\n[Stage 3d] Fetching landuse data...")
t3d = time.time()

landuse_gdf = fetch_from_cli(
    tag_type='landuse',
    south=south, west=west, north=north, east=east,
    pbf_file=PBF_FILE
)

if landuse_gdf is not None and len(landuse_gdf) > 0:
    print(f"  Features: {len(landuse_gdf)}")
    print(f"  Time: {time.time() - t3d:.1f}s")
    landuse_gdf = project_geodataframe(landuse_gdf, utm_crs, origin, clip_bbox=utm_bbox)
else:
    print("  No landuse features found")
    landuse_gdf = None

# =====================================================================
# Stage 4: Build terrain mesh (obj_4: terrain + water hollow)
# =====================================================================
print(f"\n[Stage 4] Building terrain mesh (obj_4: terrain + water hollow)...")
t4 = time.time()

if water_gdf is not None and len(water_gdf) > 0:
    try:
        has_roads = roads_gdf is not None and len(roads_gdf) > 0
        terrain_result = build_terrain_with_water_holes_manifold(
            elevation_grid, width_m, height_m, area_km2, scale,
            water_gdf,
            roads_gdf=roads_gdf if has_roads else None,
            enable_roads_fusion=has_roads,
            bridges_only=True,
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
# Stage 4.5: 5 步预处理（geometry 减法 + 精度过滤）
# =====================================================================
print(f"\n[Stage 4.5] Preprocessing layers (subtraction + precision filter)...")
t45 = time.time()

bbox_local = (bbox_x_min, bbox_y_min, bbox_x_max, bbox_y_max)
layers = preprocess_layers(
    buildings_gdf=buildings_gdf,
    roads_gdf=roads_gdf,
    water_gdf=water_gdf,
    vegetation_gdf=vegetation_gdf,
    bbox_local=bbox_local,
    scale=scale,
    enable_hotspot=True,
    hotspot_relax=BUILDING_V2_HOTSPOT_RELAX,
    area_km2=area_km2,
    landuse_gdf=landuse_gdf,
    narrow_threshold=cli_args.narrow_threshold,
    narrow_penalty=cli_args.narrow_penalty,
    bbox_wgs84=(south, west, north, east),
    utm_crs=utm_crs,
    origin=origin,
)
print(f"  {layers.summary()}")
print(f"  Time: {time.time() - t45:.1f}s")

# =====================================================================
# Stage 5: Build buildings (v3 — preprocessed polygons)
# =====================================================================
print(f"\n[Stage 5] Building buildings (v3)...")
t5 = time.time()

buildings_mesh = None
landmarks_mesh = None
terrain_solid_no_buildings = terrain_solid
if MERGE_BLOCK_LAYERS:
    # 2-layer 模式：BO 不单独建 mesh，合入 block_base 统一出
    print(f"  MERGE_BLOCK_LAYERS=True: BO 将合入 block_base，此处只建 landmarks")
    if layers.BL:
        try:
            bldg_result = build_deepseek_buildings_v3(
                layers.BL, [], terrain_solid, scale,
                bbox_local=bbox_local)
            if isinstance(bldg_result, dict):
                landmarks_mesh = bldg_result.get("landmarks")
                n_lm = len(landmarks_mesh.faces) if landmarks_mesh is not None else 0
                print(f"  Landmarks mesh: {n_lm:,} faces (E5 暖砂石)")
            else:
                print(f"  No landmarks generated")
        except Exception as e:
            print(f"  Landmarks processing failed (skipping): {e}")
elif layers.BL or layers.BO:
    try:
        bldg_result = build_deepseek_buildings_v3(
            layers.BL, layers.BO, terrain_solid, scale,
            bbox_local=bbox_local)
        if isinstance(bldg_result, dict):
            landmarks_mesh = bldg_result.get("landmarks")
            buildings_mesh = bldg_result.get("buildings")
            n_lm = len(landmarks_mesh.faces) if landmarks_mesh is not None else 0
            n_amb = len(buildings_mesh.faces) if buildings_mesh is not None else 0
            print(f"  Landmarks mesh: {n_lm:,} faces (E5 暖砂石)")
            print(f"  Buildings mesh: {n_amb:,} faces (E1 灰，街区填充)")
        else:
            print(f"  No buildings generated (all filtered out)")
    except Exception as e:
        print(f"  Buildings processing failed (skipping): {e}")
print(f"  Time: {time.time() - t5:.1f}s")

# =====================================================================
# Stage 6: Build roads (v3 — preprocessed lines)
# =====================================================================
print(f"\n[Stage 6] Building roads (v3)...")
t6 = time.time()

roads_mesh = None
if layers.roads_lines:
    try:
        roads_mesh = build_deepseek_roads_v3(layers.roads_lines, terrain_solid, scale)
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
# Stage 7: Build water (v3 — preprocessed WL/WO)
# =====================================================================
print(f"\n[Stage 7] Building water plate (v3: base + WL/WO relief)...")
t7 = time.time()

water_mesh = None
if layers.WL or layers.WO:
    try:
        water_mesh = build_deepseek_water_v3(
            layers.WL, layers.WO,
            bbox_x_min, bbox_y_min, bbox_x_max, bbox_y_max, scale)
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
# Stage 8: Build vegetation (v3 — preprocessed VL/VO)
# =====================================================================
print(f"\n[Stage 8] Building vegetation features (v3)...")
t8 = time.time()

vegetation_mesh = None
if not ENABLE_VEGETATION:
    print(f"  Vegetation DISABLED via ENABLE_VEGETATION flag")
elif layers.VL or layers.VO:
    try:
        vegetation_mesh = build_deepseek_vegetation_v3(
            layers.VL, layers.VO, terrain_solid, scale)
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
# Stage 8.5: Build block_base (v3 — PNG layer 1.5 暖米色城市底)
# =====================================================================
print(f"\n[Stage 8.5] Building block_base (v3 — city tessellation)...")
t85 = time.time()

block_base_mesh = None
if not ENABLE_BLOCK_BASE:
    print(f"  BlockBase DISABLED via ENABLE_BLOCK_BASE flag")
elif layers.block_base:
    try:
        merged_polys = list(layers.block_base)
        merged_classes = list(layers.block_base_classes) if layers.block_base_classes else None
        merge_thickness = None
        if MERGE_BLOCK_LAYERS and layers.BO:
            merged_polys.extend(layers.BO)
            if merged_classes is not None:
                merged_classes.extend(["unclassified"] * len(layers.BO))
            merge_thickness = 0.625  # 合并模式用 BO 高度，比 0.3mm 更有存在感
            print(f"  MERGE: block_base({len(layers.block_base)}) + BO({len(layers.BO)}) "
                  f"= {len(merged_polys)} polys, thickness={merge_thickness}mm")
        block_base_mesh = build_deepseek_block_base_v3(
            merged_polys, terrain_solid, scale,
            bbox_local=bbox_local, thickness_mm=merge_thickness,
            block_classes=merged_classes)
        if block_base_mesh is not None:
            print(f"  BlockBase faces: {len(block_base_mesh.faces):,}")
        else:
            print(f"  No block_base mesh generated")
    except Exception as e:
        print(f"  BlockBase processing failed (skipping): {e}")
else:
    print(f"  No block_base polygons available")
print(f"  Time: {time.time() - t85:.1f}s")

# =====================================================================
# Stage 9: Export 3MF（buildings 拆 landmarks + ambient 两个 sub-mesh，分色）
# =====================================================================
print(f"\n[Stage 9] Preparing and exporting 3MF...")
t9 = time.time()

print(f"  Terrain solid faces: {len(terrain_solid.faces):,} (single closed mesh)")

meshes = {
    'terrain': terrain_solid,
    'buildings': buildings_mesh,    # block_fill 街区填充 (E1 灰)
    'landmarks': landmarks_mesh,    # 地标个体 (E5 暖砂石)
    'roads': roads_mesh,
    'water': water_mesh,
    'vegetation': vegetation_mesh,
    'block_base': block_base_mesh,  # PNG layer 1.5 暖米色城市底 (E6)
}

print(f"\n  Mesh stats:")
print(f"    Terrain - Vertices: {len(terrain_solid.vertices)}, Faces: {len(terrain_solid.faces)}, "
      f"Watertight: {terrain_solid.is_watertight}")
tb = terrain_solid.bounds
print(f"    Terrain - Bounds: X[{tb[0][0]:.1f}, {tb[1][0]:.1f}] Y[{tb[0][1]:.1f}, {tb[1][1]:.1f}] Z[{tb[0][2]:.1f}, {tb[1][2]:.1f}] mm")
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
from datetime import datetime
_suffix = "_2layer" if MERGE_BLOCK_LAYERS else ""
_timestamp = datetime.now().strftime("%m%d_%H%M")
output_path = os.path.join(OUTPUT_DIR, f"full_{CITY_NAME}{_suffix}_{_timestamp}.3mf")
export_deepseek_3mf(meshes, output_path)

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

