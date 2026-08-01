"""Layer preprocessor — 5 步几何减法 + 精度过滤。

把 raw OSM gdf 转成"已减去高优先级区域 + 已精度过滤"的 6 类 polygon list，
供 v3 builder 直接 extrude。

核心策略：
  1. 几何层面用 shapely 做 5 步布尔减法，确保任意两 sub-mesh 在 polygon 层不重叠
  2. 精度过滤：area < min_area_m2 的 polygon 直接舍弃
  3. 道路保持 LineString，不做 polygon buffer（由 roads v3 builder 处理）

参考：doc/spec_png_to_3mf_migration.md
"""

from __future__ import annotations

import math

import time
from dataclasses import dataclass, field
from typing import List, Tuple, Dict, Optional, Set

import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import (
    LineString,
    MultiLineString,
    MultiPolygon,
    Polygon,
    box,
)
from shapely.ops import unary_union, polygonize
from shapely.strtree import STRtree

from _TEXTURE_STYLE_OF_DEEPSEEK._landmark import (
    is_tag_landmark,
    classify_landmark,
    LandmarkCategory,
    is_vegetation_landmark,
    is_water_landmark,
    is_road_landmark,
    compute_top_percent_threshold,
    compute_hotspot_block_ids,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.config import (
    INTERNAL_SPAN_MM,
    BUILDING_PRINT_LIMIT_M2,
    BUILDING_V2_USE_LANDMARK_TAGS,
    BUILDING_V2_HOTSPOT_RELAX,
    BUILDING_V2_BLOCK_FILL_CONVEX,
    BUILDING_V2_MIN_BLOCK_COMPACTNESS,
    BUILDING_V2_MAX_BLOCK_AREA_M2,
    BUILDING_V2_COUNT_THRESHOLD,
    BUILDING_V2_DENSITY_THRESHOLD,
    BUILDING_V2_MODE,
    NOZZLE_DIAM_MM,
    MIN_PRINTABLE_AREA_M2,
    WATERWAY_HALF_WIDTH,
    BUILDING_AGGREGATE_HEIGHT_MM,
    BUILDING_DEFAULT_HEIGHT_M,
    BLOCK_BASE_MIN_AREA_M2,
    ROAD_FILTER,
    get_area_class,
    HEIGHT_QUALITY_COVERAGE_THRESHOLD,
    BUILDING_FLAT_HEIGHT_LOW_MM,
    BUILDING_FLAT_HEIGHT_MID_MM,
    BUILDING_FLAT_HEIGHT_HIGH_MM,
    BUILDING_FLAT_AREA_MID_M2,
    BUILDING_FLAT_AREA_HIGH_M2,
    LANDMARK_CATEGORY_PARAMS,
    LANDMARK_HEIGHT_TOP_PERCENT,
    LANDMARK_AREA_TOP_PERCENT,
    LANDMARK_HEIGHT_BOOST,
    LANDMARK_HEIGHT_BOOST_CAP_MM,
    LANDMARK_BUFFER_M,
    LANDMARK_EXCLUSION_BUFFER_M,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.buildings import (
    _build_city_blocks,
    _aggregate_in_blocks,
    _convex_quadrilateral,
    _compress_height,
    _narrow_building_penalty,
)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class LayerPolygons:
    """7 类 polygon 集合 + 精度元信息。"""
    BL: List[Tuple[Polygon, float]] = field(default_factory=list)
    BL_categories: List = field(default_factory=list)  # List[LandmarkCategory], parallel to BL
    BO: List[Polygon] = field(default_factory=list)
    VL: List[Polygon] = field(default_factory=list)
    VO: List[Polygon] = field(default_factory=list)
    WL: List[Polygon] = field(default_factory=list)
    WO: List[Polygon] = field(default_factory=list)
    block_base: List[Polygon] = field(default_factory=list)  # PNG layer 1.5 暖米色城市底
    block_base_classes: List[str] = field(default_factory=list)  # semantic class per block_base polygon
    roads_lines: List[Tuple[LineString, str, bool]] = field(default_factory=list)
    nozzle_real_m: float = 0.0
    min_area_m2: float = 0.0

    def summary(self) -> str:
        from collections import Counter
        cat_dist = ""
        if self.BL_categories:
            counts = Counter(c.name for c in self.BL_categories)
            cat_dist = " " + " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        return (
            f"BL={len(self.BL)}{cat_dist} BO={len(self.BO)} "
            f"VL={len(self.VL)} VO={len(self.VO)} "
            f"WL={len(self.WL)} WO={len(self.WO)} "
            f"block_base={len(self.block_base)} "
            f"roads={len(self.roads_lines)}"
        )


# ---------------------------------------------------------------------------
# Helper: geometry subtraction
# ---------------------------------------------------------------------------

def _subtract(polys: List[Polygon], minus_geom) -> List[Polygon]:
    """从 polys 中扣掉 minus_geom，返回 polygon list。

    minus_geom 可为 None / Polygon / MultiPolygon / GeometryCollection。
    输出已展平 MultiPolygon。
    """
    if minus_geom is None or minus_geom.is_empty:
        return list(polys)
    out = []
    for p in polys:
        if not isinstance(p, Polygon) or p.is_empty:
            continue
        if not p.intersects(minus_geom):
            out.append(p)
            continue
        diff = p.difference(minus_geom)
        if diff.is_empty:
            continue
        if isinstance(diff, Polygon):
            out.append(diff)
        elif hasattr(diff, "geoms"):
            for g in diff.geoms:
                if isinstance(g, Polygon) and not g.is_empty:
                    out.append(g)
    return out


def _filter_by_area(polys: List[Polygon], min_area: float) -> List[Polygon]:
    """过滤 area < min_area 的 polygon。"""
    return [p for p in polys if isinstance(p, Polygon) and not p.is_empty and p.area >= min_area]


# ---------------------------------------------------------------------------
# B.1.4.1b: 建筑高度数据质量评判
# ---------------------------------------------------------------------------

def assess_height_data_quality(buildings_gdf: gpd.GeoDataFrame) -> dict:
    """Assess OSM building height data quality for this region.

    Returns dict with:
        coverage: fraction of buildings with non-default height
        median_tagged_m: median height of buildings with real data
        mode: "height" (data good, use real heights) or "flat" (data poor, use area tiers)
    """
    if buildings_gdf is None or len(buildings_gdf) == 0 or "est_height" not in buildings_gdf.columns:
        return {"coverage": 0.0, "median_tagged_m": 0.0, "mode": "flat"}

    est_heights = buildings_gdf["est_height"]
    total = len(est_heights)
    at_default = (est_heights == BUILDING_DEFAULT_HEIGHT_M).sum()
    coverage = 1.0 - at_default / total

    tagged = est_heights[est_heights != BUILDING_DEFAULT_HEIGHT_M]
    median_tagged = float(tagged.median()) if len(tagged) > 0 else 0.0

    mode = "height" if coverage >= HEIGHT_QUALITY_COVERAGE_THRESHOLD else "flat"

    return {"coverage": coverage, "median_tagged_m": median_tagged, "mode": mode}


def _flat_mode_height(area_m2: float) -> float:
    """Flat mode: area-based tier height for landmarks when height data is unreliable."""
    if area_m2 >= BUILDING_FLAT_AREA_HIGH_M2:
        return BUILDING_FLAT_HEIGHT_HIGH_MM
    elif area_m2 >= BUILDING_FLAT_AREA_MID_M2:
        return BUILDING_FLAT_HEIGHT_MID_MM
    else:
        return BUILDING_FLAT_HEIGHT_LOW_MM


# ---------------------------------------------------------------------------
# B.1.4.2: 提取建筑地标 + 计算高度
# ---------------------------------------------------------------------------

import os as _os
_USE_VECTORIZED_BL = True
_VERIFY_EXTRACT_BL = _os.environ.get("VERIFY_EXTRACT_BL", "") == "1"


def _extract_BL(
    buildings_gdf: gpd.GeoDataFrame,
    city_blocks: List[Polygon],
    enable_hotspot: bool,
    hotspot_relax: float,
    height_mode: str = "height",
    narrow_threshold: float = 6.0,
    narrow_penalty_factor: float = 0.5,
) -> Tuple[List[Tuple[Polygon, float]], List[Polygon], List]:
    """返回 (BL_with_heights, BO_input_smalls, BL_categories)。Dispatches to vectorized or legacy."""
    args = (buildings_gdf, city_blocks, enable_hotspot, hotspot_relax,
            height_mode, narrow_threshold, narrow_penalty_factor)

    if _VERIFY_EXTRACT_BL:
        t0 = time.time()
        bl_leg, bo_leg, cat_leg = _extract_BL_legacy(*args)
        t_leg = time.time() - t0
        t0 = time.time()
        bl_vec, bo_vec, cat_vec = _extract_BL_vectorized(*args)
        t_vec = time.time() - t0
        # Compare
        areas_leg = sorted([p.area for p, _ in bl_leg])
        areas_vec = sorted([p.area for p, _ in bl_vec])
        bl_match = len(bl_leg) == len(bl_vec)
        bo_match = len(bo_leg) == len(bo_vec)
        print(f"  [verify] BL: legacy={len(bl_leg)}, vec={len(bl_vec)}, match={bl_match}")
        print(f"  [verify] BO: legacy={len(bo_leg)}, vec={len(bo_vec)}, match={bo_match}")
        print(f"  [verify] time: legacy={t_leg:.1f}s, vec={t_vec:.1f}s, speedup={t_leg/max(t_vec,0.01):.1f}x")
        if not bl_match or not bo_match:
            print(f"  [verify] WARNING: mismatch! Using legacy result.")
            return bl_leg, bo_leg, cat_leg
        return bl_vec, bo_vec, cat_vec

    if _USE_VECTORIZED_BL:
        return _extract_BL_vectorized(*args)
    return _extract_BL_legacy(*args)


def _extract_BL_vectorized(
    buildings_gdf: gpd.GeoDataFrame,
    city_blocks: List[Polygon],
    enable_hotspot: bool,
    hotspot_relax: float,
    height_mode: str = "height",
    narrow_threshold: float = 6.0,
    narrow_penalty_factor: float = 0.5,
) -> Tuple[List[Tuple[Polygon, float]], List[Polygon], List]:
    """Vectorized version of _extract_BL using geopandas batch operations."""
    # Function-level import: allows runtime monkey-patch from auto-params
    from _TEXTURE_STYLE_OF_DEEPSEEK.config import (
        BUILDING_SIMPLIFY_TOL_M, BUILDING_V2_LANDMARK_TOP_PERCENT,
    )
    if buildings_gdf is None or len(buildings_gdf) == 0:
        return [], [], []

    use_landmark_tags = BUILDING_V2_USE_LANDMARK_TAGS

    # ---- 动态高度覆盖率：替代写死的 BUILDING_VERIFIED_HEIGHT_ONLY ----
    if "height_source" in buildings_gdf.columns:
        _n_verified = (buildings_gdf["height_source"] == "overture").sum()
        _height_coverage = _n_verified / max(len(buildings_gdf), 1)
    else:
        _height_coverage = 0.0
    print(f"  _extract_BL_vectorized: height_coverage={_height_coverage:.1%}")

    # ---- Step 1: Vectorized explode + simplify + filter ----
    gdf = buildings_gdf[buildings_gdf.geometry.notnull()].copy()
    gdf = gdf[gdf.geometry.type.isin(['Polygon', 'MultiPolygon'])]
    if len(gdf) == 0:
        return [], [], []

    exploded = gdf.explode(index_parts=False)
    exploded = exploded[exploded.geometry.area >= 1.0]
    if len(exploded) == 0:
        return [], [], []

    exploded = exploded.copy()
    exploded['geometry'] = exploded.geometry.simplify(BUILDING_SIMPLIFY_TOL_M)

    valid = (~exploded.geometry.is_empty) & (exploded.geometry.area >= 1.0)
    exploded = exploded[valid]
    if len(exploded) == 0:
        return [], [], []

    mp_mask = exploded.geometry.type == 'MultiPolygon'
    if mp_mask.any():
        exploded.loc[mp_mask, 'geometry'] = exploded.loc[mp_mask].geometry.apply(
            lambda g: max(g.geoms, key=lambda p: p.area))
    exploded = exploded[exploded.geometry.type == 'Polygon']
    exploded = exploded[~exploded.geometry.is_empty]
    if len(exploded) == 0:
        return [], [], []

    # ---- Step 2: Top percentile thresholds ----
    all_areas = exploded.geometry.area.values
    if BUILDING_V2_LANDMARK_TOP_PERCENT > 0:
        landmark_top_thr = compute_top_percent_threshold(
            all_areas.tolist(), BUILDING_V2_LANDMARK_TOP_PERCENT)
    else:
        landmark_top_thr = float("inf")

    # Cat 3 geometric outlier thresholds
    area_top_thr_5pct = compute_top_percent_threshold(
        all_areas.tolist(), LANDMARK_AREA_TOP_PERCENT)
    if "est_height" in exploded.columns:
        all_heights = exploded["est_height"].values
        nonzero_heights = [h for h in all_heights
                          if h > 0 and h != BUILDING_DEFAULT_HEIGHT_M]
        height_top_thr = (compute_top_percent_threshold(
            nonzero_heights, LANDMARK_HEIGHT_TOP_PERCENT)
            if nonzero_heights else float("inf"))
    else:
        height_top_thr = float("inf")

    # ---- Hotspot blocks ----
    hotspot_blocks: Set[int] = set()
    if enable_hotspot and hotspot_relax > 0 and city_blocks:
        all_polys = exploded.geometry.tolist()
        hotspot_blocks = compute_hotspot_block_ids(
            city_blocks, all_polys, hotspot_relax)

    htree: Optional[STRtree] = None
    if hotspot_blocks:
        hot_polys = [city_blocks[i] for i in hotspot_blocks]
        if hot_polys:
            htree = STRtree(hot_polys)

    # ---- Step 3: Classify ----
    BL_with_heights: List[Tuple[Polygon, float]] = []
    BL_categories: List[LandmarkCategory] = []
    BO_input_smalls: List[Polygon] = []
    n_lm_cat = {c.name: 0 for c in LandmarkCategory if c != LandmarkCategory.NONE}
    n_lm_other = 0

    for _, row in exploded.iterrows():
        poly = row.geometry
        area = poly.area

        is_hotspot = False
        if htree is not None:
            centroid = poly.centroid
            candidates = htree.query(centroid)
            if len(candidates) > 0:
                is_hotspot = True

        est_height = row.get("est_height", 0)

        # Dynamic height coverage: only skip non-verified when data is rich
        # (replaces hardcoded BUILDING_VERIFIED_HEIGHT_ONLY=True)
        if _height_coverage >= 0.30:
            h_source = row.get("height_source", "default")
            if h_source != "overture":
                continue

        # 4-category classification
        cat = (classify_landmark(
            row, area_m2=area, est_height_m=est_height,
            height_top_thr=height_top_thr,
            area_top_thr_5pct=area_top_thr_5pct,
            hotspot=is_hotspot)
            if use_landmark_tags else LandmarkCategory.NONE)

        size_lm = area >= BUILDING_PRINT_LIMIT_M2
        top_lm = area >= landmark_top_thr

        if height_mode == "height":
            is_landmark = (cat != LandmarkCategory.NONE) or size_lm or top_lm
        else:
            has_real_height = (est_height != BUILDING_DEFAULT_HEIGHT_M and est_height > 0)
            is_landmark = ((cat != LandmarkCategory.NONE) and has_real_height) or \
                          (size_lm and has_real_height) or \
                          (top_lm and has_real_height)

        if is_landmark:
            # Determine effective category (size_lm/top_lm fallback → GEOMETRIC)
            if cat == LandmarkCategory.NONE:
                effective_cat = LandmarkCategory.GEOMETRIC
                n_lm_other += 1
            else:
                effective_cat = cat
                n_lm_cat[effective_cat.name] += 1

            params = LANDMARK_CATEGORY_PARAMS[effective_cat]

            h_mm = _compress_height(est_height, area)
            h_mm = _narrow_building_penalty(poly, h_mm,
                                            threshold=narrow_threshold,
                                            factor=narrow_penalty_factor)
            # Category-specific additive height offset + 2D expansion
            h_mm = h_mm + params["height_add_mm"]
            if params["buffer_m"] > 0:
                poly = poly.buffer(params["buffer_m"], join_style=2)
                if poly.is_empty:
                    continue
                if isinstance(poly, MultiPolygon):
                    poly = max(poly.geoms, key=lambda g: g.area)
                if not isinstance(poly, Polygon) or poly.is_empty:
                    continue
            BL_with_heights.append((poly, h_mm))
            BL_categories.append(effective_cat)
        else:
            BO_input_smalls.append(poly)

    cat_summary = ", ".join(f"{k}={v}" for k, v in n_lm_cat.items() if v > 0)
    if n_lm_other > 0:
        cat_summary += f", size/top={n_lm_other}"
    print(f"  _extract_BL: {len(BL_with_heights)} BL ({cat_summary}), "
          f"{len(BO_input_smalls)} smalls, "
          f"hotspot_blocks={len(hotspot_blocks)}")

    return BL_with_heights, BO_input_smalls, BL_categories


def _extract_BL_legacy(
    buildings_gdf: gpd.GeoDataFrame,
    city_blocks: List[Polygon],
    enable_hotspot: bool,
    hotspot_relax: float,
    height_mode: str = "height",
    narrow_threshold: float = 6.0,
    narrow_penalty_factor: float = 0.5,
) -> Tuple[List[Tuple[Polygon, float]], List[Polygon], List]:
    """Legacy iterrows-based implementation of _extract_BL."""
    # Function-level import: allows runtime monkey-patch from auto-params
    from _TEXTURE_STYLE_OF_DEEPSEEK.config import (
        BUILDING_SIMPLIFY_TOL_M, BUILDING_V2_LANDMARK_TOP_PERCENT,
    )
    if buildings_gdf is None or len(buildings_gdf) == 0:
        return [], [], []

    use_landmark_tags = BUILDING_V2_USE_LANDMARK_TAGS

    # ---- First pass: simplify all polygons ----
    all_simplified: List[Tuple[Polygon, int]] = []  # (poly, original_idx)
    all_areas: List[float] = []

    for idx, row in buildings_gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        polys = geom.geoms if isinstance(geom, MultiPolygon) else [geom]
        for poly in polys:
            if poly.area < 1.0:
                continue
            simplified = poly.simplify(BUILDING_SIMPLIFY_TOL_M, preserve_topology=True)
            if simplified.is_empty or simplified.area < 1.0:
                continue
            if isinstance(simplified, MultiPolygon):
                simplified = max(simplified.geoms, key=lambda g: g.area)
            if not isinstance(simplified, Polygon) or simplified.is_empty:
                continue
            all_simplified.append((simplified, idx))
            all_areas.append(simplified.area)

    if not all_simplified:
        return [], [], []

    # ---- Top percentile thresholds ----
    if BUILDING_V2_LANDMARK_TOP_PERCENT > 0:
        landmark_top_thr = compute_top_percent_threshold(
            all_areas, BUILDING_V2_LANDMARK_TOP_PERCENT)
    else:
        landmark_top_thr = float("inf")

    area_top_thr_5pct = compute_top_percent_threshold(
        all_areas, LANDMARK_AREA_TOP_PERCENT)
    if "est_height" in buildings_gdf.columns:
        nonzero_heights = [h for h in buildings_gdf["est_height"].values
                          if h > 0 and h != BUILDING_DEFAULT_HEIGHT_M]
        height_top_thr = (compute_top_percent_threshold(
            nonzero_heights, LANDMARK_HEIGHT_TOP_PERCENT)
            if nonzero_heights else float("inf"))
    else:
        height_top_thr = float("inf")

    # ---- Hotspot blocks ----
    hotspot_blocks: Set[int] = set()
    if enable_hotspot and hotspot_relax > 0 and city_blocks:
        hotspot_blocks = compute_hotspot_block_ids(
            city_blocks,
            [p for p, _ in all_simplified],
            hotspot_relax,
        )

    # ---- 动态高度覆盖率：替代写死的 BUILDING_VERIFIED_HEIGHT_ONLY ----
    if "height_source" in buildings_gdf.columns:
        _n_verified = (buildings_gdf["height_source"] == "overture").sum()
        _height_coverage = _n_verified / max(len(buildings_gdf), 1)
    else:
        _height_coverage = 0.0
    print(f"  _extract_BL_scalar: height_coverage={_height_coverage:.1%}")
    htree: Optional[STRtree] = None
    if hotspot_blocks:
        hot_polys = [city_blocks[i] for i in hotspot_blocks]
        if hot_polys:
            htree = STRtree(hot_polys)

    # ---- Second pass: classify ----
    BL_with_heights: List[Tuple[Polygon, float]] = []
    BL_categories: List[LandmarkCategory] = []
    BO_input_smalls: List[Polygon] = []
    n_lm_cat = {c.name: 0 for c in LandmarkCategory if c != LandmarkCategory.NONE}
    n_lm_other = 0

    for poly, idx in all_simplified:
        row = buildings_gdf.loc[idx]
        area = poly.area

        # Check hotspot
        is_hotspot = False
        if htree is not None:
            centroid = poly.centroid
            candidates = htree.query(centroid)
            if len(candidates) > 0:
                is_hotspot = True

        est_height = row.get("est_height", 0)

        # Dynamic height coverage: only skip non-verified when data is rich
        # (replaces hardcoded BUILDING_VERIFIED_HEIGHT_ONLY=True)
        if _height_coverage >= 0.30:
            h_source = row.get("height_source", "default")
            if h_source != "overture":
                continue

        # 4-category classification
        cat = (classify_landmark(
            row, area_m2=area, est_height_m=est_height,
            height_top_thr=height_top_thr,
            area_top_thr_5pct=area_top_thr_5pct,
            hotspot=is_hotspot)
            if use_landmark_tags else LandmarkCategory.NONE)

        size_lm = area >= BUILDING_PRINT_LIMIT_M2
        top_lm = area >= landmark_top_thr

        if height_mode == "height":
            is_landmark = (cat != LandmarkCategory.NONE) or size_lm or top_lm
        else:
            has_real_height = (est_height != BUILDING_DEFAULT_HEIGHT_M and est_height > 0)
            is_landmark = ((cat != LandmarkCategory.NONE) and has_real_height) or \
                          (size_lm and has_real_height) or \
                          (top_lm and has_real_height)

        if is_landmark:
            if cat == LandmarkCategory.NONE:
                effective_cat = LandmarkCategory.GEOMETRIC
                n_lm_other += 1
            else:
                effective_cat = cat
                n_lm_cat[effective_cat.name] += 1

            params = LANDMARK_CATEGORY_PARAMS[effective_cat]

            h_mm = _compress_height(est_height, area)
            h_mm = _narrow_building_penalty(poly, h_mm,
                                            threshold=narrow_threshold,
                                            factor=narrow_penalty_factor)
            # Category-specific additive height offset + 2D expansion
            h_mm = h_mm + params["height_add_mm"]
            if params["buffer_m"] > 0:
                poly = poly.buffer(params["buffer_m"], join_style=2)
                if poly.is_empty:
                    continue
                if isinstance(poly, MultiPolygon):
                    poly = max(poly.geoms, key=lambda g: g.area)
                if not isinstance(poly, Polygon) or poly.is_empty:
                    continue
            BL_with_heights.append((poly, h_mm))
            BL_categories.append(effective_cat)
        else:
            BO_input_smalls.append(poly)

    cat_summary = ", ".join(f"{k}={v}" for k, v in n_lm_cat.items() if v > 0)
    if n_lm_other > 0:
        cat_summary += f", size/top={n_lm_other}"
    print(f"  _extract_BL: {len(BL_with_heights)} BL ({cat_summary}), "
          f"{len(BO_input_smalls)} smalls, "
          f"hotspot_blocks={len(hotspot_blocks)}")

    return BL_with_heights, BO_input_smalls, BL_categories


# ---------------------------------------------------------------------------
# B.1.4.3: 计算 BO (block_fill)
# ---------------------------------------------------------------------------

def _compute_BO(
    smalls: List[Polygon],
    city_blocks: List[Polygon],
    BL_polys: List[Polygon],
    nozzle_real_m: float,
    *,
    density_threshold_override: Optional[float] = None,
    count_threshold_override: Optional[int] = None,
    print_limit_m2_override: Optional[float] = None,
    aggregate_simplify_m_override: Optional[float] = None,
    mode_override: Optional[str] = None,
) -> Tuple[List[Polygon], set]:
    """返回 (BO_polys, filled_block_ids)。

    - 用 aggregate_in_blocks 算法（block_fill mode）
    - landmark 参与 count/density 计算（不参与几何）
    - 过滤 area > BUILDING_V2_MAX_BLOCK_AREA_M2 的巨型 block
    """
    if not smalls or not city_blocks:
        return [], set()

    eff_density = density_threshold_override if density_threshold_override is not None else BUILDING_V2_DENSITY_THRESHOLD
    eff_count = count_threshold_override if count_threshold_override is not None else BUILDING_V2_COUNT_THRESHOLD
    eff_print_limit = print_limit_m2_override if print_limit_m2_override is not None else BUILDING_PRINT_LIMIT_M2
    eff_simplify = aggregate_simplify_m_override if aggregate_simplify_m_override is not None else 60.0
    eff_mode = mode_override if mode_override is not None else BUILDING_V2_MODE

    blocks = _aggregate_in_blocks(
        smalls, city_blocks,
        mode=eff_mode,
        print_limit_m2=eff_print_limit,
        simplify_m=eff_simplify,
        bldg_buffer_m=20.0,
        density_threshold=eff_density,
        count_threshold=eff_count,
        min_block_compactness=BUILDING_V2_MIN_BLOCK_COMPACTNESS,
        block_fill_convex=BUILDING_V2_BLOCK_FILL_CONVEX,
        landmark_polys=BL_polys,
    )

    # Filter out oversized blocks
    max_area = BUILDING_V2_MAX_BLOCK_AREA_M2
    n_filtered = 0
    kept: List[Polygon] = []
    for p in blocks:
        if p.area > max_area:
            n_filtered += 1
            continue
        kept.append(p)

    if n_filtered:
        print(f"  _compute_BO: filtered {n_filtered} blocks > {max_area:.0f}m²")

    # Track which blocks were filled (for subtraction awareness)
    # We don't need exact IDs here; filled_block_ids is used in subtraction
    # to mark which blocks shouldn't be subtracted from BO.
    # For now, return empty set; subtraction still works.
    filled_block_ids: Set[int] = set()

    print(f"  _compute_BO: {len(smalls)} smalls → {len(kept)} blocks")
    return kept, filled_block_ids


# ---------------------------------------------------------------------------
# B.1.4.4: 提取植被 (VL / VO)
# ---------------------------------------------------------------------------

def _extract_VL_VO(
    vegetation_gdf: gpd.GeoDataFrame,
) -> Tuple[List[Polygon], List[Polygon]]:
    """返回 (VL_polys, VO_polys)。

    - VL: is_vegetation_landmark(row, area_m2) 命中
    - VO: 其它所有 raw OSM 植被 polygon
    """
    if vegetation_gdf is None or len(vegetation_gdf) == 0:
        return [], []

    VL_polys: List[Polygon] = []
    VO_polys: List[Polygon] = []

    for _, row in vegetation_gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        if not isinstance(geom, (Polygon, MultiPolygon)):
            continue

        polys = geom.geoms if isinstance(geom, MultiPolygon) else [geom]
        for poly in polys:
            if poly.is_empty or poly.area < 10.0:
                continue
            area_m2 = poly.area
            if is_vegetation_landmark(row, area_m2=area_m2):
                VL_polys.append(poly)
            else:
                VO_polys.append(poly)

    print(f"  _extract_VL_VO: {len(VL_polys)} VL, {len(VO_polys)} VO")
    return VL_polys, VO_polys


# ---------------------------------------------------------------------------
# B.1.4.5: 提取水体 (WL / WO)
# ---------------------------------------------------------------------------

def _extract_WL_WO(
    water_gdf: gpd.GeoDataFrame,
    nozzle_real_m: float,
) -> Tuple[List[Polygon], List[Polygon], List[Tuple[LineString, str]]]:
    """返回 (WL_polys, WO_polys, wl_lines_raw)。

    - Polygon/MultiPolygon: is_water_landmark → WL，否则 → WO（仅当 area >= 1000m²）
    - LineString/MultiLineString: is_water_landmark → buffer 到
        max(WATERWAY_HALF_WIDTH[wway], nozzle_real_m * 1.5)
        → 加入 WL（已 buffer 后的 polygon）
    - LineString 非地标 → 忽略
    - wl_lines_raw: 原始 WL LineString + waterway type（buffer 前），供水体补全使用
    """
    if water_gdf is None or len(water_gdf) == 0:
        return [], [], []

    WL_polys: List[Polygon] = []
    WO_polys: List[Polygon] = []
    wl_lines_raw: List[Tuple[LineString, str]] = []
    min_buffer = nozzle_real_m * 1.5

    for _, row in water_gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue

        # Polygon / MultiPolygon
        if isinstance(geom, (Polygon, MultiPolygon)):
            polys = geom.geoms if isinstance(geom, MultiPolygon) else [geom]
            for poly in polys:
                if poly.is_empty:
                    continue
                area = poly.area
                if is_water_landmark(row, area_m2=area):
                    WL_polys.append(poly)
                elif area >= 1000.0:
                    WO_polys.append(poly)

        # LineString / MultiLineString
        elif isinstance(geom, (LineString, MultiLineString)):
            if not is_water_landmark(row):
                continue
            lines = geom.geoms if isinstance(geom, MultiLineString) else [geom]
            waterway_type = row.get("waterway", "river")
            # 宽度解析：OSM width 标签 > 自适应回退（不查硬编码表）
            osm_width = row.get("width", None)
            if osm_width is not None:
                try:
                    if not (isinstance(osm_width, float) and math.isnan(osm_width)):
                        parsed_w = float(osm_width)
                        if 0 < parsed_w < 5000:
                            half_width = parsed_w / 2.0
                        else:
                            half_width = 30.0  # 无效标签回退
                    else:
                        half_width = 30.0
                except (TypeError, ValueError):
                    half_width = 30.0
            else:
                # 无标签：按类型给保守默认值（不查 WATERWAY_HALF_WIDTH）
                conservative_defaults = {
                    "river": 30.0, "riverbank": 100.0,
                    "canal": 15.0, "stream": 6.0,
                    "drain": 3.0, "ditch": 2.0,
                }
                half_width = conservative_defaults.get(waterway_type, 15.0)
            buffer_width = max(half_width, min_buffer)
            for line in lines:
                if line.length < 10.0:
                    continue
                wl_lines_raw.append((line, waterway_type))
                buf = line.buffer(buffer_width, cap_style=2, join_style=2)
                if buf.is_empty:
                    continue
                if isinstance(buf, MultiPolygon):
                    WL_polys.extend(buf.geoms)
                elif isinstance(buf, Polygon):
                    WL_polys.append(buf)

    print(f"  _extract_WL_WO: {len(WL_polys)} WL, {len(WO_polys)} WO, "
          f"{len(wl_lines_raw)} raw lines (min_buffer={min_buffer:.1f}m)")
    return WL_polys, WO_polys, wl_lines_raw


# ---------------------------------------------------------------------------
# B.1.4.7: 提取道路（含桥梁分类）
# ---------------------------------------------------------------------------

def _extract_roads(
    roads_gdf: gpd.GeoDataFrame,
    BL_polys: List[Polygon],
    area_km2: float = 0,
) -> List[Tuple[LineString, str, bool]]:
    """返回 [(line, highway_type, is_bridge)]。

    - 应用 ROAD_FILTER（如 large 城市仅取 motorway/trunk/primary/secondary）
    - 减去 BL footprint（路从大楼底下不画）
    - is_bridge = is_road_landmark(row)
    """
    if roads_gdf is None or len(roads_gdf) == 0:
        return []

    area_class = get_area_class(area_km2)
    highway_filter = ROAD_FILTER.get(area_class, None)

    # Build BL union for subtraction
    bl_union = None
    if BL_polys:
        try:
            bl_union = unary_union(BL_polys)
        except Exception:
            pass

    result: List[Tuple[LineString, str, bool]] = []
    n_skip_filter = 0
    n_skip_short = 0
    n_subtracted = 0

    for _, row in roads_gdf.iterrows():
        highway = row.get("highway", "residential")
        if highway_filter is not None and highway not in highway_filter:
            n_skip_filter += 1
            continue

        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        if not isinstance(geom, (LineString, MultiLineString)):
            continue

        lines = geom.geoms if isinstance(geom, MultiLineString) else [geom]
        is_bridge = is_road_landmark(row)

        for line in lines:
            if line.length < 10.0:
                n_skip_short += 1
                continue

            # Subtract BL footprint
            if bl_union is not None and line.intersects(bl_union):
                try:
                    diff = line.difference(bl_union)
                    if diff.is_empty:
                        n_subtracted += 1
                        continue
                    if isinstance(diff, LineString):
                        result.append((diff, highway, is_bridge))
                    elif isinstance(diff, MultiLineString):
                        for seg in diff.geoms:
                            if isinstance(seg, LineString) and seg.length >= 10.0:
                                result.append((seg, highway, is_bridge))
                    elif hasattr(diff, "geoms"):
                        for g in diff.geoms:
                            if isinstance(g, LineString) and g.length >= 10.0:
                                result.append((g, highway, is_bridge))
                except Exception:
                    # Fallback: keep original line
                    result.append((line, highway, is_bridge))
            else:
                result.append((line, highway, is_bridge))

    n_bridges = sum(1 for _, _, b in result if b)
    print(f"  _extract_roads: {len(result)} lines ({n_bridges} bridges), "
          f"filter skipped {n_skip_filter}, short {n_skip_short}, "
          f"BL subtracted {n_subtracted}")
    return result


# ---------------------------------------------------------------------------
# B.1.4.6: 5 步减法 + 精度过滤
# ---------------------------------------------------------------------------

def _apply_subtraction_and_filter(
    BL: List[Tuple[Polygon, float]],
    BL_categories: List,
    BO: List[Polygon],
    VL: List[Polygon],
    VO: List[Polygon],
    WL: List[Polygon],
    WO: List[Polygon],
    BO_filled_ids: set,
    min_area_m2: float,
) -> Dict:
    """应用 5 步减法（与 PNG spec 一致）：

      WO_clean = WO − WL
      BO_clean = BO − all_landmarks − roads_buffered
      VO_clean = VO − all_landmarks − BO_clean
      VL_clean = VL − BL
    """
    t0 = time.time()

    BL_polys = [p for p, _ in BL]

    # Exclusion zone: buffer BL polygons with category-specific radius
    # (gives landmarks a "plaza" — surrounding small buildings pushed back)
    default_excl = LANDMARK_EXCLUSION_BUFFER_M
    BL_exclusion = []
    for i, p in enumerate(BL_polys):
        # Determine exclusion radius from category
        if BL_categories and i < len(BL_categories):
            cat = BL_categories[i]
            params = LANDMARK_CATEGORY_PARAMS.get(cat, {})
            excl_m = params.get("exclusion_buffer_m", default_excl)
        else:
            excl_m = default_excl

        if excl_m > 0:
            try:
                buffered = p.buffer(excl_m, join_style=2)
                if not buffered.is_empty:
                    BL_exclusion.append(buffered)
                else:
                    BL_exclusion.append(p)
            except Exception:
                BL_exclusion.append(p)
        else:
            BL_exclusion.append(p)

    # all_landmarks = BL(exclusion) ∪ WL ∪ VL
    all_bits = BL_exclusion + WL + VL
    all_landmarks = None
    try:
        all_landmarks = unary_union([p for p in all_bits if not p.is_empty])
    except Exception:
        pass

    # Step 1: WO_clean = WO − WL
    wl_union = None
    if WL:
        try:
            wl_union = unary_union(WL)
        except Exception:
            pass
    WO_clean = _subtract(WO, wl_union)
    print(f"  Step 1: WO {len(WO)} → {len(WO_clean)} (WO − WL)")

    # Step 2: BO_clean = BO − all_landmarks
    # Note: spec also says BO − roads_buffered, but roads are line-based.
    # We do BO − all_landmarks here; roads v3 builder handles road/BO overlap
    # via z-height separation (BO at +2.5mm, RO at +0.51mm).
    BO_clean = _subtract(BO, all_landmarks)
    print(f"  Step 2: BO {len(BO)} → {len(BO_clean)} (BO − all_landmarks)")

    # Step 3: VO_clean = VO − all_landmarks − BO_clean
    VO_tmp = _subtract(VO, all_landmarks)
    bo_clean_union = None
    if BO_clean:
        try:
            bo_clean_union = unary_union(BO_clean)
        except Exception:
            pass
    VO_clean = _subtract(VO_tmp, bo_clean_union)
    print(f"  Step 3: VO {len(VO)} → {len(VO_clean)} (VO − all_landmarks − BO_clean)")

    # Step 4: VL_clean = VL − BL
    bl_union = None
    if BL_polys:
        try:
            bl_union = unary_union(BL_polys)
        except Exception:
            pass
    VL_clean = _subtract(VL, bl_union)
    print(f"  Step 4: VL {len(VL)} → {len(VL_clean)} (VL − BL)")

    # Step 5: Precision filter
    BL_clean = [(p, h) for (p, h) in BL
                if isinstance(p, Polygon) and not p.is_empty and p.area >= min_area_m2 * 0.5]
    BO_clean = _filter_by_area(BO_clean, min_area_m2 * 0.5)
    VL_clean_filt = _filter_by_area(VL_clean, min_area_m2 * 0.5)
    VO_clean_filt = _filter_by_area(VO_clean, min_area_m2 * 0.5)
    WL_clean = _filter_by_area(WL, min_area_m2 * 0.5)
    WO_clean_filt = _filter_by_area(WO_clean, min_area_m2 * 0.5)

    print(f"  Precision filter (min={min_area_m2:.0f}m²): "
          f"BL={len(BL_clean)}/{len(BL)}, BO={len(BO_clean)}/{len(BO)}, "
          f"VL={len(VL_clean_filt)}/{len(VL_clean)}, VO={len(VO_clean_filt)}/{len(VO_clean)}, "
          f"WL={len(WL_clean)}/{len(WL)}, WO={len(WO_clean_filt)}/{len(WO_clean)}")
    print(f"  _apply_subtraction_and_filter time: {time.time() - t0:.1f}s")

    return {
        "BL": BL_clean,
        "BO": BO_clean,
        "VL": VL_clean_filt,
        "VO": VO_clean_filt,
        "WL": WL_clean,
        "WO": WO_clean_filt,
    }


# ---------------------------------------------------------------------------
# B.1.4.6: block_base classification by landuse
# ---------------------------------------------------------------------------

_LANDUSE_CLASS_MAP = {
    "residential": "residential",
    "apartments": "residential",
    "housing": "residential",
    "commercial": "commercial",
    "retail": "commercial",
    "industrial": "industrial",
    "railway": "industrial",
    "construction": "industrial",
    "farmland": "farmland",
    "farmyard": "farmland",
    "orchard": "farmland",
    "vineyard": "farmland",
    "meadow": "farmland",
    "forest": "forest",
    "wood": "forest",
}


def _classify_block_base(
    polys: List[Polygon],
    landuse_gdf: "gpd.GeoDataFrame | None",
    water_gdf: "gpd.GeoDataFrame | None",
    buildings_gdf: "gpd.GeoDataFrame | None",
) -> List[str]:
    """Classify each block_base polygon by landuse overlay."""
    if not polys:
        return []

    classes = ["unclassified"] * len(polys)

    # Phase 1: landuse spatial join (majority overlap > 15%)
    if landuse_gdf is not None and len(landuse_gdf) > 0:
        # Vectorized landuse extraction (no iterrows)
        lu_valid = landuse_gdf[
            landuse_gdf.geometry.notnull()
            & ~landuse_gdf.geometry.is_empty
            & landuse_gdf.geometry.type.isin(['Polygon', 'MultiPolygon'])
        ].copy()
        lu_polys: list = []
        lu_classes: list = []
        if len(lu_valid) > 0:
            lu_valid['_cls'] = lu_valid.get('landuse', pd.Series('', index=lu_valid.index)).map(
                _LANDUSE_CLASS_MAP
            )
            lu_valid = lu_valid[lu_valid['_cls'].notna()]
        if len(lu_valid) > 0:
            lu_exploded = lu_valid.explode(index_parts=False)
            lu_exploded = lu_exploded[~lu_exploded.geometry.is_empty]
            lu_polys = lu_exploded.geometry.tolist()
            lu_classes = lu_exploded['_cls'].tolist()

        if lu_polys:
            tree = STRtree(lu_polys)
            for i, block in enumerate(polys):
                if block.is_empty:
                    continue
                block_area = block.area
                if block_area < 1.0:
                    continue
                candidates = tree.query(block)
                best_cls = None
                best_overlap = 0.0
                for j in candidates:
                    try:
                        isect = block.intersection(lu_polys[j])
                        overlap = isect.area / block_area
                    except Exception:
                        continue
                    if overlap > best_overlap:
                        best_overlap = overlap
                        best_cls = lu_classes[j]
                if best_cls and best_overlap > 0.15:
                    classes[i] = best_cls

    # Phase 2: water_adjacent (buffer 80m, >30% overlap)
    if water_gdf is not None and len(water_gdf) > 0:
        water_polys = []
        for geom in water_gdf.geometry:
            if geom is None or geom.is_empty:
                continue
            if isinstance(geom, Polygon):
                water_polys.append(geom)
            elif isinstance(geom, MultiPolygon):
                water_polys.extend(g for g in geom.geoms if not g.is_empty)
        if water_polys:
            water_union = unary_union(water_polys)
            water_buf = water_union.buffer(80.0)
            for i, block in enumerate(polys):
                if classes[i] != "unclassified":
                    continue
                if block.is_empty or block.area < 1.0:
                    continue
                try:
                    overlap = block.intersection(water_buf).area / block.area
                except Exception:
                    continue
                if overlap > 0.30:
                    classes[i] = "water_adjacent"

    # Phase 3: fallback — blocks with buildings → residential
    if buildings_gdf is not None and len(buildings_gdf) > 0:
        bldg_centroids = []
        for geom in buildings_gdf.geometry:
            if geom is None or geom.is_empty:
                continue
            if isinstance(geom, (Polygon, MultiPolygon)):
                bldg_centroids.append(geom.centroid)
        if bldg_centroids:
            from shapely.geometry import Point
            bldg_pts = [c for c in bldg_centroids]
            bldg_tree = STRtree(bldg_pts)
            for i, block in enumerate(polys):
                if classes[i] != "unclassified":
                    continue
                if block.is_empty:
                    continue
                hits = bldg_tree.query(block)
                count = sum(1 for j in hits if block.contains(bldg_pts[j]))
                if count >= 2:
                    classes[i] = "residential"

    from collections import Counter
    dist = Counter(classes)
    print(f"[preprocess] block_base classes: {dict(dist)}")
    return classes


# ---------------------------------------------------------------------------
# B.1.4.7: block_base — PNG layer 1.5 "暖米色城市底" 的几何
# ---------------------------------------------------------------------------

def _compute_block_base(
    city_blocks: List[Polygon],
    min_area_m2: float,
    water_gdf: "gpd.GeoDataFrame | None" = None,
    roads_gdf: "gpd.GeoDataFrame | None" = None,
    buildings_gdf: "gpd.GeoDataFrame | None" = None,
    veg_landmark_polys: "List[Polygon] | None" = None,
    water_inset: float = 40.0,
    road_inset: float = 25.0,
    max_area_m2: float = 0,
    min_buildings: int = 0,
) -> List[Polygon]:
    """从 city_blocks 生成 block_base — 对齐 PNG brick_render 的行为。

    调用 tools/block_polygonize_viz 里的:
      - _filter_blocks_with_buildings (含 break 逻辑)
      - _build_exclusion_mask (water+veg+road corridor)
      - _subtract_exclusions (并行减法 + 碎片过滤)

    不减 BL/BO/VL/VO — block_base Z 轴低于 buildings/landmarks，物理不冲突。
    """
    from _TEXTURE_STYLE_OF_DEEPSEEK._block_filter import (
        _build_exclusion_mask,
        _subtract_exclusions,
        _subtract_exclusions_grid,
        _filter_blocks_with_buildings,
    )

    # --- area 过滤 ---
    blocks_filt = [b for b in city_blocks
                   if isinstance(b, Polygon) and not b.is_empty
                   and b.area >= min_area_m2
                   and (max_area_m2 <= 0 or b.area <= max_area_m2)]
    if not blocks_filt:
        return []

    # --- 只保留含建筑的 block（仅当 min_buildings > 0 时启用）---
    if buildings_gdf is not None and len(buildings_gdf) > 0 and min_buildings > 0:
        # 从 GDF 提取 Polygon list（同 load_data 的展平逻辑）
        bldg_polys = []
        for geom in buildings_gdf.geometry:
            if geom is None or geom.is_empty:
                continue
            if isinstance(geom, Polygon):
                bldg_polys.append(geom)
            elif isinstance(geom, MultiPolygon):
                bldg_polys.extend(g for g in geom.geoms if not g.is_empty)
        blocks_filt = _filter_blocks_with_buildings(
            blocks_filt, bldg_polys, min_buildings)
        if not blocks_filt:
            return []

    # --- exclusion parts (individual buffers for STRtree locality) ---
    excl_parts = []
    if veg_landmark_polys:
        excl_parts.extend(p for p in veg_landmark_polys
                          if isinstance(p, Polygon) and not p.is_empty)
    if water_gdf is not None and len(water_gdf) > 0:
        water_valid = water_gdf[water_gdf.geometry.notnull() & (~water_gdf.geometry.is_empty)]
        if len(water_valid) > 0:
            if water_inset > 0:
                water_buf = water_valid.geometry.buffer(water_inset)
            else:
                water_buf = water_valid.geometry
            for geom in water_buf:
                if geom is None or geom.is_empty:
                    continue
                if isinstance(geom, MultiPolygon):
                    excl_parts.extend(g for g in geom.geoms if not g.is_empty)
                else:
                    excl_parts.append(geom)
    if roads_gdf is not None and road_inset > 0:
        roads_valid = roads_gdf[roads_gdf.geometry.notnull() & (~roads_gdf.geometry.is_empty)]
        if len(roads_valid) > 0:
            road_buf = roads_valid.geometry.buffer(road_inset)
            excl_parts.extend(g for g in road_buf if g is not None and not g.is_empty)

    # --- grid 分治减法 + 碎片过滤 ---
    bbox_for_grid = (
        min(b.bounds[0] for b in blocks_filt),
        min(b.bounds[1] for b in blocks_filt),
        max(b.bounds[2] for b in blocks_filt),
        max(b.bounds[3] for b in blocks_filt),
    )
    blocks_filt = _subtract_exclusions_grid(
        blocks_filt, excl_parts, min_area=100.0, bbox_local=bbox_for_grid)
    return blocks_filt


# ---------------------------------------------------------------------------
# B.1.4: 主入口
# ---------------------------------------------------------------------------

def preprocess_layers(
    buildings_gdf: gpd.GeoDataFrame,
    roads_gdf: gpd.GeoDataFrame,
    water_gdf: gpd.GeoDataFrame,
    vegetation_gdf: gpd.GeoDataFrame,
    bbox_local: Tuple[float, float, float, float],
    scale: float,
    *,
    enable_hotspot: bool = True,
    hotspot_relax: float = 10.0,
    area_km2: float = 0,
    landuse_gdf: "gpd.GeoDataFrame | None" = None,
    narrow_threshold: float = 6.0,
    narrow_penalty: float = 0.5,
    bbox_wgs84: Optional[Tuple[float, float, float, float]] = None,
    utm_crs=None,
    origin: Optional[Tuple[float, float]] = None,
    merge_mode: bool = False,
    # --- auto-param overrides (None = use config defaults) ---
    road_tier_override: Optional[int] = None,
    density_threshold_override: Optional[float] = None,
    count_threshold_override: Optional[int] = None,
    print_limit_m2_override: Optional[float] = None,
    height_mode_override: Optional[str] = None,
    aggregate_simplify_m_override: Optional[float] = None,
    bo_mode_override: Optional[str] = None,
) -> LayerPolygons:
    """主入口。把 raw OSM gdf 转成 6 类 polygon + roads_lines。

    步骤：
      1. 计算 nozzle_real_m + min_area_m2
      2. 切 city blocks (路网 + 水网 polygonize)
      3. 提取 BL（建筑地标 + 高度），含 hotspot relax
      4. 计算 BO（block_fill 街区，已含地标参与 count/density）
         ↑ merge_mode=True 时跳过（block_base 已覆盖建筑街区）
      5. 提取 VL（含 protected_area）
      6. 提取 WL（含 LineString buffer）
      7. 5 步几何减法（保证独占）
      8. 精度过滤（< min_area_m2 舍弃）
      9. 提取道路 lines
      10. 返回 LayerPolygons

    merge_mode:
      当 MERGE_BLOCK_LAYERS=True 时设为 True。跳过 _compute_BO()，
      因为 block_base 已通过路网+水网 polygonize 覆盖了建筑街区。
      buildings_gdf 可以只传入地标建筑（用 building_landmarks 过滤器），
      避免加载全量建筑数据（1.9M → 几百个），节省 20+ 分钟。
    """
    t0 = time.time()

    # ---- Step 1: precision params ----
    nozzle_real_m = NOZZLE_DIAM_MM / scale if scale > 0 else 51.0
    min_area_m2 = MIN_PRINTABLE_AREA_M2
    print(f"\n[preprocess] nozzle_real={nozzle_real_m:.1f}m, min_area={min_area_m2:.0f}m²")

    # ---- Step 1b: height data quality assessment ----
    height_quality = assess_height_data_quality(buildings_gdf)
    height_mode = height_mode_override if height_mode_override else height_quality["mode"]
    print(f"[preprocess] height_quality: coverage={height_quality['coverage']:.1%}, "
          f"median_tagged={height_quality['median_tagged_m']:.1f}m → mode={height_mode}"
          f"{' (override)' if height_mode_override else ''}")

    # ---- Step 2: city blocks ----
    t2 = time.time()
    effective_road_tier = road_tier_override if road_tier_override is not None else 5
    wgdf = water_gdf if water_gdf is not None and len(water_gdf) > 0 else None
    if roads_gdf is not None and len(roads_gdf) > 0:
        city_blocks = _build_city_blocks(roads_gdf, wgdf, road_tier=effective_road_tier, bbox_local=bbox_local)
    else:
        city_blocks = []
    print(f"[preprocess] city_blocks: {len(city_blocks)} (road_tier={effective_road_tier}) "
          f"after {time.time() - t2:.1f}s")

    # ---- Step 3: BL ----
    t3 = time.time()
    BL_with_heights, BO_input_smalls, BL_categories = _extract_BL(
        buildings_gdf, city_blocks, enable_hotspot, hotspot_relax,
        height_mode=height_mode,
        narrow_threshold=narrow_threshold,
        narrow_penalty_factor=narrow_penalty)
    BL_polys = [p for p, _ in BL_with_heights]
    print(f"[preprocess] _extract_BL: {time.time() - t3:.1f}s")

    # ---- Step 4: BO (skip in merge_mode — block_base 已覆盖) ----
    t4 = time.time()
    if merge_mode:
        BO_polys, BO_filled_ids = [], set()
        print(f"[preprocess] _compute_BO: SKIPPED (merge_mode=True, "
              f"block_base covers building blocks)")
    else:
        BO_polys, BO_filled_ids = _compute_BO(
            BO_input_smalls, city_blocks, BL_polys, nozzle_real_m,
            density_threshold_override=density_threshold_override,
            count_threshold_override=count_threshold_override,
            print_limit_m2_override=print_limit_m2_override,
            aggregate_simplify_m_override=aggregate_simplify_m_override,
            mode_override=bo_mode_override)
    print(f"[preprocess] _compute_BO: {time.time() - t4:.1f}s")

    # ---- Step 5: VL / VO ----
    t5 = time.time()
    VL_polys, VO_polys = _extract_VL_VO(vegetation_gdf)
    print(f"[preprocess] _extract_VL_VO: {time.time() - t5:.1f}s")

    # ---- Step 6: WL / WO ----
    t6 = time.time()
    WL_polys, WO_polys, wl_lines_raw = _extract_WL_WO(water_gdf, nozzle_real_m)
    print(f"[preprocess] _extract_WL_WO: {time.time() - t6:.1f}s")

    # ---- Step 6b: 水体补全 (高德 + 自适应 buffer) ----
    if bbox_wgs84 and wl_lines_raw:
        try:
            from ._water_supplement import supplement_wl_coverage
            t6b = time.time()
            WL_polys = supplement_wl_coverage(
                WL_polys, wl_lines_raw, bbox_wgs84,
                utm_crs=utm_crs, origin=origin,
            )
            print(f"[preprocess] water_supplement: {time.time() - t6b:.1f}s")
        except Exception as e:
            print(f"[preprocess] water_supplement failed (non-fatal): {e}")

    # ---- Step 7 + 8: subtraction + filter ----
    t7 = time.time()
    filtered = _apply_subtraction_and_filter(
        BL_with_heights, BL_categories, BO_polys, VL_polys, VO_polys,
        WL_polys, WO_polys, BO_filled_ids, min_area_m2)
    print(f"[preprocess] subtraction+filter: {time.time() - t7:.1f}s")

    # ---- Step 8.5: block_base (与 PNG brick_render 对齐) ----
    t85 = time.time()
    # 对齐 PNG load_data：roads 只取 LineString/MultiLineString（排除 Point/Polygon）
    roads_lines_only = None
    if roads_gdf is not None and len(roads_gdf) > 0:
        mask = roads_gdf.geometry.type.isin(["LineString", "MultiLineString"])
        roads_lines_only = roads_gdf[mask] if mask.any() else None
    block_base_polys = _compute_block_base(
        city_blocks, BLOCK_BASE_MIN_AREA_M2,
        water_gdf=water_gdf,
        roads_gdf=roads_lines_only,
        buildings_gdf=buildings_gdf,
        veg_landmark_polys=VL_polys,
    )
    print(f"[preprocess] _compute_block_base: {len(block_base_polys)} "
          f"polys after {time.time() - t85:.1f}s")

    # ---- Step 8.6: classify block_base by landuse ----
    t86 = time.time()
    block_base_classes = _classify_block_base(
        block_base_polys, landuse_gdf, water_gdf, buildings_gdf)
    print(f"[preprocess] _classify_block_base: {time.time() - t86:.1f}s")

    # ---- Step 9: roads ----
    t9 = time.time()
    roads_lines = _extract_roads(roads_gdf, [p for p, _ in filtered["BL"]], area_km2)
    print(f"[preprocess] _extract_roads: {time.time() - t9:.1f}s")

    # ---- Step 10: assemble ----
    result = LayerPolygons(
        BL=filtered["BL"],
        BL_categories=BL_categories,
        BO=filtered["BO"],
        VL=filtered["VL"],
        VO=filtered["VO"],
        WL=filtered["WL"],
        WO=filtered["WO"],
        block_base=block_base_polys,
        block_base_classes=block_base_classes,
        roads_lines=roads_lines,
        nozzle_real_m=nozzle_real_m,
        min_area_m2=min_area_m2,
    )

    print(f"[preprocess] Total: {time.time() - t0:.1f}s, {result.summary()}")
    return result
