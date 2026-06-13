"""共享几何工具：winding 归一化、边界加密、Shapely → Manifold CrossSection。

这些 helper 之前散落在 water.py / vegetation.py / water_column.py /
vegetation_exclusion.py。统一到此处，避免行为漂移（特别是 winding 规则）。
"""

from __future__ import annotations

import math
from typing import Iterable, List

import numpy as np
import manifold3d
from shapely.geometry import (
    LineString,
    MultiLineString,
    MultiPolygon,
    Polygon,
)
from shapely.ops import unary_union

from _TEXTURE_STYLE_OF_DEEPSEEK.config import WATERWAY_WIDTHS


# ---------------------------------------------------------------------------
# Winding helpers — Manifold CrossSection(FillRule.Positive) 期望
#                   exterior=CCW, holes=CW。OSM 数据常给反，需要归一化。
# ---------------------------------------------------------------------------


def signed_area_2d(contour: np.ndarray) -> float:
    """Signed area of a 2D closed contour (positive = CCW)."""
    x = contour[:, 0]
    y = contour[:, 1]
    return 0.5 * float(np.sum(x[:-1] * y[1:] - x[1:] * y[:-1]))


def ensure_ccw(contour: np.ndarray) -> np.ndarray:
    """Reverse contour if it is clockwise."""
    if signed_area_2d(contour) < 0:
        return contour[::-1]
    return contour


def ensure_cw(contour: np.ndarray) -> np.ndarray:
    """Reverse contour if it is counter-clockwise."""
    if signed_area_2d(contour) > 0:
        return contour[::-1]
    return contour


# ---------------------------------------------------------------------------
# Densification
# ---------------------------------------------------------------------------


def densify_ring(coords: np.ndarray, max_edge_m: float) -> np.ndarray:
    """Insert vertices so no edge exceeds *max_edge_m* (meters)."""
    if len(coords) < 2:
        return coords

    result = [coords[0]]
    for i in range(1, len(coords)):
        p0 = coords[i - 1]
        p1 = coords[i]
        seg_len = float(np.linalg.norm(p1[:2] - p0[:2]))
        if seg_len > max_edge_m:
            n_splits = int(np.ceil(seg_len / max_edge_m))
            for j in range(1, n_splits + 1):
                t = j / n_splits
                result.append(p0 + t * (p1 - p0))
        else:
            result.append(p1)

    if np.linalg.norm(result[-1][:2] - result[0][:2]) > 1e-10:
        result.append(result[0])
    return np.array(result)


def densify_polygon(poly: Polygon, max_edge_m: float) -> Polygon:
    """Densify a polygon's exterior boundary (preserves holes as-is)."""
    if poly.is_empty or max_edge_m <= 0:
        return poly
    boundary = np.array(poly.exterior.coords)
    dense = densify_ring(boundary, max_edge_m)
    try:
        return Polygon(
            dense,
            holes=[np.array(h.coords) for h in poly.interiors],
        )
    except Exception:
        return poly


# ---------------------------------------------------------------------------
# Shapely → Manifold CrossSection
# ---------------------------------------------------------------------------


def shapely_poly_to_crosssection(poly: Polygon) -> manifold3d.CrossSection:
    """Convert a Shapely Polygon to a Manifold CrossSection.

    Normalises winding (exterior=CCW, holes=CW) to satisfy
    FillRule.Positive. Returns empty CrossSection on failure.
    """
    if poly.is_empty or len(poly.exterior.coords) < 4:
        return manifold3d.CrossSection()

    try:
        exterior = np.array(poly.exterior.coords, dtype=np.float64)
        if exterior.shape[1] >= 3:
            exterior = exterior[:, :2]
        contours = [ensure_ccw(exterior)]

        for interior in poly.interiors:
            hole = np.array(interior.coords, dtype=np.float64)
            if hole.shape[1] >= 3:
                hole = hole[:, :2]
            if len(hole) >= 3:
                contours.append(ensure_cw(hole))

        return manifold3d.CrossSection(contours)
    except Exception:
        return manifold3d.CrossSection()


