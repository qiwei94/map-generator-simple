"""统一城市 3MF 模型生成脚本（标准 CLI 管线）

基于当前正式西湖入口 generate_city.py 的管线流程，支持任意城市。

标准管线流程:
  PBF → osmium extract → tags-filter → export → GeoJSON → GeoDataFrame
  → 高德水体补偿 → 对象4(地形+水洞+桥梁) → 预处理 → 各层mesh → 3MF

用法:
  python generate_cli.py --preset westlake
  python generate_cli.py --bbox 30.13,120.01,30.36,120.29 --pbf pbf_cache/zhejiang-latest.osm.pbf --city westlake
  python generate_cli.py --preset chicago --no-vegetation

前置要求:
  conda install -c conda-forge osmium-tool
"""

import argparse
import hashlib
import json
import os
import sys
import time
import numpy as np
import pandas as pd

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
from _TEXTURE_STYLE_OF_DEEPSEEK._pipeline_cache import PipelineCache
from _TEXTURE_STYLE_OF_DEEPSEEK._process_lock import acquire_lock
from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf, split_terrain_mesh
from _TEXTURE_STYLE_OF_DEEPSEEK import config as _cfg
from _TEXTURE_STYLE_OF_DEEPSEEK.config import compute_scale, WATERWAY_WIDTHS, TERRAIN_GRID, get_area_class, BUILDING_V2_HOTSPOT_RELAX

# ---------------------------------------------------------------------------
# 城市预设
# ---------------------------------------------------------------------------
PRESETS = {
    "westlake": {
        "bbox": (30.13, 120.01, 30.36, 120.29),
        "pbf": "pbf_cache/zhejiang-latest.osm.pbf",
    },
    "chicago": {
        "bbox": (41.76, -87.77, 42.00, -87.49),
        "pbf": "pbf_cache/illinois-latest.osm.pbf",
    },
    "chongqing": {
        "bbox": (29.43, 106.41, 29.66, 106.66),
        "pbf": "pbf_cache/chongqing-latest.osm.pbf",
    },
}

# ---------------------------------------------------------------------------
# CLI 参数
# ---------------------------------------------------------------------------
def parse_args():
    parser = argparse.ArgumentParser(
        description="统一城市 3MF 模型生成 (标准 CLI 管线: 对象4 + 高德水体补偿 + 缓存)"
    )
    parser.add_argument(
        '--preset', choices=list(PRESETS.keys()),
        help='使用内置城市预设（坐标+PBF 路径）'
    )
    parser.add_argument(
        '--bbox', type=str, metavar='S,W,N,E',
        help='边界框坐标: south,west,north,east (WGS84)'
    )
    parser.add_argument(
        '--pbf', type=str, metavar='PATH',
        help='OSM PBF 文件路径'
    )
    parser.add_argument(
        '--city', type=str, metavar='NAME',
        help='城市名称（用于输出文件命名）'
    )
    parser.add_argument(
        '--elevation-file', type=str, default=None, metavar='PATH',
        help='本地 DEM GeoTIFF 文件路径，跳过网络下载'
    )
    parser.add_argument(
        '--use-ndsm', action='store_true', default=False,
        help='使用 nDSM (Copernicus DSM - SRTM) 估算建筑高度'
    )
    parser.add_argument(
        '--narrow-threshold', type=float, default=6.0,
        help='细长建筑 aspect ratio 阈值（默认 6.0）'
    )
    parser.add_argument(
        '--narrow-penalty', type=float, default=0.5,
        help='细长建筑高度缩放系数（默认 0.5）'
    )
    parser.add_argument(
        '--no-vegetation', action='store_true', default=False,
        help='跳过植被层'
    )
    parser.add_argument(
        '--no-block-base', action='store_true', default=False,
        help='跳过 block_base 层'
    )
    parser.add_argument(
        '--merge-layers', action='store_true', default=False,
        help='强制合并 block_base + BO 为 2-layer 模式（覆盖自动检测）'
    )
    parser.add_argument(
        '--no-cache', action='store_true', default=False,
        help='禁用管线缓存，强制重新计算所有阶段'
    )
    parser.add_argument(
        '--no-water-supplement', action='store_true', default=False,
        help='跳过高德水体补偿'
    )
    parser.add_argument(
        '--png', action='store_true', default=False,
        help='同时渲染 PNG 预览图（brick 风格 top-down 2D）'
    )
    parser.add_argument(
        '--debug-obj', action='store_true', default=False,
        help='逐个导出每个 mesh 为独立 OBJ 文件（用于 debug）'
    )
    parser.add_argument(
        '--aesthetic-config', type=str, default=None, metavar='PATH',
        help='美学闭环 best_config.json 路径（默认自动探测 output/aesthetic/<city>/best_config.json）'
    )
    parser.add_argument(
        '--no-aesthetic', action='store_true', default=False,
        help='禁用美学闭环学成参数注入，使用 config 默认值'
    )

    args = parser.parse_args()

    # 合并 preset + 显式参数
    if args.preset:
        p = PRESETS[args.preset]
        if not args.bbox:
            s, w, n, e = p["bbox"]
            args.bbox = f"{s},{w},{n},{e}"
        if not args.pbf:
            args.pbf = p["pbf"]
        if not args.city:
            args.city = args.preset

    # 验证必须参数
    if not args.bbox or not args.pbf or not args.city:
        parser.error("需要 --preset 或 (--bbox + --pbf + --city)")

    # 解析 bbox 字符串为 tuple
    try:
        parts = [float(x.strip()) for x in args.bbox.split(',')]
        if len(parts) != 4:
            raise ValueError
        args.bbox_tuple = tuple(parts)
    except (ValueError, AttributeError):
        parser.error("--bbox 格式: south,west,north,east (逗号分隔的4个浮点数)")

    return args


