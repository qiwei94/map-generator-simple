"""旧版统一城市 3MF 模型生成脚本

通过 CLI 参数传入坐标和 PBF 路径，或使用内置预设。

用法:
  python generate_city_legacy.py --preset westlake
  python generate_city_legacy.py --bbox 30.13,120.01,30.36,120.29 --pbf pbf_cache/zhejiang-latest.osm.pbf --city westlake
  python generate_city_legacy.py --preset chicago --merge-layers --narrow-threshold 8.0

前置要求:
  conda install -c conda-forge osmium-tool
"""

import argparse
import json
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
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osmium_cli_fetcher import fetch_from_cli, fetch_tiled_from_cli, get_cli_fetcher
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.elevation import fetch_elevation_grid, fetch_elevation_grid_tiled
from _TEXTURE_STYLE_OF_DEEPSEEK._tile_grid import snap_bbox as _snap_bbox
from _TEXTURE_STYLE_OF_DEEPSEEK._pipeline_cache import PipelineCache
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import build_deepseek_terrain
from _TEXTURE_STYLE_OF_DEEPSEEK.object4_terrain_with_holes import build_terrain_with_water_holes_manifold
from _TEXTURE_STYLE_OF_DEEPSEEK.buildings import build_deepseek_buildings, build_deepseek_buildings_v3
from _TEXTURE_STYLE_OF_DEEPSEEK.roads import build_deepseek_roads, build_deepseek_roads_v3
from _TEXTURE_STYLE_OF_DEEPSEEK.water import build_deepseek_water, build_deepseek_water_v3
from _TEXTURE_STYLE_OF_DEEPSEEK.vegetation_exclusion import build_deepseek_vegetation_v3
from _TEXTURE_STYLE_OF_DEEPSEEK.block_base import build_deepseek_block_base_v3
from _TEXTURE_STYLE_OF_DEEPSEEK._layer_preprocess import preprocess_layers
from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf, split_terrain_mesh
from _TEXTURE_STYLE_OF_DEEPSEEK.design_spec import (
    build_design_spec, layer_evidence, write_design_spec,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.print_profile import (
    DEFAULT_PRINTER_PROFILE,
    PrinterProfile,
    PrintScale,
    build_printability_report,
)
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
        "pbf": "pbf_cache/chongqing-260508.osm.pbf",
    },
}

