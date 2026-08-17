"""缓存感知最小重算底座（闭环核心）.

一次性准备（冷）:
    bbox → fetch gdfs（terrain3d fetchers，自带 tile cache + est_height 富化）
    → 投影 → elevation（自带 npy tile cache，失败回退平面）
    → CityProfile → resolve_params（基线参数）
    以上结果用 PipelineCache 持久化，重复运行秒级命中。

每轮重算（热）:
    只跑 preprocess_layers（带 override），按参数指纹缓存。
    不建 mesh、不建地形（地形参数前置定死；评审纯 2D 不需要 Z）。
"""

import os
import sys
import time

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import numpy as np
import geopandas as gpd
import pandas as pd

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import (
    bbox_to_utm, project_geodataframe,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.elevation import (
    fetch_elevation_grid, fetch_elevation_grid_tiled,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import (
    fetch_buildings, fetch_roads, fetch_water, fetch_vegetation,
)
from _TEXTURE_STYLE_OF_DEEPSEEK import config as _cfg
from _TEXTURE_STYLE_OF_DEEPSEEK.config import (
    TERRAIN_GRID, get_area_class, compute_scale,
)
from _TEXTURE_STYLE_OF_DEEPSEEK._layer_preprocess import preprocess_layers
from _TEXTURE_STYLE_OF_DEEPSEEK._pipeline_cache import PipelineCache
from _TEXTURE_STYLE_OF_DEEPSEEK.auto_params.city_profile import detect_city_profile
from _TEXTURE_STYLE_OF_DEEPSEEK.auto_params.param_resolver import resolve_params

from .presets import CityPreset


class CityHarness:
    """单城市闭环底座：prepare 一次，run_round 多次。"""

    def __init__(self, preset: CityPreset, use_cache: bool = True):
        self.preset = preset
        self.cache = PipelineCache(f"{preset.name}_aesthetic", enabled=use_cache)
        self.ctx: dict = {}
        self.profile = None
        self.base_params = None      # ResolvedParams（规则引擎基线）
        self._prepared = False

    # ─── 一次性准备 ──────────────────────────────────────────────────

    def prepare(self) -> dict:
        if self._prepared:
            return self.ctx
        t0 = time.time()
        p = self.preset
        print(f"\n[harness] prepare: {p.name} ({p.prototype}) bbox={p.bbox}")

        # 指定 PBF（terrain3d fetchers 读 OSM_PBF_FILE）
        os.environ["OSM_PBF_FILE"] = p.pbf_abs

        south, west, north, east = p.bbox
        bbox = bbox_to_utm(south, west, north, east)
        area_km2 = bbox["area_km2"]
        area_class = get_area_class(area_km2)
        resolution = TERRAIN_GRID.get(area_class, 512)
        scale = compute_scale(bbox["width_m"], bbox["height_m"])
        utm_crs, origin, utm_bbox = bbox["utm_crs"], bbox["origin"], bbox["utm_bbox"]
        bbox_local = (utm_bbox[0] - origin[0], utm_bbox[1] - origin[1],
                      utm_bbox[2] - origin[0], utm_bbox[3] - origin[1])

        # ── gdfs（缓存：PBF 提取很贵）──
        def _fetch_all():
            return {
                "buildings": fetch_buildings(south, west, north, east),
                "roads": fetch_roads(south, west, north, east),
                "water": fetch_water(south, west, north, east),
                "vegetation": fetch_vegetation(south, west, north, east),
            }

        gdfs = self.cache.get_or_compute(
            "gdfs_v1", {"bbox": p.bbox, "pbf": os.path.basename(p.pbf)},
            _fetch_all, label="fetch gdfs")

        # 投影到本地坐标（origin 相对）
        for key in ("buildings", "roads", "water", "vegetation"):
            g = gdfs.get(key)
            if g is not None and len(g) > 0:
                gdfs[key] = project_geodataframe(g, utm_crs, origin,
                                                 clip_bbox=utm_bbox)

        # 国内山水区域的 OSM 水面经常只有零碎 polygon/中心线。画廊若只
        # 看 OSM 会把千岛湖一类区域误判成“低水体城市”。高德无标注图层
        # 是产品已有的第二地图源；这里一次提取后放进 ctx，2D 与 GLB 共用。
        amap_water_polys = []
        try:
            from _TEXTURE_STYLE_OF_DEEPSEEK._water_supplement import (
                fetch_amap_water_local,
            )
            amap_water_polys = fetch_amap_water_local(
                p.bbox, utm_crs, origin, bbox_local=bbox_local)
        except Exception as e:
            print(f"  [harness] secondary water unavailable: {e}")

        if amap_water_polys:
            water = gdfs.get("water")
            water_crs = getattr(water, "crs", None) or utm_crs
            supplement = gpd.GeoDataFrame(
                {
                    "natural": ["water"] * len(amap_water_polys),
                    "water": ["lake"] * len(amap_water_polys),
                    "source": ["amap_nolabel"] * len(amap_water_polys),
                    "geometry": amap_water_polys,
                },
                geometry="geometry", crs=water_crs,
            )
            if water is None or len(water) == 0:
                gdfs["water"] = supplement
            else:
                gdfs["water"] = gpd.GeoDataFrame(
                    pd.concat([water, supplement], ignore_index=True,
                              sort=False),
                    geometry="geometry", crs=water_crs,
                )
            print(f"  [harness] secondary water: +{len(amap_water_polys)} "
                  "full-shape polygons")

        # ── elevation（瓦片级 npy 缓存，偏移请求可部分复用；失败回退平面）──
        try:
            tiled_grid = fetch_elevation_grid_tiled(
                south, west, north, east, resolution)
            # 瓦片拼接网格覆盖量化框；重采样回用户精确框
            # （形状算法与 fetch_elevation_grid 内部一致）
            from _TEXTURE_STYLE_OF_DEEPSEEK._tile_grid import snap_bbox
            from scipy.ndimage import map_coordinates
            fs, fw, fn, fe = snap_bbox(south, west, north, east)
            lat_span, lon_span = north - south, east - west
            if lat_span >= lon_span:
                tr, tc = resolution, max(2, int(resolution * lon_span / lat_span))
            else:
                tr, tc = max(2, int(resolution * lat_span / lon_span)), resolution
            rows, cols = tiled_grid.shape
            r = np.linspace((south - fs) / (fn - fs) * (rows - 1),
                            (north - fs) / (fn - fs) * (rows - 1), tr)
            c = np.linspace((west - fw) / (fe - fw) * (cols - 1),
                            (east - fw) / (fe - fw) * (cols - 1), tc)
            rr, cc = np.meshgrid(r, c, indexing="ij")
            elevation_grid = map_coordinates(
                tiled_grid, [rr, cc], order=1,
                mode="nearest").astype(tiled_grid.dtype, copy=False)
        except Exception as e:
            print(f"  [harness] elevation fetch failed ({e}); "
                  f"fallback to flat grid (profile relief=flat)")
            elevation_grid = np.zeros((resolution, resolution),
                                      dtype=np.float32)

        # ── 城市画像 + 规则引擎基线 ──
        self.profile = detect_city_profile(
            bbox_area_km2=area_km2,
            elevation_grid=elevation_grid,
            buildings_gdf=gdfs["buildings"],
            roads_gdf=gdfs["roads"],
            water_gdf=gdfs["water"],
            vegetation_gdf=gdfs["vegetation"],
            bbox_local_area_m2=bbox["width_m"] * bbox["height_m"],
        )
        self.base_params = resolve_params(self.profile)
        print(f"  [harness] profile: relief={self.profile.relief_ratio}, "
              f"water={self.profile.water_ratio:.2f}, "
              f"density={self.profile.building_density:.0f}/km2, "
              f"height_cov={self.profile.height_tag_coverage:.2f}")
        print(f"  [harness] base: style={self.base_params.style}, "
              f"flat_mode={self.base_params.flat_mode}, "
              f"height_max={self.base_params.building_height_mm_max}mm")

        self.ctx = {
            "bbox": bbox, "area_km2": area_km2, "scale": scale,
            "utm_crs": utm_crs, "origin": origin, "utm_bbox": utm_bbox,
            "bbox_local": bbox_local,
            "width_m": bbox["width_m"], "height_m": bbox["height_m"],
            "elevation_grid": elevation_grid,
            "bbox_wgs84": p.bbox,
            "amap_water_polys": amap_water_polys,
            **gdfs,
        }
        self._prepared = True
        print(f"  [harness] prepared in {time.time() - t0:.1f}s")
        return self.ctx

    # ─── 基线参数 → 闭环种子 dict ────────────────────────────────────

    def seed_params(self) -> dict:
        rp = self.base_params
        return {
            "bo_mode": str(getattr(_cfg, "BUILDING_V2_MODE", "oriented_bbox")),
            "building_density_threshold": rp.building_density_threshold,
            "building_count_threshold": rp.building_count_threshold,
            "building_print_limit_m2": rp.building_print_limit_m2,
            "building_v2_road_tier": rp.building_v2_road_tier,
            "road_width_multiplier": rp.road_width_multiplier,
            "building_height_mm_max": rp.building_height_mm_max,
            "building_simplify_tol_m": rp.building_simplify_tol_m,
            "aggregate_simplify_m": float(
                getattr(_cfg, "BUILDING_V2_AGGREGATE_SIMPLIFY_M", 60.0)),
        }

    # 注：seed 含全部基线参数；控制器只迭代 PARAM_SPACE 中的活杠杆，
    # 其余作为固定上下文透传（run_round 的 .get 兜底）。

    # ─── 每轮最小重算 ────────────────────────────────────────────────

    def run_round(self, params: dict):
        """按 params 重跑 preprocess（缓存命中则直接返回 layers）。"""
        assert self._prepared, "call prepare() first"
        ctx = self.ctx

        # 高度杠杆：猴补丁（buildings._compress_height 已函数级 import）
        _cfg.BUILDING_HEIGHT_MAX_MM = float(params["building_height_mm_max"])
        # BL 轮廓简化杠杆（_extract_BL 两条路径均已函数级 import）
        _cfg.BUILDING_SIMPLIFY_TOL_M = float(params.get(
            "building_simplify_tol_m", self.base_params.building_simplify_tol_m))

        overrides = {
            "road_tier_override": int(params["building_v2_road_tier"]),
            "density_threshold_override": float(params.get(
                "building_density_threshold",
                self.base_params.building_density_threshold)),
            "count_threshold_override": int(params.get(
                "building_count_threshold",
                self.base_params.building_count_threshold)),
            "print_limit_m2_override": float(params["building_print_limit_m2"]),
            "aggregate_simplify_m_override": float(params.get(
                "aggregate_simplify_m",
                getattr(_cfg, "BUILDING_V2_AGGREGATE_SIMPLIFY_M", 60.0))),
            "bo_mode_override": str(params.get(
                "bo_mode", getattr(_cfg, "BUILDING_V2_MODE", "oriented_bbox"))),
        }
        if self.base_params.flat_mode:
            overrides["height_mode_override"] = "flat"

        # 缓存指纹：只含影响 preprocess 的参数（road_width_multiplier 仅影响渲染）
        cache_key = dict(overrides)
        cache_key["height_max_mm"] = float(params["building_height_mm_max"])
        cache_key["simplify_tol_m"] = float(_cfg.BUILDING_SIMPLIFY_TOL_M)

        def _compute():
            return preprocess_layers(
                buildings_gdf=ctx["buildings"],
                roads_gdf=ctx["roads"],
                water_gdf=ctx["water"],
                vegetation_gdf=ctx["vegetation"],
                bbox_local=ctx["bbox_local"],
                scale=ctx["scale"],
                enable_hotspot=True,
                hotspot_relax=_cfg.BUILDING_V2_HOTSPOT_RELAX,
                area_km2=ctx["area_km2"],
                bbox_wgs84=ctx["bbox_wgs84"],
                utm_crs=ctx["utm_crs"],
                origin=ctx["origin"],
                **overrides,
            )

        return self.cache.get_or_compute(
            "preprocess_v3", cache_key, _compute, label="preprocess")
