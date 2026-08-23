"""Deterministic separation of topology, structural, and visible roads.

One OSM road layer currently serves three incompatible jobs.  This module
selects three views of that same source without changing coordinates:

* topology: enough detail to polygonize city blocks;
* structural: roads allowed to leave seams in block-base geometry;
* visible: roads rendered as the contrasting road material.

The selection is driven by the real-world printer footprint.  It contains no
mesh, Z, boolean, rendering, or LLM decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

import geopandas as gpd
from shapely.geometry import Point
from shapely.ops import unary_union

from .buildings import ROAD_TIERS
from .config import ROAD_DEFAULT_WIDTH_M, ROAD_FILTER, ROAD_WIDTHS


POLICY_VERSION = "print-road-roles-v3"

_WIDTH_FACTORS = {
    "motorway": 1.35,
    "motorway_link": 1.10,
    "trunk": 1.25,
    "trunk_link": 1.05,
    "primary": 1.10,
    "primary_link": 1.0,
    "secondary": 1.0,
    "secondary_link": 1.0,
}

# Conservative line-length × printable-width budgets.  The estimate counts
# overlaps twice, so the resulting raster/union coverage is lower.  Separate
# class quotas keep a dense motorway system from consuming every visible slot
# before primary/radial streets are considered.
_LARGE_INK_QUOTAS = {
    "motorway": 0.012,
    "trunk": 0.004,
    "primary": 0.014,
    "secondary": 0.005,
}


@dataclass
class RoadRoleSelection:
    topology: gpd.GeoDataFrame
    structural: gpd.GeoDataFrame
    visible: gpd.GeoDataFrame
    evidence: dict[str, Any]


def _line_features(roads: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if roads is None or len(roads) == 0:
        return roads.iloc[0:0] if roads is not None else gpd.GeoDataFrame(
            {"geometry": []}, geometry="geometry")
    mask = (roads.geometry.notnull() & ~roads.geometry.is_empty
            & roads.geometry.type.isin(("LineString", "MultiLineString")))
    return roads.loc[mask]


def _highway_subset(
    roads: gpd.GeoDataFrame,
    allowed: set[str],
) -> gpd.GeoDataFrame:
    if roads is None or len(roads) == 0:
        return roads
    if "highway" not in roads.columns:
        return roads
    return roads.loc[roads["highway"].isin(allowed)]


def resolve_structural_tier(topology_tier: int, nozzle_real_m: float) -> int:
    """Keep block seams denser than visible roads, but not sub-print detail."""

    if topology_tier not in ROAD_TIERS:
        raise ValueError(f"unsupported topology road tier: {topology_tier}")
    if nozzle_real_m <= 0:
        raise ValueError("nozzle_real_m must be positive")
    if nozzle_real_m >= 25.0:
        ceiling = 3
    elif nozzle_real_m >= 10.0:
        ceiling = 4
    else:
        ceiling = 5
    return min(topology_tier, ceiling)


def resolve_visible_highways(nozzle_real_m: float) -> set[str]:
    """Choose the contrasting road-material candidates for the print scale."""

    if nozzle_real_m <= 0:
        raise ValueError("nozzle_real_m must be positive")
    if nozzle_real_m >= 25.0:
        return set(ROAD_FILTER["large"])
    return set(ROAD_TIERS[3])


def resolve_printable_road_width_m(
    highway: str,
    *,
    scale_mm_per_m: float,
    road_width_multiplier: float,
    min_colored_strip_mm: float,
) -> float:
    """Return one physical road width shared by previews and formal meshes."""

    if scale_mm_per_m <= 0 or not math.isfinite(scale_mm_per_m):
        raise ValueError("scale_mm_per_m must be positive")
    if road_width_multiplier <= 0 or not math.isfinite(road_width_multiplier):
        raise ValueError("road_width_multiplier must be positive")
    if min_colored_strip_mm <= 0 or not math.isfinite(min_colored_strip_mm):
        raise ValueError("min_colored_strip_mm must be positive")
    source_width_m = (ROAD_WIDTHS.get(highway, ROAD_DEFAULT_WIDTH_M)
                      * road_width_multiplier)
    hierarchy_floor_mm = min_colored_strip_mm * _WIDTH_FACTORS.get(highway, 1.0)
    return max(source_width_m, hierarchy_floor_mm / scale_mm_per_m)


def road_width_multiplier_from_layers(layers: Any, fallback: float) -> float:
    """Read the multiplier captured during preprocessing.

    Preview and formal exporters must consume the same resolved value.  Old
    cached ``LayerPolygons`` objects have no role evidence, so callers may
    provide their historical default as a compatibility fallback.
    """

    policy = (getattr(layers, "road_roles", {}) or {}).get(
        "width_policy", {}) or {}
    value = policy.get("road_width_multiplier", fallback)
    try:
        resolved = float(value)
    except (TypeError, ValueError):
        resolved = float(fallback)
    if resolved <= 0 or not math.isfinite(resolved):
        resolved = float(fallback)
    return resolved


def _apply_large_area_ink_budget(
    visible: gpd.GeoDataFrame,
    *,
    bbox_local,
    scale_mm_per_m: float,
    road_width_multiplier: float,
    min_colored_strip_mm: float,
    nozzle_real_m: float,
) -> tuple[gpd.GeoDataFrame, dict[str, Any]]:
    """Select city-scale corridors, then restore only necessary connectors."""

    xmin, ymin, xmax, ymax = (float(v) for v in bbox_local)
    frame_area = max((xmax - xmin) * (ymax - ymin), 1.0)
    work = visible.copy().reset_index(drop=True)
    if work.empty or "highway" not in work.columns:
        return work, {
            "applied": False,
            "reason": "no_tagged_candidates",
        }

    groups: dict[tuple[str, str], list[int]] = {}
    lengths: dict[tuple[str, str], float] = {}
    widths: dict[tuple[str, str], float] = {}
    scores: dict[tuple[str, str], float] = {}
    for pos, row in work.iterrows():
        highway = str(row.get("highway") or "")
        # Interchange ramps are useful for routing, but at 15/25 km they turn
        # junctions into black knots.  The through road remains in topology.
        if highway.endswith("_link"):
            continue
        base = highway if highway in _LARGE_INK_QUOTAS else ""
        if not base:
            continue
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        raw_name = row.get("name")
        raw_ref = row.get("ref")
        if isinstance(raw_name, str) and raw_name.strip():
            identity = f"name:{raw_name.strip().casefold()}"
        elif isinstance(raw_ref, str) and raw_ref.strip():
            identity = f"ref:{raw_ref.strip().casefold()}"
        else:
            identity = f"@{pos}"
        key = (base, identity)
        groups.setdefault(key, []).append(pos)
        lengths[key] = lengths.get(key, 0.0) + float(geom.length)
        widths[key] = resolve_printable_road_width_m(
            highway,
            scale_mm_per_m=scale_mm_per_m,
            road_width_multiplier=road_width_multiplier,
            min_colored_strip_mm=min_colored_strip_mm,
        )

    for key, positions in groups.items():
        geoms = [work.iloc[pos].geometry for pos in positions]
        union = unary_union(geoms)
        gxmin, gymin, gxmax, gymax = union.bounds
        span_x = min(1.0, max(0.0, (gxmax - gxmin) / max(xmax - xmin, 1.0)))
        span_y = min(1.0, max(0.0, (gymax - gymin) / max(ymax - ymin, 1.0)))
        spread = max(span_x, span_y)
        two_axis = min(span_x, span_y)
        named_bonus = 1.15 if not key[1].startswith("@") else 1.0
        # Frame-spanning radials and two-axis rings are city identity.  Total
        # length remains the base signal, while geometry breaks name ties.
        scores[key] = (lengths[key] * named_bonus
                       * (1.0 + 0.55 * spread + 0.75 * two_axis))

    selected_positions: set[int] = set()
    class_evidence = {}
    for highway, quota in _LARGE_INK_QUOTAS.items():
        candidates = [key for key in groups if key[0] == highway]
        candidates.sort(key=lambda key: (-scores[key], key[1]))
        used = 0.0
        selected_groups = 0
        for key in candidates:
            estimated_ratio = lengths[key] * widths[key] / frame_area
            if selected_groups and used + estimated_ratio > quota:
                continue
            selected_positions.update(groups[key])
            used += estimated_ratio
            selected_groups += 1
        class_evidence[highway] = {
            "quota_ratio": quota,
            "candidate_groups": len(candidates),
            "selected_groups": selected_groups,
            "estimated_ink_ratio": round(used, 6),
        }

    # Budgeting whole corridors can legitimately omit most roads, but it must
    # not leave a short OSM name-change segment or interchange link missing
    # between two already selected corridors.  Add only short features whose
    # two endpoints land on the selected network; dangling links stay hidden.
    connector_positions: set[int] = set()
    connector_ratio = 0.0
    connector_ratio_limit = 0.0008
    connector_max_length = min(max(xmax - xmin, ymax - ymin) * 0.04, 1000.0)
    ordinary_connector_max_length = max(nozzle_real_m * 4.0, 250.0)
    snap_distance = max(nozzle_real_m * 0.35, 15.0)
    for _ in range(2):
        if not selected_positions:
            break
        selected_union = unary_union(
            [work.iloc[pos].geometry for pos in sorted(selected_positions)])
        added_this_pass = 0
        remaining = [pos for pos in range(len(work))
                     if pos not in selected_positions]
        remaining.sort(key=lambda pos: float(work.iloc[pos].geometry.length))
        for pos in remaining:
            row = work.iloc[pos]
            geom = row.geometry
            highway = str(row.get("highway") or "")
            max_length = (connector_max_length if highway.endswith("_link")
                          else ordinary_connector_max_length)
            if geom is None or geom.is_empty or geom.length > max_length:
                continue
            parts = (list(geom.geoms)
                     if geom.geom_type == "MultiLineString" else [geom])
            endpoints = []
            for part in parts:
                try:
                    endpoints.extend((Point(part.coords[0]), Point(part.coords[-1])))
                except (AttributeError, IndexError, NotImplementedError):
                    continue
            if len(endpoints) < 2:
                continue
            near = sum(point.distance(selected_union) <= snap_distance
                       for point in endpoints)
            if near < 2:
                continue
            width = resolve_printable_road_width_m(
                highway,
                scale_mm_per_m=scale_mm_per_m,
                road_width_multiplier=road_width_multiplier,
                min_colored_strip_mm=min_colored_strip_mm,
            )
            ratio = float(geom.length) * width / frame_area
            if connector_ratio + ratio > connector_ratio_limit:
                continue
            selected_positions.add(pos)
            connector_positions.add(pos)
            connector_ratio += ratio
            added_this_pass += 1
        if not added_this_pass:
            break

    selected = work.iloc[sorted(selected_positions)].copy()
    all_estimated = sum(
        lengths[key] * widths[key] / frame_area for key in groups)
    selected_estimated = 0.0
    for pos in selected_positions:
        row = work.iloc[pos]
        selected_estimated += (
            float(row.geometry.length)
            * resolve_printable_road_width_m(
                str(row.get("highway") or ""),
                scale_mm_per_m=scale_mm_per_m,
                road_width_multiplier=road_width_multiplier,
                min_colored_strip_mm=min_colored_strip_mm,
            )
            / frame_area
        )
    return selected, {
        "applied": True,
        "method": "city_identity_corridor_budget_v2",
        "candidate_features_without_links": sum(len(v) for v in groups.values()),
        "selected_features": len(selected),
        "candidate_estimated_ink_ratio": round(all_estimated, 6),
        "selected_estimated_ink_ratio": round(selected_estimated, 6),
        "connector_features": len(connector_positions),
        "connector_estimated_ink_ratio": round(connector_ratio, 6),
        "connector_max_length_m": round(connector_max_length, 3),
        "ordinary_connector_max_length_m": round(
            ordinary_connector_max_length, 3),
        "connector_snap_m": round(snap_distance, 3),
        "class_budgets": class_evidence,
    }


def select_road_roles(
    roads: gpd.GeoDataFrame,
    *,
    topology_tier: int,
    nozzle_real_m: float,
    bbox_local=None,
    scale_mm_per_m: float | None = None,
    road_width_multiplier: float = 2.0,
    min_colored_strip_mm: float = 0.63,
) -> RoadRoleSelection:
    """Return role-specific GeoDataFrames and auditable candidate counts."""

    if topology_tier not in ROAD_TIERS:
        raise ValueError(f"unsupported topology road tier: {topology_tier}")
    lines = _line_features(roads)
    structural_tier = resolve_structural_tier(topology_tier, nozzle_real_m)
    topology = _highway_subset(lines, set(ROAD_TIERS[topology_tier]))
    structural = _highway_subset(lines, set(ROAD_TIERS[structural_tier]))
    visible_highways = resolve_visible_highways(nozzle_real_m)
    visible_candidates = _highway_subset(lines, visible_highways)
    visible = visible_candidates
    ink_budget = {"applied": False, "reason": "small_or_medium_print_footprint"}
    if (nozzle_real_m >= 25.0 and bbox_local is not None
            and scale_mm_per_m is not None):
        visible, ink_budget = _apply_large_area_ink_budget(
            visible_candidates,
            bbox_local=bbox_local,
            scale_mm_per_m=scale_mm_per_m,
            road_width_multiplier=road_width_multiplier,
            min_colored_strip_mm=min_colored_strip_mm,
            nozzle_real_m=nozzle_real_m,
        )

    fallback = None
    if roads is not None and len(roads) and "highway" not in roads.columns:
        fallback = "missing_highway_column_keep_all_lines"
    evidence = {
        "policy_version": POLICY_VERSION,
        "nozzle_real_m": float(nozzle_real_m),
        "topology_tier": int(topology_tier),
        "structural_tier": int(structural_tier),
        "visible_highways": sorted(visible_highways),
        "source_features": 0 if roads is None else len(roads),
        "source_line_features": len(lines),
        "topology_candidates": len(topology),
        "structural_candidates": len(structural),
        "visible_candidates": len(visible_candidates),
        "visible_selected": len(visible),
        "width_policy": {
            "road_width_multiplier": float(road_width_multiplier),
            "min_colored_strip_mm": float(min_colored_strip_mm),
            "class_floor_factors": dict(_WIDTH_FACTORS),
        },
        "ink_budget": ink_budget,
        "fallback": fallback,
    }
    return RoadRoleSelection(
        topology=topology,
        structural=structural,
        visible=visible,
        evidence=evidence,
    )
