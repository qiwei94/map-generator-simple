"""Turn directed OSM coastline ways into clipped sea polygons.

OSM represents most oceans as directed ``natural=coastline`` ways instead of
``natural=water`` polygons.  The coastline convention is land on the left and
sea on the right.  This module closes clipped coastlines against the requested
bbox and polygonizes the resulting cells.  Each directed segment casts a vote
for the cell immediately on its left (land) and right (sea); only cells with a
net sea vote are returned.  Sampling the two sides of the actual boundary is
more reliable than classifying a whole cell from one distant representative
point in a dense island/harbour network.

The conservative border-connected rule is intentional: incomplete coastline
data must never flood an inland part of the model with water.
"""

from __future__ import annotations

from typing import Iterable, Iterator

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString, MultiLineString, Point, box
from shapely.ops import polygonize, unary_union


def _iter_lines(geometries: Iterable[object]) -> Iterator[LineString]:
    """Yield non-empty LineStrings while preserving their OSM direction."""
    for geometry in geometries:
        if geometry is None or geometry.is_empty:
            continue
        if isinstance(geometry, LineString):
            yield geometry
        elif isinstance(geometry, MultiLineString):
            yield from (line for line in geometry.geoms if not line.is_empty)
        elif hasattr(geometry, "geoms"):
            yield from _iter_lines(geometry.geoms)


def _vote_coastline_cells(candidates, coastlines, epsilon: float) -> list[int]:
    """Vote +1 for land-side cells and -1 for sea-side cells."""
    votes = [0] * len(candidates)
    for coastline in coastlines:
        coords = list(coastline.coords)
        for (x1, y1), (x2, y2) in zip(coords, coords[1:]):
            dx, dy = x2 - x1, y2 - y1
            length = (dx * dx + dy * dy) ** 0.5
            if length <= 0:
                continue
            mx, my = (x1 + x2) * 0.5, (y1 + y2) * 0.5
            nx, ny = -dy / length * epsilon, dx / length * epsilon
            land_point = Point(mx + nx, my + ny)
            sea_point = Point(mx - nx, my - ny)
            for index, candidate in enumerate(candidates):
                if candidate.covers(land_point):
                    votes[index] += 1
                if candidate.covers(sea_point):
                    votes[index] -= 1
    return votes


def coastline_to_sea_polygon(
    coastlines: Iterable[object],
    bbox_wgs84: tuple[float, float, float, float],
):
    """Build a conservative sea Polygon/MultiPolygon inside a WGS84 bbox.

    Args:
        coastlines: Directed OSM coastline geometries.
        bbox_wgs84: ``(south, west, north, east)``.

    Returns:
        Sea geometry clipped to the bbox, or ``None`` when the coastline does
        not form classifiable border-connected cells.
    """
    south, west, north, east = bbox_wgs84
    frame = box(west, south, east, north)
    directed_lines = []
    for line in _iter_lines(coastlines):
        clipped = line.intersection(frame)
        directed_lines.extend(_iter_lines([clipped]))
    if not directed_lines:
        return None

    # unary_union nodes intersections for polygonize.  Classification still
    # uses directed_lines because union operations do not promise orientation.
    noded = unary_union([frame.boundary, *directed_lines])
    candidates = list(polygonize(noded))
    if not candidates:
        return None

    span = max(east - west, north - south)
    votes = _vote_coastline_cells(
        candidates, directed_lines, epsilon=max(span * 1e-5, 1e-9))
    sea_cells = [candidate for candidate, vote in zip(candidates, votes)
                 if vote < 0 and not candidate.is_empty]
    if not sea_cells:
        return None
    sea = unary_union(sea_cells).intersection(frame)
    if sea.is_empty:
        return None
    if not sea.is_valid:
        sea = sea.buffer(0)
    return sea if not sea.is_empty else None


def materialize_coastal_water(
    water_gdf: gpd.GeoDataFrame,
    bbox_wgs84: tuple[float, float, float, float],
) -> gpd.GeoDataFrame:
    """Replace coastline ways with a normal ``water=sea`` polygon row."""
    if water_gdf is None or len(water_gdf) == 0:
        return water_gdf
    natural = (water_gdf.get("natural")
               if "natural" in water_gdf.columns else None)
    if natural is None:
        return water_gdf
    coastline_mask = natural.fillna("").astype(str).eq("coastline")
    if not coastline_mask.any():
        return water_gdf

    inland = water_gdf.loc[~coastline_mask].copy()
    sea = coastline_to_sea_polygon(
        water_gdf.loc[coastline_mask, "geometry"], bbox_wgs84)
    if sea is None:
        # Never pass raw coastline lines downstream: the waterway preprocessor
        # could buffer them into an artificial black stroke.
        return inland

    sea_row = gpd.GeoDataFrame(
        [{
            "natural": "water",
            "water": "sea",
            "source": "osm_coastline",
            "geometry": sea,
        }],
        geometry="geometry",
        crs=water_gdf.crs or "EPSG:4326",
    )
    return gpd.GeoDataFrame(
        pd.concat([inland, sea_row], ignore_index=True, sort=False),
        geometry="geometry",
        crs=water_gdf.crs or "EPSG:4326",
    )
