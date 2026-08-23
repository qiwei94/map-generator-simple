"""Deterministic water roles for printable city compositions.

The source water network has two separate jobs:

* structural water may split city blocks and describe shoreline context;
* visible water becomes the high-contrast material exposed in the model.

Large-area renders must therefore select complete named corridors instead of
ranking individual OSM ways.  This module contains no mesh, Z or boolean
operations; it only selects and lightly reconnects source LineStrings.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Iterable

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString, MultiLineString
from shapely.ops import linemerge, unary_union


POLICY_VERSION = "print-water-roles-v1"

_LINE_INK_QUOTAS = {
    "river": 0.0060,
    "riverbank": 0.0030,
    "canal": 0.0030,
    "stream": 0.0010,
    "drain": 0.0004,
    "ditch": 0.0003,
}

@dataclass(frozen=True)
class WaterLineCandidate:
    geometry: LineString
    waterway: str
    identity: str
    half_width_m: float


@dataclass
class VisibleWaterSelection:
    lines: list[tuple[LineString, str, float]]
    evidence: dict[str, Any]


def _text(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip().casefold()


def water_identity(row: pd.Series, fallback: str) -> str:
    """Return a stable semantic identity for grouping split OSM ways."""

    for key in ("name", "name:en", "ref", "wikidata", "wikipedia"):
        value = _text(row.get(key))
        if value:
            return f"{key}:{value}"
    return fallback


def retain_continuous_water_source(
    water: gpd.GeoDataFrame,
    *,
    max_polygon_features: int = 1500,
) -> tuple[gpd.GeoDataFrame, dict[str, int | str]]:
    """Cap only excess polygon noise while retaining every source water line.

    The historical global top-500 cap ranked individual ways and could remove
    a short connector from the middle of an otherwise selected river.  Linear
    features are now never truncated here.  A generous polygon-only safety cap
    still bounds pathological extracts without breaking line topology.
    """

    if water is None or len(water) == 0:
        return water, {
            "policy_version": POLICY_VERSION,
            "source_features": 0,
            "retained_features": 0,
            "retained_line_features": 0,
            "retained_polygon_features": 0,
            "dropped_polygon_features": 0,
        }
    if max_polygon_features <= 0:
        raise ValueError("max_polygon_features must be positive")

    geom_types = water.geometry.geom_type
    line_mask = geom_types.isin(("LineString", "MultiLineString"))
    polygon_mask = geom_types.isin(("Polygon", "MultiPolygon"))
    lines = water.loc[line_mask]
    polygons = water.loc[polygon_mask]

    if len(polygons) > max_polygon_features:
        if "est_area" in polygons.columns:
            polygons = polygons.nlargest(max_polygon_features, "est_area")
        else:
            polygons = polygons.assign(_role_area=polygons.geometry.area)
            polygons = polygons.nlargest(max_polygon_features, "_role_area")
            polygons = polygons.drop(columns="_role_area")

    retained_index = list(lines.index) + list(polygons.index)
    retained = water.loc[retained_index].sort_index().copy()
    return retained, {
        "policy_version": POLICY_VERSION,
        "source_features": int(len(water)),
        "retained_features": int(len(retained)),
        "retained_line_features": int(len(lines)),
        "retained_polygon_features": int(len(polygons)),
        "dropped_polygon_features": int(polygon_mask.sum() - len(polygons)),
    }


def _line_parts(geometry) -> list[LineString]:
    if geometry is None or geometry.is_empty:
        return []
    if isinstance(geometry, LineString):
        return [geometry]
    if isinstance(geometry, MultiLineString):
        return [part for part in geometry.geoms if not part.is_empty]
    if hasattr(geometry, "geoms"):
        return [part for part in geometry.geoms
                if isinstance(part, LineString) and not part.is_empty]
    return []


def _merge_lines(lines: Iterable[LineString]) -> list[LineString]:
    source = [line for line in lines if line is not None and not line.is_empty]
    if not source:
        return []
    merged = unary_union(source)
    if isinstance(merged, LineString):
        return [merged]
    try:
        merged = linemerge(merged)
    except (ValueError, TypeError):
        pass
    return _line_parts(merged)


def _bridge_named_corridor(
    lines: list[LineString],
    *,
    tolerance_m: float,
) -> tuple[list[LineString], int]:
    """Close only small endpoint gaps inside one semantic water corridor."""

    merged = _merge_lines(lines)
    if len(merged) < 2 or tolerance_m <= 0:
        return merged, 0

    connectors: list[LineString] = []
    # Recompute after each accepted bridge.  Named river groups are small, and
    # this avoids joining endpoints that are already connected by an earlier
    # bridge in the same pass.
    while len(merged) > 1:
        best = None
        for left_index, left in enumerate(merged):
            left_ends = (left.coords[0], left.coords[-1])
            for right_index in range(left_index + 1, len(merged)):
                right = merged[right_index]
                right_ends = (right.coords[0], right.coords[-1])
                for left_point in left_ends:
                    for right_point in right_ends:
                        distance = math.hypot(
                            left_point[0] - right_point[0],
                            left_point[1] - right_point[1],
                        )
                        if best is None or distance < best[0]:
                            best = (distance, left_point, right_point)
        if best is None or best[0] > tolerance_m:
            break
        if best[0] > 1e-6:
            connectors.append(LineString([best[1], best[2]]))
        new_merged = _merge_lines([*lines, *connectors])
        if len(new_merged) >= len(merged) and best[0] <= 1e-6:
            break
        merged = new_merged

    return merged, len(connectors)


def _group_score(lines: list[LineString], bbox_local) -> float:
    total_length = sum(float(line.length) for line in lines)
    if not bbox_local or total_length <= 0:
        return total_length
    xmin, ymin, xmax, ymax = (float(value) for value in bbox_local)
    width = max(xmax - xmin, 1.0)
    height = max(ymax - ymin, 1.0)
    union = unary_union(lines)
    gxmin, gymin, gxmax, gymax = union.bounds
    span_x = min(1.0, max(0.0, (gxmax - gxmin) / width))
    span_y = min(1.0, max(0.0, (gymax - gymin) / height))
    # Long frame-spanning rivers and bends are the strongest city dividers.
    spread = max(span_x, span_y)
    two_axis = min(span_x, span_y)
    return total_length * (1.0 + 0.70 * spread + 0.35 * two_axis)


def select_visible_water_lines(
    candidates: list[WaterLineCandidate],
    *,
    bbox_local=None,
    nozzle_real_m: float,
) -> VisibleWaterSelection:
    """Select complete high-contrast water corridors under a physical budget."""

    if nozzle_real_m <= 0 or not math.isfinite(nozzle_real_m):
        raise ValueError("nozzle_real_m must be positive")
    if not candidates:
        return VisibleWaterSelection([], {
            "policy_version": POLICY_VERSION,
            "source_line_segments": 0,
            "candidate_groups": 0,
            "selected_groups": 0,
            "visible_line_segments": 0,
            "gap_bridges": 0,
            "budget_applied": False,
        })

    groups: dict[tuple[str, str], list[WaterLineCandidate]] = {}
    for candidate in candidates:
        key = (candidate.waterway, candidate.identity)
        groups.setdefault(key, []).append(candidate)

    apply_budget = bbox_local is not None and nozzle_real_m >= 25.0
    if bbox_local is not None:
        xmin, ymin, xmax, ymax = (float(value) for value in bbox_local)
        frame_area = max((xmax - xmin) * (ymax - ymin), 1.0)
    else:
        frame_area = 1.0

    selected_keys: set[tuple[str, str]] = set()
    class_evidence = {}
    classes = sorted({key[0] for key in groups})
    for waterway in classes:
        keys = [key for key in groups if key[0] == waterway]
        keys.sort(key=lambda key: (
            -_group_score([item.geometry for item in groups[key]], bbox_local),
            key[1],
        ))
        quota = _LINE_INK_QUOTAS.get(waterway, 0.0005)
        used = 0.0
        for key in keys:
            items = groups[key]
            length = sum(float(item.geometry.length) for item in items)
            full_width = max(
                nozzle_real_m,
                max(item.half_width_m for item in items) * 2.0,
            )
            estimated_ratio = length * full_width / frame_area
            if apply_budget and any(k[0] == waterway for k in selected_keys):
                if used + estimated_ratio > quota:
                    continue
            selected_keys.add(key)
            used += estimated_ratio
        class_evidence[waterway] = {
            "candidate_groups": len(keys),
            "selected_groups": sum(1 for key in selected_keys
                                   if key[0] == waterway),
            "quota_ratio": quota,
            "estimated_ink_ratio": round(used, 6),
        }

    output: list[tuple[LineString, str, float]] = []
    bridge_count = 0
    # Two nozzle widths close OSM way splits and bridge/culvert tag gaps while
    # remaining far below a city-block-scale invented connection.
    bridge_tolerance = nozzle_real_m * 2.0
    for key in sorted(selected_keys):
        items = groups[key]
        merged, added = _bridge_named_corridor(
            [item.geometry for item in items],
            tolerance_m=bridge_tolerance,
        )
        half_width = max(item.half_width_m for item in items)
        output.extend((line, key[0], half_width) for line in merged)
        bridge_count += added

    return VisibleWaterSelection(output, {
        "policy_version": POLICY_VERSION,
        "method": "semantic_corridor_ink_budget_v1",
        "source_line_segments": len(candidates),
        "candidate_groups": len(groups),
        "selected_groups": len(selected_keys),
        "visible_line_segments": len(output),
        "gap_bridges": bridge_count,
        "budget_applied": apply_budget,
        "class_budgets": class_evidence,
    })
