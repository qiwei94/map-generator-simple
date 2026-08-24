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
import re
from typing import Any

import geopandas as gpd
from shapely.geometry import Point
from shapely.ops import unary_union

from .buildings import ROAD_TIERS
from .config import ROAD_DEFAULT_WIDTH_M, ROAD_FILTER, ROAD_WIDTHS


POLICY_VERSION = "print-road-roles-v6"

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
    "motorway": 0.015,
    "trunk": 0.005,
    "primary": 0.018,
    "secondary": 0.007,
}
_LARGE_CONTEXT_INK_QUOTA = 0.008
_LARGE_HARD_INK_LIMIT = 0.055
_CONTEXT_GRID_SIZE = 6
_PROTECTED_RING_INK_LIMIT = 0.018
_VISUAL_SALIENCE_MIN_COVERAGE = 0.30
_VISUAL_SALIENCE_MIN_WEIGHT = 0.20

_NUMBERED_RING_RE = re.compile(
    r"(?:([一二三四五六七八九十\d]+)\s*环|"
    r"\b(\d+(?:st|nd|rd|th)?)\s+ring(?:\s+road)?\b)",
    re.IGNORECASE,
)
_GENERIC_RING_TERMS = (
    "inner ring", "outer ring", "ring road", "beltway", "orbital",
    "périphérique", "peripherique", "tangenziale",
)


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


def _sampled_grid_cells(geometry, bbox_local, grid_size: int) -> set[int]:
    """Return grid cells traversed by a line without using its full bounds.

    Bounds make a diagonal or ring appear to fill every interior cell.  Point
    samples at under half a cell width retain the actual corridor footprint
    and keep the context-selection pass deterministic and inexpensive.
    """

    xmin, ymin, xmax, ymax = (float(value) for value in bbox_local)
    frame_width = max(xmax - xmin, 1.0)
    frame_height = max(ymax - ymin, 1.0)
    sample_step = min(frame_width, frame_height) / grid_size * 0.45
    if geometry is None or geometry.is_empty:
        return set()
    if geometry.geom_type == "LineString":
        parts = [geometry]
    elif geometry.geom_type == "MultiLineString":
        parts = list(geometry.geoms)
    elif hasattr(geometry, "geoms"):
        parts = [part for part in geometry.geoms
                 if part.geom_type == "LineString"]
    else:
        parts = []

    cells: set[int] = set()
    for part in parts:
        samples = max(1, int(math.ceil(float(part.length) / sample_step)))
        for index in range(samples + 1):
            point = part.interpolate(index / samples, normalized=True)
            gx = int((point.x - xmin) / frame_width * grid_size)
            gy = int((point.y - ymin) / frame_height * grid_size)
            gx = min(grid_size - 1, max(0, gx))
            gy = min(grid_size - 1, max(0, gy))
            cells.add(gy * grid_size + gx)
    return cells


def ring_corridor_identity(value: Any) -> str:
    """Collapse directional names of the same semantic urban ring.

    OSM commonly maps one ring as east/west/north/south names and sometimes
    changes highway class along the route.  A physical identity selector must
    see one ring, not four unrelated high-scoring roads.
    """

    if not isinstance(value, str) or not value.strip():
        return ""
    normalized = " ".join(value.casefold().split())
    match = _NUMBERED_RING_RE.search(normalized)
    if match:
        number = match.group(1) or match.group(2)
        return f"numbered-ring:{number}"
    for term in _GENERIC_RING_TERMS:
        if term in normalized:
            return f"named-ring:{term}"
    return ""


