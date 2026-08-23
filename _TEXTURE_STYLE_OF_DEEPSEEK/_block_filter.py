"""Block filtering utilities shared between PNG and 3MF pipelines.

Contains the three core functions for block_base generation:
  - _build_exclusion_mask: water + veg + road corridor → union geometry
  - _subtract_exclusions: parallel subtraction from blocks
  - _filter_blocks_with_buildings: keep only blocks containing buildings

Both tools/block_polygonize_viz.py (PNG) and _layer_preprocess.py (3MF)
import from here to guarantee identical behavior.
"""

from __future__ import annotations

from typing import List

from shapely.geometry import (
    LineString,
    MultiLineString,
    MultiPolygon,
    Polygon,
)
from shapely.ops import unary_union
from shapely.strtree import STRtree


def _union_and_prepare(geometries):
    """Merge a cell's exclusions and prepare it for repeated predicates.

    ``shapely.prepare`` builds an in-place segment index.  The grid path tests
    every block in a cell against the same exclusion, so preparing once avoids
    rebuilding topology for each vectorized ``intersects`` call.  Preparation
    does not change coordinates or the subsequent difference result.
    """
    import shapely

    merged = shapely.union_all(geometries)
    shapely.prepare(merged)
    return merged


def _build_exclusion_mask(water_gdf, veg_landmarks,
                          roads_gdf=None, road_inset: float = 0.0,
                          water_inset: float = 0.0):
    """合并 water + vegetation + road corridor 为 exclusion geometry。

    road_inset / water_inset > 0 时，把路 LineString / 水 polygon 加宽 buffer 当作
    corridor 加入 exclusion —— 块从 block 减去后块之间形成"加宽的路网/水网"间隙。
    """
    polys = []

    # vegetation / park / protected_area
    if veg_landmarks:
        polys.extend(p for p in veg_landmarks
                     if isinstance(p, Polygon) and not p.is_empty)

    # water polygon — 可选 buffer 加宽
    if water_gdf is not None and len(water_gdf) > 0:
        water_polys = []
        for geom in water_gdf.geometry:
            if geom is None or geom.is_empty:
                continue
            if isinstance(geom, MultiPolygon):
                water_polys.extend(g for g in geom.geoms if not g.is_empty)
            elif isinstance(geom, Polygon):
                water_polys.append(geom)
            else:
                # LineString waterway → buffer
                if water_inset > 0:
                    polys.append(geom.buffer(water_inset))
        if water_inset > 0:
            polys.extend(p.buffer(water_inset) for p in water_polys)
        else:
            polys.extend(water_polys)

    # road LineString — buffer 当 corridor
    if roads_gdf is not None and road_inset > 0:
        for geom in roads_gdf.geometry:
            if geom is None or geom.is_empty:
                continue
            polys.append(geom.buffer(road_inset))

    if not polys:
        return None
    return unary_union(polys)


def _subtract_one_block(args):
    """Worker: 单个 block 减 exclusion。"""
    b, exclusion_wkb, min_area = args
    from shapely import wkb
    exclusion = wkb.loads(exclusion_wkb)
    if not b.intersects(exclusion):
        return [b]
    diff = b.difference(exclusion)
    if diff.is_empty:
        return []
    if isinstance(diff, Polygon):
        return [diff] if diff.area >= min_area else []
    elif isinstance(diff, MultiPolygon):
        return [g for g in diff.geoms
                if isinstance(g, Polygon) and not g.is_empty and g.area >= min_area]
    return []


def _subtract_exclusions(blocks, exclusion, min_area: float = 100.0,
                         bbox_local=None):
    """从 block 里减去 exclusion 几何，MultiPolygon 拆成独立 block。
    碎片 (area < min_area) 丢弃。

    当提供 bbox_local 且 exclusion 由 _build_exclusion_mask 返回的 parts list
    构成时，使用 grid 分治加速（16×16 网格 + 500m margin）。
    否则回退到逐 block difference。
    """
    if exclusion is None or exclusion.is_empty:
        return blocks
    if len(blocks) > 500:
        import multiprocessing as mp
        import os
        from shapely import wkb
        n_workers = max(1, os.cpu_count() - 1)
        excl_wkb = wkb.dumps(exclusion)
        args = [(b, excl_wkb, min_area) for b in blocks]
        ctx = mp.get_context("fork" if hasattr(os, "fork") else "spawn")
        with ctx.Pool(n_workers) as pool:
            results = pool.map(_subtract_one_block, args, chunksize=200)
        out = []
        for r in results:
            out.extend(r)
        return out
    out = []
    for b in blocks:
        if not b.intersects(exclusion):
            out.append(b); continue
        diff = b.difference(exclusion)
        if diff.is_empty:
            continue
        if isinstance(diff, Polygon):
            if diff.area >= min_area:
                out.append(diff)
        elif isinstance(diff, MultiPolygon):
            for g in diff.geoms:
                if isinstance(g, Polygon) and not g.is_empty and g.area >= min_area:
                    out.append(g)
    return out