# ---------------------------------------------------------------------------
# CLI 参数
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="统一城市 3MF 模型生成 (osmium CLI pipeline)"
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
        help='合并 block_base + BO 为 2-layer 模式'
    )
    parser.add_argument(
        '--auto-params', action='store_true', default=False,
        help='启用自动参数系统（检测城市特征 → 自适应参数）'
    )
    parser.add_argument(
        '--params-json', type=str, default=None, metavar='PATH',
        help='JSON 参数覆盖文件（画廊风格参数，最高优先级；自动启用 --auto-params）'
    )
    parser.add_argument(
        '--ai-review', action='store_true', default=False,
        help='启用 AI 视觉评审（需要 ANTHROPIC_API_KEY + --png）'
    )
    parser.add_argument(
        '--art-direction', action='store_true', default=False,
        help='启用 AI 艺术指导（Layer 2，为新城市生成风格策略）'
    )
    parser.add_argument(
        '--png', action='store_true', default=False,
        help='同时渲染 PNG 预览图（pipeline 诊断图，带图例与统计）'
    )
    parser.add_argument(
        '--review-png', action='store_true', default=False,
        help='同时渲染画廊级俯视图（无文字、超采样，风格画廊同源）'
    )
    parser.add_argument(
        '--draft', action='store_true', default=False,
        help='Draft 模式：跳过 brick/boolean，快速导出 GLB 预览后退出'
    )
    parser.add_argument(
        '--preview-fast', action='store_true', default=False,
        help='快速预览：降低 DEM/GLB 精度，跳过植被与 landuse 取数（仅影响 --draft）'
    )
    parser.add_argument(
        '--base-thickness-mm', type=float, default=0.4, metavar='MM',
        help='公共打印底层厚度；GLB 预览与正式 3MF 共用（默认 0.4mm）'
    )
    parser.add_argument(
        '--printer-profile-json', type=str, default=None, metavar='PATH',
        help=('打印机物理约束 JSON；P0 阶段喷嘴参与几何尺度，线宽/层高/'
              '间隙进入 design_spec 审计')
    )
    parser.add_argument(
        '--marker', action='append', default=None, metavar='LAT,LON',
        help='在 draft GLB 中插红色大头针标注（可重复，如照片 GPS 点）'
    )
    parser.add_argument(
        '--debug-obj', action='store_true', default=False,
        help='逐个导出每个 mesh 为独立 OBJ 文件（用于 debug）'
    )
    parser.add_argument(
        '--no-snap', action='store_true', default=False,
        help='关闭取数框网格量化（回退精确 bbox 取数，不做跨请求缓存复用）'
    )
    parser.add_argument(
        '--no-cache', action='store_true', default=False,
        help='禁用 preprocess 阶段缓存（强制重算）'
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

    # 解析 --marker 为 [(lat, lon), ...]
    args.marker_points = []
    for m in (args.marker or []):
        try:
            lat_s, lon_s = m.split(',')
            args.marker_points.append((float(lat_s), float(lon_s)))
        except ValueError:
            parser.error(f"--marker 格式: lat,lon（收到: {m}）")

    return args


def _load_printer_profile(path):
    """Load an explicit physical profile, or return the immutable default."""
    if not path:
        return DEFAULT_PRINTER_PROFILE
    try:
        with open(path, encoding="utf-8") as stream:
            payload = json.load(stream)
        if not isinstance(payload, dict):
            raise ValueError("root must be a JSON object")
        return PrinterProfile(**payload)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise SystemExit(f"invalid --printer-profile-json: {exc}")


# =====================================================================
# 取数框量化（snap-to-grid）辅助：重叠区域跨请求复用缓存
# =====================================================================

def _grid_shape_for(south, west, north, east, resolution):
    """与 fetch_elevation_grid 内部一致的 rows/cols 计算。"""
    lat_range = north - south
    lon_range = east - west
    if lat_range >= lon_range:
        return resolution, max(2, int(resolution * lon_range / lat_range))
    return max(2, int(resolution * lat_range / lon_range)), resolution


def _crop_grid_to_bbox(grid, snap_bbox, exact_bbox, target_shape):
    """从量化框高程网格中双线性重采样出精确框子网格。

    网格约定：row 0 = south，col 0 = west（与 fetch_elevation_grid 一致）。
    """
    from scipy.ndimage import map_coordinates
    fs, fw, fn, fe = snap_bbox
    south, west, north, east = exact_bbox
    rows, cols = grid.shape
    tr, tc = target_shape
    r = np.linspace((south - fs) / (fn - fs) * (rows - 1),
                    (north - fs) / (fn - fs) * (rows - 1), tr)
    c = np.linspace((west - fw) / (fe - fw) * (cols - 1),
                    (east - fw) / (fe - fw) * (cols - 1), tc)
    rr, cc = np.meshgrid(r, c, indexing="ij")
    out = map_coordinates(grid, [rr, cc], order=1, mode="nearest")
    return out.astype(grid.dtype, copy=False)


def _split_polygons(geom):
    """展平 MultiPolygon/GeometryCollection 为 Polygon 列表（丢弃非面要素）。"""
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    if geom.geom_type in ("MultiPolygon", "GeometryCollection"):
        return [g for g in geom.geoms
                if g.geom_type == "Polygon" and not g.is_empty]
    return []


def _transform_layers_to_exact(layers, dx, dy, clip_box):
    """把 snap 本地坐标系下的 LayerPolygons 平移 (dx, dy) 并裁剪到精确框。

    缓存的 preprocess 结果是在量化框坐标系里算的；复用时平移到本次请求的
    精确坐标系，再裁到精确 bbox，语义等价于原生按精确框计算。
    """
    from shapely.affinity import translate
    from shapely.geometry import box as _box
    try:
        from shapely import make_valid
    except ImportError:  # shapely < 2.0
        from shapely.validation import make_valid
    clip = _box(*clip_box)

    def _proc_poly(p):
        moved = translate(p, xoff=dx, yoff=dy)
        try:
            cut = moved.intersection(clip)
        except Exception:
            # 非法多边形（自交环/游离孔洞）会让 GEOS 抛 TopologyException；
            # make_valid 修复后重试，仍失败则丢弃该要素（不阻断整体）。
            try:
                cut = make_valid(moved).intersection(clip)
            except Exception:
                return []
        return _split_polygons(cut)

    BL, BL_cat = [], []
    cats = list(layers.BL_categories)
    if len(cats) < len(layers.BL):  # 长度异常时补齐，避免 zip 静默丢弃
        cats.extend([None] * (len(layers.BL) - len(cats)))
    for (p, h), cat in zip(layers.BL, cats):
        for q in _proc_poly(p):
            BL.append((q, h))
            BL_cat.append(cat)

    def _proc_list(polys):
        out = []
        for p in polys:
            out.extend(_proc_poly(p))
        return out

    bb, bb_cls = [], []
    if layers.block_base_classes and len(layers.block_base_classes) == len(layers.block_base):
        for p, cls in zip(layers.block_base, layers.block_base_classes):
            for q in _proc_poly(p):
                bb.append(q)
                bb_cls.append(cls)
    else:
        bb = _proc_list(layers.block_base)
        bb_cls = list(layers.block_base_classes)

    roads = []
    for line, tier, flag in layers.roads_lines:
        moved = translate(line, xoff=dx, yoff=dy)
        try:
            seg = moved.intersection(clip)
        except Exception:
            try:
                seg = make_valid(moved).intersection(clip)
            except Exception:
                continue
        if seg.is_empty:
            continue
        if seg.geom_type == "LineString":
            roads.append((seg, tier, flag))
        elif seg.geom_type == "MultiLineString":
            roads.extend((g, tier, flag) for g in seg.geoms if not g.is_empty)

    layers.BL = BL
    layers.BL_categories = BL_cat
    layers.BO = _proc_list(layers.BO)
    layers.VL = _proc_list(layers.VL)
    layers.VO = _proc_list(layers.VO)
    layers.WL = _proc_list(layers.WL)
    layers.WO = _proc_list(layers.WO)
    layers.block_base = bb
    layers.block_base_classes = bb_cls
    layers.roads_lines = roads
    if getattr(layers, "road_roles", None):
        layers.road_roles["visible_segments"] = len(roads)
    return layers


def main():
    cli_args = parse_args()
    if not 0.4 <= cli_args.base_thickness_mm <= 3.0:
        raise SystemExit("--base-thickness-mm must be between 0.4 and 3.0")
    if cli_args.preview_fast and not cli_args.draft:
        raise SystemExit("--preview-fast requires --draft")

    LAT1, LON1, LAT2, LON2 = cli_args.bbox_tuple
    CITY_NAME = cli_args.city
    OUTPUT_DIR = f"output/{CITY_NAME}"
    ENABLE_VEGETATION = not cli_args.no_vegetation
    ENABLE_BLOCK_BASE = not cli_args.no_block_base
    MERGE_BLOCK_LAYERS = cli_args.merge_layers

    # PBF 文件
    PBF_FILE = cli_args.pbf
    if not os.path.isabs(PBF_FILE):
        PBF_FILE = os.path.join(_project_root, PBF_FILE)
    if not os.path.exists(PBF_FILE):
        print(f"ERROR: PBF file not found: {PBF_FILE}")
        sys.exit(1)

    print("=" * 70)
    print(f"  City: {CITY_NAME}")
    print(f"  BBox: ({LAT1}, {LON1}) → ({LAT2}, {LON2})")
    print(f"  PBF: {PBF_FILE}")
    print(f"  Options: vegetation={'ON' if ENABLE_VEGETATION else 'OFF'}, "
          f"block_base={'ON' if ENABLE_BLOCK_BASE else 'OFF'}, "
          f"merge_layers={'ON' if MERGE_BLOCK_LAYERS else 'OFF'}")
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
    if cli_args.preview_fast:
        resolution = min(resolution, 256)
    printer_profile = _load_printer_profile(cli_args.printer_profile_json)
    print_scale = PrintScale(width_m, height_m)
    scale = print_scale.scale_mm_per_m
    south, west, north, east = bbox["wgs84_bbox"]
    utm_crs = bbox["utm_crs"]
    origin = bbox["origin"]
    utm_bbox = bbox["utm_bbox"]

    bbox_x_min = utm_bbox[0] - origin[0]
    bbox_y_min = utm_bbox[1] - origin[1]
    bbox_x_max = utm_bbox[2] - origin[0]
    bbox_y_max = utm_bbox[3] - origin[1]

    # 取数框量化（snap-to-grid）：取数用量化框（跨请求缓存复用），
    # 输出度量与最终裁剪仍用用户精确框。--no-snap 回退旧行为。
    # 快速预览优先减少本次解析量：精确 bbox 通常远小于量化后的缓存框。
    # 已选画廊风格的预览仍由 generate_gallery_draft 直接复用预处理缓存。
    snap_active = not cli_args.no_snap and not cli_args.preview_fast
    if snap_active:
        fs, fw, fn, fe = _snap_bbox(south, west, north, east)
        snap_info = bbox_to_utm(fs, fw, fn, fe)
        # 量化框跨 UTM 分区时平移近似失效，回退精确取数
        if snap_info["utm_crs"].to_string() != utm_crs.to_string():
            print(f"  Snap disabled: fetch bbox crosses UTM zone")
            snap_active = False
    if not snap_active:
        fs, fw, fn, fe = south, west, north, east
        snap_info = bbox

    print(f"  Area: {width_m:.0f}m × {height_m:.0f}m = {area_km2:.1f} km² ({area_class})")
    print(f"  Scale: {scale:.6f} mm/m")
    print(f"  Resolution: {resolution}x{resolution}")
    if cli_args.preview_fast:
        print("  [preview-fast] exact bbox, reduced DEM/GLB geometry")
    if snap_active:
        print(f"  Fetch bbox (snapped): ({fs:.4f}, {fw:.4f}) → ({fn:.4f}, {fe:.4f}) "
              f"[snap={_snap_bbox.__defaults__[0]:.2f}°]")
    print(f"  Time: {time.time() - t1:.1f}s")

    # =====================================================================
    # Stage 1b: Fetch elevation data (SRTM HGT tiles)
    # =====================================================================
    print(f"\n[Stage 1b] Fetching elevation data...")
    if cli_args.elevation_file:
        print(f"  Using local DEM: {cli_args.elevation_file}")
    t1b = time.time()

    try:
        # 按量化框取数（缓存 key 稳定，重叠请求命中）；分辨率按跨度比例
        # 放大以保持采样密度，取回后重采样裁剪到精确框。
        exact_span = max(north - south, east - west)
        snap_span = max(fn - fs, fe - fw)
        res_fetch = resolution if not snap_active else int(
            (resolution - 1) * snap_span / exact_span) + 1
        if snap_active and not cli_args.elevation_file:
            # 瓦片级高程缓存（Phase 2）：跨网格线偏移也能部分复用
            elevation_grid_snap = fetch_elevation_grid_tiled(
                fs, fw, fn, fe, res_fetch)
        else:
            elevation_grid_snap = fetch_elevation_grid(
                fs, fw, fn, fe, res_fetch,
                elevation_file=cli_args.elevation_file,
            )
        if snap_active:
            target_shape = _grid_shape_for(south, west, north, east, resolution)
            elevation_grid = _crop_grid_to_bbox(
                elevation_grid_snap, (fs, fw, fn, fe),
                (south, west, north, east), target_shape)
        else:
            elevation_grid = elevation_grid_snap
        print(f"  Grid shape: {elevation_grid.shape}")
        print(f"  Elevation range: {elevation_grid.min():.1f}m to {elevation_grid.max():.1f}m")
        print(f"  Time: {time.time() - t1b:.1f}s")
    except Exception as e:
        print(f"  WARNING: Elevation fetch failed: {e}")
        print(f"  Using flat terrain (0m elevation)")
        elevation_grid = np.zeros((resolution, resolution), dtype=np.float64)
        elevation_grid_snap = elevation_grid
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

    water_gdf = (fetch_tiled_from_cli if snap_active else fetch_from_cli)(
        tag_type='water',
        south=fs, west=fw, north=fn, east=fe,
        pbf_file=PBF_FILE
    )

    water_fetch_time = time.time() - t2

    if water_gdf is None or len(water_gdf) == 0:
        print("  WARNING: No water features found, continuing without water")
        water_gdf = None
    else:
        print(f"  Features: {len(water_gdf)}")
        print(f"  Geometry types: {water_gdf.geometry.type.value_counts().to_dict()}")

    print(f"  Time: {water_fetch_time:.1f}s")

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
    # Stage 3: Fetch vegetation data (CLI only)
    # =====================================================================
    print(f"\n[Stage 3] Fetching vegetation data...")
    t3 = time.time()

    if cli_args.preview_fast:
        print("  [preview-fast] skipped (formal 3MF still includes vegetation)")
        vegetation_gdf = None
    else:
        vegetation_gdf = (fetch_tiled_from_cli if snap_active else fetch_from_cli)(
            tag_type='vegetation',
            south=fs, west=fw, north=fn, east=fe,
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

    buildings_gdf = (fetch_tiled_from_cli if snap_active else fetch_from_cli)(
        tag_type='building',
        south=fs, west=fw, north=fn, east=fe,
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

    roads_gdf = (fetch_tiled_from_cli if snap_active else fetch_from_cli)(
        tag_type='road',
        south=fs, west=fw, north=fn, east=fe,
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

    if cli_args.preview_fast:
        print("  [preview-fast] skipped (block-base classification simplified)")
        landuse_gdf = None
    else:
        landuse_gdf = (fetch_tiled_from_cli if snap_active else fetch_from_cli)(
            tag_type='landuse',
            south=fs, west=fw, north=fn, east=fe,
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
    # Stage 3e: Auto-parameter detection (optional)
    # =====================================================================
    auto_resolved = None  # will be set if --auto-params
    profile = None
    style_overrides = {}  # from --params-json (画廊风格参数)
    if cli_args.params_json:
        with open(cli_args.params_json, 'r', encoding='utf-8') as f:
            style_overrides = json.load(f)
        if not cli_args.auto_params:
            print("  [Stage 3e] --params-json 需基于规则引擎，自动启用 --auto-params")
            cli_args.auto_params = True
    if cli_args.auto_params:
        print(f"\n[Stage 3e] Auto-parameter detection...")
        t3e = time.time()

        from _TEXTURE_STYLE_OF_DEEPSEEK.auto_params import (
            detect_city_profile, resolve_params, save_decision_report,
        )
        from _TEXTURE_STYLE_OF_DEEPSEEK.auto_params.ai_art_direction import ai_art_direction
        from _TEXTURE_STYLE_OF_DEEPSEEK.auto_params.preference_store import PreferenceStore
        from _TEXTURE_STYLE_OF_DEEPSEEK import config as _cfg

        # snap 模式下 profile 用量化框数据：同一网格内不同请求得到完全
        # 一致的 auto 参数，保证 preprocess 缓存指纹稳定。
        profile_area_km2 = snap_info["area_km2"] if snap_active else area_km2
        profile_local_area_m2 = (snap_info["width_m"] * snap_info["height_m"]
                                 if snap_active else width_m * height_m)
        profile = detect_city_profile(
            bbox_area_km2=profile_area_km2,
            elevation_grid=elevation_grid_snap,
            buildings_gdf=buildings_gdf,
            roads_gdf=roads_gdf,
            water_gdf=water_gdf,
            vegetation_gdf=vegetation_gdf,
            bbox_local_area_m2=profile_local_area_m2,
        )

        # Layer 2: AI art direction (optional, --art-direction flag)
        ai_overrides = {}
        if cli_args.art_direction:
            # Pass reference PNGs from output dir if they exist
            _ref_pngs = []
            _ref_dir = os.path.join(OUTPUT_DIR, "..", "reference")
            if os.path.isdir(_ref_dir):
                _ref_pngs = [os.path.join(_ref_dir, f)
                             for f in os.listdir(_ref_dir) if f.endswith(".png")]
            art = ai_art_direction(profile, CITY_NAME,
                                   reference_pngs=_ref_pngs or None)
            if art and art.get("param_overrides"):
                ai_overrides = art["param_overrides"]
                print(f"  AI art direction: emphasis={art.get('emphasis', [])}, "
                      f"style=\"{art.get('style_notes', '')}\"")
                print(f"  AI param_overrides: {ai_overrides}")

        # Layer 3: Preference bias (if enough history)
        pref_store = PreferenceStore(
            log_path=os.path.join(OUTPUT_DIR, "..", "preference_log.jsonl"))
        pref_bias = pref_store.extract_bias(min_records=10)
        if pref_bias:
            print(f"  Preference bias ({pref_store.count} records): {pref_bias}")

        # Merge overrides: user CLI > AI art > preference bias
        merged_overrides = {}
        merged_overrides.update(pref_bias)     # lowest priority
        merged_overrides.update(ai_overrides)  # AI art direction
        # Layer 4: 显式风格参数（--params-json，最高优先级；
        #   bo_mode/aggregate_simplify_m 非 ResolvedParams 字段，
        #   由 Stage 4.5 的 preprocess override 显式传参生效）
        if style_overrides:
            print(f"  Style overrides (--params-json): {style_overrides}")
            merged_overrides.update(style_overrides)

        auto_resolved = resolve_params(profile, user_overrides=merged_overrides or None)
        save_decision_report(profile, auto_resolved, OUTPUT_DIR, CITY_NAME)

        # Apply resolved params to config (runtime monkey-patch for downstream modules)
        _cfg.Z_GAMMA = auto_resolved.z_gamma
        _cfg.TERRAIN_THICKNESS_MM = auto_resolved.terrain_thickness_mm
        _cfg.ELEVATION_SMOOTHING_SIGMA = auto_resolved.elevation_smoothing_sigma
        # Also patch terrain3d.config (elevation.py reads from there)
        from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d import config as _t3d_cfg
        _t3d_cfg.ELEVATION_SMOOTHING_SIGMA = auto_resolved.elevation_smoothing_sigma
        _cfg.BUILDING_V2_DENSITY_THRESHOLD = auto_resolved.building_density_threshold
        _cfg.BUILDING_V2_COUNT_THRESHOLD = auto_resolved.building_count_threshold
        _cfg.BUILDING_PRINT_LIMIT_M2 = auto_resolved.building_print_limit_m2
        _cfg.BUILDING_V2_ROAD_TIER = auto_resolved.building_v2_road_tier
        _cfg.BUILDING_V2_HOTSPOT_RELAX = auto_resolved.building_v2_hotspot_relax
        _cfg.ROAD_WIDTH_MULTIPLIER = auto_resolved.road_width_multiplier
        _cfg.VEGETATION_MIN_AREA_M2 = auto_resolved.vegetation_min_area_m2
        _cfg.BUILDING_SIMPLIFY_TOL_M = auto_resolved.building_simplify_tol_m
        _cfg.BUILDING_V2_LANDMARK_TOP_PERCENT = auto_resolved.building_v2_landmark_top_percent
        _cfg.WATER_MIN_AREA_M2 = auto_resolved.water_min_area_m2
        _cfg.BRICK_PERLIN_AMP = auto_resolved.brick_perlin_amp
        _cfg.BRICK_CORNER_R_M = auto_resolved.brick_corner_r_m
        # 建筑高度动态范围（_compress_height 函数级 import，补丁生效）
        _cfg.BUILDING_HEIGHT_MAX_MM = auto_resolved.building_height_mm_max
        _cfg.BUILDING_HEIGHT_MIN_MM = auto_resolved.building_height_mm_min
        if auto_resolved.road_filter_tier is not None:
            _cfg.ROAD_FILTER["large"] = auto_resolved.road_filter_tier

        print(f"  Style: {auto_resolved.style}")
        print(f"  Profile: relief={profile.relief_ratio}, water={profile.water_ratio:.2f}, "
              f"density={profile.building_density:.0f}/km²")
        print(f"  Key params: Z_GAMMA={auto_resolved.z_gamma}, "
              f"flat_mode={auto_resolved.flat_mode}, "
              f"road_tier={auto_resolved.building_v2_road_tier}, "
              f"brick_amp={auto_resolved.brick_perlin_amp}")
        print(f"  Time: {time.time() - t3e:.1f}s")

    # =====================================================================
    # Stage 4: Build terrain mesh (obj_4: terrain + water hollow)
    # =====================================================================
    print(f"\n[Stage 4] Building terrain mesh (obj_4: terrain + water hollow)...")
    t4 = time.time()

    if cli_args.draft:
        # Draft 模式：跳过正式地形（GLB 用降采样 heightfield 自建）
        print(f"  DRAFT mode: skipping full terrain build (GLB heightfield instead)")
        terrain_solid = None
    elif water_gdf is not None and len(water_gdf) > 0:
        # Skip Manifold boolean water hollowing — it destroys terrain surface
        # detail (527K→141K faces, radial artifacts, lake disappearance).
        # Water is represented as a separate mesh layer instead.
        print(f"  Skipping Manifold water hollow (preserves terrain detail)")
        terrain_solid = build_deepseek_terrain(
            elevation_grid, width_m, height_m, area_km2, scale, water_gdf,
            base_thickness_mm=cli_args.base_thickness_mm,
        )
    else:
        terrain_solid = build_deepseek_terrain(
            elevation_grid, width_m, height_m, area_km2, scale, water_gdf,
            base_thickness_mm=cli_args.base_thickness_mm,
        )
        print(f"  Terrain (no water data) faces: {len(terrain_solid.faces):,}")

    print(f"  Time: {time.time() - t4:.1f}s")

    # =====================================================================
    # Stage 4.5: 5 步预处理（geometry 减法 + 精度过滤）
    # =====================================================================
    print(f"\n[Stage 4.5] Preprocessing layers (subtraction + precision filter)...")
    t45 = time.time()

    bbox_local = (bbox_x_min, bbox_y_min, bbox_x_max, bbox_y_max)

    # Build override kwargs from auto_resolved (if available)
    _preprocess_overrides = {}
    if auto_resolved is not None:
        _preprocess_overrides["road_tier_override"] = auto_resolved.building_v2_road_tier
        _preprocess_overrides["density_threshold_override"] = auto_resolved.building_density_threshold
        _preprocess_overrides["count_threshold_override"] = auto_resolved.building_count_threshold
        _preprocess_overrides["print_limit_m2_override"] = auto_resolved.building_print_limit_m2
        _preprocess_overrides["road_width_multiplier_override"] = (
            auto_resolved.road_width_multiplier)
        if auto_resolved.flat_mode:
            _preprocess_overrides["height_mode_override"] = "flat"
    # 风格参数中的 config 级参数（模块顶层 import 吃不到补丁，必须显式传参）
    if "bo_mode" in style_overrides:
        _preprocess_overrides["bo_mode_override"] = str(style_overrides["bo_mode"])
    if "aggregate_simplify_m" in style_overrides:
        _preprocess_overrides["aggregate_simplify_m_override"] = float(
            style_overrides["aggregate_simplify_m"])

    layers = None
    if snap_active:
        # ---- snap 模式：preprocess 在量化框坐标系计算，跨请求缓存复用 ----
        # 命中后平移回本次精确坐标系并裁到精确 bbox。
        from shapely.affinity import translate as _sh_translate
        _snap_origin = snap_info["origin"]
        _sxoff = origin[0] - _snap_origin[0]  # exact-local → snap-local
        _syoff = origin[1] - _snap_origin[1]

        def _gdf_to_snap(g):
            if g is None or len(g) == 0:
                return g
            g2 = g.copy()
            g2["geometry"] = g2["geometry"].apply(
                lambda geom: _sh_translate(geom, xoff=_sxoff, yoff=_syoff))
            return g2

        _snap_utm_bbox = snap_info["utm_bbox"]
        _snap_bbox_local = (_snap_utm_bbox[0] - _snap_origin[0],
                            _snap_utm_bbox[1] - _snap_origin[1],
                            _snap_utm_bbox[2] - _snap_origin[0],
                            _snap_utm_bbox[3] - _snap_origin[1])
        _scale_snap = compute_scale(snap_info["width_m"], snap_info["height_m"])
        _hotspot_relax = (auto_resolved.building_v2_hotspot_relax
                          if auto_resolved is not None
                          else BUILDING_V2_HOTSPOT_RELAX)

        _auto_fp = "none" if auto_resolved is None else json.dumps({
            "road_tier": auto_resolved.building_v2_road_tier,
            "density": auto_resolved.building_density_threshold,
            "count": auto_resolved.building_count_threshold,
            "print_limit": auto_resolved.building_print_limit_m2,
            "flat": auto_resolved.flat_mode,
            "hotspot_relax": auto_resolved.building_v2_hotspot_relax,
            "road_width_multiplier": auto_resolved.road_width_multiplier,
        }, sort_keys=True)
        _style_fp = json.dumps({
            "bo_mode": style_overrides.get("bo_mode"),
            "aggregate_simplify_m": style_overrides.get("aggregate_simplify_m"),
        }, sort_keys=True)

        _snap_cache = PipelineCache(
            f"snap_{fs:.4f}_{fw:.4f}_{fn:.4f}_{fe:.4f}",
            enabled=not cli_args.no_cache)

        def _compute_layers_snap():
            return preprocess_layers(
                buildings_gdf=_gdf_to_snap(buildings_gdf),
                roads_gdf=_gdf_to_snap(roads_gdf),
                water_gdf=_gdf_to_snap(water_gdf),
                vegetation_gdf=_gdf_to_snap(vegetation_gdf),
                bbox_local=_snap_bbox_local,
                scale=_scale_snap,
                enable_hotspot=True,
                hotspot_relax=_hotspot_relax,
                area_km2=snap_info["area_km2"],
                landuse_gdf=_gdf_to_snap(landuse_gdf),
                narrow_threshold=cli_args.narrow_threshold,
                narrow_penalty=cli_args.narrow_penalty,
                bbox_wgs84=(fs, fw, fn, fe),
                utm_crs=utm_crs,
                origin=_snap_origin,
                printer_profile=printer_profile,
                **_preprocess_overrides,
            )

        layers = _snap_cache.get_or_compute(
            "preprocess_v2",
            input_keys={
                "snap_bbox": f"{fs:.4f},{fw:.4f},{fn:.4f},{fe:.4f}",
                "auto": _auto_fp,
                "style": _style_fp,
                "narrow": f"{cli_args.narrow_threshold}/{cli_args.narrow_penalty}",
                "veg": ENABLE_VEGETATION,
                "block_base": ENABLE_BLOCK_BASE,
                "merge": MERGE_BLOCK_LAYERS,
                "printer_profile": printer_profile.to_dict(),
            },
            compute_fn=_compute_layers_snap,
            label="preprocess(snap)",
        )

        # snap 坐标系 → 精确坐标系，并裁剪到用户精确 bbox
        _dx = _snap_origin[0] - origin[0]
        _dy = _snap_origin[1] - origin[1]
        layers = _transform_layers_to_exact(layers, _dx, _dy, bbox_local)
    else:
        layers = preprocess_layers(
            buildings_gdf=buildings_gdf,
            roads_gdf=roads_gdf,
            water_gdf=water_gdf,
            vegetation_gdf=vegetation_gdf,
            bbox_local=bbox_local,
            scale=scale,
            enable_hotspot=True,
            hotspot_relax=(auto_resolved.building_v2_hotspot_relax
                           if auto_resolved is not None
                           else BUILDING_V2_HOTSPOT_RELAX),
            area_km2=area_km2,
            landuse_gdf=landuse_gdf,
            narrow_threshold=cli_args.narrow_threshold,
            narrow_penalty=cli_args.narrow_penalty,
            bbox_wgs84=(south, west, north, east),
            utm_crs=utm_crs,
            origin=origin,
            printer_profile=printer_profile,
            **_preprocess_overrides,
        )
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
    # Stage 4.65: 画廊级漂亮俯视图（--review-png）
    # 与 Stage 4.6 的诊断图不同：无文字标注、超采样抗锯齿、带山体阴影，
    # 就是风格画廊用的那张图（aesthetic/review_render.py）。
    # =====================================================================
    if getattr(cli_args, "review_png", False):
        print(f"\n[Stage 4.65] Rendering gallery-grade preview...")
        t465 = time.time()
        try:
            from aesthetic.review_render import render_review_bundle
            _rw = (auto_resolved.road_width_multiplier
                   if auto_resolved is not None else 1.0)
            bundle = render_review_bundle(
                layers, {"bbox_local": bbox_local, "scale": scale}, _rw,
                OUTPUT_DIR, CITY_NAME)
            print(f"  topdown: {bundle.get('topdown')}")
            print(f"  Time: {time.time() - t465:.1f}s")
        except Exception as e:
            print(f"  WARNING: gallery preview failed: {type(e).__name__}: {e}")

    # =====================================================================
    # Stage 4.8: Draft GLB 快速预览（--draft：导出后提前退出）
    # =====================================================================
    if cli_args.draft:
        print(f"\n[Stage 4.8] Exporting draft GLB preview...")
        t48 = time.time()
        from _TEXTURE_STYLE_OF_DEEPSEEK.render_glb import render_glb_preview
        glb_path = os.path.join(OUTPUT_DIR, f"{CITY_NAME}_draft.glb")
        # --marker: lat/lon → 本地米（与图层同一 UTM 投影 + origin 平移）
        marker_local = []
        if cli_args.marker_points:
            from pyproj import Transformer
            _tf = Transformer.from_crs("EPSG:4326", utm_crs, always_xy=True)
            for _lat, _lon in cli_args.marker_points:
                _mx, _my = _tf.transform(_lon, _lat)
                marker_local.append((_mx - origin[0], _my - origin[1]))
        render_glb_preview(
            layers,
            {"bbox_local": bbox_local, "scale": scale,
             "bbox_wgs84": (south, west, north, east),
             "utm_crs": utm_crs, "origin": origin},
            glb_path,
            elevation_grid=elevation_grid,
            markers=marker_local or None,
            water_gdf=water_gdf,
            base_thickness_mm=cli_args.base_thickness_mm,
            terrain_relief_mm=(_cfg.TERRAIN_THICKNESS_MM
                               if auto_resolved is not None else None),
            preview_quality=("fast" if cli_args.preview_fast else "balanced"),
        )
        glb_size = os.path.getsize(glb_path) / (1024 * 1024)
        total_time = time.time() - t_start
        print(f"\n{'=' * 70}")
        print(f"  DRAFT Summary — {CITY_NAME}")
        print(f"  Output: {glb_path} ({glb_size:.2f} MB)")
        print(f"  Total time: {total_time:.1f}s (draft; full 3MF skipped)")
        print(f"{'=' * 70}\n")
        return

    # =====================================================================
    # Stage 4.7: AI vision review — advisory (optional, --ai-review + --png)
    # NOTE: 评审结果记录到 trajectory，参数调整影响下游 builder（buildings v3
    #       的 BRICK_* 为函数级 import 可生效），但不重渲 PNG（render_png
    #       不消费这些参数，重渲是无效操作）。完整闭环需重跑 preprocess。
    # =====================================================================
    if cli_args.ai_review and cli_args.png and auto_resolved is not None:
        print(f"\n[Stage 4.7] AI vision review (advisory)...")
        t47 = time.time()
        from _TEXTURE_STYLE_OF_DEEPSEEK.auto_params.ai_review import ai_review_png
        png_path = os.path.join(OUTPUT_DIR, f"{CITY_NAME}_preview.png")
        if os.path.exists(png_path):
            adjusted_params, review_result = ai_review_png(
                png_path, profile, auto_resolved,
                city_name=CITY_NAME, max_rounds=3)
            if review_result.overall > 0:
                print(f"  AI score: {review_result.overall}/5")
                print(f"  Issues: {review_result.issues}")
                if adjusted_params is not auto_resolved:
                    # Re-apply adjusted params (affects downstream builders
                    # that use function-level imports, e.g. BRICK_* in v3)
                    auto_resolved = adjusted_params
                    _cfg.Z_GAMMA = auto_resolved.z_gamma
                    _cfg.BRICK_PERLIN_AMP = auto_resolved.brick_perlin_amp
                    _cfg.BRICK_CORNER_R_M = auto_resolved.brick_corner_r_m
                    _cfg.ROAD_WIDTH_MULTIPLIER = auto_resolved.road_width_multiplier
                    print(f"  Params adjusted by AI review (effective in downstream builders)")
                    # Re-save decision report with adjusted params (P1-5)
                    save_decision_report(profile, auto_resolved, OUTPUT_DIR,
                                         CITY_NAME + "_ai_adjusted")

                # Trajectory logging (P1-5: 逐轮记录参数/分数)
                _trajectory_path = os.path.join(OUTPUT_DIR, "..", "trajectory.jsonl")
                try:
                    import json as _json
                    _traj_record = {
                        "city": CITY_NAME,
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "score": review_result.overall,
                        "issues": review_result.issues,
                        "params": auto_resolved.to_dict(),
                        "png_path": png_path,
                    }
                    with open(_trajectory_path, "a", encoding="utf-8") as _tf:
                        _tf.write(_json.dumps(_traj_record, ensure_ascii=False) + "\n")
                    print(f"  Trajectory logged: {_trajectory_path}")
                except Exception as _te:
                    print(f"  Trajectory log failed (non-fatal): {_te}")
            else:
                print(f"  AI review skipped (no API key or parse error)")
        else:
            print(f"  PNG not found, skipping AI review")
        print(f"  Time: {time.time() - t47:.1f}s")

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
                printer_profile=printer_profile,
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
                bbox_x_min, bbox_y_min, bbox_x_max, bbox_y_max, scale,
                base_thickness_mm=cli_args.base_thickness_mm)
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
    # Stage 8.5: Build block_base (v3 — PNG layer 1.5 暖米色城市底)
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

    # DesignSpec is an audit sidecar for the exact exported artifact.  It is
    # intentionally written only after a successful 3MF export and does not
    # participate in geometry generation.
    _source_features = {
        "buildings": len(buildings_gdf) if buildings_gdf is not None else 0,
        "roads": len(roads_gdf) if roads_gdf is not None else 0,
        "water": len(water_gdf) if water_gdf is not None else 0,
        "vegetation": len(vegetation_gdf) if vegetation_gdf is not None else 0,
        "landuse": len(landuse_gdf) if landuse_gdf is not None else 0,
    }
    _resolved_params = auto_resolved.to_dict() if auto_resolved is not None else {}
    _decisions = (auto_resolved.reasons if auto_resolved is not None else {})
    _profile = profile.to_dict() if profile is not None else {}
    _block_base_enabled = not cli_args.no_block_base
    _design_spec = build_design_spec(
        city=CITY_NAME,
        bbox_wgs84=(south, west, north, east),
        artifact_path=output_path,
        params={**_resolved_params, **style_overrides,
                "base_thickness_mm": cli_args.base_thickness_mm},
        decisions=_decisions,
        profile=_profile,
        source_features=_source_features,
        printable_features=layer_evidence(layers),
        block_base={
            "requested_mode": "textured" if _block_base_enabled else "off",
            "resolved_mode": "textured" if _block_base_enabled else "off",
            "policy_version": "legacy-explicit-v1",
            "reason": ("enabled by the selected legacy visual profile"
                       if _block_base_enabled else "explicitly disabled by CLI"),
            "metrics": {
                "polygon_count": len(layers.block_base),
                "osm_quality": _profile.get("osm_quality"),
                "building_density_per_km2": _profile.get("building_density"),
            },
            "thresholds": {},
        },
        printability=build_printability_report(
            printer_profile,
            print_scale,
            current_thresholds={
                "preprocess_nozzle_real_m": layers.nozzle_real_m,
                "min_printable_area_m2": layers.min_area_m2,
                "building_height_mm_min": _cfg.BUILDING_HEIGHT_MIN_MM,
                "building_height_mm_max": _cfg.BUILDING_HEIGHT_MAX_MM,
            },
            z_thicknesses_mm={
                "road_thickness_mm": _cfg.ROAD_THICKNESS_MM,
                "water_thickness_mm": _cfg.WATER_THICKNESS_MM,
                "base_thickness_mm": cli_args.base_thickness_mm,
            },
        ),
        road_roles=getattr(layers, "road_roles", {}),
    )
    _design_spec_path = write_design_spec(OUTPUT_DIR, _design_spec)
    print(f"  DesignSpec: {_design_spec_path}")

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
    print(f"  Area: {area_km2:.1f} km², Scale: {scale:.6f} mm/m")
    print(f"  Terrain faces: {len(terrain_solid.faces):,}")
    print(f"  Water faces: {len(water_mesh.faces):,}" if water_mesh is not None else "  Water: None")
    print(f"  Vegetation faces: {len(vegetation_mesh.faces):,}" if vegetation_mesh is not None else "  Vegetation: None")
    print(f"  Buildings faces: {len(buildings_mesh.faces):,}" if buildings_mesh is not None else "  Buildings: None")
    print(f"  Roads faces: {len(roads_mesh.faces):,}" if roads_mesh is not None else "  Roads: None")
    print(f"  Output: {output_path} ({file_size:.2f} MB)")
    print(f"  Total time: {total_time:.1f}s")
    print(f"{'=' * 70}\n")

    # =====================================================================
    # Post: Preference recording hint (auto-params mode)
    # =====================================================================
    if auto_resolved is not None and cli_args.png:
        png_path_final = os.path.join(OUTPUT_DIR, f"{CITY_NAME}_preview.png")
        print(f"  [preference] To record your judgment on this output:")
        print(f"    python -c \"from _TEXTURE_STYLE_OF_DEEPSEEK.auto_params.preference_store import "
              f"PreferenceStore, PreferenceRecord; "
              f"s=PreferenceStore('output/preference_log.jsonl'); "
              f"s.record(PreferenceRecord(city='{CITY_NAME}', "
              f"params={auto_resolved.to_dict()}, "
              f"png_path='{png_path_final}', verdict='accept'))\"")


if __name__ == "__main__":
    main()
