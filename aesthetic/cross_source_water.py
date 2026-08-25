"""Read-only water cross-checks between OSM and a secondary map source.

The comparison only reports evidence.  It never supplements water geometry or
changes printable layers, because raster-derived AMap polygons contain both
real omissions and colour-segmentation artefacts.
"""
from __future__ import annotations

import math

from shapely.geometry import GeometryCollection, box
from shapely.ops import unary_union


CROSSCHECK_VERSION = "water-cross-source-v1"


def _polygon_parts(geometry):
    if geometry is None or geometry.is_empty:
        return []
    if geometry.geom_type == "Polygon":
        return [geometry]
    if hasattr(geometry, "geoms"):
        parts = []
        for child in geometry.geoms:
            parts.extend(_polygon_parts(child))
        return parts
    return []


def _line_parts(geometry):
    if geometry is None or geometry.is_empty:
        return []
    if geometry.geom_type == "LineString":
        return [geometry]
    if geometry.geom_type == "MultiLineString":
        return [part for part in geometry.geoms if not part.is_empty]
    if hasattr(geometry, "geoms"):
        parts = []
        for child in geometry.geoms:
            parts.extend(_line_parts(child))
        return parts
    return []


def _safe_union(geometries):
    valid = []
    for geometry in geometries:
        if geometry is None or geometry.is_empty:
            continue
        try:
            valid.append(geometry if geometry.is_valid else geometry.buffer(0))
        except Exception:
            continue
    if not valid:
        return None
    try:
        return unary_union(valid)
    except Exception:
        return None


