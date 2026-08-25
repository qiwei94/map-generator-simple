"""Road processor — shapely union → Manifold extrude.

Strategy（与 reference 风格一致 + 干净的 Manifold 输出）：
  1. 按 highway 类型过滤（large 区域只留 motorway/trunk/primary/secondary）
  2. 每条 LineString 按等级 buffer 成多边形（cap_style=2 平角，避免长尾）
  3. shapely.unary_union 一次合并所有 buffered 多边形
     —— T 字交叉、网络分叉处的几何关系都自然处理，不再有非流形 ribbon 拼接问题
  4. 对 union 出来的每个连通块单独 Manifold extrude（按各自质心采地形 Z）
  5. batch_boolean(Add) 输出单一 watertight 体

注意：
  - 旧的 per-vertex terrain Z 采样（让 ribbon 沿地形起伏）丢失了：一个连通块
    取一个 Z 值。在 25km × 4mm 厚的模型尺度上，单 mesh 内部 0.5mm 的相对
    起伏不可见，trade-off 值。
  - 桥梁过滤（filter_bridges_only）保留兼容；object4 仍用桥梁段做 terrain
    布尔并。
"""

from __future__ import annotations

import time
from typing import List

import numpy as np
import trimesh
import manifold3d
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Polygon
from shapely.ops import unary_union
from shapely.strtree import STRtree
import geopandas as gpd

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.terrain import sample_terrain_z
from _TEXTURE_STYLE_OF_DEEPSEEK._geom_utils import shapely_poly_to_crosssection
from _TEXTURE_STYLE_OF_DEEPSEEK.bridge_filter import filter_bridges_only as filter_bridge_roads
from _TEXTURE_STYLE_OF_DEEPSEEK.print_profile import (
    DEFAULT_PRINTER_PROFILE,
    PrinterProfile,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.road_roles import resolve_composed_road_width_m

from _TEXTURE_STYLE_OF_DEEPSEEK.config import (
    ROAD_THICKNESS_MM,
    Z_ROAD_ABOVE_TERRAIN_MM,
    ROAD_WIDTHS,
    ROAD_FILTER,
    ROAD_MIN_LINE_LENGTH_M,
    ROAD_DEFAULT_WIDTH_M,
    ROAD_BRIDGE_EXTRA_MM,
    get_area_class,
)


def _buffer_lines(gdf: gpd.GeoDataFrame, highway_filter) -> List[Polygon]:
    """把 gdf 里所有 LineString 按 highway 等级 buffer 成多边形。

    单条线失败（shapely 2.x 偶现 'Component rings have coordinate sequences,
    but the polygon does not'）时跳过该条，不让整批挂掉。
    """
    # Function-level import: allows runtime monkey-patch from auto-params
    from _TEXTURE_STYLE_OF_DEEPSEEK.config import ROAD_WIDTH_MULTIPLIER
    polys: List[Polygon] = []
    n_skip_filter = 0
    n_skip_short = 0
    n_skip_err = 0
    for idx, row in gdf.iterrows():
        highway = row.get("highway", "residential")
        if highway_filter is not None and highway not in highway_filter:
            n_skip_filter += 1
            continue
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        lines = geom.geoms if isinstance(geom, MultiLineString) else [geom]
        if not isinstance(geom, (LineString, MultiLineString)):
            continue
        width = ROAD_WIDTHS.get(highway, ROAD_DEFAULT_WIDTH_M) * ROAD_WIDTH_MULTIPLIER
        half_w = width / 2.0
        for line in lines:
            if line.length < ROAD_MIN_LINE_LENGTH_M:
                n_skip_short += 1
                continue
            try:
                buf = line.buffer(half_w, cap_style=2, join_style=2)
            except Exception:
                n_skip_err += 1
                continue
            if buf is None or buf.is_empty:
                continue
            if isinstance(buf, MultiPolygon):
                polys.extend(buf.geoms)
            elif isinstance(buf, Polygon):
                polys.append(buf)
    print(f"  Roads: {len(polys)} buffered polygons "
          f"(filter skipped {n_skip_filter}, short {n_skip_short}, errors {n_skip_err})")
    return polys


def _polygon_to_manifold_road(poly: Polygon, terrain_z_mm: float, scale: float
                              ) -> manifold3d.Manifold:
    """道路 polygon 挤出 ROAD_THICKNESS_MM，顶面 = terrain_z + Z_ROAD_ABOVE_TERRAIN_MM。"""
    cs = shapely_poly_to_crosssection(poly)
    if cs.is_empty():
        return manifold3d.Manifold()
    try:
        cs = cs.scale((scale, scale))
        z_top = terrain_z_mm + Z_ROAD_ABOVE_TERRAIN_MM
        z_bottom = z_top - ROAD_THICKNESS_MM
        return cs.extrude(height=ROAD_THICKNESS_MM).translate((0, 0, z_bottom))
    except Exception:
        return manifold3d.Manifold()


def build_deepseek_roads(gdf: gpd.GeoDataFrame,
                         terrain_mesh: trimesh.Trimesh,
                         area_km2: float = 0,
                         scale: float = 1.0,
                         water_gdf: gpd.GeoDataFrame = None,
                         filter_bridges_only: bool = False) -> trimesh.Trimesh:
    """Build a single watertight road mesh via shapely union + Manifold extrude.

    Args:
        gdf: GeoDataFrame of road LineStrings in local UTM meters.
        terrain_mesh: scaled terrain mesh (model mm) for centroid Z sampling.
        area_km2: area for highway-type LOD filtering.
        scale: mm per meter.
        water_gdf: optional, used when filter_bridges_only=True.
        filter_bridges_only: if True, only build bridge segments crossing water.

    Returns:
        Single watertight trimesh, or None if no qualifying roads.
    """
    if gdf is None or len(gdf) == 0:
        return None

    # 桥梁过滤（旧 obj4 路径用过；独立 roads 一般不开）
    bridges_mode = False
    if filter_bridges_only and water_gdf is not None and len(water_gdf) > 0:
        print("\n[道路处理] 启用桥梁过滤模式...")
        gdf = filter_bridge_roads(gdf, water_gdf, extract_water_crossing_only=True)
        if len(gdf) == 0:
            print("  过滤后无桥梁道路，返回空")
            return None
        bridges_mode = True

    area_class = get_area_class(area_km2)
    # 桥梁模式下不做 highway 二次过滤 — 已经是桥就该保留所有等级（含小桥）
    highway_filter = None if bridges_mode else ROAD_FILTER.get(area_class, None)

    # Step 1: 每条 LineString 按等级 buffer
    t1 = time.time()
    polys = _buffer_lines(gdf, highway_filter)
    print(f"  Roads: buffer 耗时 {time.time() - t1:.1f}s")
    if not polys:
        return None

    # Step 2: shapely.unary_union 合并所有 buffered polygons
    t2 = time.time()
    merged = unary_union(polys)
    print(f"  Roads: unary_union 耗时 {time.time() - t2:.1f}s")

    if merged.is_empty:
        return None

    # Step 3: 拆成连通块
    if isinstance(merged, MultiPolygon):
        sub_polys = list(merged.geoms)
    elif isinstance(merged, Polygon):
        sub_polys = [merged]
    else:
        return None
    print(f"  Roads: union 后 {len(sub_polys)} 个连通块")

    # Step 4: 各块按质心采地形 Z，单独 Manifold extrude
    t4 = time.time()
    cents_x = np.array([p.centroid.x for p in sub_polys])
    cents_y = np.array([p.centroid.y for p in sub_polys])
    terrain_zs = sample_terrain_z(terrain_mesh, cents_x * scale, cents_y * scale)

    parts: List[manifold3d.Manifold] = []
    n_failed = 0
    for poly, tz in zip(sub_polys, terrain_zs):
        if np.isnan(tz):
            continue
        man = _polygon_to_manifold_road(poly, float(tz), scale)
        if man.is_empty():
            n_failed += 1
            continue
        parts.append(man)
    print(f"  Roads: {len(parts)} 个 Manifold parts "
          f"({n_failed} failed) 耗时 {time.time() - t4:.1f}s")

    if not parts:
        return None

    # Step 5: batch_boolean Add
    t5 = time.time()
    if len(parts) == 1:
        combined = parts[0]
    else:
        combined = manifold3d.Manifold.batch_boolean(parts, manifold3d.OpType.Add)
    print(f"  Roads: batch_boolean Add 耗时 {time.time() - t5:.1f}s")

    if combined.is_empty():
        return None

    # Step 6: Manifold → trimesh
    mesh_data = combined.to_mesh()
    verts = np.array(mesh_data.vert_properties, dtype=np.float64)
    faces = np.array(mesh_data.tri_verts, dtype=np.int64)
    return trimesh.Trimesh(vertices=verts, faces=faces, process=False)


# ---------------------------------------------------------------------------
# V3 builder: 接收 preprocess 输出的 road lines，区分普通路/桥梁高度
# ---------------------------------------------------------------------------

def build_deepseek_roads_v3(
    roads_lines: List[Tuple],
    terrain_mesh: trimesh.Trimesh,
    scale: float,
    *,
    printer_profile: PrinterProfile = DEFAULT_PRINTER_PROFILE,
    road_width_multiplier: float | None = None,
) -> trimesh.Trimesh:
    """V3 roads builder — 接收 preprocess 已过滤/分类的 road lines。

    Args:
        roads_lines: [(line, highway_type, is_bridge, composition_role?)].
            Old 3-tuples remain supported.
        terrain_mesh: 地形网格（用于 Z 采样）
        scale: mm/m

    Returns:
        Single watertight trimesh, or None.
    """
    if not roads_lines:
        return None

    from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.terrain import sample_terrain_z
    from _TEXTURE_STYLE_OF_DEEPSEEK._geom_utils import shapely_poly_to_crosssection

    import manifold3d as m3d  # type: ignore

    RO_THICKNESS = Z_ROAD_ABOVE_TERRAIN_MM
    RL_THICKNESS = Z_ROAD_ABOVE_TERRAIN_MM + ROAD_BRIDGE_EXTRA_MM

    # Step 1: Buffer all lines
    # Function-level import: allows runtime monkey-patch from auto-params
    from _TEXTURE_STYLE_OF_DEEPSEEK.config import ROAD_WIDTH_MULTIPLIER
    effective_multiplier = (float(ROAD_WIDTH_MULTIPLIER)
                            if road_width_multiplier is None
                            else float(road_width_multiplier))
    t1 = time.time()
    all_polys: List[Tuple[Polygon, str, bool]] = []  # (poly, highway, is_bridge)
    n_skip_short = 0
    n_skip_err = 0

    for item in roads_lines:
        line, highway, is_bridge = item[0], item[1], item[2]
        composition_role = item[3] if len(item) > 3 else "foreground"
        if line.length < ROAD_MIN_LINE_LENGTH_M:
            n_skip_short += 1
            continue
        width = resolve_composed_road_width_m(
            highway,
            composition_role=composition_role,
            scale_mm_per_m=scale,
            road_width_multiplier=effective_multiplier,
            min_colored_strip_mm=printer_profile.min_colored_strip_mm,
        )
        half_w = width / 2.0
        try:
            buf = line.buffer(half_w, cap_style=2, join_style=2)
        except Exception:
            n_skip_err += 1
            continue
        if buf is None or buf.is_empty:
            continue
        if isinstance(buf, MultiPolygon):
            all_polys.extend((p, highway, is_bridge) for p in buf.geoms)
        elif isinstance(buf, Polygon):
            all_polys.append((buf, highway, is_bridge))
    print(f"  Roads(v3): {len(all_polys)} buffered polygons "
          f"(short {n_skip_short}, errors {n_skip_err}) "
          f"time {time.time() - t1:.1f}s")

    if not all_polys:
        return None

    # Step 2: unary_union
    t2 = time.time()
    merged = unary_union([p for p, _, _ in all_polys])
    print(f"  Roads(v3): unary_union time {time.time() - t2:.1f}s")
    if merged.is_empty:
        return None

    if isinstance(merged, MultiPolygon):
        sub_polys = list(merged.geoms)
    elif isinstance(merged, Polygon):
        sub_polys = [merged]
    else:
        return None
    print(f"  Roads(v3): union → {len(sub_polys)} 连通块")

    # Step 3: Build bridge mask for each sub_poly
    # A sub_poly is a bridge if it overlaps any bridge buffered poly
    bridge_polys = [p for p, _, b in all_polys if b]
    is_bridge_flags = [False] * len(sub_polys)
    if bridge_polys:
        bridge_tree = STRtree(bridge_polys)
        for i, sp in enumerate(sub_polys):
            centroid = sp.centroid
            candidates = bridge_tree.query(centroid)
            if len(candidates) > 0:
                is_bridge_flags[i] = True

    n_bridges = sum(is_bridge_flags)
    print(f"  Roads(v3): {n_bridges}/{len(sub_polys)} bridge blocks")

    # Step 4: Extrude each sub_poly (bridge → thicker)
    t4 = time.time()
    cents_x = np.array([p.centroid.x for p in sub_polys])
    cents_y = np.array([p.centroid.y for p in sub_polys])
    terrain_zs = sample_terrain_z(terrain_mesh, cents_x * scale, cents_y * scale)

    parts: List = []
    n_failed = 0
    for poly, tz, is_b in zip(sub_polys, terrain_zs, is_bridge_flags):
        if np.isnan(tz):
            continue
        thickness = RL_THICKNESS if is_b else RO_THICKNESS
        # Reuse road extrude logic (single polygon → manifold)
        cs = shapely_poly_to_crosssection(poly)
        if cs.is_empty():
            n_failed += 1
            continue
        try:
            cs = cs.scale((scale, scale))
            z_top = float(tz) + Z_ROAD_ABOVE_TERRAIN_MM
            z_bottom = z_top - thickness
            man = cs.extrude(height=thickness).translate((0, 0, z_bottom))
            if not man.is_empty():
                parts.append(man)
        except Exception:
            n_failed += 1
    print(f"  Roads(v3): {len(parts)} Manifold parts "
          f"({n_failed} failed) time {time.time() - t4:.1f}s")

    if not parts:
        return None

    # Step 5: batch_boolean Add
    t5 = time.time()
    if len(parts) == 1:
        combined = parts[0]
    else:
        combined = m3d.Manifold.batch_boolean(parts, m3d.OpType.Add)
    print(f"  Roads(v3): batch_boolean Add time {time.time() - t5:.1f}s")

    if combined.is_empty():
        return None

    mesh_data = combined.to_mesh()
    verts = np.array(mesh_data.vert_properties, dtype=np.float64)
    faces = np.array(mesh_data.tri_verts, dtype=np.int64)
    return trimesh.Trimesh(vertices=verts, faces=faces, process=False)