def load_aesthetic_config(cli_args, city_name):
    """加载美学闭环学成参数（output/aesthetic/<city>/best_config.json）。

    Returns:
        (aes_params dict | None, preprocess_overrides dict)
        aes_params 为 None 时表示未找到/已禁用，管线用 config 默认值。
    """
    if cli_args.no_aesthetic:
        print("  [aesthetic] disabled via --no-aesthetic")
        return None, {}
    path = cli_args.aesthetic_config or os.path.join(
        _project_root, "output", "aesthetic", city_name, "best_config.json")
    if not os.path.exists(path):
        print(f"  [aesthetic] no best_config found ({path}), using config defaults")
        return None, {}
    with open(path, "r", encoding="utf-8") as f:
        aes = json.load(f)
    print(f"  [aesthetic] loaded best_config: {path}")
    print(f"  [aesthetic] learned_at={aes.get('learned_at')} "
          f"score={aes.get('best_score')} bo_mode={aes.get('bo_mode')}")

    # 猴补丁（与 aesthetic/rerun_harness.run_round 注入面一致；
    # buildings/_extract_BL 已函数级 import，运行期改 _cfg 生效）
    if "building_height_mm_max" in aes:
        _cfg.BUILDING_HEIGHT_MAX_MM = float(aes["building_height_mm_max"])
    if "building_simplify_tol_m" in aes:
        _cfg.BUILDING_SIMPLIFY_TOL_M = float(aes["building_simplify_tol_m"])

    # preprocess_layers 显式 override；道路倍率会固化进 layer evidence，
    # 供 PNG/GLB/3MF 共用，避免“预览一套、正式一套”。
    overrides = {}
    if "building_v2_road_tier" in aes:
        overrides["road_tier_override"] = int(aes["building_v2_road_tier"])
    if "building_density_threshold" in aes:
        overrides["density_threshold_override"] = float(aes["building_density_threshold"])
    if "building_count_threshold" in aes:
        overrides["count_threshold_override"] = int(aes["building_count_threshold"])
    if "building_print_limit_m2" in aes:
        overrides["print_limit_m2_override"] = float(aes["building_print_limit_m2"])
    if "aggregate_simplify_m" in aes:
        overrides["aggregate_simplify_m_override"] = float(aes["aggregate_simplify_m"])
    if "bo_mode" in aes:
        overrides["bo_mode_override"] = str(aes["bo_mode"])
    if "road_width_multiplier" in aes:
        overrides["road_width_multiplier_override"] = float(
            aes["road_width_multiplier"])
    return aes, overrides