def _apply_large_area_ink_budget(
    visible: gpd.GeoDataFrame,
    *,
    bbox_local,
    scale_mm_per_m: float,
    road_width_multiplier: float,
    min_colored_strip_mm: float,
    nozzle_real_m: float,
    visual_salience_guide=None,
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
    position_ratios: dict[int, float] = {}
    group_geometries = {}
    visual_group_support = {}
    ring_positions: dict[str, set[int]] = {}
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
        raw_name_en = row.get("name:en")
        ring_identity = (ring_corridor_identity(raw_name)
                         or ring_corridor_identity(raw_name_en))
        if ring_identity:
            ring_positions.setdefault(ring_identity, set()).add(pos)
        if isinstance(raw_name, str) and raw_name.strip():
            identity = f"name:{raw_name.strip().casefold()}"
        elif isinstance(raw_ref, str) and raw_ref.strip():
            identity = f"ref:{raw_ref.strip().casefold()}"
        else:
            identity = f"@{pos}"
        key = (base, identity)
        groups.setdefault(key, []).append(pos)
        lengths[key] = lengths.get(key, 0.0) + float(geom.length)
        width = resolve_printable_road_width_m(
            highway,
            scale_mm_per_m=scale_mm_per_m,
            road_width_multiplier=road_width_multiplier,
            min_colored_strip_mm=min_colored_strip_mm,
        )
        widths[key] = width
        position_ratios[pos] = float(geom.length) * width / frame_area

    for key, positions in groups.items():
        geoms = [work.iloc[pos].geometry for pos in positions]
        union = unary_union(geoms)
        group_geometries[key] = union
        gxmin, gymin, gxmax, gymax = union.bounds
        span_x = min(1.0, max(0.0, (gxmax - gxmin) / max(xmax - xmin, 1.0)))
        span_y = min(1.0, max(0.0, (gymax - gymin) / max(ymax - ymin, 1.0)))
        spread = max(span_x, span_y)
        two_axis = min(span_x, span_y)
        named_bonus = 1.15 if not key[1].startswith("@") else 1.0
        # Frame-spanning radials and two-axis rings are city identity.  Total
        # length remains the base signal, while geometry breaks name ties.
        base_score = (lengths[key] * named_bonus
                      * (1.0 + 0.55 * spread + 0.75 * two_axis))
        if visual_salience_guide is not None:
            support = visual_salience_guide.road_support(union)
            visual_group_support[key] = support
            base_score *= (
                1.0
                + 2.0 * float(support.get("weighted_salience", 0.0))
                + 0.5 * float(support.get("covered_fraction", 0.0))
            )
        scores[key] = base_score

    selected_positions: set[int] = set()
    protected_ring_evidence = {
        "selected": False,
        "candidate_groups": len(ring_positions),
    }

    # Choose at most one complete inner urban ring before class quotas.  This
    # prevents directional OSM names or class transitions from punching large
    # gaps into a defining landmark.  The closest compact two-axis ring wins;
    # outer frame-edge beltways stay ordinary candidates.
    protected_candidates = []
    frame_width = max(xmax - xmin, 1.0)
    frame_height = max(ymax - ymin, 1.0)
    frame_center = Point((xmin + xmax) * 0.5, (ymin + ymax) * 0.5)
    for identity, positions in ring_positions.items():
        geometries = [work.iloc[pos].geometry for pos in sorted(positions)]
        union = unary_union(geometries)
        gxmin, gymin, gxmax, gymax = union.bounds
        span_x = (gxmax - gxmin) / frame_width
        span_y = (gymax - gymin) / frame_height
        two_axis = min(span_x, span_y)
        if two_axis < 0.16 or max(span_x, span_y) > 0.82:
            continue
        # A generic "Inner Ring"/"Ring Road" covering most of a 25 km frame
        # is useful topology, but is not automatically the composition's hero.
        # Numbered rings carry stronger city-specific semantics (for example
        # Beijing's directional 二环 segments) and may use the wider envelope.
        generic_limit = (0.64 if identity == "named-ring:inner ring"
                         else 0.52)
        if (identity.startswith("named-ring:")
                and max(span_x, span_y) > generic_limit):
            continue
        ratio = 0.0
        for pos in positions:
            row = work.iloc[pos]
            ratio += (
                float(row.geometry.length)
                * resolve_printable_road_width_m(
                    str(row.get("highway") or ""),
                    scale_mm_per_m=scale_mm_per_m,
                    road_width_multiplier=road_width_multiplier,
                    min_colored_strip_mm=min_colored_strip_mm,
                )
                / frame_area
            )
        if ratio > _PROTECTED_RING_INK_LIMIT:
            continue
        center_offset = union.centroid.distance(frame_center) / max(
            min(frame_width, frame_height) * 0.5, 1.0)
        compact_span = math.exp(
            -((max(span_x, span_y) - 0.36) / 0.16) ** 2)
        score = (two_axis * (1.0 - min(center_offset, 1.0))
                 * compact_span)
        protected_candidates.append((score, identity, positions, ratio,
                                     span_x, span_y))

    if protected_candidates:
        protected_candidates.sort(key=lambda item: (-item[0], item[1]))
        _, identity, positions, ratio, span_x, span_y = protected_candidates[0]
        selected_positions.update(positions)
        protected_ring_evidence = {
            "selected": True,
            "candidate_groups": len(ring_positions),
            "eligible_groups": len(protected_candidates),
            "identity": identity,
            "features": len(positions),
            "estimated_ink_ratio": round(ratio, 6),
            "span_x": round(span_x, 4),
            "span_y": round(span_y, 4),
            "ink_limit_ratio": _PROTECTED_RING_INK_LIMIT,
        }

    class_evidence = {}
    for highway, quota in _LARGE_INK_QUOTAS.items():
        candidates = [key for key in groups if key[0] == highway]
        candidates.sort(key=lambda key: (-scores[key], key[1]))
        used = sum(
            position_ratios.get(pos, 0.0)
            for pos in selected_positions
            if str(work.iloc[pos].get("highway") or "") == highway
        )
        selected_groups = 0
        for key in candidates:
            remaining = [pos for pos in groups[key]
                         if pos not in selected_positions]
            if not remaining:
                continue
            estimated_ratio = sum(position_ratios[pos] for pos in remaining)
            if visual_salience_guide is not None:
                current_ratio = sum(
                    position_ratios.get(pos, 0.0)
                    for pos in selected_positions)
                if current_ratio + estimated_ratio > _LARGE_HARD_INK_LIMIT:
                    continue
            # Do not let the first very long/duplicated corridor bypass its
            # entire class budget.  Continue scanning for a smaller complete
            # identity; otherwise one trunk name can consume the global hard
            # ceiling and prevent the spatial pass from restoring the city's
            # other defining axes.
            if used + estimated_ratio > quota:
                continue
            selected_positions.update(remaining)
            used += estimated_ratio
            selected_groups += 1
        class_evidence[highway] = {
            "quota_ratio": quota,
            "candidate_groups": len(candidates),
            "selected_groups": selected_groups,
            "estimated_ink_ratio": round(used, 6),
        }

    empty_selection_fallback = None
    if not selected_positions and groups:
        # A one-road fixture or genuinely sparse OSM frame must not collapse
        # to zero merely because its only complete identity exceeds a narrow
        # per-class quota.  The global physical ceiling remains mandatory.
        eligible = [
            key for key in groups
            if sum(position_ratios[pos] for pos in groups[key])
            <= _LARGE_HARD_INK_LIMIT
        ]
        if eligible:
            eligible.sort(key=lambda key: (-scores[key], key[1]))
            key = eligible[0]
            selected_positions.update(groups[key])
            empty_selection_fallback = {
                "highway": key[0],
                "identity": key[1],
                "features": len(groups[key]),
                "estimated_ink_ratio": round(
                    sum(position_ratios[pos] for pos in groups[key]), 6),
            }

    # The class budgets above protect long ring/radial/arterial identities,
    # but a few high-scoring corridors can still leave most of the city blank.
    # Add a limited second pass that fills underrepresented frame cells with
    # complete named corridors.  Ink is now a hard ceiling, not the ranking
    # objective: a candidate must first improve spatial structure.
    group_cells = {
        key: _sampled_grid_cells(
            group_geometries[key], bbox_local, _CONTEXT_GRID_SIZE)
        for key in groups
    }
    source_cell_counts = [0] * (_CONTEXT_GRID_SIZE ** 2)
    selected_cell_counts = [0] * (_CONTEXT_GRID_SIZE ** 2)
    for key, cells in group_cells.items():
        for cell in cells:
            source_cell_counts[cell] += 1
    for pos in selected_positions:
        for cell in _sampled_grid_cells(
                work.iloc[pos].geometry, bbox_local, _CONTEXT_GRID_SIZE):
            selected_cell_counts[cell] += 1

    cell_targets = []
    for cell, source_count in enumerate(source_cell_counts):
        gx = cell % _CONTEXT_GRID_SIZE
        gy = cell // _CONTEXT_GRID_SIZE
        central = gx in (2, 3) and gy in (2, 3)
        desired = 4 if central else 3
        cell_targets.append(min(source_count, desired))

    def _target_satisfaction(counts) -> int:
        return sum(min(count, target)
                   for count, target in zip(counts, cell_targets))

    context_before = _target_satisfaction(selected_cell_counts)
    base_group_ratio = sum(position_ratios.get(pos, 0.0)
                           for pos in selected_positions)
    context_group_keys: set[tuple[str, str]] = set()
    context_positions: set[int] = set()
    context_ratio = 0.0
    while True:
        best = None
        for key, cells in group_cells.items():
            remaining = [pos for pos in groups[key]
                         if pos not in selected_positions]
            if not remaining or len(cells) < 2:
                continue
            # Unnamed fragments must cross several cells to qualify as a
            # corridor; otherwise dense OSM segmentation wins by tiny cost.
            if key[1].startswith("@") and len(cells) < 3:
                continue
            ratio = sum(position_ratios[pos] for pos in remaining)
            if (context_ratio + ratio > _LARGE_CONTEXT_INK_QUOTA
                    or base_group_ratio + context_ratio + ratio
                    > _LARGE_HARD_INK_LIMIT):
                continue
            contribution = sum(
                max(0, cell_targets[cell] - selected_cell_counts[cell])
                for cell in cells)
            if contribution <= 0:
                continue
            connected_cells = sum(
                selected_cell_counts[cell] > 0 for cell in cells)
            # Contribution dominates.  Connectivity and identity score only
            # break ties, so a famous edge motorway cannot consume a context
            # slot that would complete the centre or an empty quadrant.
            rank = (contribution, connected_cells, scores[key], -ratio,
                    key[1])
            if best is None or rank > best[0]:
                best = (rank, key, remaining, ratio)
        if best is None:
            break
        key, remaining, ratio = best[1:]
        context_group_keys.add(key)
        selected_positions.update(remaining)
        context_positions.update(remaining)
        context_ratio += ratio
        for cell in group_cells[key]:
            selected_cell_counts[cell] += 1

    context_after = _target_satisfaction(selected_cell_counts)
    context_target_total = sum(cell_targets)

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
    selected_visual_keys = [
        key for key, support in visual_group_support.items()
        if (any(pos in selected_positions for pos in groups[key])
            and not key[1].startswith("@")
            and float(support.get("covered_fraction", 0.0))
            >= _VISUAL_SALIENCE_MIN_COVERAGE
            and float(support.get("weighted_salience", 0.0))
            >= _VISUAL_SALIENCE_MIN_WEIGHT)
    ]
    selected_visual_ratio = sum(
        position_ratios.get(pos, 0.0)
        for key in selected_visual_keys for pos in groups[key]
        if pos in selected_positions)
    return selected, {
        "applied": True,
        "method": "identity_spatial_context_and_optional_salience_v6",
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
        "context_grid_size": _CONTEXT_GRID_SIZE,
        "context_selected_groups": len(context_group_keys),
        "context_selected_features": len(context_positions),
        "context_estimated_ink_ratio": round(context_ratio, 6),
        "context_ink_quota_ratio": _LARGE_CONTEXT_INK_QUOTA,
        "hard_ink_limit_ratio": _LARGE_HARD_INK_LIMIT,
        "protected_ring": protected_ring_evidence,
        "visual_salience": {
            "enabled": visual_salience_guide is not None,
            "mode": "rank_complete_osm_groups_within_existing_budgets",
            "reference_version": (getattr(
                visual_salience_guide, "version", None)
                if visual_salience_guide is not None else None),
            "supported_candidate_groups": sum(
                1 for support in visual_group_support.values()
                if (float(support.get("covered_fraction", 0.0))
                    >= _VISUAL_SALIENCE_MIN_COVERAGE
                    and float(support.get("weighted_salience", 0.0))
                    >= _VISUAL_SALIENCE_MIN_WEIGHT)),
            "selected_groups": len(selected_visual_keys),
            "selected_features": sum(
                sum(pos in selected_positions for pos in groups[key])
                for key in selected_visual_keys),
            "selected_estimated_ink_ratio": round(
                selected_visual_ratio, 6),
            "ink_quota_ratio": None,
            "selected_identities": [
                f"{highway}:{identity}"
                for highway, identity in sorted(selected_visual_keys)
            ],
        },
        "empty_selection_fallback": empty_selection_fallback,
        "context_target_units": context_target_total,
        "context_satisfied_before": context_before,
        "context_satisfied_after": context_after,
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
    visual_salience_guide=None,
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
            visual_salience_guide=visual_salience_guide,
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
