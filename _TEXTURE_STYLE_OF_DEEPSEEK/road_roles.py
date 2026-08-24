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


POLICY_VERSION = "print-road-roles-v9"
COMPOSITION_ROLE_COLUMN = "_composition_role"

# A role changes visual hierarchy, never source geometry.  The secondary and
# connector factors may reduce an OSM source width, but the physical printer
# floor in ``resolve_composed_road_width_m`` remains absolute.
_COMPOSITION_WIDTH_FACTORS = {
    "primary": 1.0,
    "secondary": 0.90,
    "connector": 0.85,
    "context": 0.72,
    "foreground": 1.0,
}
_COMPOSITION_FLOOR_FACTORS = {
    "primary": 1.35,
    "foreground": 1.0,
    "secondary": 1.0,
    "connector": 1.0,
    "context": 1.0,
}

# AMap's no-label cartography is used as a spatial composition template when
# explicitly enabled.  These are overlap confidences, not global ink budgets:
# the reference map decides how much skeleton exists in each city, while the
# printer profile still decides whether the matched OSM line can be produced.
_TEMPLATE_PRIMARY_MAJOR_FRACTION = 0.24
_TEMPLATE_PRIMARY_WEIGHT = 0.76
_TEMPLATE_SECONDARY_ARTERIAL_FRACTION = 0.30
_TEMPLATE_SECONDARY_WEIGHT = 0.50
_TEMPLATE_CONTEXT_FRACTION = 0.58
_TEMPLATE_CONTEXT_WEIGHT = 0.28
_TEMPLATE_MAX_DANGLING_CHAIN_FRAME_FRACTION = 0.024
_TEMPLATE_CONTINUATION_COSINE = 0.70

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


def resolve_composed_road_width_m(
    highway: str,
    *,
    composition_role: str = "foreground",
    scale_mm_per_m: float,
    road_width_multiplier: float,
    min_colored_strip_mm: float,
) -> float:
    """Resolve one role-aware width without violating the printer floor.

    ``primary`` corridors retain the resolved foreground width.  ``secondary``
    and ``connector`` corridors become quieter only when their factual/source
    width is above the physical minimum; they can never become sub-printable.
    """

    if scale_mm_per_m <= 0 or not math.isfinite(scale_mm_per_m):
        raise ValueError("scale_mm_per_m must be positive")
    if road_width_multiplier <= 0 or not math.isfinite(road_width_multiplier):
        raise ValueError("road_width_multiplier must be positive")
    if min_colored_strip_mm <= 0 or not math.isfinite(min_colored_strip_mm):
        raise ValueError("min_colored_strip_mm must be positive")
    role_factor = _COMPOSITION_WIDTH_FACTORS.get(
        str(composition_role or "foreground"), 1.0)
    source_width_m = (
        ROAD_WIDTHS.get(highway, ROAD_DEFAULT_WIDTH_M)
        * road_width_multiplier
        * role_factor
    )
    hierarchy_factor = max(
        _COMPOSITION_FLOOR_FACTORS.get(
            str(composition_role or "foreground"), 1.0),
        _WIDTH_FACTORS.get(highway, 1.0) * role_factor,
    )
    floor_m = min_colored_strip_mm * hierarchy_factor / scale_mm_per_m
    return max(source_width_m, floor_m)


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


def _line_terminals(geometry) -> tuple[Point, Point] | None:
    """Return endpoints for one simple source feature, if available."""

    if geometry is None or geometry.is_empty \
            or geometry.geom_type != "LineString":
        return None
    try:
        coordinates = list(geometry.coords)
    except (AttributeError, NotImplementedError):
        return None
    if len(coordinates) < 2:
        return None
    return Point(coordinates[0]), Point(coordinates[-1])


def _terminal_direction(geometry, terminal_index: int) \
        -> tuple[float, float] | None:
    """Return a unit vector pointing from an endpoint into the feature."""

    try:
        coordinates = list(geometry.coords)
    except (AttributeError, NotImplementedError):
        return None
    if len(coordinates) < 2:
        return None
    if terminal_index == 0:
        start, end = coordinates[0], coordinates[1]
    else:
        start, end = coordinates[-1], coordinates[-2]
    dx = float(end[0]) - float(start[0])
    dy = float(end[1]) - float(start[1])
    magnitude = math.hypot(dx, dy)
    if magnitude <= 1e-9:
        return None
    return dx / magnitude, dy / magnitude


