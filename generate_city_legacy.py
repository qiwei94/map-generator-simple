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
import hashlib
import json
import os
import re
import shutil
import sys
import time
from uuid import uuid4
import numpy as np

_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm, project_geodataframe
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osmium_cli_fetcher import fetch_from_cli, fetch_tiled_from_cli, get_cli_fetcher
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.elevation import (
    fetch_elevation_grid,
    fetch_elevation_grid_tiled,
    require_usable_elevation_grid,
)
from _TEXTURE_STYLE_OF_DEEPSEEK._tile_grid import snap_bbox as _snap_bbox
from _TEXTURE_STYLE_OF_DEEPSEEK._pipeline_cache import PipelineCache
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import (
    build_deepseek_terrain,
    build_terrain_evidence,
    resolve_terrain_surface_plan,
    terrain_surface_plan_evidence,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.object4_terrain_with_holes import build_terrain_with_water_holes_manifold
from _TEXTURE_STYLE_OF_DEEPSEEK.buildings import build_deepseek_buildings, build_deepseek_buildings_v3
from _TEXTURE_STYLE_OF_DEEPSEEK.roads import build_deepseek_roads, build_deepseek_roads_v3
from _TEXTURE_STYLE_OF_DEEPSEEK.water import (
    build_deepseek_water,
    build_deepseek_water_v3,
    prepare_deepseek_water_relief,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.vegetation_exclusion import build_deepseek_vegetation_v3
from _TEXTURE_STYLE_OF_DEEPSEEK.block_base import build_deepseek_block_base_v3
from _TEXTURE_STYLE_OF_DEEPSEEK._layer_preprocess import (
    PREPROCESS_POLICY_VERSION,
    preprocess_layers,
)
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
from _TEXTURE_STYLE_OF_DEEPSEEK.water_roles import retain_continuous_water_source
from _TEXTURE_STYLE_OF_DEEPSEEK import config as _cfg
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d import config as _t3d_cfg
from _TEXTURE_STYLE_OF_DEEPSEEK.config import compute_scale, WATERWAY_WIDTHS, TERRAIN_GRID, get_area_class, BUILDING_V2_HOTSPOT_RELAX
from aesthetic.composition_spec import (
    build_composition_spec,
    write_composition_spec,
)
from aesthetic.scene_character import (
    write_scene_character,
)
from aesthetic.scene_policy import (
    write_scene_policy,
)
from aesthetic.pipeline_domain import (
    CarriedFingerprints,
    PipelineContextV3Runtime,
    ProjectedSources,
    RuntimeIdentity,
    RuntimeInputs,
    context_handoff_ledger_value,
    final_layer_counts,
    run_s4_observation,
    run_s5_policy,
    run_s6_building_roles,
    run_s7_review,
    run_s8_mesh_materialization,
    run_s9_mesh_gate,
    require_s10_export_context,
    run_s10_artifact_bundle,
    thaw_json,
    thaw_layer_containers,
)
from aesthetic.pipeline_observation import (
    build_pipeline_observation_report,
    summarize_meshes,
    write_pipeline_observation,
)
from aesthetic.measurement_report import (
    SCHEMA_VERSION as MEASUREMENT_REPORT_SCHEMA_VERSION,
    build_measurement_report,
    write_measurement_report,
)
from aesthetic.pipeline_contract import (
    CONTRACT_VERSION,
    SUPPORTED_MODES,
    feature_source_counts_fingerprint,
)
from aesthetic.pipeline_ledger import PipelineLedger

# Fetch elevation with the same policy recorded in DesignSpec.  The generic
# terrain3d package intentionally has a different standalone default.
_t3d_cfg.ELEVATION_SMOOTHING_SIGMA = _cfg.ELEVATION_SMOOTHING_SIGMA


_ACTIVE_PIPELINE_LEDGER = None


def _resolve_dem_failure(error: BaseException, resolution: int, *,
                         allow_flat_fallback: bool):
    """Fail closed unless a diagnostic run explicitly permits flat DEM."""

    if not allow_flat_fallback:
        raise RuntimeError(
            "DEM acquisition failed; refusing an implicit flat terrain. "
            "Provide --elevation-file or use --allow-flat-dem-fallback only "
            "for an explicit diagnostic run"
        ) from error
    grid = np.zeros((int(resolution), int(resolution)), dtype=np.float64)
    evidence = {
        "status": "flat_fallback",
        "source": "explicit_diagnostic_fallback",
        "flat_fallback": True,
        "shape": list(grid.shape),
        "range_m": [0.0, 0.0],
        "failure_type": type(error).__name__,
    }
    return grid, evidence


def _safe_run_identity(value: str) -> str:
    """Return a filename-safe run identity without exposing user input."""

    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value)).strip("-._")
    return cleaned[:96] or uuid4().hex


def _pipeline_mode(cli_args) -> str:
    requested = os.environ.get("MAP_PIPELINE_MODE", "").strip()
    cli_mode = (
        "review" if getattr(cli_args, "review_only", False)
        else "draft" if getattr(cli_args, "draft", False)
        else "full"
    )
    if not requested:
        return cli_mode
    if requested not in SUPPORTED_MODES:
        raise ValueError(f"unsupported MAP_PIPELINE_MODE: {requested}")
    if requested == cli_mode:
        return requested
    # fetch/styles have no geometry-control CLI switch; their canonical
    # terminal Stage is selected by the trusted job envelope.  Draft/review
    # do change generator control flow and therefore must agree with flags.
    if requested in {"fetch", "styles"} and cli_mode == "full":
        return requested
    raise ValueError(
        "MAP_PIPELINE_MODE disagrees with generator flags: "
        f"environment={requested!r}, cli={cli_mode!r}"
    )


def _start_pipeline_ledger(output_dir: str, cli_args, city: str):
    """Create an attempt-isolated, read-only execution ledger."""

    run_id = _safe_run_identity(
        os.environ.get("MAP_PIPELINE_RUN_ID") or uuid4().hex)
    attempt_id = _safe_run_identity(
        os.environ.get("MAP_PIPELINE_ATTEMPT_ID") or uuid4().hex)
    state_root = os.environ.get("MAP_PIPELINE_STATE_DIR")
    if state_root:
        state_dir = os.path.abspath(state_root)
    else:
        state_dir = os.path.join(output_dir, ".pipeline_runs")
    os.makedirs(state_dir, exist_ok=True)
    state_path = os.path.join(
        state_dir, f"pipeline_state.{run_id}.{attempt_id}.json")
    ledger = PipelineLedger.create(
        state_path,
        output_dir=output_dir,
        run_id=run_id,
        attempt_id=attempt_id,
        mode=_pipeline_mode(cli_args),
        metadata={"city": city, "contract_version": CONTRACT_VERSION},
    )
    return ledger


def _ledger_stage_start(stage_id: str, context=None):
    if _ACTIVE_PIPELINE_LEDGER is not None:
        _ACTIVE_PIPELINE_LEDGER.start_stage(stage_id, context_in=context)


def _absolute_artifact_paths(artifacts):
    """Normalize generator paths at the Ledger boundary.

    The legacy generator constructs project-relative paths such as
    ``output/city/report.json``.  PipelineLedger deliberately interprets a
    relative artifact as relative to the city's output directory.  Convert at
    this adapter boundary so the two conventions can never be concatenated.
    """

    if not artifacts:
        return artifacts
    return {
        str(name): os.path.abspath(os.fspath(path))
        for name, path in artifacts.items()
    }