def _subtract_exclusions_grid(blocks, excl_parts, min_area: float = 100.0,
                              bbox_local=None):
    """Grid-accelerated subtraction: 8×8 网格预聚合 exclusion，每 block 只做局部 difference。

    Uses shapely 2.x vectorized ops for centroid computation, intersects checks,
    and batch difference within each grid cell.

    Args:
        blocks: List[Polygon] — 待减法的 block
        excl_parts: List[Polygon] — exclusion 各部分（未 union），逐个加入 STRtree
        min_area: 碎片过滤阈值
        bbox_local: (xmin, ymin, xmax, ymax) 本地坐标 bbox
    """
    import shapely
    import numpy as np
    from shapely.geometry import box as shapely_box

    if not excl_parts or not blocks:
        return blocks

    blocks_arr = np.asarray(blocks, dtype=object)

    if bbox_local is None:
        all_bounds = shapely.bounds(blocks_arr)
        xmin = all_bounds[:, 0].min()
        ymin = all_bounds[:, 1].min()
        xmax = all_bounds[:, 2].max()
        ymax = all_bounds[:, 3].max()
    else:
        xmin, ymin, xmax, ymax = bbox_local

    N = 8
    dx = (xmax - xmin) / N
    dy = (ymax - ymin) / N

    # Build per-cell exclusion union via STRtree spatial index
    excl_arr = np.asarray(excl_parts, dtype=object)
    tree = STRtree(excl_parts)
    grid_excl = {}
    for ix in range(N):
        for iy in range(N):
            cell = shapely_box(
                xmin + ix * dx, ymin + iy * dy,
                xmin + (ix + 1) * dx, ymin + (iy + 1) * dy,
            )
            hits = tree.query(cell)
            if len(hits) > 0:
                hit_geoms = excl_arr[hits]
                grid_excl[(ix, iy)] = _union_and_prepare(hit_geoms)

    # Vectorized centroid + grid cell assignment
    centroids = shapely.centroid(blocks_arr)
    cx_arr = shapely.get_x(centroids)
    cy_arr = shapely.get_y(centroids)
    ix_arr = np.clip(((cx_arr - xmin) / dx).astype(int), 0, N - 1)
    iy_arr = np.clip(((cy_arr - ymin) / dy).astype(int), 0, N - 1)

    # Per-cell vectorized difference
    out = []
    processed = np.zeros(len(blocks_arr), dtype=bool)

    for (ix, iy), excl in grid_excl.items():
        cell_mask = (ix_arr == ix) & (iy_arr == iy)
        processed |= cell_mask
        cell_indices = np.where(cell_mask)[0]
        if len(cell_indices) == 0:
            continue

        cell_blocks = blocks_arr[cell_indices]

        # Vectorized intersects check — skip blocks that don't touch exclusion
        # Prepared geometries are accelerated as the first predicate operand.
        # ``intersects`` is symmetric, so keep the cached exclusion first.
        hits_mask = shapely.intersects(excl, cell_blocks)
        out.extend(cell_blocks[~hits_mask].tolist())

        hit_blocks = cell_blocks[hits_mask]
        if len(hit_blocks) == 0:
            continue

        # Vectorized difference + explode MultiPolygons
        diffs = shapely.difference(hit_blocks, excl)
        parts = shapely.get_parts(diffs)
        if len(parts) == 0:
            continue
        valid = parts[~shapely.is_empty(parts)]
        if len(valid) == 0:
            continue
        areas = shapely.area(valid)
        out.extend(valid[areas >= min_area].tolist())

    # Blocks not assigned to any excl cell — keep as-is
    out.extend(blocks_arr[~processed].tolist())
    return out


def build_and_subtract_exclusions(blocks, water_gdf, veg_landmarks,
                                   roads_gdf=None, road_inset=25.0,
                                   water_inset=40.0, min_area=100.0,
                                   bbox_local=None):
    """高层 API：构建 exclusion parts + grid 分治减法。

    替代旧的 _build_exclusion_mask() + _subtract_exclusions() 两步调用，
    避免中间产生巨型 union 几何体。
    """
    excl_parts = []

    if veg_landmarks:
        excl_parts.extend(p for p in veg_landmarks
                          if isinstance(p, Polygon) and not p.is_empty)

    if water_gdf is not None and len(water_gdf) > 0:
        for geom in water_gdf.geometry:
            if geom is None or geom.is_empty:
                continue
            if isinstance(geom, MultiPolygon):
                for g in geom.geoms:
                    if not g.is_empty:
                        excl_parts.append(g.buffer(water_inset) if water_inset > 0 else g)
            elif isinstance(geom, Polygon):
                excl_parts.append(geom.buffer(water_inset) if water_inset > 0 else geom)
            elif water_inset > 0:
                excl_parts.append(geom.buffer(water_inset))

    if roads_gdf is not None and road_inset > 0:
        for geom in roads_gdf.geometry:
            if geom is None or geom.is_empty:
                continue
            excl_parts.append(geom.buffer(road_inset))

    if not excl_parts:
        return blocks

    return _subtract_exclusions_grid(blocks, excl_parts, min_area=min_area,
                                     bbox_local=bbox_local)


def _filter_blocks_with_buildings(blocks, polys, min_count: int):
    """只保留含至少 min_count 栋建筑的 block。"""
    if not polys:
        return blocks
    centroids = [p.centroid for p in polys if isinstance(p, Polygon) and not p.is_empty]
    tree = STRtree(blocks)
    counts = [0] * len(blocks)
    for c in centroids:
        for bi in tree.query(c):
            if blocks[bi].contains(c):
                counts[bi] += 1
                break
    keep_idx = set()
    for i, n in enumerate(counts):
        if n >= min_count:
            keep_idx.add(i)
    return [b for i, b in enumerate(blocks) if i in keep_idx]