def _truthy_osm_tag(value: Any) -> bool:
    if value is None:
        return False
    return str(value).strip().casefold() not in {
        "", "0", "false", "nan", "no", "none",
    }


def _prune_template_dangling_chains(
    work: gpd.GeoDataFrame,
    role_by_position: dict[int, str],
    *,
    bbox_local,
) -> tuple[dict[int, str], dict[str, Any]]:
    """Hide short selected leaf chains while preserving source geometry.

    A dilated reference road mask can overlap the first few metres of an OSM
    side street.  Per-feature selection then renders only those source
    segments and creates a thread-like spur.  This pass traces the selected
    OSM graph from free endpoints and removes short leaf chains that terminate
    at the retained network.  Removed roads remain in structural/Block base.
    """

    selected_positions = sorted(role_by_position)
    xmin, ymin, xmax, ymax = (float(value) for value in bbox_local)
    frame_span = max(xmax - xmin, ymax - ymin, 1.0)
    max_chain_length_m = (
        frame_span * _TEMPLATE_MAX_DANGLING_CHAIN_FRAME_FRACTION)
    endpoint_snap_m = max(0.5, min(2.0, frame_span * 0.00004))
    frame_margin_m = max(endpoint_snap_m * 2.0, frame_span * 0.0025)
    base_evidence = {
        "method": "selected_osm_graph_leaf_chain_v1",
        "max_chain_length_m": round(max_chain_length_m, 3),
        "endpoint_snap_m": round(endpoint_snap_m, 3),
        "frame_margin_m": round(frame_margin_m, 3),
        "candidate_features": len(selected_positions),
    }
    if len(selected_positions) < 2:
        return role_by_position, {
            **base_evidence,
            "traced_leaf_chains": 0,
            "removed_features": 0,
            "removed_length_m": 0.0,
            "removed_roles": {},
        }

    selected = work.iloc[selected_positions].copy().reset_index(drop=True)
    terminals = {
        local_pos: _line_terminals(row.geometry)
        for local_pos, row in selected.iterrows()
    }
    spatial_index = selected.sindex
    connection_cache: dict[tuple[int, int], list[tuple[int, int | None]]] = {}

    def _at_frame_edge(point: Point) -> bool:
        return (point.x - xmin <= frame_margin_m
                or xmax - point.x <= frame_margin_m
                or point.y - ymin <= frame_margin_m
                or ymax - point.y <= frame_margin_m)

    def _connections(local_pos: int, terminal_index: int) \
            -> list[tuple[int, int | None]]:
        cache_key = (local_pos, terminal_index)
        if cache_key in connection_cache:
            return connection_cache[cache_key]
        feature_terminals = terminals.get(local_pos)
        if feature_terminals is None:
            connection_cache[cache_key] = []
            return []
        point = feature_terminals[terminal_index]
        matches = spatial_index.query(
            point.buffer(endpoint_snap_m), predicate="intersects")
        connections = []
        for other_local in sorted(int(value) for value in matches):
            if other_local == local_pos:
                continue
            other_geometry = selected.iloc[other_local].geometry
            if point.distance(other_geometry) > endpoint_snap_m:
                continue
            other_terminal_index = None
            other_terminals = terminals.get(other_local)
            if other_terminals is not None:
                distances = [point.distance(candidate)
                             for candidate in other_terminals]
                closest = min(range(2), key=lambda index: distances[index])
                if distances[closest] <= endpoint_snap_m:
                    other_terminal_index = int(closest)
            connections.append((other_local, other_terminal_index))
        connection_cache[cache_key] = connections
        return connections

    def _protected(local_pos: int) -> bool:
        row = selected.iloc[local_pos]
        return (_truthy_osm_tag(row.get("bridge"))
                or _truthy_osm_tag(row.get("tunnel")))

    def _trace_leaf(start_local: int, free_terminal: int) \
            -> tuple[list[int], float] | None:
        chain: list[int] = []
        chain_length_m = 0.0
        current = start_local
        entry_terminal = free_terminal
        seen: set[int] = set()
        while True:
            if current in seen or terminals.get(current) is None:
                return None
            if _protected(current):
                return None
            seen.add(current)
            chain.append(current)
            geometry = selected.iloc[current].geometry
            chain_length_m += float(geometry.length)
            if chain_length_m > max_chain_length_m:
                return None

            exit_terminal = 1 - entry_terminal
            exit_point = terminals[current][exit_terminal]
            if _at_frame_edge(exit_point):
                return None
            connections = _connections(current, exit_terminal)
            unique_others = sorted({other for other, _ in connections})
            if not unique_others:
                return chain, chain_length_m
            if len(unique_others) > 1:
                return chain, chain_length_m

            other = unique_others[0]
            other_terminal_values = {
                terminal for candidate, terminal in connections
                if candidate == other
            }
            if None in other_terminal_values or not other_terminal_values:
                # A free branch meeting the interior of another selected OSM
                # feature is the canonical line-thread failure.
                return chain, chain_length_m
            other_terminal = min(int(value)
                                 for value in other_terminal_values)
            if other in seen:
                return None

            current_direction = _terminal_direction(
                geometry, exit_terminal)
            other_geometry = selected.iloc[other].geometry
            other_direction = _terminal_direction(
                other_geometry, other_terminal)
            if current_direction is None or other_direction is None:
                return chain, chain_length_m
            alignment = abs(
                current_direction[0] * other_direction[0]
                + current_direction[1] * other_direction[1])
            if alignment < _TEMPLATE_CONTINUATION_COSINE:
                return chain, chain_length_m
            current = other
            entry_terminal = other_terminal

    removed_local: set[int] = set()
    traced_chains: set[tuple[int, ...]] = set()
    for local_pos, feature_terminals in terminals.items():
        if feature_terminals is None:
            continue
        for terminal_index, point in enumerate(feature_terminals):
            if _connections(local_pos, terminal_index) or _at_frame_edge(point):
                continue
            traced = _trace_leaf(local_pos, terminal_index)
            if traced is None:
                continue
            chain, _ = traced
            key = tuple(sorted(chain))
            if key in traced_chains:
                continue
            traced_chains.add(key)
            removed_local.update(chain)

    removed_positions = {
        selected_positions[local] for local in removed_local
    }
    kept_roles = {
        position: role for position, role in role_by_position.items()
        if position not in removed_positions
    }
    removed_roles: dict[str, int] = {}
    for position in removed_positions:
        role = role_by_position[position]
        removed_roles[role] = removed_roles.get(role, 0) + 1
    return kept_roles, {
        **base_evidence,
        "traced_leaf_chains": len(traced_chains),
        "removed_features": len(removed_positions),
        "removed_length_m": round(sum(
            float(work.iloc[position].geometry.length)
            for position in removed_positions), 3),
        "removed_roles": removed_roles,
    }


