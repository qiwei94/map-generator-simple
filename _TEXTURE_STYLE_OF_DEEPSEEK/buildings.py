"""Building processor — 单阈值方案 1（buffer-union 聚合）。

流程:
  ≥ BUILDING_PRINT_LIMIT_M2 → 个体保留（OSM 高度压缩）
  < BUILDING_PRINT_LIMIT_M2 → 聚合管道:
        buffer(+B) → unary_union → buffer(-B+slack) → simplify
        聚合后再次 ≥ PRINT_LIMIT 才保留
  < MIN_AREA               → 直接丢弃（OSM 噪声）

PRINT_LIMIT 的物理意义: 0.4mm 喷嘴 × scale (~0.0078mm/m) ≈ 51m 实地宽度,
51 × 51 ≈ 2600m² 是打印能保留形状的下限; 3500m² 留 35% 余量。
"""

from __future__ import annotations

import time
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd
import trimesh
import manifold3d
from shapely.geometry import (
    GeometryCollection, LineString, MultiLineString, MultiPolygon, Polygon, box,
)
from shapely.ops import polygonize, unary_union
from shapely.strtree import STRtree
from shapely import concave_hull as _shapely_concave_hull, make_valid
from shapely.errors import GEOSException
import geopandas as gpd

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.terrain import sample_terrain_z
from _TEXTURE_STYLE_OF_DEEPSEEK._geom_utils import shapely_poly_to_crosssection
from _TEXTURE_STYLE_OF_DEEPSEEK._landmark import (
    is_tag_landmark, compute_top_percent_threshold,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.config import (
    BUILDING_HEIGHT_OSM_MIN_M,
    BUILDING_HEIGHT_OSM_MAX_M,
    BUILDING_DEFAULT_HEIGHT_M,
    Z_BUILDING_EMBED_MM,
    BUILDING_SIMPLIFY_TOL_M,
    BUILDING_MIN_AREA,
    BUILDING_PRINT_LIMIT_M2,
    BUILDING_AGGREGATE_BUFFER_M,
    BUILDING_AGGREGATE_SHRINK_SLACK_M,
    BUILDING_AGGREGATE_SIMPLIFY_M,
    BUILDING_AGGREGATE_HEIGHT_MM,
    BUILDING_V2_ENABLED,
    BUILDING_V2_MODE,
    BUILDING_V2_ROAD_TIER,
    BUILDING_V2_USE_WATER_BLOCKS,
    BUILDING_V2_BLOCK_BUFFER_M,
    BUILDING_V2_DENSITY_THRESHOLD,
    BUILDING_V2_CONCAVE_RATIO,
    BUILDING_V2_MIN_BLOCK_COMPACTNESS,
    BUILDING_V2_MIN_BLOCK_AREA_M2,
    BUILDING_V2_INDIVIDUAL_SHAPE,
    BUILDING_V2_AGGREGATE_SIMPLIFY_M,
    BUILDING_V2_MIN_BUILDINGS_PER_BLOCK,
    BUILDING_V2_COUNT_THRESHOLD,
    BUILDING_V2_USE_LANDMARK_TAGS,
    BUILDING_V2_LANDMARK_TOP_PERCENT,
    BUILDING_V2_BLOCK_FILL_CONVEX,
    get_area_class,
    estimate_building_height_from_area,
)

# 道路等级分层（高到低）— 越高 tier 越精细，街区切得越多
ROAD_TIERS = {
    1: ["motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link"],
    2: ["motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link",
        "secondary", "secondary_link", "tertiary", "tertiary_link"],
    3: ["motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link",
        "secondary", "secondary_link", "tertiary", "tertiary_link",
        "residential", "unclassified", "living_street"],
    4: ["motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link",
        "secondary", "secondary_link", "tertiary", "tertiary_link",
        "residential", "unclassified", "living_street",
        "service"],
    5: ["motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link",
        "secondary", "secondary_link", "tertiary", "tertiary_link",
        "residential", "unclassified", "living_street",
        "service",
        "pedestrian", "footway", "path", "steps", "track"],
}


HEIGHT_MAPPING_POLICY_VERSION = "city-relative-log-layer-v1"
_VERIFIED_HEIGHT_SOURCES = {"osm_height", "osm_levels", "wikidata", "overture"}
_CEILING_HEIGHT_SOURCES = {"osm_height", "wikidata", "overture"}


def building_height_mapping_context(buildings_gdf) -> dict:
    """Resolve a robust city-relative ceiling for printable landmark Z.

    A fixed 150 m ceiling makes every skyscraper look equally tall.  The
    99.5th percentile retains the city's own vertical character while keeping
    isolated bad values from controlling the complete model.  Small samples
    use their maximum because those rows are usually explicitly identified
    landmarks rather than a statistical building survey.
    """
    empty = {
        "policy_version": HEIGHT_MAPPING_POLICY_VERSION,
        "verified_height_count": 0,
        "height_ceiling_m": float(BUILDING_HEIGHT_OSM_MAX_M),
        "height_p50_m": None,
        "height_p95_m": None,
        "height_p99_5_m": None,
        "height_ceiling_sample_count": 0,
        "height_ceiling_source_counts": {},
        "identity_height_ceiling_m": None,
        "source_counts": {},
    }
    if buildings_gdf is None or len(buildings_gdf) == 0:
        return empty

    sources = (buildings_gdf["height_source"].fillna("unknown").astype(str)
               if "height_source" in buildings_gdf.columns
               else None)
    source_counts = (sources.value_counts().sort_index().to_dict()
                     if sources is not None else {})
    if "est_height" not in buildings_gdf.columns:
        return {**empty, "source_counts": {
            str(key): int(value) for key, value in source_counts.items()}}

    heights = np.asarray(pd.to_numeric(
        buildings_gdf["est_height"], errors="coerce"), dtype=float)
    valid = np.isfinite(heights) & (heights > 0) & (heights <= 1200)
    if sources is not None:
        valid &= sources.isin(_VERIFIED_HEIGHT_SOURCES).to_numpy()
    values = heights[valid]
    if len(values) == 0:
        return {**empty, "source_counts": {
            str(key): int(value) for key, value in source_counts.items()}}

    p50, p95, p99_5 = np.percentile(values, [50, 95, 99.5])
    ceiling_mask = valid
    if sources is not None:
        ceiling_mask = valid & sources.isin(_CEILING_HEIGHT_SOURCES).to_numpy()
    ceiling_values = heights[ceiling_mask]
    if len(ceiling_values) == 0:
        ceiling_values = values
    ceiling_p99_5 = float(np.percentile(ceiling_values, 99.5))
    statistical_ceiling = float(
        ceiling_values.max() if len(ceiling_values) < 20 else ceiling_p99_5)
    identity_values = np.asarray([], dtype=float)
    if sources is not None:
        identity_mask = valid & sources.eq("wikidata").to_numpy()
        identity_values = heights[identity_mask]
    identity_ceiling = (float(identity_values.max())
                        if len(identity_values) else None)
    if identity_ceiling is not None:
        statistical_ceiling = max(statistical_ceiling, identity_ceiling)
    ceiling = min(1200.0, max(float(BUILDING_HEIGHT_OSM_MAX_M),
                              statistical_ceiling))
    ceiling_source_counts = {}
    if sources is not None:
        selected_sources = sources.iloc[np.flatnonzero(ceiling_mask)]
        ceiling_source_counts = {
            str(key): int(value) for key, value in
            selected_sources.value_counts().sort_index().items()}
    return {
        "policy_version": HEIGHT_MAPPING_POLICY_VERSION,
        "verified_height_count": int(len(values)),
        "height_ceiling_m": round(ceiling, 3),
        "height_p50_m": round(float(p50), 3),
        "height_p95_m": round(float(p95), 3),
        "height_p99_5_m": round(float(p99_5), 3),
        "height_ceiling_sample_count": int(len(ceiling_values)),
        "height_ceiling_source_counts": ceiling_source_counts,
        "identity_height_ceiling_m": (
            round(identity_ceiling, 3)
            if identity_ceiling is not None else None),
        "source_counts": {
            str(key): int(value) for key, value in source_counts.items()},
    }


def _compress_height(
    est_height_m: float,
    area_m2: float,
    *,
    height_ceiling_m: Optional[float] = None,
) -> float:
    """压缩建筑高度到 model mm 范围 (BUILDING_HEIGHT_MIN_MM..MAX_MM)。

    当高度为默认回退值(10m)时，改用面积估算以区分大小建筑，
    避免小棚子和大楼获得同样高度导致"细长棍"效果。

    Uses log compression: common heights (8-60m) get more model space,
    while extreme heights (100m+) are compressed together.
    """
    # Function-level import: allows runtime monkey-patch from auto-params
    from _TEXTURE_STYLE_OF_DEEPSEEK.config import (
        BUILDING_HEIGHT_MIN_MM, BUILDING_HEIGHT_MAX_MM,
    )
    import math
    # 默认回退值 → 用面积估算，区分大小建筑
    if est_height_m == BUILDING_DEFAULT_HEIGHT_M or est_height_m <= 0:
        effective = estimate_building_height_from_area(area_m2)
    else:
        effective = est_height_m
    h_min = BUILDING_HEIGHT_OSM_MIN_M
    h_max = max(
        float(BUILDING_HEIGHT_OSM_MAX_M),
        min(1200.0, float(height_ceiling_m or BUILDING_HEIGHT_OSM_MAX_M)),
    )
    m_min = BUILDING_HEIGHT_MIN_MM
    m_max = BUILDING_HEIGHT_MAX_MM
    clamped = max(h_min, min(h_max, effective))
    t = (math.log(clamped) - math.log(h_min)) / (math.log(h_max) - math.log(h_min))
    return m_min + t * (m_max - m_min)


def _quantize_height_mm(height_mm: float, layer_height_mm: float) -> float:
    """Round a positive model height up to a complete printable layer."""
    import math

    layer = float(layer_height_mm)
    if not math.isfinite(layer) or layer <= 0:
        return float(height_mm)
    return round(math.ceil(float(height_mm) / layer - 1e-9) * layer, 10)


def _narrow_building_penalty(poly, h_mm: float,
                             threshold: float = 6.0,
                             factor: float = 0.5) -> float:
    """Reduce height for narrow/elongated buildings exceeding aspect ratio threshold."""
    if threshold <= 0:
        return h_mm
    try:
        mrr = poly.minimum_rotated_rectangle
        coords = list(mrr.exterior.coords)
        edge1 = ((coords[0][0] - coords[1][0])**2 + (coords[0][1] - coords[1][1])**2) ** 0.5
        edge2 = ((coords[1][0] - coords[2][0])**2 + (coords[1][1] - coords[2][1])**2) ** 0.5
        short = min(edge1, edge2)
        long = max(edge1, edge2)
        if short < 1e-3:
            return h_mm * factor
        aspect = long / short
        if aspect > threshold:
            return h_mm * factor
    except Exception:
        pass
    return h_mm


def _extrude_polygon_manifold(footprint: Polygon, height_mm: float,
                               terrain_z: float, scale: float
                              ) -> manifold3d.Manifold:
    """单 polygon 挤出成 watertight Manifold solid。"""
    cs = shapely_poly_to_crosssection(footprint)
    if cs.is_empty():
        return manifold3d.Manifold()
    try:
        cs = cs.scale((scale, scale))
        z_bottom = terrain_z - Z_BUILDING_EMBED_MM
        return cs.extrude(height=height_mm).translate((0, 0, z_bottom))
    except Exception:
        return manifold3d.Manifold()


def _aggregate_small_buildings(small_polys: List[Polygon]
                              ) -> List[Polygon]:
    """聚合管道（方案 1）:
        buffer(+B) → unary_union → buffer(-B + slack) → simplify
    输出保留 ≥ PRINT_LIMIT 的连通块。
    """
    if not small_polys:
        return []
    B = BUILDING_AGGREGATE_BUFFER_M
    slack = BUILDING_AGGREGATE_SHRINK_SLACK_M

    # 外扩
    buffered = [p.buffer(B, join_style=2) for p in small_polys if not p.is_empty]
    merged = unary_union(buffered)
    if merged.is_empty:
        return []

    # 收缩（少收一点，街区边缘留 slack 厚度，更"块"）
    shrunk = merged.buffer(-(B - slack), join_style=2)
    if shrunk.is_empty:
        return []

    # 简化 — 去掉 buffer 圆角，让边界变直
    shrunk = shrunk.simplify(BUILDING_AGGREGATE_SIMPLIFY_M, preserve_topology=True)
    if shrunk.is_empty:
        return []

    polys = list(shrunk.geoms) if isinstance(shrunk, MultiPolygon) else [shrunk]
    # 聚合后还要 ≥ PRINT_LIMIT 才有意义
    return [p for p in polys
            if isinstance(p, Polygon) and not p.is_empty
            and p.area >= BUILDING_PRINT_LIMIT_M2]


# ---------------------------------------------------------------------------
# v2: 路网 polygonize 聚合
# ---------------------------------------------------------------------------

def _compactness(poly: Polygon) -> float:
    """Polsby–Popper 紧凑度: 4π·area / perimeter².
    圆=1.0, 正方形≈0.785, 长条/三角→0。"""
    L = poly.length
    if L <= 0:
        return 0.0
    return 4.0 * np.pi * poly.area / (L * L)


def _convex_quadrilateral(poly: Polygon) -> Polygon:
    """保证输出凸 + ≥ 4 顶点（审美：不要三角形 / 内凹）。
    convex_hull 兜底；若 hull 仍是三角形，升级到 min_rotated_rectangle。
    """
    if not isinstance(poly, Polygon) or poly.is_empty:
        return poly
    hull = poly.convex_hull
    if not isinstance(hull, Polygon) or hull.is_empty:
        return poly.minimum_rotated_rectangle
    n_unique = len(hull.exterior.coords) - 1
    if n_unique < 4:
        return poly.minimum_rotated_rectangle
    return hull



def _build_city_blocks(roads_gdf: gpd.GeoDataFrame,
                       water_gdf: gpd.GeoDataFrame,
                       road_tier: int,
                       bbox_local: Tuple[float, float, float, float]
                      ) -> List[Polygon]:
    """用道路 + 水体边界 + bbox 多边形化出城市街区。"""
    allowed = set(ROAD_TIERS[road_tier])

    if "highway" in roads_gdf.columns:
        rfilt = roads_gdf[roads_gdf["highway"].isin(allowed)].copy()
    else:
        rfilt = roads_gdf.copy()

    bbox_lines = box(*bbox_local).boundary

    lines: list = [bbox_lines]
    for geom in rfilt.geometry:
        if geom is None or geom.is_empty:
            continue
        if isinstance(geom, MultiLineString):
            lines.extend(geom.geoms)
        elif isinstance(geom, LineString):
            lines.append(geom)

    if water_gdf is not None and len(water_gdf) > 0:
        for geom in water_gdf.geometry:
            if geom is None or geom.is_empty:
                continue
            if isinstance(geom, Polygon):
                lines.append(geom.exterior)
                for hole in geom.interiors:
                    lines.append(hole)
            elif isinstance(geom, MultiPolygon):
                for g in geom.geoms:
                    if g.is_empty:
                        continue
                    lines.append(g.exterior)
                    for hole in g.interiors:
                        lines.append(hole)
            elif isinstance(geom, MultiLineString):
                lines.extend(geom.geoms)
            elif isinstance(geom, LineString):
                lines.append(geom)

    noded = unary_union(lines)
    blocks = list(polygonize(noded))
    return blocks


def _aggregate_in_blocks(small_polys: List[Polygon],
                         blocks: List[Polygon],
                         mode: str = "oriented_bbox",
                         print_limit_m2: float = 2500.0,
                         simplify_m: float = 80.0,
                         bldg_buffer_m: float = 20.0,
                         density_threshold: float = 0.25,
                         concave_ratio: float = 0.7,
                         min_block_compactness: float = 0.0,
                         min_block_area_m2: float = 0.0,
                         min_buildings_per_block: int = 0,
                         count_threshold: int = 1,
                         block_fill_convex: bool = True,
                         landmark_polys: List[Polygon] = None,
                        ) -> List[Polygon]:
    """每 block 内小楼聚合成街区 footprint。

    Mode:
      'union'          直接 unary_union(polys) ∩ block
      'buffered_union'  unary_union(polys.buffer(B)) ∩ block
      'density_fill'    block 密 → 整 block 当 footprint，否则 buffered_union 兜底
      'block_fill'      count ≥ N AND density ≥ D → 整 block，否则丢弃（reference 风格）
      'convex_hull'     unary_union(polys).convex_hull ∩ block
      'concave_hull'    concave_hull(union, ratio) ∩ block
      'oriented_bbox'   min_rotated_rectangle ∩ block

    compactness < min_block_compactness 的 block 被跳过（去三角 sliver）。

    landmark_polys（仅 block_fill 触发用）:
      地标 footprint 列表，参与 count + density 计算（让带地标的 block 更容易触发 fill），
      但不进入输出几何（地标在 build_deepseek_buildings 里独立 extrude 成 landmarks_mesh）。
      主入口仍是"有小楼的 block"，纯地标 / 无小楼的 block 不会被填。
    """
    if not small_polys or not blocks:
        return []

    def _safe_union(polys):
        try:
            return unary_union(polys)
        except GEOSException:
            repaired = []
            for geom in polys:
                try:
                    repaired.append(geom if geom.is_valid else make_valid(geom))
                except GEOSException:
                    continue
            try:
                return unary_union(repaired) if repaired else GeometryCollection()
            except GEOSException:
                return GeometryCollection()

    def _safe_intersection(left, right):
        try:
            return left.intersection(right)
        except GEOSException:
            try:
                safe_left = left if left.is_valid else make_valid(left)
                safe_right = right if right.is_valid else make_valid(right)
                return safe_left.intersection(safe_right)
            except GEOSException:
                try:
                    return left.buffer(0).intersection(right.buffer(0))
                except GEOSException:
                    return GeometryCollection()

    centroids = [p.centroid for p in small_polys]
    block_tree = STRtree(blocks)

    bldg_in_block: dict[int, list] = {}
    for bi, c in enumerate(centroids):
        for ci in block_tree.query(c):
            if blocks[ci].contains(c):
                bldg_in_block.setdefault(ci, []).append(bi)
                break

    # 地标 centroid → block 平行映射（仅用于 count/density 触发）
    landmark_in_block: dict[int, list] = {}
    if landmark_polys:
        for li, c in enumerate([p.centroid for p in landmark_polys]):
            for ci in block_tree.query(c):
                if blocks[ci].contains(c):
                    landmark_in_block.setdefault(ci, []).append(li)
                    break

    n_skipped_compact = 0
    n_skipped_area = 0
    n_skipped_few = 0
    aggregated: list[Polygon] = []
    for ci, bi_list in bldg_in_block.items():
        block = blocks[ci]

        # block 内建筑数量不足 → 跳过（防水体 / 空地被 oriented_bbox 等模式凭空"造楼"）
        if min_buildings_per_block > 0 and len(bi_list) < min_buildings_per_block:
            n_skipped_few += 1
            continue
        if min_block_area_m2 > 0 and block.area < min_block_area_m2:
            n_skipped_area += 1
            continue
        if min_block_compactness > 0 and _compactness(block) < min_block_compactness:
            n_skipped_compact += 1
            continue

        block_polys = [small_polys[bi] for bi in bi_list]

        if mode == "block_fill":
            # reference 风格：count + density 双阈值通过 → 整 block 当 footprint
            # 不达标 → 直接丢弃，无 fallback
            # 地标参与 count/density 计算，但不画几何（地标独立 extrude）
            lm_in_this = [landmark_polys[li] for li in landmark_in_block.get(ci, [])] \
                          if landmark_polys else []
            n_polys = len(bi_list) + len(lm_in_this)
            total_area = sum(p.area for p in block_polys) + sum(p.area for p in lm_in_this)
            density = total_area / max(block.area, 1.0)
            if n_polys >= count_threshold and density >= density_threshold:
                shape = _convex_quadrilateral(block) if block_fill_convex else block
            else:
                continue
        elif mode == "density_fill":
            total_area = sum(p.area for p in block_polys)
            if total_area / max(block.area, 1.0) >= density_threshold:
                shape = block
            else:
                buffered = [p.buffer(bldg_buffer_m, join_style=2) for p in block_polys]
                shape = _safe_intersection(_safe_union(buffered), block)
        elif mode == "buffered_union":
            buffered = [p.buffer(bldg_buffer_m, join_style=2) for p in block_polys]
            shape = _safe_intersection(_safe_union(buffered), block)
        elif mode == "convex_hull":
            shape = _safe_intersection(_safe_union(block_polys).convex_hull,
                                       block)
        elif mode == "concave_hull":
            try:
                hull = _shapely_concave_hull(
                    _safe_union(block_polys), ratio=concave_ratio,
                    allow_holes=False
                )
                shape = _safe_intersection(hull, block)
            except Exception:
                buffered = [p.buffer(bldg_buffer_m, join_style=2) for p in block_polys]
                shape = _safe_intersection(_safe_union(buffered), block)
        elif mode == "oriented_bbox":
            shape = _safe_intersection(
                _safe_union(block_polys).minimum_rotated_rectangle, block)
        else:  # 'union'
            shape = _safe_intersection(_safe_union(block_polys), block)

        if shape.is_empty:
            continue
        if simplify_m > 0:
            shape = shape.simplify(simplify_m, preserve_topology=True)
        if shape.is_empty:
            continue

        if isinstance(shape, Polygon):
            polys_out = [shape]
        elif hasattr(shape, "geoms"):
            polys_out = [g for g in shape.geoms if isinstance(g, Polygon)]
        else:
            continue
        # oriented_bbox: min_rotated_rectangle ∩ block 再 simplify 后会出三角，
        # 走和 block_fill 一样的"凸+≥4 顶点"guard
        if mode == "oriented_bbox":
            polys_out = [_convex_quadrilateral(p) for p in polys_out]
            polys_out = [p for p in polys_out
                         if isinstance(p, Polygon) and not p.is_empty]
        # block_fill 跳过 print_limit 出口面积过滤（block 已被前置阈值过滤；
        # 否则把小街区全杀掉）。其它模式仍要 ≥ print_limit 才印得出。
        if mode == "block_fill":
            for p in polys_out:
                if not p.is_empty:
                    aggregated.append(p)
        else:
            for p in polys_out:
                if not p.is_empty and p.area >= print_limit_m2:
                    aggregated.append(p)

    if n_skipped_compact or n_skipped_area or n_skipped_few:
        print(f"    v2 跳过 block: n<{min_buildings_per_block}={n_skipped_few}, "
              f"compact<{min_block_compactness}={n_skipped_compact}, "
              f"area<{min_block_area_m2}={n_skipped_area}")
    return aggregated


def build_deepseek_buildings(gdf: gpd.GeoDataFrame,
                             terrain_mesh: trimesh.Trimesh,
                             area_km2: float = 0,
                             scale: float = 1.0,
                             roads_gdf: gpd.GeoDataFrame = None,
                             water_gdf: gpd.GeoDataFrame = None,
                             bbox_local: Tuple[float, float, float, float] = None,
                            ) -> dict:
    """Buildings 双 mesh 构建 — 返回 {"landmarks": Trimesh, "buildings": Trimesh}.
    landmarks = 标签/大面积识别的地标个体（E5 暖砂石突出）
    buildings = block_fill 出来的街区填充（E1 灰，融入 terrain）
    任一字段可能为 None（无对应几何时）。
    """
    if gdf is None or len(gdf) == 0:
        return None

    area_class = get_area_class(area_km2)
    min_area = BUILDING_MIN_AREA.get(area_class, 30)

    # Pre-pass: 计算 percentile 兜底面积阈值
    use_landmark_tags = BUILDING_V2_ENABLED and BUILDING_V2_USE_LANDMARK_TAGS
    if BUILDING_V2_ENABLED and BUILDING_V2_LANDMARK_TOP_PERCENT > 0:
        all_areas = [g.area for g in gdf.geometry
                     if g is not None and not g.is_empty and g.area >= 1.0]
        landmark_top_thr = compute_top_percent_threshold(
            all_areas, BUILDING_V2_LANDMARK_TOP_PERCENT)
    else:
        landmark_top_thr = float("inf")

    # Step 1: 简化 + 大小分流（is_landmark = 标签 OR ≥ print_limit OR ≥ top%）
    individuals: list[Tuple[Polygon, float]] = []   # 地标个体保留
    smalls: list[Polygon] = []                       # 待 block_fill
    n_dropped = 0
    n_lm_tag = 0
    n_lm_size = 0
    n_lm_top = 0
    for idx, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        polys = geom.geoms if isinstance(geom, MultiPolygon) else [geom]
        for poly in polys:
            if poly.area < 1.0:    # 退化几何
                n_dropped += 1
                continue
            simplified = poly.simplify(BUILDING_SIMPLIFY_TOL_M, preserve_topology=True)
            if simplified.is_empty or simplified.area < 1.0:
                continue
            if isinstance(simplified, MultiPolygon):
                simplified = max(simplified.geoms, key=lambda g: g.area)
            if not isinstance(simplified, Polygon) or simplified.is_empty:
                continue

            # Landmark 三层 OR 判定
            tag_lm = use_landmark_tags and is_tag_landmark(row, area_m2=poly.area)
            size_lm = simplified.area >= BUILDING_PRINT_LIMIT_M2
            top_lm = simplified.area >= landmark_top_thr
            is_landmark = tag_lm or size_lm or top_lm

            if is_landmark:
                if tag_lm: n_lm_tag += 1
                elif size_lm: n_lm_size += 1
                elif top_lm: n_lm_top += 1
                # v2: 大楼外形规整化（默认 raw 保持原样）
                if BUILDING_V2_ENABLED:
                    iva = BUILDING_V2_INDIVIDUAL_SHAPE
                    if iva == "convex":
                        simplified = simplified.convex_hull
                    elif iva == "bbox":
                        simplified = simplified.minimum_rotated_rectangle
                    if not isinstance(simplified, Polygon) or simplified.is_empty:
                        continue
                est_height = gdf.loc[idx].get("est_height", 0)
                h_mm = _compress_height(est_height, simplified.area)
                h_mm = _narrow_building_penalty(simplified, h_mm)
                individuals.append((simplified, h_mm))
            else:
                smalls.append(simplified)

    print(f"  Buildings input: {len(individuals)} 个体 "
          f"(tag={n_lm_tag}, size≥{BUILDING_PRINT_LIMIT_M2:.0f}={n_lm_size}, "
          f"top{BUILDING_V2_LANDMARK_TOP_PERCENT}%={n_lm_top}), "
          f"{len(smalls)} 待聚合, {n_dropped} 噪声丢弃")

    # Step 2: 小楼聚合（v1 buffer-union 或 v2 路网 polygonize）
    if BUILDING_V2_ENABLED and roads_gdf is not None:
        # bbox_local 优先用调用方传入的真实地图 bbox（pipeline/cli 知道 utm_bbox - origin）
        # 退化时才用 buildings.total_bounds — 但那会把外围路/水切丢，仅作兜底
        if bbox_local is None:
            bbox_local = tuple(gdf.total_bounds)
            print(f"  [warn] bbox_local 未传入，fallback 到 buildings.total_bounds "
                  f"= {bbox_local}（外围路网/水体可能丢失）")
        use_water = BUILDING_V2_USE_WATER_BLOCKS and water_gdf is not None
        wgdf = water_gdf if use_water else None
        city_blocks = _build_city_blocks(roads_gdf, wgdf,
                                         BUILDING_V2_ROAD_TIER, bbox_local)
        blocks = _aggregate_in_blocks(
            smalls, city_blocks,
            mode=BUILDING_V2_MODE,
            print_limit_m2=BUILDING_PRINT_LIMIT_M2,
            simplify_m=BUILDING_V2_AGGREGATE_SIMPLIFY_M,
            bldg_buffer_m=BUILDING_V2_BLOCK_BUFFER_M,
            density_threshold=BUILDING_V2_DENSITY_THRESHOLD,
            concave_ratio=BUILDING_V2_CONCAVE_RATIO,
            min_block_compactness=BUILDING_V2_MIN_BLOCK_COMPACTNESS,
            min_block_area_m2=BUILDING_V2_MIN_BLOCK_AREA_M2,
            min_buildings_per_block=BUILDING_V2_MIN_BUILDINGS_PER_BLOCK,
            count_threshold=BUILDING_V2_COUNT_THRESHOLD,
            block_fill_convex=BUILDING_V2_BLOCK_FILL_CONVEX,
            landmark_polys=[p for p, _ in individuals],   # 地标参与 count/density
        )
        print(f"  v2 路网聚合: {len(smalls)} → {len(blocks)} 街区柱体 "
              f"(mode={BUILDING_V2_MODE}, tier={BUILDING_V2_ROAD_TIER}, "
              f"simplify={BUILDING_V2_AGGREGATE_SIMPLIFY_M}m, "
              f"compact≥{BUILDING_V2_MIN_BLOCK_COMPACTNESS})")
    else:
        blocks = _aggregate_small_buildings(smalls)
        print(f"  v1 缓冲聚合: {len(smalls)} → {len(blocks)} 街区柱体 "
              f"(buffer={BUILDING_AGGREGATE_BUFFER_M}m, "
              f"simplify={BUILDING_AGGREGATE_SIMPLIFY_M}m)")

    # Step 3-6 抽成 helper，分别跑两次：地标个体 (E5 暖砂石) + 街区填充 (E1 灰)
    landmarks_mesh = _build_mesh_from_items(individuals, terrain_mesh, scale,
                                             label="Landmarks")
    ambient_items = [(p, BUILDING_AGGREGATE_HEIGHT_MM) for p in blocks]
    ambient_mesh = _build_mesh_from_items(ambient_items, terrain_mesh, scale,
                                           label="Block-fill")

    if landmarks_mesh is None and ambient_mesh is None:
        return None
    return {"landmarks": landmarks_mesh, "buildings": ambient_mesh}


def _build_mesh_from_items(items: List[Tuple[Polygon, float]],
                           terrain_mesh: trimesh.Trimesh,
                           scale: float,
                           label: str = "") -> trimesh.Trimesh:
    """对 [(poly, h_mm), ...] 整体 extrude → batch_boolean Add → 单一 watertight Trimesh。"""
    if not items:
        return None

    cents_x = np.array([p.centroid.x for p, _ in items])
    cents_y = np.array([p.centroid.y for p, _ in items])
    terrain_z = sample_terrain_z(terrain_mesh, cents_x * scale, cents_y * scale)

    t_extrude = time.time()
    parts: List[manifold3d.Manifold] = []
    for (poly, h_mm), tz in zip(items, terrain_z):
        if np.isnan(tz):
            continue
        man = _extrude_polygon_manifold(poly, h_mm, float(tz), scale)
        if not man.is_empty():
            parts.append(man)
    print(f"  [{label}] {len(parts)} Manifold parts (extrude {time.time()-t_extrude:.1f}s)")

    if not parts:
        return None

    t_union = time.time()
    if len(parts) == 1:
        combined = parts[0]
    else:
        combined = manifold3d.Manifold.batch_boolean(parts, manifold3d.OpType.Add)
    print(f"  [{label}] batch_boolean Add {time.time()-t_union:.1f}s")

    if combined.is_empty():
        return None

    mesh_data = combined.to_mesh()
    verts = np.array(mesh_data.vert_properties, dtype=np.float64)
    faces = np.array(mesh_data.tri_verts, dtype=np.int64)
    out = trimesh.Trimesh(vertices=verts, faces=faces, process=False)
    out.metadata["manifold"] = combined
    return out


# ---------------------------------------------------------------------------
# V3 builder: 接收 preprocess 输出的 polygon，直接 extrude
# ---------------------------------------------------------------------------

def build_deepseek_buildings_v3(
    BL_with_heights: List[Tuple[Polygon, float]],
    BO_polys: List[Polygon],
    terrain_mesh: trimesh.Trimesh,
    scale: float,
    brick_style: bool = True,
    bbox_local: Tuple[float, float, float, float] = None,
) -> "Dict[str, Optional[trimesh.Trimesh]]":
    """V3 buildings builder — geometry 已在 preprocess 阶段去重，这里只负责 extrude。

    Args:
        BL_with_heights: [(poly, height_mm)] 地标建筑
        BO_polys: [poly] 街区填充
        terrain_mesh: 地形网格（用于 Z 采样）
        scale: mm/m
        brick_style: True 则对 BO + BL 做 brick 几何变换再 extrude
        bbox_local: (xmin, ymin, xmax, ymax) 裁剪边界，防止 brick 变换后超出地形

    Returns:
        {"landmarks": Trimesh|None, "buildings": Trimesh|None}
    """
    from _TEXTURE_STYLE_OF_DEEPSEEK.config import BUILDING_AGGREGATE_HEIGHT_MM
    from _TEXTURE_STYLE_OF_DEEPSEEK.config import (
        BRICK_CORNER_R_M, BRICK_ROT_DEG, BRICK_SHIFT_M,
        BRICK_PERLIN_AMP, BRICK_PERLIN_FREQ, BRICK_RESAMPLE_M,
    )

    if brick_style and (BL_with_heights or BO_polys):
        from _TEXTURE_STYLE_OF_DEEPSEEK._brick_transform import brick_transform_batch
        clip_box = box(*bbox_local) if bbox_local else None
        if BO_polys:
            t0 = time.time()
            bo_transformed = brick_transform_batch(
                BO_polys,
                corner_r_m=BRICK_CORNER_R_M, rot_deg=BRICK_ROT_DEG,
                shift_m=BRICK_SHIFT_M,
                perlin_amp=BRICK_PERLIN_AMP, perlin_freq=BRICK_PERLIN_FREQ,
                resample_m=BRICK_RESAMPLE_M,
                noise_seed=2026)
            if clip_box:
                bo_transformed = [p.intersection(clip_box) for p in bo_transformed]
                bo_transformed = [p for p in bo_transformed
                                  if isinstance(p, Polygon) and not p.is_empty]
            BO_polys = bo_transformed
            print(f"  BO brick transform: {len(BO_polys)} polys in {time.time()-t0:.1f}s")
        if BL_with_heights:
            t0 = time.time()
            bl_polys = [p for p, _ in BL_with_heights]
            bl_heights = [h for _, h in BL_with_heights]
            bl_transformed = brick_transform_batch(
                bl_polys,
                corner_r_m=BRICK_CORNER_R_M, rot_deg=BRICK_ROT_DEG,
                shift_m=BRICK_SHIFT_M,
                perlin_amp=BRICK_PERLIN_AMP, perlin_freq=BRICK_PERLIN_FREQ,
                resample_m=BRICK_RESAMPLE_M,
                noise_seed=2026 + 7777)
            if clip_box:
                clipped_bl = []
                for p, h in zip(bl_transformed, bl_heights):
                    c = p.intersection(clip_box)
                    if isinstance(c, Polygon) and not c.is_empty:
                        clipped_bl.append((c, h))
                BL_with_heights = clipped_bl
            else:
                BL_with_heights = list(zip(bl_transformed, bl_heights))
            print(f"  BL brick transform: {len(BL_with_heights)} polys in {time.time()-t0:.1f}s")

    # Landmarks (E5 暖砂石) — 各自高度
    landmarks_mesh = _build_mesh_from_items(
        BL_with_heights, terrain_mesh, scale, label="Landmarks(v3)")

    # Buildings (E1 灰) — 统一 BUILDING_AGGREGATE_HEIGHT_MM
    ambient_items = [(p, BUILDING_AGGREGATE_HEIGHT_MM) for p in BO_polys]
    ambient_mesh = _build_mesh_from_items(
        ambient_items, terrain_mesh, scale, label="Block-fill(v3)")

    if landmarks_mesh is None and ambient_mesh is None:
        return {"landmarks": None, "buildings": None}
    return {"landmarks": landmarks_mesh, "buildings": ambient_mesh}