def _publish_latest_alias(source, destination):
    """Atomically refresh a non-authoritative, human-friendly sidecar name.

    Ledger artifacts always use immutable attempt-scoped filenames.  These
    aliases preserve existing tools and the product requirement that a city
    output contains ``design_spec.json`` without allowing a later same-city
    attempt to invalidate an older ledger's artifact hashes.
    """

    source = os.path.abspath(os.fspath(source))
    destination = os.path.abspath(os.fspath(destination))
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    temporary = f"{destination}.{uuid4().hex}.tmp"
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _canonical_json_fingerprint(value):
    """Return a stable identity for one JSON-safe Stage handoff object."""

    payload = json.dumps(
        value, ensure_ascii=False, allow_nan=False,
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _ledger_stage_complete(stage_id: str, context=None, artifacts=None):
    if _ACTIVE_PIPELINE_LEDGER is not None:
        _ACTIVE_PIPELINE_LEDGER.complete_stage(
            stage_id, context_out=context,
            artifacts=_absolute_artifact_paths(artifacts))


def _fail_active_pipeline(exc: BaseException) -> None:
    ledger = _ACTIVE_PIPELINE_LEDGER
    if ledger is None:
        return
    current = ledger.snapshot().get("current_stage_id")
    if current:
        try:
            ledger.fail_stage(
                str(current),
                error=f"{type(exc).__name__}: {exc}",
            )
        except Exception:
            # Never mask the original generation failure with telemetry.
            pass

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


def _height_store_evidence():
    """Describe the offline height database without creating a missing one."""
    from _TEXTURE_STYLE_OF_DEEPSEEK.config import OVERTURE_CACHE_DIR
    from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.building_height_store import (
        height_store_identity,
    )

    cache_dir = OVERTURE_CACHE_DIR
    if not os.path.isabs(cache_dir):
        cache_dir = os.path.join(_project_root, cache_dir)
    evidence = height_store_identity(
        os.path.join(cache_dir, "building_heights.sqlite3"))
    evidence["path"] = os.path.relpath(evidence["path"], _project_root)
    return evidence

# ---------------------------------------------------------------------------
# CLI 参数
# ---------------------------------------------------------------------------

def parse_args(argv=None):
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
        '--allow-flat-dem-fallback', action='store_true', default=False,
        help=(
            '仅用于诊断：DEM 获取失败时显式允许使用 0m 平面；默认失败关闭，'
            '避免把错误地形当作成功产物'
        ),
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
    vegetation = parser.add_mutually_exclusive_group()
    vegetation.add_argument(
        '--vegetation', dest='no_vegetation', action='store_false',
        help='显式开启植被覆盖层（默认关闭；源数据仍用于场景测量）'
    )
    vegetation.add_argument(
        '--no-vegetation', dest='no_vegetation', action='store_true',
        help='关闭植被覆盖层（默认行为，兼容已有命令）'
    )
    parser.set_defaults(no_vegetation=not _cfg.DEFAULT_VEGETATION_ENABLED)
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
        '--review-only', action='store_true', default=False,
        help='生成 --review-png 后立即退出；需与 --draft 同用，避免构建 3MF/GLB'
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
    parser.add_argument(
        '--amap-salience', choices=('off', 'cache', 'network'),
        default='off',
        help=(
            '高德道路/水体视觉显著性约束：off=关闭（默认），'
            'cache=仅使用已有缓存，network=缓存缺失时下载；'
            '仅在中国大陆适用，不替换 OSM 几何'
        ),
    )
    parser.add_argument(
        '--scene-policy-mode', choices=('audit', 'active'),
        default='active',
        help=(
            '场景策略模式：audit=只写证据，active=仅激活已通过'
            '测量和物理守卫的有界策略（默认 active）'
        ),
    )
    parser.add_argument(
        '--height-emphasis-zones', action='store_true', default=False,
        help=(
            '实验性 A/B：先聚合建筑块，再按测量到的城市强调区选择少量'
            '分层高度主体；不修改道路、水体、地形或全局 Z'
        ),
    )

    args = parser.parse_args(argv)

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


def _load_amap_salience_guide(mode, bbox_wgs84, bbox_local):
    """Resolve optional read-only AMap evidence without making it mandatory."""
    if mode == "off":
        return None, {
            "status": "disabled",
            "reason": "--amap-salience=off",
        }
    from aesthetic.amap_salience import build_amap_salience_guide
    return build_amap_salience_guide(
        bbox_wgs84,
        bbox_local,
        allow_network=(mode == "network"),
    )


def _load_snap_amap_salience_guide(
    mode,
    snap_bbox_wgs84,
    snap_bbox_local,
    exact_bbox_wgs84,
    exact_bbox_local,
):
    """Load snap-frame evidence, then reuse an exact-frame cache if needed.

    Historical diagnostics and gallery batches cached AMap salience against
    the finished exact frame.  The reusable preprocessing path later started
    asking only for the larger snapped fetch frame, which made an otherwise
    valid exact cache look unavailable.  Keep the snapped cache as the first
    choice, but pair an exact raster with the finished frame's own local
    bounds when falling back.  The caller also preprocesses in that exact
    coordinate frame; translating the guide into snap-local coordinates would
    shift every lookup after the sources are clipped back to exact-local.
    """

    guide, evidence = _load_amap_salience_guide(
        mode, snap_bbox_wgs84, snap_bbox_local)
    if guide is not None or mode != "cache":
        return guide, {**evidence, "preprocess_frame": "snap"}

    exact_guide, exact_evidence = _load_amap_salience_guide(
        mode, exact_bbox_wgs84, exact_bbox_local)
    if exact_guide is None:
        return None, {
            **evidence,
            "preprocess_frame": "snap",
            "exact_cache_fallback": {
                "status": exact_evidence.get("status", "unavailable"),
                "reason": exact_evidence.get("reason"),
            },
        }
    return exact_guide, {
        **exact_evidence,
        "preprocess_frame": "exact_within_snap",
        "snap_cache_fallback": True,
        "snap_cache_reason": evidence.get("reason"),
    }


def _amap_salience_cache_fingerprint(mode, evidence):
    """Keep baseline/guided preprocess objects in distinct cache entries."""
    return {
        "mode": str(mode),
        "status": str(evidence.get("status", "unknown")),
        "palette_version": evidence.get("palette_version"),
        "bbox_wgs84": evidence.get("bbox_wgs84"),
    }


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


def _clip_gdf_to_bbox(gdf, bbox):
    """Clip a projected source GeoDataFrame to the finished composition frame.

    Snapped fetches intentionally contain a margin around the requested map so
    their raw OSM data can be reused.  Salience ranking, however, is dependent
    on the final composition frame: candidates outside the finished map must
    not compete with the roads and waterways the customer will actually see.
    """
    if gdf is None or len(gdf) == 0:
        return gdf
    from shapely.geometry import box as _box

    clipped = gdf.clip(_box(*bbox))
    if len(clipped) == 0:
        return clipped
    clipped = clipped[clipped.geometry.notna() & ~clipped.geometry.is_empty]
    return clipped.reset_index(drop=True)


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

    BL, BL_cat, BL_roles = [], [], []
    cats = list(layers.BL_categories)
    roles = list(getattr(layers, "BL_height_roles", ()) or ())
    if len(cats) < len(layers.BL):  # 长度异常时补齐，避免 zip 静默丢弃
        cats.extend([None] * (len(layers.BL) - len(cats)))
    if len(roles) < len(layers.BL):
        roles.extend(["legacy_unspecified"] * (len(layers.BL) - len(roles)))
    for (p, h), cat, role in zip(layers.BL, cats, roles):
        for q in _proc_poly(p):
            BL.append((q, h))
            BL_cat.append(cat)
            BL_roles.append(role)

    def _proc_list(polys):
        out = []
        for p in polys:
            out.extend(_proc_poly(p))
        return out

    def _proc_list_with_values(polys, values):
        out, out_values = [], []
        if len(values) != len(polys):
            return _proc_list(polys), []
        for polygon, value in zip(polys, values):
            parts = _proc_poly(polygon)
            out.extend(parts)
            out_values.extend([value] * len(parts))
        return out, out_values

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
    for item in layers.roads_lines:
        line, tier, flag = item[0], item[1], item[2]
        role = item[3] if len(item) > 3 else None
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
            roads.append((seg, tier, flag, role) if role else
                         (seg, tier, flag))
        elif seg.geom_type == "MultiLineString":
            roads.extend(((g, tier, flag, role) if role else
                          (g, tier, flag))
                         for g in seg.geoms if not g.is_empty)

    layers.BL = BL
    layers.BL_categories = BL_cat
    layers.BL_height_roles = BL_roles
    layers.BO, layers.BO_heights = _proc_list_with_values(
        layers.BO, list(getattr(layers, "BO_heights", ()) or ()))
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


def _run_pipeline():
    global _ACTIVE_PIPELINE_LEDGER
    cli_args = parse_args()
    if not 0.4 <= cli_args.base_thickness_mm <= 3.0:
        raise SystemExit("--base-thickness-mm must be between 0.4 and 3.0")
    if cli_args.preview_fast and not cli_args.draft:
        raise SystemExit("--preview-fast requires --draft")
    if cli_args.review_only and not (cli_args.draft and cli_args.review_png):
        raise SystemExit("--review-only requires --draft --review-png")

    LAT1, LON1, LAT2, LON2 = cli_args.bbox_tuple
    CITY_NAME = cli_args.city
    OUTPUT_DIR = f"output/{CITY_NAME}"
    ENABLE_VEGETATION = not cli_args.no_vegetation
    ENABLE_BLOCK_BASE = not cli_args.no_block_base
    MERGE_BLOCK_LAYERS = cli_args.merge_layers

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    _ACTIVE_PIPELINE_LEDGER = _start_pipeline_ledger(
        OUTPUT_DIR, cli_args, CITY_NAME)
    _artifact_identity = (
        f"{_ACTIVE_PIPELINE_LEDGER.run_id}."
        f"{_ACTIVE_PIPELINE_LEDGER.attempt_id}"
    )
    _ledger_stage_start("S0", {
        "city": CITY_NAME,
        "mode": _ACTIVE_PIPELINE_LEDGER.mode,
        "bbox_request": list(cli_args.bbox_tuple),
    })

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

    # =====================================================================
    # Stage 0: 检查 CLI 工具可用性
    # =====================================================================
    print("\n[Stage 0] Checking CLI tools...")
    fetcher = get_cli_fetcher()
    _osmium_backend = fetcher.osmium_backend_name()
    print(f"  osmium available: {fetcher.osmium_available}")
    print(f"  extraction backend: {_osmium_backend}")

    if not fetcher.osmium_available:
        print("\n  ERROR: osmium CLI not installed!")
        print("  Install: conda install -c conda-forge osmium-tool")
        sys.exit(1)
    else:
        print("  Using osmium-compatible pipeline "
              "(extract → tags-filter → export)")

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
    bbox_local = (bbox_x_min, bbox_y_min, bbox_x_max, bbox_y_max)

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

    _ledger_stage_complete("S0", {
        "city": CITY_NAME,
        "mode": _ACTIVE_PIPELINE_LEDGER.mode,
        "bbox_wgs84": [south, west, north, east],
        "fetch_bbox_wgs84": [fs, fw, fn, fe],
        "area_km2": round(float(area_km2), 6),
        "scale_mm_per_m": round(float(scale), 12),
        "printer_profile_id": printer_profile.profile_id,
        "pbf": os.path.basename(PBF_FILE),
    })
    _ledger_stage_start("S1")

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
        _tiled_dem_sampling_evidence = None
        exact_span = max(north - south, east - west)
        snap_span = max(fn - fs, fe - fw)
        res_fetch = resolution if not snap_active else int(
            (resolution - 1) * snap_span / exact_span) + 1
        if snap_active and not cli_args.elevation_file:
            # 瓦片级高程缓存（Phase 2）：跨网格线偏移也能部分复用
            elevation_grid_snap, _tiled_dem_sampling_evidence = (
                fetch_elevation_grid_tiled(
                    fs, fw, fn, fe, res_fetch,
                    return_evidence=True,
                )
            )
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
        elevation_grid = require_usable_elevation_grid(
            elevation_grid, source="generator S1 DEM")
        _dem_evidence = {
            "status": "ready",
            "source": (
                "local_file" if cli_args.elevation_file
                else "tile_cache_or_network" if snap_active
                else "network_or_cache"
            ),
            "flat_fallback": False,
            "shape": list(elevation_grid.shape),
            "range_m": [
                round(float(np.nanmin(elevation_grid)), 4),
                round(float(np.nanmax(elevation_grid)), 4),
            ],
        }
        if _tiled_dem_sampling_evidence is not None:
            _dem_evidence["tiled_sampling"] = (
                _tiled_dem_sampling_evidence)
        print(f"  Grid shape: {elevation_grid.shape}")
        print(f"  Elevation range: {elevation_grid.min():.1f}m to {elevation_grid.max():.1f}m")
        print(f"  Time: {time.time() - t1b:.1f}s")
    except Exception as e:
        elevation_grid, _dem_evidence = _resolve_dem_failure(
            e, resolution,
            allow_flat_fallback=cli_args.allow_flat_dem_fallback,
        )
        print(f"  WARNING: Elevation fetch failed: {e}")
        print(f"  Explicit diagnostic fallback: flat terrain (0m elevation)")
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

    # =====================================================================
    # Stage 3: Fetch vegetation data (CLI only)
    # =====================================================================
    print(f"\n[Stage 3] Fetching vegetation data...")
    t3 = time.time()

    if cli_args.preview_fast:
        print("  [preview-fast] vegetation source extraction skipped")
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

    else:
        print("  No building features found")
        buildings_gdf = None

    height_store_evidence = _height_store_evidence()
    print("  Height evidence: "
          f"fingerprint={height_store_evidence['fingerprint']}, "
          f"landmarks={height_store_evidence['landmark_height_count']}, "
          f"observations={height_store_evidence['observation_count']}")

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
    else:
        print("  No landuse features found")
        landuse_gdf = None

    _raw_feature_counts = {
        "buildings": len(buildings_gdf) if buildings_gdf is not None else 0,
        "roads": len(roads_gdf) if roads_gdf is not None else 0,
        "water": len(water_gdf) if water_gdf is not None else 0,
        "vegetation": (
            len(vegetation_gdf) if vegetation_gdf is not None else 0),
        "landuse": len(landuse_gdf) if landuse_gdf is not None else 0,
    }
    if sum(_raw_feature_counts.values()) == 0:
        raise RuntimeError(
            "OSM extraction returned zero features across every requested "
            "layer; refusing to treat an empty extract as success"
        )
    _ledger_stage_complete("S1", {
        "raw_feature_counts": _raw_feature_counts,
        "dem_shape": list(elevation_grid.shape),
        "dem_range_m": [
            round(float(np.nanmin(elevation_grid)), 4),
            round(float(np.nanmax(elevation_grid)), 4),
        ],
        "dem_evidence": _dem_evidence,
        "osmium_backend": _osmium_backend,
    })
    if _ACTIVE_PIPELINE_LEDGER.mode == "fetch":
        print("  Fetch-only pipeline complete; S2-S11 are not applicable")
        return
    _ledger_stage_start("S2")

    # Canonical Stage S2 owns every coordinate transform.  Fetch functions
    # above deliberately leave raw WGS84 frames untouched so S1 and S2 no
    # longer interleave hidden state changes.
    print(f"\n[Pipeline S2] Projecting and checking source layers...")
    _projected = {}
    for _name, _gdf in (
        ("water", water_gdf),
        ("vegetation", vegetation_gdf),
        ("buildings", buildings_gdf),
        ("roads", roads_gdf),
        ("landuse", landuse_gdf),
    ):
        if _gdf is None or len(_gdf) == 0:
            _projected[_name] = None
            continue
        _projected[_name] = project_geodataframe(
            _gdf, utm_crs, origin, clip_bbox=utm_bbox)
    water_gdf = _projected["water"]
    vegetation_gdf = _projected["vegetation"]
    buildings_gdf = _projected["buildings"]
    roads_gdf = _projected["roads"]
    landuse_gdf = _projected["landuse"]

    if water_gdf is not None and len(water_gdf) > 0:
        def estimate_water_area(geom, row):
            if geom.geom_type in ['Polygon', 'MultiPolygon']:
                return geom.area
            if geom.geom_type in ['LineString', 'MultiLineString']:
                waterway_type = row.get('waterway', 'river')
                width = WATERWAY_WIDTHS.get(waterway_type, 60)
                return geom.length * width
            return 0

        water_gdf['est_area'] = water_gdf.apply(
            lambda row: estimate_water_area(row.geometry, row), axis=1)
        water_gdf, water_source_roles = retain_continuous_water_source(
            water_gdf)
        print("  Water source roles: "
              f"{water_source_roles['retained_features']}/"
              f"{water_source_roles['source_features']} retained; "
              f"lines={water_source_roles['retained_line_features']}, "
              f"polygons={water_source_roles['retained_polygon_features']}")
        if 'name' in water_gdf.columns:
            named = water_gdf['name'].dropna().unique()
            print(f"  Named water ({len(named)}): {list(named[:10])}")

    if (vegetation_gdf is not None and len(vegetation_gdf) > 0
            and 'name' in vegetation_gdf.columns):
        named_veg = vegetation_gdf['name'].dropna().unique()
        print(f"  Named vegetation ({len(named_veg)}): {list(named_veg[:10])}")
    if (buildings_gdf is not None and len(buildings_gdf) > 0
            and 'name' in buildings_gdf.columns):
        named_bld = buildings_gdf['name'].dropna().unique()
        print(f"  Named buildings ({len(named_bld)}): {list(named_bld[:10])}")

    _projected_feature_counts = {
        name: len(gdf) if gdf is not None else 0
        for name, gdf in _projected.items()
    }
    if sum(_projected_feature_counts.values()) == 0:
        raise RuntimeError(
            "all source features disappeared during projection or bbox "
            "clipping; refusing an empty projected run"
        )

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
        from aesthetic.amap_salience import summarize_amap_urban_evidence
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

        # Cross-source scene evidence must be available before the legacy
        # resolver commits to terrain-first.  It corrects only semantic style
        # and bounded topology tier; OSM vectors remain the geometry source.
        _external_urban_param_evidence = None
        if cli_args.amap_salience != "off":
            try:
                _param_guide, _param_amap_evidence = (
                    _load_amap_salience_guide(
                        cli_args.amap_salience,
                        (south, west, north, east),
                        bbox_local,
                    )
                )
                if _param_guide is not None:
                    _external_urban_param_evidence = (
                        summarize_amap_urban_evidence(
                            _param_guide.reference, grid_size=8)
                    )
                    print(
                        "  Cross-source urban evidence: "
                        f"support={_external_urban_param_evidence['urban_network_support']:.2f}, "
                        f"cells={_external_urban_param_evidence['road_presence_cell_fraction']:.2f}"
                    )
                else:
                    print(
                        "  Cross-source urban evidence unavailable: "
                        f"{_param_amap_evidence.get('reason', 'no guide')}"
                    )
            except Exception as exc:
                print(
                    "  Cross-source urban evidence failed (non-fatal): "
                    f"{type(exc).__name__}: {exc}"
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

        # Activation belongs to the explicit run request, never an old style
        # JSON, AI suggestion, or automatic garden-city classification.
        merged_overrides["vegetation_enabled"] = bool(ENABLE_VEGETATION)
        auto_resolved = resolve_params(
            profile,
            user_overrides=merged_overrides or None,
            external_urban_evidence=_external_urban_param_evidence,
        )
        save_decision_report(profile, auto_resolved, OUTPUT_DIR, CITY_NAME)

        # Apply resolved params to config (runtime monkey-patch for downstream modules)
        _cfg.Z_GAMMA = auto_resolved.z_gamma
        _cfg.TERRAIN_THICKNESS_MM = auto_resolved.terrain_thickness_mm
        # DEM smoothing is a source-stage operation.  The legacy resolver can
        # still recommend a future-run value, but applying it after S1 has
        # already fetched/cached the grid would create a false consumer and
        # leak the setting into the next job in this process.
        if (auto_resolved.elevation_smoothing_sigma !=
                _t3d_cfg.ELEVATION_SMOOTHING_SIGMA):
            print(
                "  Deferred elevation_smoothing_sigma="
                f"{auto_resolved.elevation_smoothing_sigma}; current DEM was "
                "resolved in S1 and will not be mutated retroactively"
            )
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

    # Resolve the complete S2 -> S3 handoff once.  S3 must consume this
    # snapshot instead of rebuilding kwargs from mutable module config or
    # re-querying an external guide after the Stage boundary.
    # This value is geometry-bearing, not merely report metadata.  Omitting
    # it here made ``--merge-layers`` print as enabled while preprocess_layers
    # silently ran the legacy BO aggregation path.  Keep it in the canonical
    # S2 -> S3 handoff and therefore in the cache fingerprint.
    _preprocess_overrides = {
        "merge_mode": bool(MERGE_BLOCK_LAYERS),
    }
    if auto_resolved is not None:
        _preprocess_overrides.update({
            "road_tier_override": auto_resolved.building_v2_road_tier,
            "density_threshold_override": (
                auto_resolved.building_density_threshold),
            "count_threshold_override": (
                auto_resolved.building_count_threshold),
            "print_limit_m2_override": (
                auto_resolved.building_print_limit_m2),
            "road_width_multiplier_override": (
                auto_resolved.road_width_multiplier),
        })
        if auto_resolved.flat_mode:
            _preprocess_overrides["height_mode_override"] = "flat"
    if "bo_mode" in style_overrides:
        _preprocess_overrides["bo_mode_override"] = str(
            style_overrides["bo_mode"])
    if "aggregate_simplify_m" in style_overrides:
        _preprocess_overrides["aggregate_simplify_m_override"] = float(
            style_overrides["aggregate_simplify_m"])
    _hotspot_relax = (
        auto_resolved.building_v2_hotspot_relax
        if auto_resolved is not None else BUILDING_V2_HOTSPOT_RELAX)

    if snap_active:
        _snap_origin = snap_info["origin"]
        _sxoff = origin[0] - _snap_origin[0]
        _syoff = origin[1] - _snap_origin[1]
        _snap_utm_bbox = snap_info["utm_bbox"]
        _snap_bbox_local = (
            _snap_utm_bbox[0] - _snap_origin[0],
            _snap_utm_bbox[1] - _snap_origin[1],
            _snap_utm_bbox[2] - _snap_origin[0],
            _snap_utm_bbox[3] - _snap_origin[1],
        )
        _scale_snap = compute_scale(
            snap_info["width_m"], snap_info["height_m"])
        _amap_guide, _amap_evidence = _load_snap_amap_salience_guide(
            cli_args.amap_salience,
            (fs, fw, fn, fe),
            _snap_bbox_local,
            (south, west, north, east),
            bbox_local,
        )
    else:
        _snap_origin = None
        _sxoff = _syoff = 0.0
        _snap_bbox_local = None
        _scale_snap = None
        _amap_guide, _amap_evidence = _load_amap_salience_guide(
            cli_args.amap_salience,
            (south, west, north, east),
            bbox_local,
        )
    print("[S2 source evidence] amap_salience: "
          f"{_amap_evidence.get('status')} "
          f"({_amap_evidence.get('reason', 'reference ready')})")

    _preprocess_parameters = {
        "schema_version": "preprocess-parameters-v1",
        "policy_version": PREPROCESS_POLICY_VERSION,
        "composition_frame": _amap_evidence.get(
            "preprocess_frame", "snap" if snap_active else "exact"),
        "snap_active": bool(snap_active),
        "exact_scale_mm_per_m": float(scale),
        "snap_scale_mm_per_m": (
            float(_scale_snap) if _scale_snap is not None else None),
        "enable_hotspot": True,
        "hotspot_relax": float(_hotspot_relax),
        "narrow_threshold": float(cli_args.narrow_threshold),
        "narrow_penalty": float(cli_args.narrow_penalty),
        "vegetation_enabled": bool(ENABLE_VEGETATION),
        "block_base_enabled": bool(ENABLE_BLOCK_BASE),
        "merge_block_layers": bool(MERGE_BLOCK_LAYERS),
        "effective_overrides": dict(_preprocess_overrides),
        "resolved_params": (
            auto_resolved.to_dict() if auto_resolved is not None else {}),
        "style_overrides": dict(style_overrides),
        "printer_profile": printer_profile.to_dict(),
        "height_store_fingerprint": height_store_evidence["fingerprint"],
        "amap_source_fingerprint": _amap_salience_cache_fingerprint(
            cli_args.amap_salience, _amap_evidence),
    }
    # Round-trip now so a non-finite or non-serializable Stage input fails at
    # S2, before preprocessing/cache lookup.  The same object and fingerprint
    # are consumed and recorded by S3.
    _preprocess_parameters = json.loads(json.dumps(
        _preprocess_parameters, ensure_ascii=False, allow_nan=False,
        sort_keys=True))
    _preprocess_parameters_fingerprint = _canonical_json_fingerprint(
        _preprocess_parameters)

    # Resolve the semantic terrain surface once.  S6 height ownership, Draft
    # preview evidence and S8 formal mesh all reference this fingerprint;
    # triangles are deliberately not materialized until S8.
    terrain_surface_plan = resolve_terrain_surface_plan(
        elevation_grid,
        width_m,
        height_m,
        scale,
        base_thickness_mm=cli_args.base_thickness_mm,
        max_surface_edge_mm=printer_profile.terrain_max_surface_edge_mm,
        min_surface_height_mm=printer_profile.min_surface_height_mm,
    )
    _terrain_plan_evidence = terrain_surface_plan_evidence(
        terrain_surface_plan)
    _source_feature_counts_fingerprint = feature_source_counts_fingerprint(
        _projected_feature_counts)
    print(
        "  TerrainSurfacePlan: "
        f"{terrain_surface_plan.fingerprint[:12]}… "
        f"grid={terrain_surface_plan.regular_grid_m.shape}"
    )

    _ledger_stage_complete("S2", {
        "projected_feature_counts": _projected_feature_counts,
        "utm_crs": str(utm_crs),
        "bbox_local_m": [round(float(value), 4) for value in bbox_local],
        "auto_profile": profile.to_dict() if profile is not None else {},
        "resolved_parameter_version": (
            getattr(auto_resolved, "version", None)
            if auto_resolved is not None else None),
        "deferred_source_parameters": (
            ["elevation_smoothing_sigma"] if auto_resolved is not None else []),
        "terrain_surface_plan": {
            "fingerprint": terrain_surface_plan.fingerprint,
            "grid_shape": list(terrain_surface_plan.regular_grid_m.shape),
            "surface_z_bounds_mm": _terrain_plan_evidence[
                "surface_z_bounds_mm"],
        },
        "preprocess_parameters": _preprocess_parameters,
        "preprocess_parameters_fingerprint": (
            _preprocess_parameters_fingerprint),
        "source_feature_counts_fingerprint": (
            _source_feature_counts_fingerprint),
        "terrain_surface_fingerprint": terrain_surface_plan.fingerprint,
    })
    _ledger_stage_start("S3")

    # =====================================================================
    # Formal terrain materialization belongs to canonical S8.  Keep only the
    # immutable surface plan here so review/draft/formal share resolved Z.
    # =====================================================================
    terrain_solid = None
    t4 = time.time()

    # =====================================================================
    # Stage 4.5: 5 步预处理（geometry 减法 + 精度过滤）
    # =====================================================================
    print(f"\n[Stage 4.5] Preprocessing layers (subtraction + precision filter)...")
    t45 = time.time()

    _preprocess_overrides = dict(
        _preprocess_parameters["effective_overrides"])
    _hotspot_relax = float(_preprocess_parameters["hotspot_relax"])

    layers = None
    if snap_active:
        # ---- snap 模式：preprocess 在量化框坐标系计算，跨请求缓存复用 ----
        # 命中后平移回本次精确坐标系并裁到精确 bbox。
        from shapely.affinity import translate as _sh_translate
        def _gdf_to_snap(g):
            if g is None or len(g) == 0:
                return g
            g2 = g.copy()
            g2["geometry"] = g2["geometry"].apply(
                lambda geom: _sh_translate(geom, xoff=_sxoff, yoff=_syoff))
            return g2

        _snap_cache = PipelineCache(
            f"snap_{fs:.4f}_{fw:.4f}_{fn:.4f}_{fe:.4f}",
            enabled=not cli_args.no_cache)

        _common_cache_inputs = {
            "snap_bbox": f"{fs:.4f},{fw:.4f},{fn:.4f},{fe:.4f}",
            "preprocess_parameters": _preprocess_parameters_fingerprint,
        }

        if _amap_evidence.get("preprocess_frame") == "exact_within_snap":
            # The raw fetch margin remains reusable, but an exact-frame AMap
            # guide must rank exact-frame candidates.  Ranking the full snap
            # frame changed which complete river group won in Shanghai and
            # left a visible gap after clipping.  Clip raw data first and run
            # all frame-dependent topology/salience decisions in the frame
            # that is actually rendered.
            print("[preprocess] exact-frame guide: clipping reusable snap "
                  "sources before composition")

            def _compute_layers_exact_within_snap():
                return preprocess_layers(
                    buildings_gdf=_clip_gdf_to_bbox(
                        buildings_gdf, bbox_local),
                    roads_gdf=_clip_gdf_to_bbox(roads_gdf, bbox_local),
                    water_gdf=_clip_gdf_to_bbox(water_gdf, bbox_local),
                    vegetation_gdf=_clip_gdf_to_bbox(
                        vegetation_gdf, bbox_local),
                    bbox_local=bbox_local,
                    scale=scale,
                    enable_hotspot=True,
                    hotspot_relax=_hotspot_relax,
                    area_km2=area_km2,
                    landuse_gdf=_clip_gdf_to_bbox(landuse_gdf, bbox_local),
                    narrow_threshold=cli_args.narrow_threshold,
                    narrow_penalty=cli_args.narrow_penalty,
                    bbox_wgs84=(south, west, north, east),
                    utm_crs=utm_crs,
                    origin=origin,
                    printer_profile=printer_profile,
                    amap_salience_guide=_amap_guide,
                    **_preprocess_overrides,
                )

            layers = _snap_cache.get_or_compute(
                "preprocess_exact_v1",
                input_keys={
                    **_common_cache_inputs,
                    "exact_bbox": (
                        f"{south:.7f},{west:.7f},{north:.7f},{east:.7f}"),
                    "composition_frame": "exact_within_snap_v1",
                },
                compute_fn=_compute_layers_exact_within_snap,
                label="preprocess(exact within snap)",
            )
        else:
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
                    amap_salience_guide=_amap_guide,
                    **_preprocess_overrides,
                )

            layers = _snap_cache.get_or_compute(
                "preprocess_v2",
                input_keys=_common_cache_inputs,
                compute_fn=_compute_layers_snap,
                label="preprocess(snap)",
            )

            # snap 坐标系 → 精确坐标系，并裁剪到用户精确 bbox
            _dx = _snap_origin[0] - origin[0]
            _dy = _snap_origin[1] - origin[1]
            layers = _transform_layers_to_exact(
                layers, _dx, _dy, bbox_local)
    else:
        layers = preprocess_layers(
            buildings_gdf=buildings_gdf,
            roads_gdf=roads_gdf,
            water_gdf=water_gdf,
            vegetation_gdf=vegetation_gdf,
            bbox_local=bbox_local,
            scale=scale,
            enable_hotspot=True,
            hotspot_relax=_hotspot_relax,
            area_km2=area_km2,
            landuse_gdf=landuse_gdf,
            narrow_threshold=cli_args.narrow_threshold,
            narrow_penalty=cli_args.narrow_penalty,
            bbox_wgs84=(south, west, north, east),
            utm_crs=utm_crs,
            origin=origin,
            printer_profile=printer_profile,
            amap_salience_guide=_amap_guide,
            **_preprocess_overrides,
        )
    print(f"  {layers.summary()}")
    print(f"  Time: {time.time() - t45:.1f}s")

    # Runtime Contexts carry the non-JSON domain objects that cannot live in
    # PipelineLedger.  From this point onward every production Stage receives
    # exactly its immediate predecessor rather than reaching back into a bag
    # of unrelated locals.
    _domain_context_v3 = PipelineContextV3Runtime(
        runtime=RuntimeInputs(
            identity=RuntimeIdentity(
                run_id=_ACTIVE_PIPELINE_LEDGER.run_id,
                attempt_id=_ACTIVE_PIPELINE_LEDGER.attempt_id,
            ),
            city=CITY_NAME,
            sources=ProjectedSources(
                roads=roads_gdf,
                buildings=buildings_gdf,
                water=water_gdf,
                vegetation=vegetation_gdf,
                landuse=landuse_gdf,
            ),
            bbox_local_m=tuple(float(value) for value in bbox_local),
            bbox_wgs84=(
                float(south), float(west), float(north), float(east)),
            elevation_grid=elevation_grid,
            scale_mm_per_m=float(scale),
            printer_profile=printer_profile,
            terrain_surface_plan=terrain_surface_plan,
            amap_reference=(
                _amap_guide.reference if _amap_guide is not None else None),
            amap_evidence=dict(_amap_evidence or {}),
            fingerprints=CarriedFingerprints(
                preprocess_parameters=_preprocess_parameters_fingerprint,
                source_feature_counts=_source_feature_counts_fingerprint,
                terrain_surface=terrain_surface_plan.fingerprint,
            ),
        ),
        layers=layers,
    )

    _ledger_stage_complete("S3", {
        "layer_counts": {
            "BL": len(layers.BL), "BO": len(layers.BO),
            "WL": len(layers.WL), "WO": len(layers.WO),
            "VL": len(layers.VL), "VO": len(layers.VO),
            "roads": len(layers.roads_lines),
            "block_base": len(layers.block_base),
        },
        "preprocess_policy_version": PREPROCESS_POLICY_VERSION,
        "preprocess_parameters_fingerprint": (
            _preprocess_parameters_fingerprint),
        "source_feature_counts_fingerprint": (
            _source_feature_counts_fingerprint),
        "terrain_surface_fingerprint": terrain_surface_plan.fingerprint,
        "road_role_policy": (layers.road_roles or {}).get("policy_version"),
        "water_role_policy": (layers.water_roles or {}).get("policy_version"),
    })
    _ledger_stage_start("S4")

    # Scene character and policy form the auditable design layer between
    # source evidence and perceptual roles.  Keep the boundaries explicit:
    # S4 only measures, S5 only resolves policy, and S6 is the sole owner of
    # final semantic building polygons/heights.
    print(f"\n[Pipeline S4] Measuring scene character...")
    t455 = time.time()
    _domain_context_v4 = run_s4_observation(_domain_context_v3)
    _scene_character = thaw_json(_domain_context_v4.scene_character)
    _scene_character_fingerprint = (
        _domain_context_v4.scene_character_fingerprint)
    _scene_character_path = write_scene_character(
        OUTPUT_DIR, _scene_character,
        filename=f"scene_character.{_artifact_identity}.json")
    _publish_latest_alias(
        _scene_character_path,
        os.path.join(OUTPUT_DIR, "scene_character.json"),
    )
    print(f"  SceneCharacter: {_scene_character_path}")
    print(f"  Time: {time.time() - t455:.1f}s")
    _ledger_stage_complete(
        "S4",
        {
            **context_handoff_ledger_value(
                _domain_context_v4,
                run_id=_ACTIVE_PIPELINE_LEDGER.run_id,
                attempt_id=_ACTIVE_PIPELINE_LEDGER.attempt_id,
            ),
            "scene_character_version": _scene_character.get("version"),
            "scene_status": _scene_character.get("status", "ready"),
            "source_quality": (
                (_scene_character.get("metrics", {}) or {})
                .get("building_data_quality", {})
            ),
        },
        artifacts={"scene_character": _scene_character_path},
    )

    _ledger_stage_start("S5")
    print(f"\n[Pipeline S5] Resolving bounded scene policy...")
    _domain_context_v5 = run_s5_policy(
        _domain_context_v4,
        activation=(
            "active" if cli_args.scene_policy_mode == "active"
            else "audit_only"
        ),
    )
    _scene_policy = thaw_json(_domain_context_v5.scene_policy)
    _scene_policy_fingerprint = _domain_context_v5.scene_policy_fingerprint
    _scene_policy_path = write_scene_policy(
        OUTPUT_DIR, _scene_policy,
        filename=f"scene_policy.{_artifact_identity}.json")
    _publish_latest_alias(
        _scene_policy_path,
        os.path.join(OUTPUT_DIR, "scene_policy.json"),
    )
    print("  Scene: "
          f"{_scene_policy['scene_class']} / "
          f"{_scene_policy['archetype']} / "
          f"{_scene_policy['dominant_structure']['primary']}")
    print(f"  ScenePolicy: {_scene_policy_path}")
    _ledger_stage_complete(
        "S5",
        {
            **context_handoff_ledger_value(
                _domain_context_v5,
                run_id=_ACTIVE_PIPELINE_LEDGER.run_id,
                attempt_id=_ACTIVE_PIPELINE_LEDGER.attempt_id,
            ),
            "scene_policy_version": _scene_policy.get("policy_version"),
            "activation": _scene_policy.get("activation"),
            "scene_class": _scene_policy.get("scene_class"),
            "archetype": _scene_policy.get("archetype"),
            "dominant_structure": (
                _scene_policy.get("dominant_structure", {}) or {}
            ).get("primary"),
        },
        artifacts={"scene_policy": _scene_policy_path},
    )

    _ledger_stage_start("S6")
    print(f"\n[Pipeline S6] Resolving final building mass and height roles...")
    _domain_context_v6 = run_s6_building_roles(
        _domain_context_v5,
        height_emphasis_zones=bool(cli_args.height_emphasis_zones),
        merge_block_layers=bool(MERGE_BLOCK_LAYERS),
    )
    # S6 owns a cloned set of mutable layer containers.  Rebind all legacy
    # downstream consumers to that exact Context output; the cached S3 object
    # remains untouched and reusable by another request.
    layers = thaw_layer_containers(_domain_context_v6.layers)
    _building_mass_evidence = thaw_json(
        _domain_context_v6.building_mass_evidence)
    _height_hierarchy_evidence = thaw_json(
        _domain_context_v6.height_hierarchy_evidence)
    _height_emphasis_evidence = thaw_json(
        _domain_context_v6.height_emphasis_evidence)
    print(
        "  BuildingMass: "
        f"{_building_mass_evidence['status']} "
        f"({_building_mass_evidence.get('output_components', 0)} components, "
        f"area_gain={_building_mass_evidence.get('area_gain_ratio', 1.0):.2f}x)"
    )
    if cli_args.height_emphasis_zones:
        print(
            "  HeightEmphasis: "
            f"{_height_emphasis_evidence['status']} "
            f"(zones={_height_emphasis_evidence.get('selected_zone_count', 0)}, "
            f"components={_height_emphasis_evidence.get('promoted_component_count', 0)})"
        )
    print(
        "  HeightHierarchy: "
        f"{_height_hierarchy_evidence['status']} "
        f"(heroes={_height_hierarchy_evidence.get('hero_count', len(layers.BL))}, "
        f"max={_height_hierarchy_evidence.get('after_height_mm', {}).get('max', 'n/a')}mm)"
    )
    _ledger_stage_complete("S6", {
        **context_handoff_ledger_value(
            _domain_context_v6,
            run_id=_ACTIVE_PIPELINE_LEDGER.run_id,
            attempt_id=_ACTIVE_PIPELINE_LEDGER.attempt_id,
        ),
        "final_layer_counts": final_layer_counts(layers),
        "building_mass_status": _building_mass_evidence.get("status"),
        "height_hierarchy_status": _height_hierarchy_evidence.get("status"),
    })
    _ledger_stage_start("S7")

    # S7 reports the intended water relief, but no mesh/boolean operation is
    # allowed here.  S8 will materialize the shared TerrainSurfacePlan first
    # and then prepare the printable water surface against that exact mesh.
    water_relief = {
        "surface_levels_mm": [],
        "carved_vertex_count": 0,
        "surface_thickness_mm": printer_profile.min_surface_height_mm,
        "status": "pending_s8_materialization",
    }

    # CompositionSpec is written for both review-only and formal generation.
    # It is an audit sidecar and never feeds geometry, Z, or booleans back into
    # the pipeline.
    _composition_spec = build_composition_spec(
        city=CITY_NAME,
        bbox_wgs84=(south, west, north, east),
        layers=layers,
        amap_evidence=_amap_evidence,
        scene_character=_scene_character,
        scene_policy=_scene_policy,
    )
    _composition_spec_path = write_composition_spec(
        OUTPUT_DIR, _composition_spec,
        filename=f"composition_spec.{_artifact_identity}.json")
    _publish_latest_alias(
        _composition_spec_path,
        os.path.join(OUTPUT_DIR, "composition_spec.json"),
    )
    print(f"  CompositionSpec: {_composition_spec_path}")

    # The measurement report is written before any review/draft early return,
    # then atomically enriched after formal mesh export.  It keeps every raw
    # measurement leaf and separately records which consumers actually use it;
    # the report itself is never read back by geometry code.
    _model_span_mm = max(
        float(bbox_local[2] - bbox_local[0]),
        float(bbox_local[3] - bbox_local[1]),
    ) * float(scale)
    _measurement_run = {
        "run_id": _ACTIVE_PIPELINE_LEDGER.run_id,
        "attempt_id": _ACTIVE_PIPELINE_LEDGER.attempt_id,
        "pipeline_contract_version": CONTRACT_VERSION,
        "ledger_revision": _ACTIVE_PIPELINE_LEDGER.revision,
        "city": CITY_NAME,
        "bbox_wgs84": [south, west, north, east],
        "area_km2": round(float(area_km2), 5),
        "scale_mm_per_m": round(float(scale), 9),
        "model_span_mm": round(float(_model_span_mm), 5),
        "pbf": os.path.basename(PBF_FILE),
        "printer_profile": printer_profile.to_dict(),
        "scene_policy_mode": cli_args.scene_policy_mode,
        "preview_fast": bool(cli_args.preview_fast),
        "draft": bool(cli_args.draft),
        "review_only": bool(getattr(cli_args, "review_only", False)),
        "terrain_surface_fingerprint": terrain_surface_plan.fingerprint,
    }
    _measurement_source_features = {
        "buildings": len(buildings_gdf) if buildings_gdf is not None else 0,
        "roads": len(roads_gdf) if roads_gdf is not None else 0,
        "water": len(water_gdf) if water_gdf is not None else 0,
        "vegetation": (
            len(vegetation_gdf) if vegetation_gdf is not None else 0),
        "landuse": len(landuse_gdf) if landuse_gdf is not None else 0,
    }
    try:
        _pbf_stat = os.stat(PBF_FILE)
        _measurement_pbf_provenance = {
            "basename": os.path.basename(PBF_FILE),
            "size_bytes": int(_pbf_stat.st_size),
            "mtime_ns": int(_pbf_stat.st_mtime_ns),
            "identity": (
                f"{os.path.basename(PBF_FILE)}:"
                f"{int(_pbf_stat.st_size)}:{int(_pbf_stat.st_mtime_ns)}"
            ),
        }
    except OSError as provenance_exc:
        _measurement_pbf_provenance = {
            "basename": os.path.basename(PBF_FILE),
            "status": "unavailable",
            "reason": f"{type(provenance_exc).__name__}: {provenance_exc}",
        }
    _measurement_height_sources = {}
    if (buildings_gdf is not None
            and "height_source" in buildings_gdf.columns):
        _measurement_height_sources = {
            str(source): int(count)
            for source, count in buildings_gdf["height_source"].value_counts(
                dropna=False).items()
        }
    _measurement_printability = build_printability_report(
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
    )
    _measurement_auto_evidence = {
        "profile": profile.to_dict() if profile is not None else {},
        "resolved_params": (
            auto_resolved.to_dict() if auto_resolved is not None else {}),
        "reasons": dict(
            auto_resolved.reasons if auto_resolved is not None else {}),
        "explicit_style_overrides": dict(style_overrides),
    }
    _measurement_preprocess_evidence = {
        "amap_salience": dict(_amap_evidence or {}),
        "road_roles": dict(getattr(layers, "road_roles", {}) or {}),
        "water_roles": dict(getattr(layers, "water_roles", {}) or {}),
        "composition_spec": _composition_spec,
    }
    _measurement_initial_outcomes = {
        "phase": "layers_ready_meshes_pending",
        "printable_features": layer_evidence(
            layers, vegetation_enabled=not cli_args.no_vegetation),
        "building_mass": _building_mass_evidence,
        "height_hierarchy": _height_hierarchy_evidence,
        "height_emphasis": _height_emphasis_evidence,
        "height_sources": _measurement_height_sources,
        "height_store": height_store_evidence,
        "height_mapping": (
            getattr(layers, "building_height_evidence", {}) or {}),
        "terrain": {
            **_terrain_plan_evidence,
            "status": "surface_plan_ready_mesh_pending",
        },
        "water_relief": water_relief,
        "printability": _measurement_printability,
        "validation": {
            "status": "pending",
            "reason": "project validator runs after formal 3MF export",
        },
        "slicer": {
            "status": "pending",
            "reason": "no slicer acceptance has run yet",
        },
    }
    _measurement_report = build_measurement_report(
        run=_measurement_run,
        source_features=_measurement_source_features,
        scene_character=_scene_character,
        scene_policy=_scene_policy,
        auto_parameter_evidence=_measurement_auto_evidence,
        preprocess_evidence=_measurement_preprocess_evidence,
        generation_outcomes=_measurement_initial_outcomes,
        artifacts={
            "scene_character": _scene_character_path,
            "scene_policy": _scene_policy_path,
            "composition_spec": _composition_spec_path,
        },
        provenance={
            "pbf": _measurement_pbf_provenance,
            "height_store": height_store_evidence,
            "dem": {
                **_dem_evidence,
                "cache_identity": (
                    snap_info.get("cache_key") if snap_active else None),
            },
            "cross_source": dict(_amap_evidence or {}),
        },
        acceptance_evidence={
            "validator": _measurement_initial_outcomes["validation"],
            "slicer": _measurement_initial_outcomes["slicer"],
        },
        status=(
            "measurement_error"
            if _scene_character.get("status") == "error" else "measured"),
    )
    _measurement_report_paths = write_measurement_report(
        OUTPUT_DIR, _measurement_report,
        stem=f"pipeline_measurement_report_s7.{_artifact_identity}")
    for _kind, _path in _measurement_report_paths.items():
        _publish_latest_alias(
            _path,
            os.path.join(
                OUTPUT_DIR, f"pipeline_measurement_report_s7.{_kind}"),
        )
    print(f"  Measurement report: {_measurement_report_paths['json']}")
    print(f"  Measurement visualization: "
          f"{_measurement_report_paths['html']}")
    _s7_artifacts = {
        "composition_spec": _composition_spec_path,
        "measurement_report_json": _measurement_report_paths["json"],
        "measurement_report_html": _measurement_report_paths["html"],
    }

    # =====================================================================
    # Stage 4.6: Render PNG preview (optional, --png flag)
    # =====================================================================
    if cli_args.png:
        print(f"\n[Stage 4.6] Rendering PNG preview...")
        t46 = time.time()
        from _TEXTURE_STYLE_OF_DEEPSEEK.render_png import render_from_layers
        png_path = os.path.join(
            OUTPUT_DIR, f"{CITY_NAME}_preview.{_artifact_identity}.png")
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
            vegetation_enabled=ENABLE_VEGETATION,
        )
        _publish_latest_alias(
            png_path, os.path.join(OUTPUT_DIR, f"{CITY_NAME}_preview.png"))
        _s7_artifacts["diagnostic_png"] = png_path
        print(f"  Time: {time.time() - t46:.1f}s")

    # =====================================================================
    # Stage 4.65: 画廊级漂亮俯视图（--review-png）
    # 与 Stage 4.6 的诊断图不同：无文字标注、超采样抗锯齿、带山体阴影，
    # 就是风格画廊用的那张图（aesthetic/review_render.py）。
    # =====================================================================
    if getattr(cli_args, "review_png", False):
        print(f"\n[Stage 4.65] Rendering gallery-grade preview...")
        t465 = time.time()
        from aesthetic.review_render import render_review_bundle
        _rw = (auto_resolved.road_width_multiplier
               if auto_resolved is not None else 1.0)
        bundle = render_review_bundle(
            layers, {"bbox_local": bbox_local, "scale": scale}, _rw,
            OUTPUT_DIR, f"{CITY_NAME}.{_artifact_identity}",
            vegetation_enabled=ENABLE_VEGETATION)
        _topdown_path = bundle.get("topdown")
        if not _topdown_path or not os.path.isfile(_topdown_path):
            raise RuntimeError(
                "requested gallery preview did not produce a topdown artifact")
        _s7_artifacts["review_topdown_png"] = _topdown_path
        _publish_latest_alias(
            _topdown_path,
            os.path.join(OUTPUT_DIR, f"{CITY_NAME}_topdown.png"),
        )
        _height_review_path = bundle.get("height")
        if _height_review_path and os.path.isfile(_height_review_path):
            _s7_artifacts["review_height_png"] = _height_review_path
            _publish_latest_alias(
                _height_review_path,
                os.path.join(OUTPUT_DIR, f"{CITY_NAME}_height.png"),
            )
        print(f"  topdown: {_topdown_path}")
        print(f"  Time: {time.time() - t465:.1f}s")

    if getattr(cli_args, "review_only", False):
        _domain_context_v7 = run_s7_review(
            _domain_context_v6,
            water_relief_intent=water_relief,
            composition_spec=_composition_spec,
            measurement_report=_measurement_report,
            review_artifacts=_s7_artifacts,
            terminal_disposition="review",
        )
        _ledger_stage_complete(
            "S7",
            {
                **context_handoff_ledger_value(
                    _domain_context_v7,
                    run_id=_ACTIVE_PIPELINE_LEDGER.run_id,
                    attempt_id=_ACTIVE_PIPELINE_LEDGER.attempt_id,
                ),
                "review_artifacts": sorted(_s7_artifacts),
                "terminal_mode": _ACTIVE_PIPELINE_LEDGER.mode,
            },
            artifacts=_s7_artifacts,
        )
        total_time = time.time() - t_start
        print(f"  Review-only complete in {total_time:.1f}s; "
              "GLB and 3MF skipped")
        return

    # =====================================================================
    # Stage 4.8: Draft GLB 快速预览（--draft：导出后提前退出）
    # =====================================================================
    if cli_args.draft:
        print(f"\n[Stage 4.8] Exporting draft GLB preview...")
        t48 = time.time()
        from _TEXTURE_STYLE_OF_DEEPSEEK.render_glb import render_glb_preview
        glb_path = os.path.join(
            OUTPUT_DIR, f"{CITY_NAME}_draft.{_artifact_identity}.glb")
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
            markers=marker_local or None,
            water_gdf=water_gdf,
            terrain_surface_plan=terrain_surface_plan,
            vegetation_enabled=ENABLE_VEGETATION,
            preview_quality=("fast" if cli_args.preview_fast else "balanced"),
        )
        _publish_latest_alias(
            glb_path, os.path.join(OUTPUT_DIR, f"{CITY_NAME}_draft.glb"))
        _s7_artifacts["draft_glb"] = glb_path
        glb_size = os.path.getsize(glb_path) / (1024 * 1024)
        total_time = time.time() - t_start
        print(f"\n{'=' * 70}")
        print(f"  DRAFT Summary — {CITY_NAME}")
        print(f"  Output: {glb_path} ({glb_size:.2f} MB)")
        print(f"  Total time: {total_time:.1f}s (draft; full 3MF skipped)")
        print(f"{'=' * 70}\n")
        _domain_context_v7 = run_s7_review(
            _domain_context_v6,
            water_relief_intent=water_relief,
            composition_spec=_composition_spec,
            measurement_report=_measurement_report,
            review_artifacts=_s7_artifacts,
            terminal_disposition="draft",
        )
        _ledger_stage_complete(
            "S7",
            {
                **context_handoff_ledger_value(
                    _domain_context_v7,
                    run_id=_ACTIVE_PIPELINE_LEDGER.run_id,
                    attempt_id=_ACTIVE_PIPELINE_LEDGER.attempt_id,
                ),
                "review_artifacts": sorted(_s7_artifacts),
                "terminal_mode": _ACTIVE_PIPELINE_LEDGER.mode,
            },
            artifacts=_s7_artifacts,
        )
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
        png_path = _s7_artifacts.get("diagnostic_png") or os.path.join(
            OUTPUT_DIR, f"{CITY_NAME}_preview.{_artifact_identity}.png")
        if os.path.exists(png_path):
            adjusted_params, review_result = ai_review_png(
                png_path, profile, auto_resolved,
                city_name=CITY_NAME, max_rounds=3)
            if review_result.overall > 0:
                print(f"  AI score: {review_result.overall}/5")
                print(f"  Issues: {review_result.issues}")
                suggested_params = adjusted_params
                if adjusted_params is not auto_resolved:
                    # S7 is advisory and cannot mutate the S5 policy or the
                    # S6 final polygons behind the preview's back.  Persist a
                    # next-attempt suggestion; adoption must start a new run.
                    print(
                        "  AI suggested parameter changes for a new attempt; "
                        "current S8 will keep the previewed S5/S6 context"
                    )
                    save_decision_report(
                        profile, adjusted_params, OUTPUT_DIR,
                        CITY_NAME + "_ai_suggestion")

                # Trajectory logging (P1-5: 逐轮记录参数/分数)
                _trajectory_path = os.path.join(OUTPUT_DIR, "..", "trajectory.jsonl")
                try:
                    import json as _json
                    _traj_record = {
                        "city": CITY_NAME,
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "score": review_result.overall,
                        "issues": review_result.issues,
                        "params": suggested_params.to_dict(),
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

    _s7_disposition = (
        _ACTIVE_PIPELINE_LEDGER.mode
        if _ACTIVE_PIPELINE_LEDGER.mode in {"styles", "review"}
        else "continue"
    )
    _domain_context_v7 = run_s7_review(
        _domain_context_v6,
        water_relief_intent=water_relief,
        composition_spec=_composition_spec,
        measurement_report=_measurement_report,
        review_artifacts=_s7_artifacts,
        terminal_disposition=_s7_disposition,
    )
    _ledger_stage_complete(
        "S7",
        {
            **context_handoff_ledger_value(
                _domain_context_v7,
                run_id=_ACTIVE_PIPELINE_LEDGER.run_id,
                attempt_id=_ACTIVE_PIPELINE_LEDGER.attempt_id,
            ),
            "review_artifacts": sorted(_s7_artifacts),
            "final_layer_counts": final_layer_counts(layers),
        },
        artifacts=_s7_artifacts,
    )
    if _ACTIVE_PIPELINE_LEDGER.mode in {"styles", "review"}:
        print("  Review pipeline complete; S8-S11 are not applicable")
        return
    _ledger_stage_start("S8")

    # S8 consumes the sealed S7 Context.  These assignments are compatibility
    # aliases for the remaining legacy builders, not a second source of truth.
    layers = thaw_layer_containers(_domain_context_v7.layers)
    _scene_character = thaw_json(_domain_context_v7.scene_character)
    _scene_policy = thaw_json(_domain_context_v7.scene_policy)
    water_relief = thaw_json(_domain_context_v7.water_relief_intent)

    # =====================================================================
    # Canonical S8: materialize the exact terrain plan consumed by S6, then
    # build every requested semantic mesh.  No builder is allowed to turn a
    # requested non-empty role into a warning-and-skip success.
    # =====================================================================
    print(f"\n[Pipeline S8] Materializing semantic mesh bundle...")
    t4 = time.time()
    terrain_solid = build_deepseek_terrain(
        elevation_grid,
        width_m,
        height_m,
        area_km2,
        scale,
        water_gdf,
        base_thickness_mm=cli_args.base_thickness_mm,
        max_surface_edge_mm=printer_profile.terrain_max_surface_edge_mm,
        min_surface_height_mm=printer_profile.min_surface_height_mm,
        surface_plan=terrain_surface_plan,
    )
    print(
        f"  Terrain: {len(terrain_solid.faces):,} faces, "
        f"surface={terrain_surface_plan.fingerprint[:12]}…"
    )
    if layers.WL or layers.WO:
        water_relief = prepare_deepseek_water_relief(
            terrain_solid,
            layers.WL,
            layers.WO,
            scale,
            base_thickness_mm=cli_args.base_thickness_mm,
            surface_thickness_mm=printer_profile.min_surface_height_mm,
        )
        water_relief["status"] = "materialized"
    else:
        water_relief["status"] = "not_applicable"

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
                raise RuntimeError(
                    "Landmark mesh build failed; refusing an incomplete "
                    f"formal artifact: {e}") from e
    elif layers.BL or layers.BO:
        try:
            bldg_result = build_deepseek_buildings_v3(
                layers.BL, layers.BO, terrain_solid, scale,
                bbox_local=bbox_local,
                BO_heights=(getattr(layers, "BO_heights", None) or None))
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
            raise RuntimeError(
                "Building mesh build failed; refusing an incomplete formal "
                f"artifact: {e}") from e
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
            raise RuntimeError(
                "Road mesh build failed; refusing an incomplete formal "
                f"artifact: {e}") from e
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
                flat_only=False,
                base_thickness_mm=cli_args.base_thickness_mm,
                surface_levels_mm=water_relief["surface_levels_mm"],
                surface_thickness_mm=water_relief["surface_thickness_mm"],
            )
            if water_mesh is not None:
                print(f"  Water faces: {len(water_mesh.faces):,}")
            else:
                print(f"  No water features generated")
        except Exception as e:
            raise RuntimeError(
                "Water mesh build failed; refusing an incomplete formal "
                f"artifact: {e}") from e
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
        print("  Vegetation DISABLED (default; opt in with --vegetation)")
    elif layers.VL or layers.VO:
        try:
            vegetation_mesh = build_deepseek_vegetation_v3(
                layers.VL,
                layers.VO,
                terrain_solid,
                scale,
                max_surface_edge_mm=(
                    2.0 * printer_profile.extrusion_width_mm),
            )
            if vegetation_mesh is not None:
                print(f"  Vegetation faces: {len(vegetation_mesh.faces):,}")
            else:
                print(f"  No vegetation features generated")
        except Exception as e:
            raise RuntimeError(
                f"Vegetation processing failed; refusing an incomplete "
                f"formal artifact: {e}") from e
    else:
        print(f"  No vegetation data available")
    print(f"  Time: {time.time() - t8:.1f}s")

    # =====================================================================
    # Stage 8.5: Build block_base (v3 — PNG layer 1.5 暖米色城市底)
    # =====================================================================
    print(f"\n[Stage 8.5] Building block_base (v3 — city tessellation)...")
    t85 = time.time()

    block_base_mesh = None
    block_base_clearance_evidence = None
    if not ENABLE_BLOCK_BASE:
        print(f"  BlockBase DISABLED via --no-block-base")
    elif layers.block_base or (MERGE_BLOCK_LAYERS and layers.BO):
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
            block_base_mesh, block_base_clearance_evidence = build_deepseek_block_base_v3(
                merged_polys, terrain_solid, scale,
                bbox_local=bbox_local, thickness_mm=merge_thickness,
                block_classes=merged_classes,
                clearance_lines=layers.block_base_cut_lines,
                final_clearance_mm=printer_profile.final_block_base_gap_mm,
                major_clearance_lines=getattr(
                    layers, "block_base_major_cut_lines", []),
                surface_clearance_mm=printer_profile.surface_road_gap_mm,
                return_clearance_evidence=True)
            if block_base_clearance_evidence is not None:
                block_base_clearance_evidence.update({
                    "printer_profile_id": printer_profile.profile_id,
                    "configured_min_gap_mm": printer_profile.min_gap_mm,
                    "extrusion_width_mm": printer_profile.extrusion_width_mm,
                    "surface_road_gap_mm": (
                        printer_profile.surface_road_gap_mm),
                    "derivation": (
                        "local=min_gap_mm over continuous substrate; "
                        "major=max(min_gap_mm,2*extrusion_width_mm)"),
                })
            if block_base_mesh is not None:
                print(f"  BlockBase faces: {len(block_base_mesh.faces):,}")
            else:
                print(f"  No block_base mesh generated")
        except Exception as e:
            print(f"  BlockBase processing failed; aborting formal artifact: {e}")
            # A requested formal Block base without a proven final road seam
            # is not a successful artifact.  Do not silently export a model
            # whose DesignSpec would claim a layer that is absent.
            raise
    else:
        print(f"  No block_base polygons available")
    print(f"  Time: {time.time() - t85:.1f}s")

    # =====================================================================
    # Canonical S9: fail-closed printable mesh gate
    # =====================================================================
    meshes = {
        'terrain': terrain_solid,
        'buildings': buildings_mesh,
        'landmarks': landmarks_mesh,
        'roads': roads_mesh,
        'water': water_mesh,
        'vegetation': vegetation_mesh,
        'block_base': block_base_mesh,
    }
    _domain_context_v8 = run_s8_mesh_materialization(
        _domain_context_v7,
        meshes=meshes,
        water_relief=water_relief,
        block_base_clearance=block_base_clearance_evidence,
        source_feature_counts=_projected_feature_counts,
        vegetation_enabled=ENABLE_VEGETATION,
        block_base_enabled=ENABLE_BLOCK_BASE,
        merge_block_layers=MERGE_BLOCK_LAYERS,
    )
    _ledger_stage_complete(
        "S8",
        {
            **context_handoff_ledger_value(
                _domain_context_v8,
                run_id=_ACTIVE_PIPELINE_LEDGER.run_id,
                attempt_id=_ACTIVE_PIPELINE_LEDGER.attempt_id,
            ),
        },
    )
    _ledger_stage_start("S9")
    print(f"\n[Pipeline S9] Checking printable mesh bundle...")
    _domain_context_v9 = run_s9_mesh_gate(_domain_context_v8)
    _geometry_gate = thaw_json(_domain_context_v9.geometry_gate)
    _required_mesh_roles = list(_domain_context_v8.required_roles)
    print(
        "  S9 PASS: "
        f"{', '.join(_required_mesh_roles)}; 0 errors / 0 warnings"
    )
    _ledger_stage_complete(
        "S9",
        {
            **context_handoff_ledger_value(
                _domain_context_v9,
                run_id=_ACTIVE_PIPELINE_LEDGER.run_id,
                attempt_id=_ACTIVE_PIPELINE_LEDGER.attempt_id,
            ),
        },
    )
    _ledger_stage_start("S10")

    # Recheck the exact V9 bundle immediately before exporter and file-system
    # effects.  The returned mapping contains the same opaque mesh handles;
    # no city-scale mesh copy is made.
    meshes = require_s10_export_context(_domain_context_v9)

    # =====================================================================
    # Canonical S10: export the 3MF and its audit sidecars.  S10 success is
    # generation only; S11 remains pending until validator+slicer acceptance.
    # =====================================================================
    print(f"\n[Pipeline S10] Preparing and exporting artifact bundle...")
    t9 = time.time()

    print(f"  Terrain solid faces: {len(terrain_solid.faces):,} (single closed mesh)")

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
    _output_name = (
        f"full_{CITY_NAME}{_suffix}_{_timestamp}."
        f"{_artifact_identity}.3mf"
    )
    output_path = os.path.join(OUTPUT_DIR, _output_name)
    _design_spec_name = f"design_spec.{_artifact_identity}.json"
    _measurement_stem = (
        f"pipeline_measurement_report.{_artifact_identity}")
    _observation_stem = f"pipeline_observation.{_artifact_identity}"
    _publish_dir = os.path.join(
        OUTPUT_DIR, f".publish.{_ACTIVE_PIPELINE_LEDGER.attempt_id}")
    os.makedirs(_publish_dir, exist_ok=True)
    _staged_output_path = os.path.join(_publish_dir, _output_name)
    export_deepseek_3mf(meshes, _staged_output_path)

    # DesignSpec is an audit sidecar for the exact exported artifact.  It is
    # intentionally written only after a successful 3MF export and does not
    # participate in geometry generation.
    _source_features = dict(_measurement_source_features)
    _resolved_params = auto_resolved.to_dict() if auto_resolved is not None else {}
    _decisions = dict(
        auto_resolved.reasons if auto_resolved is not None else {})
    _decisions["composition_spec"] = {
        "filename": os.path.basename(_composition_spec_path),
        "schema_version": _composition_spec["schema_version"],
        "policy_version": _composition_spec["policy_version"],
    }
    _decisions["scene_character"] = {
        "filename": os.path.basename(_scene_character_path),
        "version": _scene_character.get("version"),
        "status": _scene_character.get("status", "ready"),
    }
    _decisions["scene_policy"] = {
        "filename": (os.path.basename(_scene_policy_path)
                     if _scene_policy_path else None),
        "policy_version": _scene_policy.get("policy_version"),
        "activation": _scene_policy.get("activation"),
        "archetype": _scene_policy.get("archetype"),
    }
    _decisions["building_mass_strategy"] = _building_mass_evidence
    _decisions["building_height_hierarchy"] = _height_hierarchy_evidence
    _decisions["height_emphasis_zones"] = _height_emphasis_evidence
    _decisions["pipeline_contract"] = {
        "contract_version": CONTRACT_VERSION,
        "run_id": _ACTIVE_PIPELINE_LEDGER.run_id,
        "attempt_id": _ACTIVE_PIPELINE_LEDGER.attempt_id,
        "terrain_surface_fingerprint": terrain_surface_plan.fingerprint,
    }
    _decisions["s9_geometry_gate"] = _geometry_gate
    _decisions["pipeline_observation"] = {
        "schema_version": "pipeline-observation-v1",
        "json": f"{_observation_stem}.json",
        "html": f"{_observation_stem}.html",
        "role": "post-run evidence and strategy visualization",
    }
    _decisions["measurement_report"] = {
        "schema_version": MEASUREMENT_REPORT_SCHEMA_VERSION,
        "json": f"{_measurement_stem}.json",
        "html": f"{_measurement_stem}.html",
        "role": (
            "lossless measurement index plus measurement-to-policy-to-"
            "consumer impact chains"
        ),
    }
    _profile = profile.to_dict() if profile is not None else {}
    _block_base_enabled = not cli_args.no_block_base
    _printable_features = layer_evidence(
        layers, vegetation_enabled=not cli_args.no_vegetation)
    _printable_features.update({
        "water_mesh_faces": len(water_mesh.faces) if water_mesh is not None else 0,
        "water_relief_shells": len(water_relief["surface_levels_mm"]),
        "water_carved_terrain_vertices": water_relief["carved_vertex_count"],
    })
    _height_sources = dict(_measurement_height_sources)
    _design_spec = build_design_spec(
        city=CITY_NAME,
        bbox_wgs84=(south, west, north, east),
        artifact_path=_staged_output_path,
        params={**_resolved_params, **style_overrides,
                "base_thickness_mm": cli_args.base_thickness_mm,
                "vegetation_enabled": not cli_args.no_vegetation},
        decisions=_decisions,
        profile=_profile,
        source_features=_source_features,
        printable_features=_printable_features,
        height_sources=_height_sources,
        height_evidence={
            "store": height_store_evidence,
            "mapping": getattr(layers, "building_height_evidence", {}) or {},
        },
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
            "final_clearance": block_base_clearance_evidence,
        },
        printability=_measurement_printability,
        road_roles=getattr(layers, "road_roles", {}),
        water_roles=getattr(layers, "water_roles", {}),
        scene_policy=_scene_policy,
        terrain=build_terrain_evidence(terrain_solid),
    )
    _staged_design_spec_path = write_design_spec(_publish_dir, _design_spec)
    _design_spec_path = os.path.join(OUTPUT_DIR, _design_spec_name)
    print(f"  DesignSpec staged: {_staged_design_spec_path}")

    _measurement_final_artifacts = {
        "3mf": output_path,
        "design_spec": _design_spec_path,
        "composition_spec": _composition_spec_path,
        "scene_character": _scene_character_path,
        "scene_policy": _scene_policy_path,
        "pipeline_measurement_report_json": os.path.join(
            OUTPUT_DIR, f"{_measurement_stem}.json"),
        "pipeline_measurement_report_html": os.path.join(
            OUTPUT_DIR, f"{_measurement_stem}.html"),
    }
    _measurement_run["ledger_revision"] = _ACTIVE_PIPELINE_LEDGER.revision
    _measurement_report = build_measurement_report(
        run=_measurement_run,
        source_features=_source_features,
        scene_character=_scene_character,
        scene_policy=_scene_policy,
        auto_parameter_evidence=_measurement_auto_evidence,
        preprocess_evidence=_measurement_preprocess_evidence,
        generation_outcomes={
            "phase": "artifact_exported_validation_pending",
            "printable_features": _printable_features,
            "building_mass": _building_mass_evidence,
            "height_hierarchy": _height_hierarchy_evidence,
            "height_emphasis": _height_emphasis_evidence,
            "height_sources": _height_sources,
            "height_store": height_store_evidence,
            "height_mapping": (
                getattr(layers, "building_height_evidence", {}) or {}),
            "terrain": build_terrain_evidence(terrain_solid),
            "water_relief": water_relief,
            "meshes": summarize_meshes(meshes),
            "block_base_clearance": block_base_clearance_evidence,
            "geometry_gate": _geometry_gate,
            "printability": _measurement_printability,
            "validation": {
                "status": "pending",
                "reason": (
                    "project validator and slicer acceptance run after export; "
                    "a successful export alone is not acceptance"),
            },
            "slicer": {
                "status": "pending",
                "reason": "slicer acceptance has not run automatically",
            },
        },
        artifacts=_measurement_final_artifacts,
        provenance={
            "pbf": _measurement_pbf_provenance,
            "height_store": height_store_evidence,
            "dem": {
                **_dem_evidence,
                "cache_identity": (
                    snap_info.get("cache_key") if snap_active else None),
            },
            "cross_source": dict(_amap_evidence or {}),
        },
        acceptance_evidence={
            "validator": {
                "status": "pending",
                "reason": "project validator has not run yet",
            },
            "slicer": {
                "status": "pending",
                "reason": "slicer acceptance has not run yet",
            },
        },
        status="generated_pending_validation",
    )
    _measurement_report_paths = write_measurement_report(
        _publish_dir, _measurement_report)
    print(f"  Measurement report staged: "
          f"{_measurement_report_paths['json']}")

    _ledger_snapshot = _ACTIVE_PIPELINE_LEDGER.snapshot()
    _staged_stage_statuses = {
        item["id"]: {
            "status": item["status"],
            "status_source": {
                "kind": "pipeline_ledger_snapshot",
                "reference": (
                    f"pipeline_state revision {_ledger_snapshot['revision']}"),
                "rule": (
                    "this report was authored before the S10 commit; staged "
                    "artifacts do not become completed until the ledger "
                    "transaction succeeds"),
            },
        }
        for item in _ledger_snapshot["stages"]
    }

    _observation_report = build_pipeline_observation_report(
        run=_measurement_run,
        source_features=_source_features,
        printable_features=_printable_features,
        composition_spec=_composition_spec,
        scene_character=_scene_character,
        scene_policy=_scene_policy,
        building_mass_evidence=_building_mass_evidence,
        height_hierarchy_evidence=_height_hierarchy_evidence,
        height_emphasis_evidence=_height_emphasis_evidence,
        meshes=meshes,
        block_base_clearance=block_base_clearance_evidence,
        geometry_gate=_geometry_gate,
        artifacts={
            "3mf": output_path,
            "design_spec": _design_spec_path,
            "composition_spec": _composition_spec_path,
            "scene_character": _scene_character_path,
            "scene_policy": _scene_policy_path,
            "pipeline_observation_json": os.path.join(
                OUTPUT_DIR, f"{_observation_stem}.json"),
            "pipeline_observation_html": os.path.join(
                OUTPUT_DIR, f"{_observation_stem}.html"),
            # The observation is authored while the bundle is still staged,
            # but its links describe the published S10 bundle.  Never expose
            # the hidden attempt directory as the durable artifact identity.
            "measurement_report_json": os.path.join(
                OUTPUT_DIR, f"{_measurement_stem}.json"),
            "measurement_report_html": os.path.join(
                OUTPUT_DIR, f"{_measurement_stem}.html"),
        },
        validation={
            "status": "pending",
            "reason": (
                "project validator and slicer acceptance run after export; "
                "a successful export alone is not acceptance"),
        },
        contract_version=CONTRACT_VERSION,
        run_id=_ACTIVE_PIPELINE_LEDGER.run_id,
        attempt_id=_ACTIVE_PIPELINE_LEDGER.attempt_id,
        revision=_ledger_snapshot["revision"],
        stage_statuses=_staged_stage_statuses,
    )
    _observation_paths = write_pipeline_observation(
        _publish_dir, _observation_report)
    print(f"  Pipeline observation staged: {_observation_paths['json']}")

    # Publish sidecars first and the downloadable 3MF last.  Any failure above
    # leaves only an attempt-scoped hidden staging directory, never a new
    # apparently-successful 3MF without its DesignSpec and reports.
    _final_measurement_paths = {
        "json": os.path.join(OUTPUT_DIR, f"{_measurement_stem}.json"),
        "html": os.path.join(OUTPUT_DIR, f"{_measurement_stem}.html"),
    }
    _final_observation_paths = {
        "json": os.path.join(OUTPUT_DIR, f"{_observation_stem}.json"),
        "html": os.path.join(OUTPUT_DIR, f"{_observation_stem}.html"),
    }
    for _source, _destination in (
        (_staged_design_spec_path, _design_spec_path),
        (_measurement_report_paths["json"], _final_measurement_paths["json"]),
        (_measurement_report_paths["html"], _final_measurement_paths["html"]),
        (_observation_paths["json"], _final_observation_paths["json"]),
        (_observation_paths["html"], _final_observation_paths["html"]),
        (_staged_output_path, output_path),
    ):
        os.replace(_source, _destination)
    _measurement_report_paths = _final_measurement_paths
    _observation_paths = _final_observation_paths
    for _source, _alias in (
        (_design_spec_path, os.path.join(OUTPUT_DIR, "design_spec.json")),
        (_measurement_report_paths["json"], os.path.join(
            OUTPUT_DIR, "pipeline_measurement_report.json")),
        (_measurement_report_paths["html"], os.path.join(
            OUTPUT_DIR, "pipeline_measurement_report.html")),
        (_observation_paths["json"], os.path.join(
            OUTPUT_DIR, "pipeline_observation.json")),
        (_observation_paths["html"], os.path.join(
            OUTPUT_DIR, "pipeline_observation.html")),
    ):
        _publish_latest_alias(_source, _alias)
    print(f"  Pipeline observation: {_observation_paths['json']}")
    print(f"  Pipeline visualization: {_observation_paths['html']}")

    _s10_artifact_paths = {
        "3mf": output_path,
        "design_spec": _design_spec_path,
        "measurement_report_json": _measurement_report_paths["json"],
        "measurement_report_html": _measurement_report_paths["html"],
        "pipeline_observation_json": _observation_paths["json"],
        "pipeline_observation_html": _observation_paths["html"],
    }
    _domain_context_v10 = run_s10_artifact_bundle(
        _domain_context_v9,
        artifact_paths=_s10_artifact_paths,
    )
    _ledger_stage_complete(
        "S10",
        {
            **context_handoff_ledger_value(
                _domain_context_v10,
                run_id=_ACTIVE_PIPELINE_LEDGER.run_id,
                attempt_id=_ACTIVE_PIPELINE_LEDGER.attempt_id,
            ),
        },
        artifacts=_s10_artifact_paths,
    )

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


def main():
    """Run the generator and durably mark an interrupted Stage as failed."""

    try:
        return _run_pipeline()
    except BaseException as exc:
        _fail_active_pipeline(exc)
        raise


if __name__ == "__main__":
    main()