def _apply_amap_spatial_template(
    visible: gpd.GeoDataFrame,
    *,
    bbox_local,
    scale_mm_per_m: float,
    road_width_multiplier: float,
    min_colored_strip_mm: float,
    nozzle_real_m: float,
    visual_salience_guide,
) -> tuple[gpd.GeoDataFrame, dict[str, Any]]:
    """Map AMap's spatial hierarchy onto existing OSM linework.

    The previous guide only changed the rank of whole OSM name groups after
    class and ink budgets had already shaped the result.  It therefore could
    not reproduce the reference map's continuous skeleton.  This selector
    instead evaluates every eligible OSM feature against the aligned major,
    arterial and context masks.  The masks assign a role; coordinates always
    remain the original OSM coordinates.

    No city-independent target ink percentage is used here.  A sparse or dense
    city inherits the amount of hierarchy present in its reference template.
    Physical widths still pass through the printer floor below.
    """

    xmin, ymin, xmax, ymax = (float(value) for value in bbox_local)
    frame_area = max((xmax - xmin) * (ymax - ymin), 1.0)
    frame_span = max(xmax - xmin, ymax - ymin, 1.0)
    work = visible.copy().reset_index(drop=True)
    if work.empty:
        return work, {
            "applied": False,
            "reason": "no_template_candidates",
        }

    minimum_length_m = max(nozzle_real_m * 1.25, frame_span * 0.0025)
    role_by_position: dict[int, str] = {}
    provisional_roles: dict[int, str] = {}
    support_by_position: dict[int, dict[str, float]] = {}
    identity_stats: dict[str, dict[str, float]] = {}

    def _template_identity(row) -> str:
        ring = (ring_corridor_identity(row.get("name"))
                or ring_corridor_identity(row.get("name:en")))
        if ring:
            return ring
        for column in ("name", "ref"):
            value = row.get(column)
            if isinstance(value, str) and value.strip():
                return f"{column}:{value.strip().casefold()}"
        return ""

    for pos, row in work.iterrows():
        geometry = row.geometry
        if geometry is None or geometry.is_empty:
            continue
        highway = str(row.get("highway") or "")
        support = visual_salience_guide.road_support(geometry)
        support_by_position[pos] = support
        covered = float(support.get(
            "any_template_fraction", support.get("covered_fraction", 0.0)))
        weighted = float(support.get("weighted_salience", 0.0))
        major = max(
            float(support.get("major_mask_fraction", 0.0)),
            float(support.get("major_fraction", 0.0)),
        )
        arterial = float(support.get(
            "arterial_or_major_fraction", 0.0))
        context = float(support.get("context_mask_fraction", covered))
        identity = _template_identity(row)
        if identity:
            stats = identity_stats.setdefault(identity, {
                "length_m": 0.0,
                "covered_length_m": 0.0,
                "supported_length_m": 0.0,
            })
            length_m = float(geometry.length)
            stats["length_m"] += length_m
            stats["covered_length_m"] += length_m * covered

        # At 15/25 km interchange ramps become dark knots even when the
        # template identifies them correctly, so they remain
        # topology/block-base structure only.
        is_link = highway.endswith("_link")
        if is_link:
            continue
        if ((major >= _TEMPLATE_PRIMARY_MAJOR_FRACTION
             and covered >= 0.34)
                or (weighted >= _TEMPLATE_PRIMARY_WEIGHT
                    and covered >= 0.44)):
            role = "primary"
        elif ((arterial >= _TEMPLATE_SECONDARY_ARTERIAL_FRACTION
               and covered >= 0.40)
              or (weighted >= _TEMPLATE_SECONDARY_WEIGHT
                  and covered >= 0.48)):
            role = "secondary"
        elif (not is_link
              and float(geometry.length) >= minimum_length_m
              and context >= _TEMPLATE_CONTEXT_FRACTION
              and covered >= 0.56
              and weighted >= _TEMPLATE_CONTEXT_WEIGHT):
            role = "context"
        else:
            continue
        provisional_roles[pos] = role
        if identity:
            identity_stats[identity]["supported_length_m"] += float(
                geometry.length)

    # A short OSM way lying perpendicular to a thick reference road can have
    # 100% pixel overlap even though it is only an intersection spur.  Keep a
    # short fragment only when its full named/ref corridor has sustained
    # template coverage; long fragments are strong enough on their own.
    for pos, role in provisional_roles.items():
        row = work.iloc[pos]
        geometry = row.geometry
        length_m = float(geometry.length)
        if length_m >= minimum_length_m:
            role_by_position[pos] = role
            continue
        identity = _template_identity(row)
        stats = identity_stats.get(identity, {}) if identity else {}
        total_length = max(float(stats.get("length_m", 0.0)), 1.0)
        corridor_coverage = float(
            stats.get("covered_length_m", 0.0)) / total_length
        supported_length = float(stats.get("supported_length_m", 0.0))
        support = support_by_position.get(pos, {})
        fragment_coverage = float(support.get(
            "any_template_fraction", support.get("covered_fraction", 0.0)))
        if (identity
                and corridor_coverage >= 0.34
                and supported_length >= minimum_length_m * 4.0
                and fragment_coverage >= 0.52):
            role_by_position[pos] = role

    # Ordinary OSM segmentation gaps are handled by the sustained-corridor
    # pass above.  Interchange ramps deliberately stay structural-only at this
    # scale, so there is no generic connector restoration pass.
    connector_positions: set[int] = set()

    role_by_position, dangling_chain_evidence = (
        _prune_template_dangling_chains(
            work,
            role_by_position,
            bbox_local=bbox_local,
        )
    )

    selected_positions = set(role_by_position)
    selected = work.iloc[sorted(selected_positions)].copy()
    selected[COMPOSITION_ROLE_COLUMN] = [
        role_by_position[pos] for pos in sorted(selected_positions)
    ]

    def _row_identity(pos: int) -> str | None:
        row = work.iloc[pos]
        highway = str(row.get("highway") or "unknown")
        ring = (ring_corridor_identity(row.get("name"))
                or ring_corridor_identity(row.get("name:en")))
        if ring:
            return f"{highway}:{ring}"
        for column in ("name", "ref"):
            value = row.get(column)
            if isinstance(value, str) and value.strip():
                return f"{highway}:{column}:{value.strip().casefold()}"
        return None

    def _role_evidence(role: str) -> dict[str, Any]:
        positions = {pos for pos, assigned in role_by_position.items()
                     if assigned == role}
        weighted_values = [
            float(support_by_position.get(pos, {}).get(
                "weighted_salience", 0.0)) for pos in positions
        ]
        return {
            "role": role,
            "features": len(positions),
            "identities": sorted({identity for pos in positions
                                  if (identity := _row_identity(pos))}),
            "anonymous_features": sum(
                _row_identity(pos) is None for pos in positions),
            "mean_template_salience": round(
                sum(weighted_values) / len(weighted_values), 6)
                if weighted_values else 0.0,
            "estimated_ink_ratio": round(sum(
                float(work.iloc[pos].geometry.length)
                * resolve_composed_road_width_m(
                    str(work.iloc[pos].get("highway") or ""),
                    composition_role=role,
                    scale_mm_per_m=scale_mm_per_m,
                    road_width_multiplier=road_width_multiplier,
                    min_colored_strip_mm=min_colored_strip_mm,
                ) / frame_area
                for pos in positions
            ), 6),
        }

    selected_ink = sum(
        float(work.iloc[pos].geometry.length)
        * resolve_composed_road_width_m(
            str(work.iloc[pos].get("highway") or ""),
            composition_role=role_by_position[pos],
            scale_mm_per_m=scale_mm_per_m,
            road_width_multiplier=road_width_multiplier,
            min_colored_strip_mm=min_colored_strip_mm,
        ) / frame_area
        for pos in selected_positions
    )
    reference_evidence = getattr(
        getattr(visual_salience_guide, "reference", None), "evidence", {}) or {}
    composition_roles = {
        "schema_version": "1.0",
        "method": "amap_spatial_template_to_osm_roles_v3",
        "primary": _role_evidence("primary"),
        "secondary": _role_evidence("secondary"),
        "context": _role_evidence("context"),
        "connector": _role_evidence("connector"),
        "background": {
            "role": "block_base_only",
            "features": max(0, len(work) - len(selected_positions)),
            "reason": (
                "retained for topology and structural seams; absent from "
                "the reference skeleton or below matching confidence"),
        },
    }
    selected_identities = sorted({
        identity for pos in selected_positions
        if (identity := _row_identity(pos))
    })
    return selected, {
        "applied": True,
        "method": "amap_spatial_template_existing_osm_v3",
        "candidate_features": len(work),
        "selected_features": len(selected),
        "selected_estimated_ink_ratio": round(selected_ink, 6),
        "connector_features": len(connector_positions),
        "reference_mask_ratios": {
            key: reference_evidence.get(key)
            for key in (
                "road_major_ratio", "road_arterial_ratio",
                "road_context_ratio")
        },
        "thresholds": {
            "primary_major_fraction": _TEMPLATE_PRIMARY_MAJOR_FRACTION,
            "primary_weight": _TEMPLATE_PRIMARY_WEIGHT,
            "secondary_arterial_fraction": (
                _TEMPLATE_SECONDARY_ARTERIAL_FRACTION),
            "secondary_weight": _TEMPLATE_SECONDARY_WEIGHT,
            "context_fraction": _TEMPLATE_CONTEXT_FRACTION,
            "context_weight": _TEMPLATE_CONTEXT_WEIGHT,
            "minimum_length_m": round(minimum_length_m, 3),
            "short_corridor_coverage": 0.34,
            "short_supported_length_factor": 4.0,
            "short_fragment_coverage": 0.52,
        },
        "dangling_chain_pruning": dangling_chain_evidence,
        "visual_salience": {
            "enabled": True,
            "mode": "spatial_template_match_existing_osm_segments",
            "reference_version": getattr(
                visual_salience_guide, "version", None),
            "template_policy_version": getattr(
                visual_salience_guide, "template_policy_version", None),
            "selected_features": len(selected),
            "selected_identities": selected_identities,
        },
        "composition_roles": composition_roles,
    }


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

    def _semantic_identity_for_key(key: tuple[str, str]) -> str:
        positions = groups[key]
        ring_identities = sorted({
            ring_corridor_identity(work.iloc[pos].get("name"))
            or ring_corridor_identity(work.iloc[pos].get("name:en"))
            for pos in positions
        } - {""})
        if ring_identities:
            return ring_identities[0]
        return "" if key[1].startswith("@") else key[1]

    # A class budget decides what is visible, not what is a hero.  Collapse
    # the already selected OSM groups into semantic identities and choose a
    # small adaptive set of main corridors.  sqrt(N), bounded to 3..8 for a
    # non-trivial city, avoids both a fixed global ink ratio and an unbounded
    # list in dense cities.
    selected_group_keys = [
        key for key in groups
        if any(pos in selected_positions - connector_positions
               for pos in groups[key])
    ]
    identity_candidates: dict[str, dict[str, Any]] = {}
    for key in selected_group_keys:
        identity = _semantic_identity_for_key(key)
        if not identity:
            continue
        entry = identity_candidates.setdefault(identity, {
            "keys": [], "positions": set(), "cells": set(),
            "score": 0.0, "visual_salience": 0.0,
        })
        entry["keys"].append(key)
        entry["positions"].update(
            pos for pos in groups[key] if pos in selected_positions)
        entry["cells"].update(group_cells.get(key, set()))
        entry["score"] += float(scores.get(key, 0.0))
        support = visual_group_support.get(key, {})
        entry["visual_salience"] = max(
            float(entry["visual_salience"]),
            float(support.get("weighted_salience", 0.0)),
        )

    for identity, entry in identity_candidates.items():
        union = unary_union([
            work.iloc[pos].geometry for pos in sorted(entry["positions"])
        ])
        ixmin, iymin, ixmax, iymax = union.bounds
        entry["length_m"] = sum(
            float(work.iloc[pos].geometry.length)
            for pos in entry["positions"])
        entry["span"] = max(
            (ixmax - ixmin) / frame_width,
            (iymax - iymin) / frame_height,
        )
        entry["rank_score"] = (
            2.5 * float(entry["visual_salience"])
            + 2.0 * min(1.0, float(entry["span"]) / 0.50)
            + 1.0 * min(1.0, len(entry["cells"]) / 6.0)
            + 0.5 * min(1.0, float(entry["length_m"])
                        / max(frame_width, frame_height))
        )

    protected_identity = protected_ring_evidence.get("identity")
    eligible_identities = [
        identity for identity, entry in identity_candidates.items()
        if (identity == protected_identity
            or float(entry["span"]) >= 0.16
            or len(entry["cells"]) >= 4)
    ]
    candidate_count = len(identity_candidates)
    eligible_count = len(eligible_identities)
    if eligible_count <= 2:
        primary_target = eligible_count
    else:
        primary_target = min(8, max(3, int(math.ceil(
            math.sqrt(eligible_count)))))
    ranked_identities = sorted(
        eligible_identities,
        key=lambda identity: (
            -(identity == protected_identity),
            -float(identity_candidates[identity]["rank_score"]),
            -float(identity_candidates[identity]["visual_salience"]),
            -float(identity_candidates[identity]["span"]),
            identity,
        ),
    )
    primary_identities = set(ranked_identities[:primary_target])
    if protected_identity in identity_candidates:
        primary_identities.add(protected_identity)
        if len(primary_identities) > max(primary_target, 1):
            removable = [identity for identity in reversed(ranked_identities)
                         if identity != protected_identity
                         and identity in primary_identities]
            if removable:
                primary_identities.remove(removable[0])

    primary_positions = set()
    for identity in primary_identities:
        primary_positions.update(identity_candidates[identity]["positions"])
    primary_positions -= connector_positions
    secondary_positions = (
        selected_positions - primary_positions - connector_positions)
    role_by_position = {
        pos: ("connector" if pos in connector_positions
              else "secondary" if pos in secondary_positions
              else "primary")
        for pos in selected_positions
    }
    selected = work.iloc[sorted(selected_positions)].copy()
    selected[COMPOSITION_ROLE_COLUMN] = [
        role_by_position[pos] for pos in sorted(selected_positions)
    ]
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

    def _identity_for_position(pos: int) -> str | None:
        row = work.iloc[pos]
        highway = str(row.get("highway") or "unknown")
        ring = (ring_corridor_identity(row.get("name"))
                or ring_corridor_identity(row.get("name:en")))
        if ring:
            identity = ring
        elif isinstance(row.get("name"), str) and row.get("name").strip():
            identity = f"name:{row.get('name').strip().casefold()}"
        elif isinstance(row.get("ref"), str) and row.get("ref").strip():
            identity = f"ref:{row.get('ref').strip().casefold()}"
        else:
            return None
        return f"{highway}:{identity}"

    def _role_evidence(role: str, positions: set[int]) -> dict[str, Any]:
        return {
            "role": role,
            "features": len(positions),
            "identities": sorted({identity for pos in positions
                                  if (identity := _identity_for_position(pos))}),
            "anonymous_features": sum(
                _identity_for_position(pos) is None for pos in positions),
            "estimated_ink_ratio": round(sum(
                float(work.iloc[pos].geometry.length)
                * resolve_composed_road_width_m(
                    str(work.iloc[pos].get("highway") or ""),
                    composition_role=role,
                    scale_mm_per_m=scale_mm_per_m,
                    road_width_multiplier=road_width_multiplier,
                    min_colored_strip_mm=min_colored_strip_mm,
                ) / frame_area
                for pos in positions
            ), 6),
        }

    rejected_features = max(0, len(visible) - len(selected_positions))
    composition_roles = {
        "schema_version": "1.0",
        "method": "adaptive_semantic_identity_hierarchy_v2",
        "candidate_identities": candidate_count,
        "eligible_primary_identities": eligible_count,
        "primary_identity_target": primary_target,
        "primary_semantic_identities": sorted(primary_identities),
        "primary_identity_ranking": [
            {
                "identity": identity,
                "score": round(float(
                    identity_candidates[identity]["rank_score"]), 6),
                "visual_salience": round(float(
                    identity_candidates[identity]["visual_salience"]), 6),
                "span": round(float(
                    identity_candidates[identity]["span"]), 6),
                "grid_cells": len(identity_candidates[identity]["cells"]),
                "length_m": round(float(
                    identity_candidates[identity]["length_m"]), 3),
            }
            for identity in ranked_identities[:max(primary_target, 1)]
        ],
        "primary": _role_evidence("primary", primary_positions),
        "secondary": _role_evidence("secondary", secondary_positions),
        "connector": _role_evidence("connector", connector_positions),
        "background": {
            "role": "block_base_only",
            "features": rejected_features,
            "reason": "retained for topology/structural seams, hidden as ink",
        },
        "rejected_fragments": {
            "features": rejected_features,
            "reason": "failed identity, spatial contribution, or ink budget",
        },
    }
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
        "composition_roles": composition_roles,
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
    if visual_salience_guide is not None and nozzle_real_m >= 25.0:
        # AMap can identify a locally important tertiary road that a global
        # OSM class filter would discard before spatial matching.  Residential
        # and service streets remain structural-only at this print scale.
        visible_highways = set(ROAD_TIERS[2])
    visible_candidates = _highway_subset(lines, visible_highways)
    visible = visible_candidates
    ink_budget = {"applied": False, "reason": "small_or_medium_print_footprint"}
    if (nozzle_real_m >= 25.0 and bbox_local is not None
            and scale_mm_per_m is not None):
        if visual_salience_guide is not None:
            visible, ink_budget = _apply_amap_spatial_template(
                visible_candidates,
                bbox_local=bbox_local,
                scale_mm_per_m=scale_mm_per_m,
                road_width_multiplier=road_width_multiplier,
                min_colored_strip_mm=min_colored_strip_mm,
                nozzle_real_m=nozzle_real_m,
                visual_salience_guide=visual_salience_guide,
            )
        else:
            visible, ink_budget = _apply_large_area_ink_budget(
                visible_candidates,
                bbox_local=bbox_local,
                scale_mm_per_m=scale_mm_per_m,
                road_width_multiplier=road_width_multiplier,
                min_colored_strip_mm=min_colored_strip_mm,
                nozzle_real_m=nozzle_real_m,
                visual_salience_guide=None,
            )
    elif len(visible) > 0:
        visible = visible.copy()
        visible[COMPOSITION_ROLE_COLUMN] = "foreground"

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
        "composition_roles": ink_budget.get("composition_roles", {
            "schema_version": "1.0",
            "method": "unbudgeted_foreground_v1",
            "foreground": {
                "role": "foreground",
                "features": len(visible),
                "identities": [],
            },
            "background": {
                "role": "block_base_only",
                "features": max(0, len(structural) - len(visible)),
            },
        }),
        "fallback": fallback,
    }
    return RoadRoleSelection(
        topology=topology,
        structural=structural,
        visible=visible,
        evidence=evidence,
    )
