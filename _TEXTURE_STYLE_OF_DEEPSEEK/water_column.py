"""水体挤出柱（Manifold 流型化方案）。

只保留 Manifold 路径：CrossSection.extrude() 保证 watertight。
旧的 earcut + 手动建墙路径已删除（无外部引用，且 _fan_triangulate 有索引越界 bug）。
"""

from __future__ import annotations

import time
from typing import List

import manifold3d
from shapely.geometry import Polygon

from _TEXTURE_STYLE_OF_DEEPSEEK._geom_utils import (
    collect_water_polygons,
    shapely_poly_to_crosssection,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.config import WATER_MIN_AREA_M2, WATER_MAX_EDGE_M


def extrude_water_column_manifold(
    water_polygon: Polygon,
    z_bottom: float,
    z_top: float,
    scale: float,
) -> manifold3d.Manifold:
    """Extrude a single water polygon into a watertight Manifold column.

    The polygon is in *model meters*; ``scale`` converts XY to model mm
    so the column lives in the same coordinate space as the terrain.
    """
    cs = shapely_poly_to_crosssection(water_polygon)
    if cs.is_empty():
        return manifold3d.Manifold()

    try:
        cs = cs.scale((scale, scale))
        column = cs.extrude(height=z_top - z_bottom)
        return column.translate((0, 0, z_bottom))
    except Exception:
        return manifold3d.Manifold()


def create_water_columns_union_manifold(
    water_gdf,
    z_bottom: float,
    z_top: float,
    scale: float,
    min_area_m2: float = WATER_MIN_AREA_M2,
) -> manifold3d.Manifold:
    """Union all qualifying water features into one Manifold solid.

    Uses :func:`collect_water_polygons` so obj3 (water plate) and obj4
    (terrain holes) see the exact same set of polygons.
    """
    polys = collect_water_polygons(
        water_gdf, min_area_m2=min_area_m2, max_edge_m=WATER_MAX_EDGE_M,
    )
    print(f"  Water columns: {len(polys)} polygons after filter")
    if not polys:
        return manifold3d.Manifold()

    columns: List[manifold3d.Manifold] = []
    for poly in polys:
        col = extrude_water_column_manifold(poly, z_bottom, z_top, scale)
        if not col.is_empty():
            columns.append(col)

    if not columns:
        return manifold3d.Manifold()

    if len(columns) == 1:
        return columns[0]

    t = time.time()
    result = manifold3d.Manifold.batch_boolean(columns, manifold3d.OpType.Add)
    print(f"  ⏱ batch_boolean Union: {time.time() - t:.1f}s ({len(columns)} columns)")
    return result
