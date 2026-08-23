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
from shapely.errors import GEOSException
from shapely.ops import linemerge, unary_union


POLICY_VERSION = "print-water-roles-v4"

_LINE_INK_QUOTAS = {
    "river": 0.0060,
    "riverbank": 0.0030,
    "canal": 0.0030,
    "stream": 0.0010,
    "drain": 0.0004,
    "ditch": 0.0003,
}

# At 15/25 km every selected line must be widened to a printable strip.  A
# per-waterway quota therefore gives minor streams and drains the same visual
# entitlement as a city-defining river.  Use one shared foreground budget and
# keep the low-order network for topology/block cutting only.
_LARGE_VISIBLE_WATERWAYS = frozenset({"river", "riverbank", "canal"})
_LARGE_GLOBAL_INK_QUOTA = 0.012
_LARGE_MAX_CORRIDORS = 3
_LARGE_MIN_FRAME_SPAN = 0.08
_LARGE_MAX_LANDMARK_ENCLOSURES = 2
_LARGE_MIN_ENCLOSURE_SPAN = 0.02
_LARGE_LANDMARK_ENCLOSURE_QUOTA = 0.0015

@dataclass(frozen=True)
class WaterLineCandidate:
    geometry: LineString
    waterway: str
    identity: str
    half_width_m: float
    width_evidence: bool = False
    identity_enclosure: bool = False


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


def waterway_kind(row: pd.Series) -> str:
    """Return one normalized visible-water class from mixed OSM tagging.

    Area features such as historic moats are commonly tagged ``water=moat``
    with an empty ``waterway`` column.  Treating pandas NaN as a string used
    to turn these into an ineligible ``"nan"`` class at city scale.
    """

    waterway = _text(row.get("waterway"))
    if waterway:
        return waterway
    water = _text(row.get("water"))
    if water in {"river", "riverbank", "canal", "stream", "drain",
                 "ditch", "moat"}:
        return water
    return "river"


def is_identity_water_enclosure(row: pd.Series) -> bool:
    """Recognize a named historic water enclosure worth local exaggeration.

    This is deliberately narrower than :func:`is_water_landmark`: a named
    river is not automatically a local enclosure.  Moats with an external
    identity or a name are rare, finite city signatures such as the Forbidden
    City moat, and may be widened to the printer floor without restoring the
    surrounding minor-water network.
    """

    if waterway_kind(row) != "moat":
        return False
    has_name = bool(_text(row.get("name")) or _text(row.get("name:en")))
    has_external_identity = bool(
        _text(row.get("wikidata")) or _text(row.get("wikipedia")))
    return has_name or has_external_identity


def is_exposed_water_line(row: pd.Series) -> bool:
    """Reject mapped underground/covered water from visible material.

    The feature remains in the unfiltered source network used for structural
    block cutting.  This gate affects only the high-contrast water layer.
    """

    tunnel = _text(row.get("tunnel"))
    covered = _text(row.get("covered"))
    location = _text(row.get("location"))
    if tunnel not in ("", "no", "false", "0"):
        return False
    if covered in ("yes", "true", "1"):
        return False
    if location in ("underground", "underwater"):
        return False
    return True


def has_printable_water_mass(
    geometry,
    *,
    nozzle_real_m: float,
    min_core_ratio: float = 0.05,
) -> bool:
    """Return whether a surface reads as an area, not a sub-nozzle stroke.

    Area alone cannot distinguish a lake from a 10 km × 20 m canal.  Eroding
    by half a nozzle measures the two-dimensional printable core.  Requiring a
    small retained fraction removes long hairline polygons while preserving
    broad rivers, lakes, coastlines, and locally narrow shoreline details.
    """

    if geometry is None or geometry.is_empty or geometry.area <= 0:
        return False
    if nozzle_real_m <= 0 or not math.isfinite(nozzle_real_m):
        raise ValueError("nozzle_real_m must be positive")
    if not 0 <= min_core_ratio < 1:
        raise ValueError("min_core_ratio must be in [0, 1)")
    try:
        core = geometry.buffer(-nozzle_real_m * 0.5)
    except GEOSException:
        return False
    if core.is_empty:
        return False
    return float(core.area) / float(geometry.area) >= min_core_ratio


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


def _group_metrics(lines: list[LineString], bbox_local) -> dict[str, float]:
    total_length = sum(float(line.length) for line in lines)
    if not bbox_local or total_length <= 0:
        return {
            "length_m": total_length,
            "span_x": 0.0,
            "span_y": 0.0,
            "score": total_length,
        }
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
    return {
        "length_m": total_length,
        "span_x": span_x,
        "span_y": span_y,
        "score": total_length * (1.0 + 0.70 * spread + 0.35 * two_axis),
    }