# ---------------------------------------------------------------------------
# Water feature collection — single source of truth for obj3 / obj4
# ---------------------------------------------------------------------------


def _resolve_waterway_width(row, default: float = 60.0) -> float:
    """Resolve a waterway buffer width from the row's tags.

    Priority: explicit width tag > WATERWAY_WIDTHS[waterway] > default.
    """
    waterway = row.get("waterway", "river")
    width = WATERWAY_WIDTHS.get(waterway, default)

    osm_width = row.get("width", None)
    if osm_width is None:
        return width
    try:
        if isinstance(osm_width, float) and math.isnan(osm_width):
            return width
        parsed = float(osm_width)
        if 0 < parsed < 10000:
            return parsed
    except (TypeError, ValueError):
        pass
    return width


def _buffered_linestring_polys(geom, width_m: float) -> List[Polygon]:
    """Buffer a LineString/MultiLineString into list of Polygons."""
    polys: List[Polygon] = []
    half_w = width_m / 2.0
    lines = geom.geoms if isinstance(geom, MultiLineString) else [geom]
    for line in lines:
        if line.length < 10.0:
            continue
        buf = line.buffer(half_w, cap_style=2, join_style=2)
        if buf.is_empty:
            continue
        if isinstance(buf, MultiPolygon):
            polys.extend(buf.geoms)
        elif isinstance(buf, Polygon):
            polys.append(buf)
    return polys


def collect_water_polygons(
    water_gdf,
    min_area_m2: float,
    max_edge_m: float = 100.0,
    polygon_priority: bool = True,
    overlap_threshold: float = 0.3,
    linestring_width_scale: float = 0.5,
) -> List[Polygon]:
    """Filter + buffer water features into a list of qualifying polygons.

    Single source of truth shared between obj3 (water plate) and obj4
    (terrain hole cutter). Guarantees both pipelines see the same set of
    water polygons, so the carved hole and the relief feature mate exactly.

    Args:
        water_gdf: GeoDataFrame of water features (any geometry).
        min_area_m2: minimum polygon area to keep (real m²).
        max_edge_m: densification step for each kept polygon.
        polygon_priority: when True, a buffered LineString polygon
            overlapping a Polygon by > *overlap_threshold* is dropped
            (Polygon takes precedence, matching the reference style).
        overlap_threshold: ratio of LineString-poly area covered by
            Polygon coverage above which the LineString poly is dropped.
        linestring_width_scale: multiplier for LineString buffer width.
            Default 0.5 = half the configured WATERWAY_WIDTHS value.
            Prevents buffered rivers from cutting too wide into terrain.

    Returns:
        List of Shapely Polygons (densified) ready for extrusion.
    """
    if water_gdf is None or len(water_gdf) == 0:
        return []

    polygon_polys: List[Polygon] = []
    linestring_polys: List[Polygon] = []

    for _, row in water_gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue

        if isinstance(geom, (Polygon, MultiPolygon)):
            polys = geom.geoms if isinstance(geom, MultiPolygon) else [geom]
            for poly in polys:
                if poly.is_empty or poly.area < min_area_m2:
                    continue
                polygon_polys.append(poly)

        elif isinstance(geom, (LineString, MultiLineString)):
            width = _resolve_waterway_width(row) * linestring_width_scale
            for poly in _buffered_linestring_polys(geom, width):
                if poly.area >= min_area_m2:
                    linestring_polys.append(poly)

    # Polygon priority: drop linestring polygons covered by a real Polygon
    if polygon_priority and polygon_polys and linestring_polys:
        polygon_cover = unary_union(polygon_polys)
        kept: List[Polygon] = []
        for p in linestring_polys:
            if p.intersects(polygon_cover):
                ratio = p.intersection(polygon_cover).area / p.area
                if ratio > overlap_threshold:
                    continue
            kept.append(p)
        linestring_polys = kept

    return [densify_polygon(p, max_edge_m) for p in polygon_polys + linestring_polys]