def main():
    cli_args = parse_args()

    LAT1, LON1, LAT2, LON2 = cli_args.bbox_tuple
    CITY_NAME = cli_args.city
    OUTPUT_DIR = f"output/{CITY_NAME}"
    ENABLE_VEGETATION = not cli_args.no_vegetation
    ENABLE_BLOCK_BASE = not cli_args.no_block_base
    # MERGE_BLOCK_LAYERS 初始值由 CLI flag 设定，后续可能被密度检测覆盖
    MERGE_BLOCK_LAYERS = cli_args.merge_layers

    # PBF 文件
    PBF_FILE = cli_args.pbf
    if not os.path.isabs(PBF_FILE):
        PBF_FILE = os.path.join(_project_root, PBF_FILE)
    if not os.path.exists(PBF_FILE):
        print(f"ERROR: PBF file not found: {PBF_FILE}")
        sys.exit(1)

    print("=" * 70)
    print("  Pure Osmium CLI Pipeline: extract → tags-filter → export → 3MF")
    print("  (对象4 + 高德水体补偿 + 管线缓存)")
    print("=" * 70)

    t_start = time.time()

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Process lock: prevent multiple concurrent instances
    acquire_lock(OUTPUT_DIR, CITY_NAME)

    # Pipeline cache
    _pipeline_cache = PipelineCache(CITY_NAME, enabled=not cli_args.no_cache)
    if cli_args.no_cache:
        print("[CACHE] Disabled via --no-cache")

    # 美学闭环学成参数注入（含 _cfg 猴补丁，必须在 preprocess/建筑 mesh 之前）
    print("\n[Stage 0b] Aesthetic learned config...")
    _aes_config, _aes_overrides = load_aesthetic_config(cli_args, CITY_NAME)

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
        print("  Using pure osmium CLI pipeline (extract → tags-filter → export)")

    # =====================================================================
    # Stage 0c: 自动检测建筑密度 — 决定过滤器和管线模式
    # =====================================================================
    print(f"\n[Stage 0c] Auto-detecting building density...")
    t0c = time.time()
    _density_info = sample_building_density(PBF_FILE, LAT1, LON1, LAT2, LON2)
    _bldg_tag = 'building_landmarks' if _density_info['should_filter'] else 'building'
    # --merge-layers CLI flag 强制启用; 否则由密度自动检测决定
    if not cli_args.merge_layers:
        MERGE_BLOCK_LAYERS = _density_info['should_filter']
    # 美学学成 bo_mode 需要 _compute_BO（MERGE 模式会跳过）且基于全量建筑学成，
    # 故覆盖密度自动判定强制三层模式；显式 --merge-layers 仍优先（但 bo_mode 失效）
    if _aes_config is not None and "bo_mode" in _aes_config:
        if cli_args.merge_layers:
            print(f"  [aesthetic] WARNING: --merge-layers 下 _compute_BO 被跳过，"
                  f"bo_mode={_aes_config['bo_mode']} 不会生效")
        elif MERGE_BLOCK_LAYERS:
            MERGE_BLOCK_LAYERS = False
            _bldg_tag = 'building'
            print(f"  [aesthetic] 覆盖密度判定: 全量 building + 三层模式"
                  f"（bo_mode={_aes_config['bo_mode']} 需要 _compute_BO）")
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

    print("  Method: osmium extract → tags-filter → export")
    water_gdf = fetch_from_cli(
        tag_type='water',
        south=south, west=west, north=north, east=east,
        pbf_file=PBF_FILE
    )

    water_fetch_time = time.time() - t2

    if water_gdf is None or len(water_gdf) == 0:
        print("  WARNING: No water features found, continuing without water")
        water_gdf = None
    else:
        print(f"  Features: {len(water_gdf)}")
        print(f"  Geometry types: {water_gdf.geometry.type.value_counts().to_dict()}")
        print(f"  Time: {water_fetch_time:.1f}s (CLI)")

    # 投影到 UTM
    if water_gdf is not None and len(water_gdf) > 0:
        water_gdf = project_geodataframe(water_gdf, utm_crs, origin, clip_bbox=utm_bbox)

    # 计算面积并筛选
    if water_gdf is not None and len(water_gdf) > 0:
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

        if 'name' in water_gdf.columns:
            named = water_gdf['name'].dropna().unique()
            print(f"  Named features ({len(named)}): {list(named[:10])}")

    # =====================================================================
    # Stage 2b: 水体补全 — 高德卫星 + 自适应 buffer
    # =====================================================================
    print(f"\n[Stage 2b] Water supplement (Gaode + adaptive buffer)...")
    t2b = time.time()

    if not cli_args.no_water_supplement and water_gdf is not None and len(water_gdf) > 0:
        from shapely.geometry import LineString as _LS, MultiLineString as _MLS
        from shapely.geometry import Polygon as _Poly, MultiPolygon as _MPoly

        _wl_polys = []
        _wl_lines = []
        for _, _row in water_gdf.iterrows():
            _g = _row.geometry
            if _g is None or _g.is_empty:
                continue
            if isinstance(_g, (_Poly, _MPoly)):
                _polys = _g.geoms if isinstance(_g, _MPoly) else [_g]
                _wl_polys.extend(p for p in _polys if not p.is_empty)
            elif isinstance(_g, (_LS, _MLS)):
                _ww = _row.get('waterway', 'river')
                _lines = _g.geoms if isinstance(_g, _MLS) else [_g]
                _wl_lines.extend((l, _ww) for l in _lines if not l.is_empty)

        if _wl_lines:
            try:
                from _TEXTURE_STYLE_OF_DEEPSEEK._water_supplement import supplement_wl_coverage
                _enhanced = supplement_wl_coverage(
                    _wl_polys, _wl_lines, (south, west, north, east),
                    utm_crs=utm_crs, origin=origin,
                )
                _n_new = len(_enhanced) - len(_wl_polys)
                if _n_new > 0:
                    print(f"  Supplemented: {_n_new} new polygons from Gaode")
                    import geopandas as gpd
                    _new_rows = []
                    for _p in _enhanced:
                        if not _p.is_empty and _p.area > 0:
                            _new_rows.append({'geometry': _p, 'waterway': 'river', 'name': 'supplemented'})
                    if _new_rows:
                        _suppl_gdf = gpd.GeoDataFrame(_new_rows, crs=water_gdf.crs)
                        _poly_mask = water_gdf.geometry.type.isin(['Polygon', 'MultiPolygon'])
                        water_gdf = gpd.GeoDataFrame(
                            pd.concat([water_gdf[_poly_mask], _suppl_gdf], ignore_index=True),
                            crs=water_gdf.crs)
                        print(f"  water_gdf updated: {len(water_gdf)} features (polygons only)")
                else:
                    print(f"  No new polygons from Gaode supplement")
            except Exception as _e:
                print(f"  Water supplement failed (non-fatal): {_e}")
        else:
            print(f"  No LineString rivers to supplement")
    else:
        if cli_args.no_water_supplement:
            print(f"  Water supplement DISABLED via --no-water-supplement")
        else:
            print(f"  No water data to supplement")
    print(f"  Time: {time.time() - t2b:.1f}s")

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

        vegetation_gdf = project_geodataframe(vegetation_gdf, utm_crs, origin, clip_bbox=utm_bbox)

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
        tag_type=_bldg_tag,
        south=south, west=west, north=north, east=east,
        pbf_file=PBF_FILE
    )

    if buildings_gdf is not None and len(buildings_gdf) > 0:
        print(f"  Features: {len(buildings_gdf)}")
        print(f"  Geometry types: {buildings_gdf.geometry.type.value_counts().to_dict()}")
        print(f"  Time: {time.time() - t3b:.1f}s")

        buildings_gdf = project_geodataframe(buildings_gdf, utm_crs, origin, clip_bbox=utm_bbox)

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
    # Stage 4: Build terrain mesh (obj_4: terrain + water hollow + bridge fusion)
    # =====================================================================
    print(f"\n[Stage 4] Building terrain mesh (obj_4: terrain + water hollow + bridge fusion)...")
    t4 = time.time()

    # Cache key
    _elev_hash = hashlib.md5(elevation_grid.tobytes()).hexdigest()[:12] if elevation_grid is not None else 'none'
    _n_water = len(water_gdf) if water_gdf is not None else 0
    _n_roads = len(roads_gdf) if roads_gdf is not None else 0
    _terrain_cache_key = {
        'elev_hash': _elev_hash, 'scale': round(scale, 8),
        'area_km2': round(area_km2, 2), 'width_m': round(width_m, 1),
        'height_m': round(height_m, 1), 'n_water': _n_water, 'n_roads': _n_roads,
    }

    def _compute_terrain():
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
                _mesh = terrain_result["mesh"]
                print(f"  Terrain (with water holes) faces: {len(_mesh.faces):,}")
                print(f"  Watertight: {_mesh.is_watertight}")
            except Exception as e:
                print(f"  WARNING: Terrain with water holes failed: {e}")
                print(f"  Falling back to basic terrain...")
                _mesh = build_deepseek_terrain(
                    elevation_grid, width_m, height_m, area_km2, scale, water_gdf
                )
        else:
            _mesh = build_deepseek_terrain(
                elevation_grid, width_m, height_m, area_km2, scale, water_gdf
            )
            print(f"  Terrain (no water data) faces: {len(_mesh.faces):,}")
        return _mesh

    terrain_solid = _pipeline_cache.get_or_compute(
        'terrain_v1', _terrain_cache_key, _compute_terrain, label='terrain mesh')

    print(f"  Time: {time.time() - t4:.1f}s")

    # =====================================================================
    # Stage 4.5: 5 步预处理（geometry 减法 + 精度过滤）
    # =====================================================================
    print(f"\n[Stage 4.5] Preprocessing layers (subtraction + precision filter)...")
    t45 = time.time()

    bbox_local = (bbox_x_min, bbox_y_min, bbox_x_max, bbox_y_max)

    # Cache key
    _n_bldg = len(buildings_gdf) if buildings_gdf is not None else 0
    _n_veg = len(vegetation_gdf) if vegetation_gdf is not None else 0
    _n_landuse = len(landuse_gdf) if landuse_gdf is not None else 0
    _preprocess_cache_key = {
        'n_bldg': _n_bldg, 'n_roads': _n_roads, 'n_water': _n_water,
        'n_veg': _n_veg, 'n_landuse': _n_landuse,
        'scale': round(scale, 8), 'area_km2': round(area_km2, 2),
        'merge_mode': MERGE_BLOCK_LAYERS,
        'narrow_threshold': cli_args.narrow_threshold,
        'narrow_penalty': cli_args.narrow_penalty,
        # 美学注入参数进缓存键（含猴补丁值），否则新旧几何互相污染
        'aesthetic': sorted(_aes_overrides.items()),
        'height_max_mm': float(getattr(_cfg, 'BUILDING_HEIGHT_MAX_MM', 0.0)),
        'simplify_tol_m': float(getattr(_cfg, 'BUILDING_SIMPLIFY_TOL_M', 0.0)),
    }

    def _compute_preprocess():
        return preprocess_layers(
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
            merge_mode=MERGE_BLOCK_LAYERS,
            **_aes_overrides,
        )

    layers = _pipeline_cache.get_or_compute(
        'preprocess_v2', _preprocess_cache_key, _compute_preprocess,
        label='preprocess layers')

    print(f"  {layers.summary()}")
    print(f"  Time: {time.time() - t45:.1f}s")

    # =====================================================================
    # Stage 4.6: Render PNG preview (optional, --png flag)
    # =====================================================================
    if cli_args.png:
        print(f"\n[Stage 4.6] Rendering PNG preview...")
        t46 = time.time()
        from _TEXTURE_STYLE_OF_DEEPSEEK.render_png import render_from_layers
        png_path = os.path.join(OUTPUT_DIR, f"{CITY_NAME}_preview.png")
        png_ctx = {
            "bbox_utm": utm_bbox,
            "origin": origin,
            "width_m": width_m,
            "height_m": height_m,
            "utm_crs": utm_crs,
            "bbox_wgs84": (south, west, north, east),
        }
        render_from_layers(
            layers, png_ctx, png_path,
            city_name=CITY_NAME,
            water_gdf=water_gdf,
            landuse_gdf=landuse_gdf,
        )
        print(f"  Time: {time.time() - t46:.1f}s")

    # =====================================================================
    # Stage 5: Build buildings (v3 — preprocessed polygons)
    # =====================================================================
    print(f"\n[Stage 5] Building buildings (v3)...")
    t5 = time.time()

    buildings_mesh = None
    landmarks_mesh = None
    terrain_solid_no_buildings = terrain_solid
    if MERGE_BLOCK_LAYERS:
        print(f"  MERGE_BLOCK_LAYERS=True: BO 将合入 block_base，此处只建 landmarks")
        if layers.BL:
            try:
                bldg_result = build_deepseek_buildings_v3(
                    layers.BL, [], terrain_solid, scale,
                    bbox_local=bbox_local)
                if isinstance(bldg_result, dict):
                    landmarks_mesh = bldg_result.get("landmarks")
                    n_lm = len(landmarks_mesh.faces) if landmarks_mesh is not None else 0
                    print(f"  Landmarks mesh: {n_lm:,} faces")
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
                print(f"  Landmarks mesh: {n_lm:,} faces")
                print(f"  Buildings mesh: {n_amb:,} faces")
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
            roads_mesh = build_deepseek_roads_v3(
                layers.roads_lines,
                terrain_solid,
                scale,
                road_width_multiplier=(getattr(layers, "road_roles", {})
                                       .get("width_policy", {})
                                       .get("road_width_multiplier")),
            )
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
        print(f"  Vegetation DISABLED via --no-vegetation")
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
    # Stage 8.5: Build block_base (v3 — city tessellation)
    # =====================================================================
    print(f"\n[Stage 8.5] Building block_base (v3 — city tessellation)...")
    t85 = time.time()

    block_base_mesh = None
    if not ENABLE_BLOCK_BASE:
        print(f"  BlockBase DISABLED via --no-block-base")
    elif layers.block_base:
        try:
            merged_polys = list(layers.block_base)
            merged_classes = list(layers.block_base_classes) if layers.block_base_classes else None
            merge_thickness = None
            if MERGE_BLOCK_LAYERS and layers.BO:
                merged_polys.extend(layers.BO)
                if merged_classes is not None:
                    merged_classes.extend(["unclassified"] * len(layers.BO))
                merge_thickness = 0.625
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
    # Stage 9: Export 3MF
    # =====================================================================
    print(f"\n[Stage 9] Preparing and exporting 3MF...")
    t9 = time.time()

    print(f"  Terrain solid faces: {len(terrain_solid.faces):,} (single closed mesh)")

    meshes = {
        'terrain': terrain_solid,
        'buildings': buildings_mesh,
        'landmarks': landmarks_mesh,
        'roads': roads_mesh,
        'water': water_mesh,
        'vegetation': vegetation_mesh,
        'block_base': block_base_mesh,
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

    # Debug: export each mesh as separate OBJ
    if cli_args.debug_obj:
        print(f"\n  [DEBUG] Exporting individual OBJ files...")
        debug_dir = os.path.join(OUTPUT_DIR, "debug_obj")
        os.makedirs(debug_dir, exist_ok=True)
        for name, mesh in meshes.items():
            if mesh is not None:
                obj_path = os.path.join(debug_dir, f"{name}.obj")
                mesh.export(obj_path)
                print(f"    {name}: {len(mesh.faces):,} faces -> {obj_path}")
        print(f"  [DEBUG] OBJ files in: {debug_dir}")

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
    print(f"  Pipeline Summary — {CITY_NAME}")
    print(f"{'=' * 70}")
    print(f"  Method: CLI (对象4 + 高德水体补偿 + 缓存)")
    print(f"  Area: {area_km2:.1f} km², Scale: {scale:.6f} mm/m")
    print(f"  Terrain faces: {len(terrain_solid.faces):,}")
    print(f"  Water faces: {len(water_mesh.faces):,}" if water_mesh is not None else "  Water: None")
    print(f"  Vegetation faces: {len(vegetation_mesh.faces):,}" if vegetation_mesh is not None else "  Vegetation: None")
    print(f"  Buildings faces: {len(buildings_mesh.faces):,}" if buildings_mesh is not None else "  Buildings: None")
    print(f"  Roads faces: {len(roads_mesh.faces):,}" if roads_mesh is not None else "  Roads: None")
    print(f"  Output: {output_path} ({file_size:.2f} MB)")
    print(f"  Total time: {total_time:.1f}s")
    print(f"{'=' * 70}\n")


if __name__ == "__main__":
    main()