def select_visible_water_lines(
    candidates: list[WaterLineCandidate],
    *,
    bbox_local=None,
    nozzle_real_m: float,
    visible_surface_ratio: float = 0.0,
) -> VisibleWaterSelection:
    """Select complete high-contrast water corridors under a physical budget."""

    if nozzle_real_m <= 0 or not math.isfinite(nozzle_real_m):
        raise ValueError("nozzle_real_m must be positive")
    if visible_surface_ratio < 0 or not math.isfinite(visible_surface_ratio):
        raise ValueError("visible_surface_ratio must be finite and non-negative")
    if not candidates:
        return VisibleWaterSelection([], {
            "policy_version": POLICY_VERSION,
            "source_line_segments": 0,
            "candidate_groups": 0,
            "selected_groups": 0,
            "visible_line_segments": 0,
            "gap_bridges": 0,
            "budget_applied": False,
            "landmark_enclosure_groups": 0,
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
    group_metrics = {
        key: _group_metrics(
            [item.geometry for item in items], bbox_local)
        for key, items in groups.items()
    }
    group_ink = {}
    group_has_width_evidence = {}
    for key, items in groups.items():
        full_width = max(
            nozzle_real_m,
            max(item.half_width_m for item in items) * 2.0,
        )
        group_ink[key] = group_metrics[key]["length_m"] * full_width / frame_area
        group_has_width_evidence[key] = (
            key[0] == "riverbank"
            or any(item.width_evidence for item in items)
        )

    fallback_without_surface = False
    landmark_enclosure_keys: set[tuple[str, str]] = set()
    if apply_budget:
        # Preserve a tiny number of compact, two-axis semantic enclosures
        # before selecting frame-spanning rivers.  Their separate small quota
        # prevents the exception from weakening the global water ink budget.
        enclosure_candidates = [
            key for key, items in groups.items()
            if (any(item.identity_enclosure for item in items)
                and min(group_metrics[key]["span_x"],
                        group_metrics[key]["span_y"])
                >= _LARGE_MIN_ENCLOSURE_SPAN)
        ]
        enclosure_candidates.sort(key=lambda key: (
            -group_metrics[key]["score"], key[1]))
        enclosure_ink = 0.0
        for key in enclosure_candidates:
            if len(landmark_enclosure_keys) >= _LARGE_MAX_LANDMARK_ENCLOSURES:
                break
            ratio = group_ink[key]
            if (landmark_enclosure_keys
                    and enclosure_ink + ratio
                    > _LARGE_LANDMARK_ENCLOSURE_QUOTA):
                continue
            landmark_enclosure_keys.add(key)
            selected_keys.add(key)
            enclosure_ink += ratio

        eligible = [
            key for key in groups
            if (key[0] in _LARGE_VISIBLE_WATERWAYS
                and group_has_width_evidence[key]
                and max(group_metrics[key]["span_x"],
                        group_metrics[key]["span_y"])
                >= _LARGE_MIN_FRAME_SPAN)
        ]
        # If no surface polygon expresses water at all, keep at most one
        # frame-spanning centreline as a data-quality fallback.  Where lakes,
        # sea, or river polygons already anchor the scene, unsupported narrow
        # centrelines add noise rather than identity.
        if not eligible and visible_surface_ratio < 0.002:
            eligible = [
                key for key in groups
                if (key[0] in _LARGE_VISIBLE_WATERWAYS
                    and max(group_metrics[key]["span_x"],
                            group_metrics[key]["span_y"])
                    >= _LARGE_MIN_FRAME_SPAN)
            ]
            if not eligible:
                eligible = [
                    key for key in groups
                    if (key[0] == "stream"
                        and max(group_metrics[key]["span_x"],
                                group_metrics[key]["span_y"])
                        >= _LARGE_MIN_FRAME_SPAN)
                ]
            fallback_without_surface = bool(eligible)

        class_priority = {"riverbank": 1.35, "river": 1.25, "canal": 1.0,
                          "stream": 0.7}
        eligible.sort(key=lambda key: (
            -(group_metrics[key]["score"] * class_priority.get(key[0], 0.5)),
            key[1],
        ))
        used = 0.0
        for key in eligible:
            max_corridors = (1 if fallback_without_surface
                             else _LARGE_MAX_CORRIDORS)
            selected_corridors = len(selected_keys - landmark_enclosure_keys)
            if selected_corridors >= max_corridors:
                break
            if key in selected_keys:
                continue
            ratio = group_ink[key]
            if selected_corridors and used + ratio > _LARGE_GLOBAL_INK_QUOTA:
                continue
            selected_keys.add(key)
            used += ratio
    else:
        selected_keys.update(groups)

    class_evidence = {}
    classes = sorted({key[0] for key in groups})
    for waterway in classes:
        class_keys = [key for key in groups if key[0] == waterway]
        selected_class_keys = [key for key in selected_keys
                               if key[0] == waterway]
        class_evidence[waterway] = {
            "candidate_groups": len(class_keys),
            "selected_groups": len(selected_class_keys),
            "legacy_class_quota_ratio": _LINE_INK_QUOTAS.get(
                waterway, 0.0005),
            "estimated_ink_ratio": round(
                sum(group_ink[key] for key in selected_class_keys), 6),
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
        "method": "surface_evidence_global_water_budget_v4",
        "source_line_segments": len(candidates),
        "candidate_groups": len(groups),
        "selected_groups": len(selected_keys),
        "visible_line_segments": len(output),
        "gap_bridges": bridge_count,
        "budget_applied": apply_budget,
        "visible_surface_ratio": round(visible_surface_ratio, 6),
        "fallback_without_surface": fallback_without_surface,
        "global_ink_quota_ratio": (_LARGE_GLOBAL_INK_QUOTA
                                   if apply_budget else None),
        "max_visible_corridors": (_LARGE_MAX_CORRIDORS
                                  if apply_budget else None),
        "landmark_enclosure_groups": len(landmark_enclosure_keys),
        "landmark_enclosure_quota_ratio": (
            _LARGE_LANDMARK_ENCLOSURE_QUOTA if apply_budget else None),
        "selected_group_identities": [
            f"{waterway}:{identity}"
            for waterway, identity in sorted(selected_keys)
        ],
        "selected_width_evidence_groups": sum(
            1 for key in selected_keys if group_has_width_evidence[key]),
        "class_budgets": class_evidence,
    })