def compare_water_sources(
    osm_water,
    reference_polygons,
    bbox_projected_m,
    *,
    grid_size: int = 8,
    line_buffer_m: float = 30.0,
    alignment_buffer_m: float = 30.0,
    min_gap_area_m2: float = 8000.0,
    compact_circularity: float = 0.15,
    inset_ratio: float = 0.05,
) -> dict:
    """Compare projected OSM water with projected reference polygons.

    `reference_polygons` currently comes from vectorized AMap no-label tiles.
    Low-circularity differences remain visible as linear/noise candidates; the
    function never silently discards or accepts them.
    """

    xmin, ymin, xmax, ymax = (float(value) for value in bbox_projected_m)
    if xmax <= xmin or ymax <= ymin:
        raise ValueError("bbox_projected_m must have positive width and height")
    if grid_size < 1:
        raise ValueError("grid_size must be positive")
    reference = _safe_union(reference_polygons or [])
    if reference is None or reference.is_empty:
        return {
            "version": CROSSCHECK_VERSION,
            "status": "unavailable",
            "reason": "secondary water source returned no polygons",
            "source": "amap_nolabel_tiles",
            "candidate_cells": [],
        }

    osm_polygons = []
    osm_lines = []
    if osm_water is not None and len(osm_water):
        for geometry in osm_water.geometry:
            osm_polygons.extend(_polygon_parts(geometry))
            osm_lines.extend(_line_parts(geometry))
    osm_parts = list(osm_polygons)
    for line in osm_lines:
        try:
            osm_parts.append(line.buffer(line_buffer_m))
        except Exception:
            continue
    osm = _safe_union(osm_parts)
    if osm is None:
        osm = GeometryCollection()

    inset_m = min(xmax - xmin, ymax - ymin) * max(0.0, inset_ratio)
    if inset_m * 2 >= min(xmax - xmin, ymax - ymin):
        inset_m = 0.0
    analysis_frame = box(
        xmin + inset_m, ymin + inset_m, xmax - inset_m, ymax - inset_m)
    try:
        reference = reference.intersection(analysis_frame)
        osm = osm.intersection(analysis_frame)
    except Exception:
        return {
            "version": CROSSCHECK_VERSION,
            "status": "error",
            "reason": "source intersection failed",
            "source": "amap_nolabel_tiles",
            "candidate_cells": [],
        }
    if reference.is_empty:
        return {
            "version": CROSSCHECK_VERSION,
            "status": "unavailable",
            "reason": "secondary water source has no interior coverage",
            "source": "amap_nolabel_tiles",
            "candidate_cells": [],
        }

    osm_match = osm.buffer(alignment_buffer_m)
    reference_match = reference.buffer(alignment_buffer_m)
    matched = reference.intersection(osm_match)
    reference_only = reference.difference(osm_match)
    osm_only = osm.difference(reference_match)

    compact = []
    linear = []
    for geometry in _polygon_parts(reference_only):
        if geometry.area < min_gap_area_m2 or geometry.length <= 0:
            continue
        circularity = 4 * math.pi * geometry.area / (geometry.length ** 2)
        record = {
            "area_m2": round(float(geometry.area), 1),
            "circularity": round(float(circularity), 4),
            "centroid_x": round(float(geometry.centroid.x), 3),
            "centroid_y": round(float(geometry.centroid.y), 3),
        }
        (compact if circularity >= compact_circularity else linear).append(
            (geometry, record))
    compact.sort(key=lambda item: -item[1]["area_m2"])
    linear.sort(key=lambda item: -item[1]["area_m2"])

    cell_width = (xmax - xmin) / grid_size
    cell_height = (ymax - ymin) / grid_size
    cells = {}
    for kind, records in (("compact", compact), ("linear", linear)):
        for geometry, _ in records:
            for row in range(grid_size):
                for column in range(grid_size):
                    cell = box(
                        xmin + column * cell_width,
                        ymin + row * cell_height,
                        xmin + (column + 1) * cell_width,
                        ymin + (row + 1) * cell_height,
                    )
                    try:
                        area = geometry.intersection(cell).area
                    except Exception:
                        continue
                    if area < min_gap_area_m2 * 0.25:
                        continue
                    entry = cells.setdefault((row, column), {
                        "row": row,
                        "column": column,
                        "compact_gap_area_m2": 0.0,
                        "linear_gap_area_m2": 0.0,
                    })
                    entry[f"{kind}_gap_area_m2"] += float(area)

    reference_area = float(reference.area)
    osm_area = float(osm.area)
    matched_area = float(matched.area)
    candidate_cells = []
    for entry in cells.values():
        candidate_cells.append({
            **entry,
            "compact_gap_area_m2": round(entry["compact_gap_area_m2"], 1),
            "linear_gap_area_m2": round(entry["linear_gap_area_m2"], 1),
        })
    candidate_cells.sort(
        key=lambda entry: (
            -(entry["compact_gap_area_m2"] + entry["linear_gap_area_m2"]),
            entry["row"], entry["column"],
        ))
    return {
        "version": CROSSCHECK_VERSION,
        "status": "evidence_only",
        "source": "amap_nolabel_tiles",
        "analysis_inset_m": round(inset_m, 1),
        "line_buffer_m": line_buffer_m,
        "alignment_buffer_m": alignment_buffer_m,
        "reference_water_area_m2": round(reference_area, 1),
        "osm_water_area_m2": round(osm_area, 1),
        "matched_reference_area_m2": round(matched_area, 1),
        "reference_covered_by_osm_ratio": round(
            matched_area / max(reference_area, 1.0), 5),
        "osm_covered_by_reference_ratio": round(
            float(osm.intersection(reference_match).area)
            / max(osm_area, 1.0), 5),
        "reference_only_area_m2": round(float(reference_only.area), 1),
        "osm_only_area_m2": round(float(osm_only.area), 1),
        "compact_gap_count": len(compact),
        "compact_gap_area_m2": round(
            sum(record["area_m2"] for _, record in compact), 1),
        "linear_or_noise_gap_count": len(linear),
        "linear_or_noise_gap_area_m2": round(
            sum(record["area_m2"] for _, record in linear), 1),
        "compact_gaps": [record for _, record in compact],
        "linear_or_noise_gaps": [record for _, record in linear],
        "candidate_cells": candidate_cells,
        "warning": (
            "AMap water is raster colour segmentation. Compact gaps are "
            "review candidates; linear gaps may be rivers or visual noise."
        ),
    }
