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
import heapq
import math
import re
from typing import Any

import geopandas as gpd
from shapely.geometry import Point
from shapely.ops import unary_union

from .buildings import ROAD_TIERS
from .config import ROAD_DEFAULT_WIDTH_M, ROAD_FILTER, ROAD_WIDTHS


POLICY_VERSION = "print-road-roles-v12.9"
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
_TEMPLATE_PRIMARY_DANGLING_CHAIN_FRAME_FRACTION = 0.012
_TEMPLATE_SECONDARY_DANGLING_CHAIN_FRAME_FRACTION = 0.018
_TEMPLATE_PRIMARY_DANGLING_CHAIN_TARGET_M = 300.0
_TEMPLATE_SECONDARY_DANGLING_CHAIN_TARGET_M = 450.0
_TEMPLATE_CONTINUATION_COSINE = 0.70
_TEMPLATE_PRIMARY_GAP_FRAME_FRACTION = 0.040
_TEMPLATE_SECONDARY_GAP_FRAME_FRACTION = 0.025
_TEMPLATE_CONTEXT_GAP_FRAME_FRACTION = 0.015
_TEMPLATE_MAX_GAP_DETOUR_RATIO = 2.0
_TEMPLATE_MIN_MASK_GAP_SUPPORT = 0.42
_TEMPLATE_SEMANTIC_PRIMARY_GAP_FRAME_FRACTION = 0.080
_TEMPLATE_SEMANTIC_SECONDARY_GAP_FRAME_FRACTION = 0.070
_TEMPLATE_SEMANTIC_CONTEXT_GAP_FRAME_FRACTION = 0.050
_TEMPLATE_MAX_LINK_GAP_M = 250.0
_TEMPLATE_MAX_LINK_GAP_DETOUR_RATIO = 1.35
_TEMPLATE_MAX_LINK_EDGES_PER_GAP = 1
_TEMPLATE_INFERRED_LINK_CONTINUATION_COSINE = 0.90
_TEMPLATE_INFERRED_LINK_MIN_COVERAGE = 0.25
_TEMPLATE_INFERRED_LINK_MIN_WEIGHT = 0.10
_TEMPLATE_MAX_CORRIDOR_PATHS_PER_ANCHOR = 3
_TEMPLATE_MAX_MASK_PATHS_PER_ANCHOR = 1
_TEMPLATE_CORRIDOR_SNAP_FRAME_FRACTION = 0.0015
_TEMPLATE_CORRIDOR_SNAP_MIN_M = 8.0
_TEMPLATE_CORRIDOR_SNAP_MAX_M = 60.0
_TEMPLATE_PROXIMITY_CONTINUATION_COSINE = 0.85
_TEMPLATE_CORRIDOR_JOIN_COSINE = 0.88
_TEMPLATE_CORRIDOR_JOIN_MIN_M = 6.0
_TEMPLATE_CORRIDOR_JOIN_MAX_M = 45.0
_TEMPLATE_PARALLEL_COLLAPSE_FACTOR = 1.10
_TEMPLATE_PARALLEL_OVERLAP_FRACTION = 0.70
_TEMPLATE_CORRIDOR_MIN_FRAME_FRACTIONS = {
    "primary": 0.015,
    "secondary": 0.022,
    "context": 0.032,
}
_TEMPLATE_CORRIDOR_MIN_NOZZLE_FACTORS = {
    "primary": 4.0,
    "secondary": 6.0,
    "context": 8.0,
}
_TEMPLATE_CORRIDOR_MIN_COVERAGE = {
    "primary": 0.18,
    "secondary": 0.26,
    "context": 0.46,
}
_TEMPLATE_CORRIDOR_MIN_SEED_FRACTION = 0.10
_TEMPLATE_SKELETON_LEAF_MIN_FRAME_FRACTIONS = {
    "primary": 0.045,
    "secondary": 0.060,
    "context": 0.080,
}
_TEMPLATE_SKELETON_ISOLATED_FACTOR = 1.20
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


def _road_class_rank(value: Any) -> int | None:
    """Return a coarse through-road rank, treating links as their parent."""

    highway = str(value or "")
    if highway.endswith("_link"):
        highway = highway[:-5]
    return {
        "motorway": 0,
        "trunk": 1,
        "primary": 2,
        "secondary": 3,
        "tertiary": 4,
    }.get(highway)


def _road_classes_compatible(left: Any, right: Any) -> bool:
    """Allow one OSM class step across a physical road corridor."""

    left_rank = _road_class_rank(left)
    right_rank = _road_class_rank(right)
    return (left_rank is not None and right_rank is not None
            and abs(left_rank - right_rank) <= 1)


def _directions_from_line_at_point(
    geometry,
    point: Point,
    *,
    endpoint_tolerance_m: float,
) -> list[tuple[float, float]]:
    """Return unit vectors pointing away from a point along a source line."""

    if geometry is None or geometry.is_empty \
            or geometry.geom_type != "LineString":
        return []
    terminals = _line_terminals(geometry)
    if terminals is None:
        return []
    terminal_distances = [point.distance(candidate) for candidate in terminals]
    closest_terminal = min(range(2), key=lambda index: terminal_distances[index])
    if terminal_distances[closest_terminal] <= endpoint_tolerance_m:
        direction = _terminal_direction(geometry, closest_terminal)
        return [direction] if direction is not None else []

    length_m = float(geometry.length)
    if length_m <= 1e-9 or point.distance(geometry) > endpoint_tolerance_m:
        return []
    projected = float(geometry.project(point))
    sample_m = max(
        endpoint_tolerance_m * 4.0,
        min(25.0, length_m * 0.08),
    )
    origin = geometry.interpolate(projected)
    directions = []
    for distance_m in (
        max(0.0, projected - sample_m),
        min(length_m, projected + sample_m),
    ):
        if abs(distance_m - projected) <= 1e-9:
            continue
        sample = geometry.interpolate(distance_m)
        dx = float(sample.x) - float(origin.x)
        dy = float(sample.y) - float(origin.y)
        magnitude = math.hypot(dx, dy)
        if magnitude > 1e-9:
            directions.append((dx / magnitude, dy / magnitude))
    return directions


def _truthy_osm_tag(value: Any) -> bool:
    if value is None:
        return False
    return str(value).strip().casefold() not in {
        "", "0", "false", "nan", "no", "none",
    }


def _template_identity_for_row(row) -> str:
    ring = (ring_corridor_identity(row.get("name"))
            or ring_corridor_identity(row.get("name:en")))
    if ring:
        return ring
    for column in ("name", "ref"):
        value = row.get(column)
        if isinstance(value, str) and value.strip():
            return f"{column}:{value.strip().casefold()}"
    return ""


def _build_physical_corridor_groups(
    work: gpd.GeoDataFrame,
    support_by_position: dict[int, dict[str, float]],
    *,
    endpoint_snap_m: float,
    corridor_join_m: float,
) -> tuple[list[list[int]], dict[str, Any]]:
    """Partition OSM source ways into complete physical road corridors.

    Semantic OSM identities are useful but not authoritative: a physical road
    can change name/class at a bridge, interchange or administrative boundary.
    Build the source topology *before* applying the raster template.  Ways of
    one identity are joined first; at a real shared endpoint, remaining
    half-edges are paired by their natural through direction and compatible
    OSM class.  A T-junction therefore keeps its straight road together and
    leaves the side branch as its own complete corridor.

    The function only unions existing source features.  It neither creates
    coordinates nor bridges a geometric gap between different identities.
    """

    parents = list(range(len(work)))

    def find(position: int) -> int:
        while parents[position] != position:
            parents[position] = parents[parents[position]]
            position = parents[position]
        return position

    def union(left: int, right: int) -> bool:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return False
        parents[right_root] = left_root
        return True

    identities: dict[str, list[int]] = {}
    identity_by_position: dict[int, str] = {}
    for position, row in work.iterrows():
        identity = _template_identity_for_row(row)
        identity_by_position[int(position)] = identity
        if identity:
            identities.setdefault(identity, []).append(int(position))

    semantic_pairs = 0
    aligned_semantic_pairs = 0
    claimed_semantic_terminals: set[tuple[int, int]] = set()
    bucket_size = max(corridor_join_m, endpoint_snap_m)
    for positions in identities.values():
        endpoint_buckets: dict[tuple[int, int], list[tuple[
            int, int, Point, tuple[float, float] | None,
        ]]] = {}
        pair_candidates = []
        for position in positions:
            row = work.iloc[position]
            terminals = _line_terminals(row.geometry)
            if terminals is None:
                continue
            for terminal_index, point in enumerate(terminals):
                direction = _terminal_direction(row.geometry, terminal_index)
                bx = int(math.floor(point.x / bucket_size))
                by = int(math.floor(point.y / bucket_size))
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        for (other, other_terminal, other_point,
                             other_direction) in \
                                endpoint_buckets.get((bx + dx, by + dy), []):
                            if other == position:
                                continue
                            distance_m = point.distance(other_point)
                            exact_join = distance_m <= endpoint_snap_m
                            if direction is None or other_direction is None:
                                continue
                            continuation = -(
                                direction[0] * other_direction[0]
                                + direction[1] * other_direction[1]
                            )
                            aligned_join = bool(
                                not exact_join
                                and distance_m <= corridor_join_m
                                and _road_classes_compatible(
                                    row.get("highway"),
                                    work.iloc[other].get("highway"))
                                and continuation
                                >= _TEMPLATE_CORRIDOR_JOIN_COSINE
                            )
                            if exact_join or aligned_join:
                                left_key = (other, other_terminal)
                                right_key = (position, terminal_index)
                                score = (
                                    (2.0 if exact_join else 0.0)
                                    + continuation * 3.0
                                    - distance_m / max(corridor_join_m, 1.0)
                                    - (2.0 if (
                                        str(row.get("highway") or "")
                                        .endswith("_link")
                                        or str(work.iloc[other].get(
                                            "highway") or "")
                                        .endswith("_link")) else 0.0)
                                )
                                pair_candidates.append((
                                    score, continuation, exact_join,
                                    left_key, right_key,
                                ))
                endpoint_buckets.setdefault((bx, by), []).append(
                    (position, terminal_index, point, direction))

        # A road identity may branch at an interchange.  Pair half-edges,
        # never whole connected components: each source endpoint is allowed
        # one natural continuation, so the result remains a path or ring.
        pair_candidates.sort(
            key=lambda item: (-item[0], -item[1], item[3], item[4]))
        used_terminals: set[tuple[int, int]] = set()
        for _, _, exact_join, left_key, right_key in pair_candidates:
            if left_key in used_terminals or right_key in used_terminals:
                continue
            used_terminals.add(left_key)
            used_terminals.add(right_key)
            claimed_semantic_terminals.add(left_key)
            claimed_semantic_terminals.add(right_key)
            union(left_key[0], right_key[0])
            semantic_pairs += 1
            aligned_semantic_pairs += int(not exact_join)

    # Cluster terminals that are factually coincident in OSM.  Cross-name
    # pairing is deliberately limited to these shared endpoints; the aligned
    # gap allowance above is reserved for one explicit semantic identity.
    terminal_directions: dict[
        tuple[int, int], tuple[float, float] | None] = {}
    terminal_neighbors: dict[
        tuple[int, int], set[tuple[int, int]]] = {}
    endpoint_bucket_size = max(endpoint_snap_m, 0.5)
    endpoint_buckets: dict[
        tuple[int, int], list[tuple[int, int, Point]]] = {}
    for position, row in work.iterrows():
        terminals = _line_terminals(row.geometry)
        if terminals is None:
            continue
        for terminal_index, point in enumerate(terminals):
            key = (int(position), terminal_index)
            terminal_directions[key] = _terminal_direction(
                row.geometry, terminal_index)
            terminal_neighbors.setdefault(key, set())
            bx = int(math.floor(point.x / endpoint_bucket_size))
            by = int(math.floor(point.y / endpoint_bucket_size))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for other_position, other_terminal, other_point in \
                            endpoint_buckets.get((bx + dx, by + dy), []):
                        if point.distance(other_point) > endpoint_snap_m:
                            continue
                        other_key = (other_position, other_terminal)
                        terminal_neighbors[key].add(other_key)
                        terminal_neighbors.setdefault(other_key, set()).add(
                            key)
            endpoint_buckets.setdefault((bx, by), []).append(
                (int(position), terminal_index, point))

    node_components: list[list[tuple[int, int]]] = []
    unseen = set(terminal_neighbors)
    while unseen:
        start = min(unseen)
        stack = [start]
        unseen.remove(start)
        node = []
        while stack:
            current = stack.pop()
            node.append(current)
            for neighbor in terminal_neighbors.get(current, ()):
                if neighbor in unseen:
                    unseen.remove(neighbor)
                    stack.append(neighbor)
        if len(node) > 1:
            node_components.append(sorted(node))

    physical_pairs = 0
    cross_identity_pairs = 0
    ambiguous_nodes = 0
    for node in node_components:
        pair_candidates = []
        for offset, left_key in enumerate(node):
            if left_key in claimed_semantic_terminals:
                continue
            left_position, _ = left_key
            left_direction = terminal_directions.get(left_key)
            if left_direction is None:
                continue
            for right_key in node[offset + 1:]:
                if right_key in claimed_semantic_terminals:
                    continue
                right_position, _ = right_key
                if left_position == right_position:
                    continue
                if find(left_position) == find(right_position):
                    continue
                right_direction = terminal_directions.get(right_key)
                if right_direction is None:
                    continue
                if not _road_classes_compatible(
                        work.iloc[left_position].get("highway"),
                        work.iloc[right_position].get("highway")):
                    continue
                continuation = -(
                    left_direction[0] * right_direction[0]
                    + left_direction[1] * right_direction[1]
                )
                # A degree-two source junction can be a genuine bend.  At a
                # multi-way junction demand a strong through direction so a
                # perpendicular side road never joins the skeleton corridor.
                minimum_continuation = 0.20 if len(node) == 2 else 0.70
                if continuation < minimum_continuation:
                    continue
                left_support = float(support_by_position.get(
                    left_position, {}).get("weighted_salience", 0.0))
                right_support = float(support_by_position.get(
                    right_position, {}).get("weighted_salience", 0.0))
                left_rank = _road_class_rank(
                    work.iloc[left_position].get("highway"))
                right_rank = _road_class_rank(
                    work.iloc[right_position].get("highway"))
                class_bonus = 0.0
                if left_rank is not None and right_rank is not None:
                    class_bonus = 0.30 if left_rank == right_rank else 0.10
                score = (
                    continuation * 4.0
                    + min(left_support, right_support) * 0.35
                    + class_bonus
                )
                pair_candidates.append((
                    score, continuation, left_key, right_key,
                ))
        pair_candidates.sort(
            key=lambda item: (-item[0], -item[1], item[2], item[3]))
        used_half_edges: set[tuple[int, int]] = set()
        accepted_at_node = 0
        for _, _, left_key, right_key in pair_candidates:
            if left_key in used_half_edges or right_key in used_half_edges:
                continue
            left_position = left_key[0]
            right_position = right_key[0]
            if find(left_position) == find(right_position):
                continue
            left_identity = identity_by_position.get(left_position, "")
            right_identity = identity_by_position.get(right_position, "")
            if union(left_position, right_position):
                physical_pairs += 1
                cross_identity_pairs += int(
                    left_identity != right_identity)
                accepted_at_node += 1
                used_half_edges.add(left_key)
                used_half_edges.add(right_key)
        if len(pair_candidates) > accepted_at_node and len(node) > 2:
            ambiguous_nodes += 1

    components: dict[int, list[int]] = {}
    for position in range(len(work)):
        components.setdefault(find(position), []).append(position)
    groups = [sorted(positions) for positions in components.values()]
    groups.sort(key=lambda positions: positions[0])
    return groups, {
        "method": "global_existing_osm_physical_corridors_v1",
        "semantic_identities": len(identities),
        "semantic_join_pairs": semantic_pairs,
        "aligned_semantic_join_pairs": aligned_semantic_pairs,
        "topology_nodes": len(node_components),
        "physical_continuation_pairs": physical_pairs,
        "cross_identity_continuation_pairs": cross_identity_pairs,
        "ambiguous_topology_nodes": ambiguous_nodes,
        "physical_corridors": len(groups),
        "geometry_policy": "union_existing_osm_features_only",
    }


def _match_template_corridors(
    work: gpd.GeoDataFrame,
    provisional_roles: dict[int, str],
    support_by_position: dict[int, dict[str, float]],
    *,
    bbox_local,
    nozzle_real_m: float,
) -> tuple[dict[int, str], dict[str, Any]]:
    """Promote complete OSM corridors from sparse template-supported seeds.

    A cartographic raster is good at saying *which corridor matters* but poor
    at selecting individual OSM ways: palette gaps and wide antialiasing masks
    respectively create broken roads and perpendicular thread-like fragments.
    This matcher therefore treats per-feature mask hits only as seeds.  Before
    any raster score is applied, existing OSM half-edges are paired into
    physical corridors by semantic identity, endpoint topology, natural
    continuation and compatible road class.  Each resulting path/ring is then
    scored and selected atomically.  No geometry is generated or modified.

    Parallel carriageway components closer than one printable strip are not
    independently useful at 15/25 km scale.  When their routes substantially
    overlap, only the better-supported source component remains visible; all
    source roads remain available to topology and Block base.
    """

    xmin, ymin, xmax, ymax = (float(value) for value in bbox_local)
    frame_span = max(xmax - xmin, ymax - ymin, 1.0)
    endpoint_snap_m = max(0.5, min(2.0, frame_span * 0.00004))
    corridor_join_m = min(
        _TEMPLATE_CORRIDOR_JOIN_MAX_M,
        max(_TEMPLATE_CORRIDOR_JOIN_MIN_M, nozzle_real_m * 0.75),
    )
    parallel_collapse_m = max(
        endpoint_snap_m * 2.0,
        nozzle_real_m * _TEMPLATE_PARALLEL_COLLAPSE_FACTOR,
    )
    role_rank = {"primary": 0, "secondary": 1, "context": 2}

    identities: dict[str, list[int]] = {}
    for position, row in work.iterrows():
        identity = _template_identity_for_row(row)
        if identity:
            identities.setdefault(identity, []).append(int(position))

    physical_groups, physical_grouping_evidence = \
        _build_physical_corridor_groups(
            work,
            support_by_position,
            endpoint_snap_m=endpoint_snap_m,
            corridor_join_m=corridor_join_m,
        )

    corridor_records: list[dict[str, Any]] = []
    component_count = 0
    joined_endpoint_pairs = 0

    for positions in physical_groups:
        group_identities = sorted({
            identity
            for position in positions
            if (identity := _template_identity_for_row(work.iloc[position]))
        })
        identity = (
            "physical:" + "|".join(group_identities)
            if group_identities else f"physical:anonymous:{positions[0]}"
        )
        parents = {position: position for position in positions}

        def find(position: int) -> int:
            while parents[position] != position:
                parents[position] = parents[parents[position]]
                position = parents[position]
            return position

        def union(left: int, right: int) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root != right_root:
                parents[right_root] = left_root

        # A small spatial bucket keeps the endpoint join near-linear even for
        # long named urban arterials split into hundreds of OSM ways.
        bucket_size = max(corridor_join_m, endpoint_snap_m)
        endpoint_buckets: dict[tuple[int, int], list[tuple[
            int, Point, tuple[float, float] | None,
        ]]] = {}
        for position in positions:
            row = work.iloc[position]
            terminals = _line_terminals(row.geometry)
            if terminals is None:
                continue
            for terminal_index, point in enumerate(terminals):
                direction = _terminal_direction(row.geometry, terminal_index)
                bx = int(math.floor(point.x / bucket_size))
                by = int(math.floor(point.y / bucket_size))
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        for other, other_point, other_direction in \
                                endpoint_buckets.get((bx + dx, by + dy), []):
                            if other == position:
                                continue
                            distance_m = point.distance(other_point)
                            exact_join = distance_m <= endpoint_snap_m
                            aligned_join = False
                            if (not exact_join
                                    and distance_m <= corridor_join_m
                                    and direction is not None
                                    and other_direction is not None
                                    and _road_classes_compatible(
                                        row.get("highway"),
                                        work.iloc[other].get("highway"))):
                                continuation = -(
                                    direction[0] * other_direction[0]
                                    + direction[1] * other_direction[1]
                                )
                                aligned_join = (
                                    continuation
                                    >= _TEMPLATE_CORRIDOR_JOIN_COSINE)
                            if exact_join or aligned_join:
                                before = find(position) != find(other)
                                union(position, other)
                                joined_endpoint_pairs += int(before)
                endpoint_buckets.setdefault((bx, by), []).append(
                    (position, point, direction))

        components: dict[int, list[int]] = {}
        for position in positions:
            components.setdefault(find(position), []).append(position)

        for component_positions in components.values():
            component_count += 1
            lengths = {
                position: float(work.iloc[position].geometry.length)
                for position in component_positions
            }
            total_length_m = sum(lengths.values())
            if total_length_m <= 0:
                continue
            seed_positions = [
                position for position in component_positions
                if position in provisional_roles
            ]
            if not seed_positions:
                continue
            seed_lengths = {role: 0.0 for role in role_rank}
            for position in seed_positions:
                seed_lengths[provisional_roles[position]] += lengths[position]
            seed_threshold_m = max(
                nozzle_real_m * 1.5,
                total_length_m * 0.08,
            )
            eligible_roles = [
                role for role in role_rank
                if seed_lengths[role] >= seed_threshold_m
            ]
            if not eligible_roles:
                continue
            role = min(eligible_roles, key=role_rank.get)
            covered_length_m = sum(
                lengths[position] * float(
                    support_by_position.get(position, {}).get(
                        "any_template_fraction",
                        support_by_position.get(position, {}).get(
                            "covered_fraction", 0.0),
                    ))
                for position in component_positions
            )
            weighted_length_m = sum(
                lengths[position] * float(
                    support_by_position.get(position, {}).get(
                        "weighted_salience", 0.0))
                for position in component_positions
            )
            coverage = covered_length_m / total_length_m
            weighted_support = weighted_length_m / total_length_m
            seed_fraction = sum(
                lengths[position] for position in seed_positions
            ) / total_length_m
            minimum_length_m = max(
                frame_span * _TEMPLATE_CORRIDOR_MIN_FRAME_FRACTIONS[role],
                nozzle_real_m * _TEMPLATE_CORRIDOR_MIN_NOZZLE_FACTORS[role],
            )
            if (total_length_m < minimum_length_m
                    or coverage < _TEMPLATE_CORRIDOR_MIN_COVERAGE[role]
                    or seed_fraction < _TEMPLATE_CORRIDOR_MIN_SEED_FRACTION):
                continue
            geometry = unary_union([
                work.iloc[position].geometry
                for position in component_positions
            ])
            corridor_records.append({
                "identity": identity,
                "positions": set(component_positions),
                "role": role,
                "geometry": geometry,
                "length_m": total_length_m,
                "coverage": coverage,
                "weighted_support": weighted_support,
                "seed_features": len(seed_positions),
                "protected": any(
                    _truthy_osm_tag(work.iloc[position].get("bridge"))
                    or _truthy_osm_tag(work.iloc[position].get("tunnel"))
                    for position in component_positions
                ),
                "score": (
                    (3 - role_rank[role]) * 10.0
                    + coverage * 3.0
                    + weighted_support
                    + min(1.0, total_length_m / frame_span)
                ),
            })

    # Collapse only strongly overlapping components of the same semantic
    # road.  End-to-end pieces remain separate and are both retained.
    dropped_record_indexes: set[int] = set()
    records_by_identity: dict[str, list[int]] = {}
    for index, record in enumerate(corridor_records):
        records_by_identity.setdefault(record["identity"], []).append(index)
    collapsed_parallel_corridors = 0
    collapsed_parallel_features = 0
    for indexes in records_by_identity.values():
        for offset, left_index in enumerate(indexes):
            if left_index in dropped_record_indexes:
                continue
            for right_index in indexes[offset + 1:]:
                if right_index in dropped_record_indexes:
                    continue
                left = corridor_records[left_index]
                right = corridor_records[right_index]
                try:
                    left_near = float(left["geometry"].intersection(
                        right["geometry"].buffer(
                            parallel_collapse_m)).length) / max(
                                float(left["length_m"]), 1.0)
                    right_near = float(right["geometry"].intersection(
                        left["geometry"].buffer(
                            parallel_collapse_m)).length) / max(
                                float(right["length_m"]), 1.0)
                except Exception:
                    continue
                if max(left_near, right_near) \
                        < _TEMPLATE_PARALLEL_OVERLAP_FRACTION:
                    continue
                # Prefer the component covering more of the route, then the
                # one more strongly supported by the cartographic skeleton.
                left_choice = (
                    float(left["length_m"]), float(left["score"]))
                right_choice = (
                    float(right["length_m"]), float(right["score"]))
                loser = right_index if left_choice >= right_choice else left_index
                dropped_record_indexes.add(loser)
                collapsed_parallel_corridors += 1
                collapsed_parallel_features += len(
                    corridor_records[loser]["positions"])
                if loser == left_index:
                    break

    # Turn the selected *corridors* into the visual skeleton before expanding
    # them back to individual OSM features.  This is deliberately one level
    # above the old leaf-feature pruning: a short side street is rejected as a
    # whole corridor, while every source way belonging to a retained corridor
    # survives together.  It therefore cannot re-introduce gaps after the
    # whole-path match.
    active_record_indexes = {
        index for index in range(len(corridor_records))
        if index not in dropped_record_indexes
    }
    skeleton_connection_m = max(
        endpoint_snap_m * 2.0,
        min(18.0, nozzle_real_m * 0.22),
    )
    frame_margin_m = max(
        skeleton_connection_m,
        min(nozzle_real_m * 1.25, frame_span * 0.004),
    )
    record_neighbors: dict[int, set[int]] = {
        index: set() for index in active_record_indexes
    }
    active_sorted = sorted(active_record_indexes)
    for offset, left_index in enumerate(active_sorted):
        left_geometry = corridor_records[left_index]["geometry"]
        for right_index in active_sorted[offset + 1:]:
            right_geometry = corridor_records[right_index]["geometry"]
            if left_geometry.distance(right_geometry) > skeleton_connection_m:
                continue
            record_neighbors[left_index].add(right_index)
            record_neighbors[right_index].add(left_index)

    def _record_reaches_frame(index: int) -> bool:
        gxmin, gymin, gxmax, gymax = corridor_records[index]["geometry"].bounds
        return (gxmin - xmin <= frame_margin_m
                or xmax - gxmax <= frame_margin_m
                or gymin - ymin <= frame_margin_m
                or ymax - gymax <= frame_margin_m)

    skeleton_dropped_indexes: set[int] = set()
    # Iteration matters: removing a decorative leaf can reveal another short
    # leaf behind it.  The unit is always a complete semantic source corridor.
    while True:
        remaining = active_record_indexes - skeleton_dropped_indexes
        newly_dropped: set[int] = set()
        for index in sorted(remaining):
            record = corridor_records[index]
            identity = str(record["identity"])
            if (identity.startswith(("numbered-ring:", "named-ring:"))
                    or bool(record.get("protected"))
                    or _record_reaches_frame(index)):
                continue
            degree = sum(
                neighbor in remaining
                for neighbor in record_neighbors[index]
            )
            role = str(record["role"])
            minimum_leaf_length_m = max(
                frame_span
                * _TEMPLATE_SKELETON_LEAF_MIN_FRAME_FRACTIONS[role],
                nozzle_real_m
                * _TEMPLATE_CORRIDOR_MIN_NOZZLE_FACTORS[role],
            )
            if (degree == 0
                    and float(record["length_m"])
                    < minimum_leaf_length_m
                    * _TEMPLATE_SKELETON_ISOLATED_FACTOR):
                newly_dropped.add(index)
            elif (degree == 1
                  and float(record["length_m"]) < minimum_leaf_length_m):
                newly_dropped.add(index)
        newly_dropped -= skeleton_dropped_indexes
        if not newly_dropped:
            break
        skeleton_dropped_indexes.update(newly_dropped)

    dropped_record_indexes.update(skeleton_dropped_indexes)
    skeleton_dropped_features = sum(
        len(corridor_records[index]["positions"])
        for index in skeleton_dropped_indexes
    )
    skeleton_dropped_length_m = sum(
        float(corridor_records[index]["length_m"])
        for index in skeleton_dropped_indexes
    )
    skeleton_dropped_roles: dict[str, int] = {}
    for index in skeleton_dropped_indexes:
        role = str(corridor_records[index]["role"])
        skeleton_dropped_roles[role] = (
            skeleton_dropped_roles.get(role, 0) + 1)

    selected_roles: dict[int, str] = {}
    selected_corridors = 0
    internal_link_features = 0

    for index, record in enumerate(corridor_records):
        if index in dropped_record_indexes:
            continue
        selected_corridors += 1
        role = record["role"]
        for position in record["positions"]:
            highway = str(work.iloc[position].get("highway") or "")
            if highway.endswith("_link"):
                # Membership was resolved while building the complete
                # physical corridor.  Re-running the legacy per-link
                # component search here is both patch-oriented and quadratic
                # on metropolitan corridors.
                selected_roles[position] = "connector"
                internal_link_features += 1
                continue
            previous = selected_roles.get(position)
            if previous is None or role_rank[role] < role_rank[previous]:
                selected_roles[position] = role

    # Anonymous OSM ways cannot form a trustworthy semantic corridor.  Keep
    # only exceptionally long, strongly mask-supported source features; short
    # anonymous hits are the classic perpendicular mask-overlap threads.
    anonymous_selected = 0
    for position, role in provisional_roles.items():
        if _template_identity_for_row(work.iloc[position]):
            continue
        geometry = work.iloc[position].geometry
        support = support_by_position.get(position, {})
        coverage = float(support.get(
            "any_template_fraction", support.get("covered_fraction", 0.0)))
        minimum_length_m = max(frame_span * 0.035, nozzle_real_m * 8.0)
        if float(geometry.length) >= minimum_length_m and coverage >= 0.70:
            selected_roles[position] = role
            anonymous_selected += 1

    selected_positions = set(selected_roles)
    provisional_positions = set(provisional_roles)
    return selected_roles, {
        "method": "amap_seed_to_complete_osm_physical_corridor_v2",
        "seed_features": len(provisional_roles),
        "semantic_identities": len(identities),
        "corridor_components": component_count,
        "selected_corridors": selected_corridors,
        "selected_features": len(selected_roles),
        "promoted_complete_path_features": len(
            selected_positions - provisional_positions),
        "rejected_seed_features": len(
            provisional_positions - selected_positions),
        "joined_endpoint_pairs": joined_endpoint_pairs,
        "anonymous_selected_features": anonymous_selected,
        "internal_link_features": internal_link_features,
        "route_matching_method": "global_existing_osm_physical_corridors_v1",
        "physical_grouping": physical_grouping_evidence,
        "endpoint_snap_m": round(endpoint_snap_m, 3),
        "corridor_join_m": round(corridor_join_m, 3),
        "parallel_collapse_m": round(parallel_collapse_m, 3),
        "parallel_overlap_fraction": (
            _TEMPLATE_PARALLEL_OVERLAP_FRACTION),
        "collapsed_parallel_corridors": collapsed_parallel_corridors,
        "collapsed_parallel_features": collapsed_parallel_features,
        "skeleton_method": "complete_corridor_leaf_graph_v1",
        "skeleton_connection_m": round(skeleton_connection_m, 3),
        "skeleton_frame_margin_m": round(frame_margin_m, 3),
        "skeleton_leaf_min_frame_fractions": dict(
            _TEMPLATE_SKELETON_LEAF_MIN_FRAME_FRACTIONS),
        "skeleton_dropped_corridors": len(skeleton_dropped_indexes),
        "skeleton_dropped_features": skeleton_dropped_features,
        "skeleton_dropped_length_m": round(
            skeleton_dropped_length_m, 3),
        "skeleton_dropped_roles": skeleton_dropped_roles,
        "minimum_coverage": dict(_TEMPLATE_CORRIDOR_MIN_COVERAGE),
        "minimum_seed_fraction": _TEMPLATE_CORRIDOR_MIN_SEED_FRACTION,
        "geometry_policy": "select_complete_source_corridor_never_draw_mask",
    }


def _restore_template_continuity(
    work: gpd.GeoDataFrame,
    role_by_position: dict[int, str],
    support_by_position: dict[int, dict[str, float]],
    *,
    bbox_local,
) -> tuple[dict[int, str], dict[str, Any]]:
    """Restore bounded road-corridor paths from the original OSM graph.

    Raster palette gaps and per-feature thresholding can leave two confirmed
    pieces of one corridor disconnected.  Missing source features are allowed
    back only when an OSM path connects two selected anchors, stays short and
    direct, and has semantic, geometric-corridor or reference-mask support.
    A small semantic proximity tolerance handles split carriageways and OSM
    endpoint offsets.  No coordinates are generated or modified.
    """

    xmin, ymin, xmax, ymax = (float(value) for value in bbox_local)
    frame_span = max(xmax - xmin, ymax - ymin, 1.0)
    endpoint_snap_m = max(0.5, min(2.0, frame_span * 0.00004))
    corridor_snap_m = min(
        _TEMPLATE_CORRIDOR_SNAP_MAX_M,
        max(_TEMPLATE_CORRIDOR_SNAP_MIN_M,
            frame_span * _TEMPLATE_CORRIDOR_SNAP_FRAME_FRACTION),
    )
    role_limits = {
        "primary": frame_span * _TEMPLATE_PRIMARY_GAP_FRAME_FRACTION,
        "secondary": frame_span * _TEMPLATE_SECONDARY_GAP_FRAME_FRACTION,
        "context": frame_span * _TEMPLATE_CONTEXT_GAP_FRAME_FRACTION,
    }
    semantic_role_limits = {
        "primary": frame_span
        * _TEMPLATE_SEMANTIC_PRIMARY_GAP_FRAME_FRACTION,
        "secondary": frame_span
        * _TEMPLATE_SEMANTIC_SECONDARY_GAP_FRAME_FRACTION,
        "context": frame_span
        * _TEMPLATE_SEMANTIC_CONTEXT_GAP_FRAME_FRACTION,
    }
    base_evidence = {
        "method": "corridor_aware_original_osm_path_v2",
        "endpoint_snap_m": round(endpoint_snap_m, 3),
        "corridor_snap_m": round(corridor_snap_m, 3),
        "role_gap_limits_m": {
            role: round(value, 3) for role, value in role_limits.items()
        },
        "semantic_role_gap_limits_m": {
            role: round(value, 3)
            for role, value in semantic_role_limits.items()
        },
        "max_detour_ratio": _TEMPLATE_MAX_GAP_DETOUR_RATIO,
        "minimum_mask_support": _TEMPLATE_MIN_MASK_GAP_SUPPORT,
        "max_link_gap_m": _TEMPLATE_MAX_LINK_GAP_M,
        "max_link_detour_ratio": _TEMPLATE_MAX_LINK_GAP_DETOUR_RATIO,
        "max_link_edges_per_gap": _TEMPLATE_MAX_LINK_EDGES_PER_GAP,
        "inferred_link_continuation_cosine": (
            _TEMPLATE_INFERRED_LINK_CONTINUATION_COSINE),
        "inferred_link_minimum_coverage": (
            _TEMPLATE_INFERRED_LINK_MIN_COVERAGE),
        "inferred_link_minimum_weight": _TEMPLATE_INFERRED_LINK_MIN_WEIGHT,
        "max_corridor_paths_per_anchor": (
            _TEMPLATE_MAX_CORRIDOR_PATHS_PER_ANCHOR),
        "max_mask_paths_per_anchor": _TEMPLATE_MAX_MASK_PATHS_PER_ANCHOR,
        "proximity_continuation_cosine": (
            _TEMPLATE_PROXIMITY_CONTINUATION_COSINE),
    }
    selected_positions = sorted(role_by_position)
    if len(selected_positions) < 2:
        return role_by_position, {
            **base_evidence,
            "eligible_missing_features": 0,
            "restored_paths": 0,
            "component_bridge_paths": 0,
            "same_component_corridor_paths": 0,
            "semantic_paths": 0,
            "proximity_corridor_paths": 0,
            "inferred_corridor_paths": 0,
            "mask_supported_paths": 0,
            "restored_features": 0,
            "restored_length_m": 0.0,
            "restored_link_features": 0,
            "restored_roles": {},
        }

    selected = work.iloc[selected_positions].copy().reset_index(drop=True)
    selected_terminals = {
        local_pos: _line_terminals(row.geometry)
        for local_pos, row in selected.iterrows()
    }
    selected_index = selected.sindex

    parents = list(range(len(selected)))
    corridor_parents = list(range(len(selected)))

    def _find(value: int) -> int:
        while parents[value] != value:
            parents[value] = parents[parents[value]]
            value = parents[value]
        return value

    def _union(left: int, right: int) -> None:
        left_root = _find(left)
        right_root = _find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    def _corridor_find(value: int) -> int:
        while corridor_parents[value] != value:
            corridor_parents[value] = corridor_parents[
                corridor_parents[value]]
            value = corridor_parents[value]
        return value

    def _corridor_union(left: int, right: int) -> None:
        left_root = _corridor_find(left)
        right_root = _corridor_find(right)
        if left_root != right_root:
            corridor_parents[right_root] = left_root

    for local_pos, feature_terminals in selected_terminals.items():
        if feature_terminals is None:
            continue
        for point in feature_terminals:
            matches = selected_index.query(
                point.buffer(endpoint_snap_m), predicate="intersects")
            for other_local in matches:
                other_local = int(other_local)
                if other_local == local_pos:
                    continue
                if point.distance(selected.iloc[other_local].geometry) \
                        <= endpoint_snap_m:
                    _union(local_pos, other_local)
                    identity = _template_identity_for_row(
                        selected.iloc[local_pos])
                    if (identity
                            and identity == _template_identity_for_row(
                                selected.iloc[other_local])):
                        _corridor_union(local_pos, other_local)

    root_identities: dict[int, set[str]] = {}
    for local_pos, row in selected.iterrows():
        root = _find(local_pos)
        identity = _template_identity_for_row(row)
        if identity:
            root_identities.setdefault(root, set()).add(identity)

    selected_identity_set = set().union(*root_identities.values()) \
        if root_identities else set()
    selected_local_by_position = {
        position: local_pos
        for local_pos, position in enumerate(selected_positions)
    }
    maximum_gap_m = max(semantic_role_limits.values())

    def _node_key(point: Point) -> tuple[int, int]:
        return (int(round(point.x / endpoint_snap_m)),
                int(round(point.y / endpoint_snap_m)))

    def _node_point(key: tuple[int, int]) -> Point:
        return Point(key[0] * endpoint_snap_m,
                     key[1] * endpoint_snap_m)

    graph: dict[tuple[int, int], list[tuple[tuple[int, int], int]]] = {}
    missing_positions = []
    for pos, row in work.iterrows():
        if pos in role_by_position:
            continue
        geometry = row.geometry
        feature_terminals = _line_terminals(geometry)
        if feature_terminals is None or float(geometry.length) > maximum_gap_m:
            continue
        support = support_by_position.get(pos, {})
        covered = float(support.get(
            "any_template_fraction", support.get("covered_fraction", 0.0)))
        weighted = float(support.get("weighted_salience", 0.0))
        identity = _template_identity_for_row(row)
        highway = str(row.get("highway") or "")
        semantic_candidate = identity and identity in selected_identity_set
        if (not semantic_candidate
                and covered < 0.08
                and weighted < 0.05
                and not highway.endswith("_link")):
            continue
        if highway == "tertiary" and not semantic_candidate \
                and covered < 0.15:
            continue
        start = _node_key(feature_terminals[0])
        end = _node_key(feature_terminals[1])
        if start == end:
            continue
        graph.setdefault(start, []).append((end, pos))
        graph.setdefault(end, []).append((start, pos))
        missing_positions.append(pos)

    anchor_cache: dict[tuple[int, int], dict[str, Any]] = {}

    def _anchor(key: tuple[int, int]) -> dict[str, Any]:
        if key in anchor_cache:
            return anchor_cache[key]
        point = _node_point(key)
        roots: set[int] = set()
        positions: set[int] = set()
        matches = selected_index.query(
            point.buffer(endpoint_snap_m), predicate="intersects")
        for local_pos in matches:
            local_pos = int(local_pos)
            if point.distance(selected.iloc[local_pos].geometry) \
                    > endpoint_snap_m:
                continue
            roots.add(_find(local_pos))
            positions.add(selected_positions[local_pos])
        corridor_positions = set(positions)
        corridor_roots = set(roots)
        node_identities = {
            identity for _, position in graph.get(key, [])
            if (identity := _template_identity_for_row(work.iloc[position]))
        }
        if node_identities:
            corridor_matches = selected_index.query(
                point.buffer(corridor_snap_m), predicate="intersects")
            for local_pos in corridor_matches:
                local_pos = int(local_pos)
                selected_row = selected.iloc[local_pos]
                if (_template_identity_for_row(selected_row)
                        not in node_identities):
                    continue
                if point.distance(selected_row.geometry) > corridor_snap_m:
                    continue
                corridor_roots.add(_find(local_pos))
                corridor_positions.add(selected_positions[local_pos])
        # Anchor semantics must be local to the touching source features.  A
        # connected downtown graph may contain hundreds of unrelated names;
        # inheriting all of them from the component would make almost any
        # missing road look like a semantic continuation.
        roles = {role_by_position[position] for position in positions}
        identities = {
            identity for position in positions
            if (identity := _template_identity_for_row(work.iloc[position]))
        }
        corridor_roles = {
            role_by_position[position] for position in corridor_positions
        }
        corridor_identities = {
            identity for position in corridor_positions
            if (identity := _template_identity_for_row(work.iloc[position]))
        }

        def _identity_roots(source_positions: set[int]) \
                -> dict[str, set[int]]:
            result: dict[str, set[int]] = {}
            for position in source_positions:
                identity = _template_identity_for_row(work.iloc[position])
                local_pos = selected_local_by_position[position]
                if identity:
                    result.setdefault(identity, set()).add(
                        _corridor_find(local_pos))
            return result

        anchor_cache[key] = {
            "roots": roots,
            "positions": positions,
            "roles": roles,
            "identities": identities,
            "corridor_roots": corridor_roots,
            "corridor_positions": corridor_positions,
            "corridor_roles": corridor_roles,
            "corridor_identities": corridor_identities,
            "identity_roots": _identity_roots(positions),
            "corridor_identity_roots": _identity_roots(
                corridor_positions),
        }
        return anchor_cache[key]

    anchor_nodes = sorted(
        key for key in graph if _anchor(key)["corridor_roots"])
    restored_paths: list[tuple[int, ...]] = []
    restored_positions: set[int] = set()
    accepted_path_keys: set[tuple[int, ...]] = set()
    component_bridge_paths = 0
    same_component_corridor_paths = 0
    semantic_paths = 0
    proximity_corridor_paths = 0
    mask_supported_paths = 0
    inferred_corridor_paths = 0
    role_rank = {"primary": 0, "secondary": 1, "context": 2}

    def _path_role(
        start_info,
        target_info,
        path: tuple[int, ...],
        *,
        use_corridor_anchors: bool = False,
    ) -> str:
        role_key = "corridor_roles" if use_corridor_anchors else "roles"
        start_roles = start_info[role_key]
        target_roles = target_info[role_key]
        if "primary" in start_roles and "primary" in target_roles:
            return "primary"
        if ({"primary", "secondary"} & start_roles
                and {"primary", "secondary"} & target_roles):
            return "secondary"
        return "context"

    def _anchor_path_alignment(
        anchor_info,
        path_position: int,
        node_key: tuple[int, int],
        *,
        allowed_identities: set[str],
        use_corridor_anchors: bool = False,
    ) -> float:
        """Measure whether a missing edge continues a selected corridor."""

        point = _node_point(node_key)
        path_row = work.iloc[path_position]
        path_directions = _directions_from_line_at_point(
            path_row.geometry,
            point,
            endpoint_tolerance_m=endpoint_snap_m * 1.5,
        )
        if not path_directions:
            return -1.0
        best = -1.0
        positions_key = (
            "corridor_positions" if use_corridor_anchors else "positions")
        selected_tolerance_m = (
            corridor_snap_m if use_corridor_anchors else endpoint_snap_m * 1.5)
        for selected_position in anchor_info[positions_key]:
            selected_row = work.iloc[selected_position]
            identity = _template_identity_for_row(selected_row)
            if identity not in allowed_identities:
                continue
            if not _road_classes_compatible(
                    path_row.get("highway"), selected_row.get("highway")):
                continue
            selected_directions = _directions_from_line_at_point(
                selected_row.geometry,
                point,
                endpoint_tolerance_m=selected_tolerance_m,
            )
            for path_direction in path_directions:
                for selected_direction in selected_directions:
                    continuation = -(
                        path_direction[0] * selected_direction[0]
                        + path_direction[1] * selected_direction[1]
                    )
                    best = max(best, continuation)
        return best

    for start in anchor_nodes:
        start_info = _anchor(start)
        queue = [(0.0, 0.0, 0, start, tuple())]
        best_cost = {start: 0.0}
        accepted_corridor_paths = 0
        accepted_mask_paths = 0
        while queue:
            cost, length_m, link_count, node, path = heapq.heappop(queue)
            if cost > best_cost.get(node, float("inf")) + 1e-9:
                continue
            if node != start and _anchor(node)["corridor_roots"] and path:
                target_info = _anchor(node)
                different_roots = bool(
                    start_info["roots"]
                    and target_info["roots"]
                    and target_info["roots"] - start_info["roots"])
                shared_identities = (
                    start_info["identities"] & target_info["identities"])
                path_identities = {
                    identity for pos in path
                    if (identity := _template_identity_for_row(work.iloc[pos]))
                }

                def _different_identity_roots(
                    identities: set[str],
                    *,
                    use_corridor_anchors: bool = False,
                ) -> bool:
                    roots_key = (
                        "corridor_identity_roots"
                        if use_corridor_anchors else "identity_roots")
                    for identity in identities:
                        start_roots = start_info[roots_key].get(identity, set())
                        target_roots = target_info[roots_key].get(
                            identity, set())
                        if (start_roots and target_roots
                                and target_roots - start_roots):
                            return True
                    return False

                different_shared_corridor_roots = (
                    _different_identity_roots(shared_identities))
                closes_ring_corridor = any(
                    identity.startswith(("numbered-ring:", "named-ring:"))
                    for identity in shared_identities)
                possible_unnamed_ring_link = bool(
                    closes_ring_corridor
                    and not path_identities
                    and all(str(work.iloc[pos].get("highway") or "")
                            .endswith("_link") for pos in path)
                )
                semantic_match = bool(
                    shared_identities
                    and path_identities
                    and path_identities <= shared_identities
                )
                corridor_shared_identities = (
                    start_info["corridor_identities"]
                    & target_info["corridor_identities"])
                proximity_start_alignment = -1.0
                proximity_target_alignment = -1.0
                proximity_corridor_match = False
                if (path_identities
                        and corridor_shared_identities
                        and path_identities <= corridor_shared_identities):
                    proximity_start_alignment = _anchor_path_alignment(
                        start_info,
                        path[0],
                        start,
                        allowed_identities=corridor_shared_identities,
                        use_corridor_anchors=True,
                    )
                    proximity_target_alignment = _anchor_path_alignment(
                        target_info,
                        path[-1],
                        node,
                        allowed_identities=corridor_shared_identities,
                        use_corridor_anchors=True,
                    )
                    proximity_corridor_match = bool(
                        proximity_start_alignment
                        >= _TEMPLATE_PROXIMITY_CONTINUATION_COSINE
                        and proximity_target_alignment
                        >= _TEMPLATE_PROXIMITY_CONTINUATION_COSINE
                        and not semantic_match
                    )
                if (different_roots or semantic_match
                        or proximity_corridor_match
                        or possible_unnamed_ring_link):
                    role = _path_role(
                        start_info,
                        target_info,
                        path,
                        use_corridor_anchors=proximity_corridor_match,
                    )
                    straight_m = _node_point(start).distance(_node_point(node))
                    detour = length_m / max(straight_m, endpoint_snap_m)
                    weighted_length = sum(
                        float(work.iloc[pos].geometry.length)
                        * float(support_by_position.get(pos, {}).get(
                            "weighted_salience", 0.0)) for pos in path)
                    average_support = weighted_length / max(length_m, 1.0)
                    covered_length = sum(
                        float(work.iloc[pos].geometry.length)
                        * float(support_by_position.get(pos, {}).get(
                            "any_template_fraction",
                            support_by_position.get(pos, {}).get(
                                "covered_fraction", 0.0),
                        ))
                        for pos in path
                    )
                    average_coverage = covered_length / max(length_m, 1.0)
                    has_link = link_count > 0
                    unnamed_link_path = bool(
                        path
                        and not path_identities
                        and all(str(work.iloc[pos].get("highway") or "")
                                .endswith("_link") for pos in path)
                    )
                    start_alignment = -1.0
                    target_alignment = -1.0
                    inferred_shared_identities = shared_identities
                    if ((different_shared_corridor_roots
                            or closes_ring_corridor)
                            and inferred_shared_identities
                            and unnamed_link_path):
                        start_alignment = _anchor_path_alignment(
                            start_info,
                            path[0],
                            start,
                            allowed_identities=inferred_shared_identities,
                        )
                        target_alignment = _anchor_path_alignment(
                            target_info,
                            path[-1],
                            node,
                            allowed_identities=inferred_shared_identities,
                        )
                    inferred_corridor_match = bool(
                        (different_shared_corridor_roots
                         or closes_ring_corridor)
                        and inferred_shared_identities
                        and unnamed_link_path
                        and start_alignment
                        >= _TEMPLATE_INFERRED_LINK_CONTINUATION_COSINE
                        and target_alignment
                        >= _TEMPLATE_INFERRED_LINK_CONTINUATION_COSINE
                        and average_coverage
                        >= _TEMPLATE_INFERRED_LINK_MIN_COVERAGE
                        and average_support
                        >= _TEMPLATE_INFERRED_LINK_MIN_WEIGHT
                    )
                    link_path_valid = (
                        not has_link
                        or ((different_roots or inferred_corridor_match)
                            and (semantic_match
                                 or proximity_corridor_match
                                 or inferred_corridor_match)
                            and length_m <= _TEMPLATE_MAX_LINK_GAP_M
                            and detour
                            <= _TEMPLATE_MAX_LINK_GAP_DETOUR_RATIO)
                    )
                    path_key = tuple(sorted(path))
                    path_limit_m = (
                        semantic_role_limits[role]
                        if (semantic_match or proximity_corridor_match)
                        else role_limits[role]
                    )
                    if (length_m <= path_limit_m
                            and detour <= _TEMPLATE_MAX_GAP_DETOUR_RATIO
                            and link_count
                            <= _TEMPLATE_MAX_LINK_EDGES_PER_GAP
                            and link_path_valid
                            and (semantic_match
                                 or proximity_corridor_match
                                 or inferred_corridor_match
                                 or (different_roots and average_support
                                     >= _TEMPLATE_MIN_MASK_GAP_SUPPORT))
                            and path_key not in accepted_path_keys):
                        is_corridor_path = bool(
                            semantic_match or proximity_corridor_match
                            or inferred_corridor_match)
                        if (is_corridor_path
                                and accepted_corridor_paths
                                >= _TEMPLATE_MAX_CORRIDOR_PATHS_PER_ANCHOR):
                            continue
                        if (not is_corridor_path
                                and accepted_mask_paths
                                >= _TEMPLATE_MAX_MASK_PATHS_PER_ANCHOR):
                            continue
                        accepted_path_keys.add(path_key)
                        restored_paths.append(path)
                        component_bridge_paths += int(different_roots)
                        same_component_corridor_paths += int(
                            not different_roots)
                        semantic_paths += int(semantic_match)
                        proximity_corridor_paths += int(
                            proximity_corridor_match)
                        inferred_corridor_paths += int(
                            inferred_corridor_match)
                        mask_supported_paths += int(
                            not semantic_match
                            and not proximity_corridor_match
                            and not inferred_corridor_match
                            and average_support
                            >= _TEMPLATE_MIN_MASK_GAP_SUPPORT)
                        accepted_corridor_paths += int(is_corridor_path)
                        accepted_mask_paths += int(not is_corridor_path)
                        for pos in path:
                            previous = role_by_position.get(pos)
                            if (previous is None
                                    or role_rank[role] < role_rank[previous]):
                                role_by_position[pos] = role
                            restored_positions.add(pos)
                        # Do not walk through an accepted anchor into a second
                        # corridor.  Other queued branches may still repair
                        # parallel carriageways at this same junction.
                        continue

            if len(path) >= 12 or length_m >= maximum_gap_m:
                continue
            for neighbor, pos in graph.get(node, []):
                if pos in path:
                    continue
                row = work.iloc[pos]
                edge_length = float(row.geometry.length)
                new_length = length_m + edge_length
                if new_length > maximum_gap_m:
                    continue
                support = support_by_position.get(pos, {})
                weighted = min(1.0, float(
                    support.get("weighted_salience", 0.0)))
                covered = min(1.0, float(support.get(
                    "any_template_fraction",
                    support.get("covered_fraction", 0.0))))
                identity = _template_identity_for_row(row)
                semantic_bonus = (
                    0.55 if identity in start_info["identities"] else 1.0)
                highway = str(row.get("highway") or "")
                link_penalty = 1.25 if highway.endswith("_link") else 1.0
                class_penalty = 1.20 if highway == "tertiary" else 1.0
                edge_cost = (edge_length
                             * (1.0 + 2.2 * (1.0 - weighted)
                                + 1.2 * (1.0 - covered))
                             * semantic_bonus * link_penalty * class_penalty)
                new_cost = cost + edge_cost
                if new_cost + 1e-9 >= best_cost.get(neighbor, float("inf")):
                    continue
                best_cost[neighbor] = new_cost
                heapq.heappush(queue, (
                    new_cost,
                    new_length,
                    link_count + int(highway.endswith("_link")),
                    neighbor,
                    path + (pos,),
                ))

    restored_roles: dict[str, int] = {}
    for position in restored_positions:
        role = role_by_position[position]
        restored_roles[role] = restored_roles.get(role, 0) + 1
    return role_by_position, {
        **base_evidence,
        "eligible_missing_features": len(missing_positions),
        "anchor_nodes": len(anchor_nodes),
        "restored_paths": len(restored_paths),
        "component_bridge_paths": component_bridge_paths,
        "same_component_corridor_paths": same_component_corridor_paths,
        "semantic_paths": semantic_paths,
        "proximity_corridor_paths": proximity_corridor_paths,
        "inferred_corridor_paths": inferred_corridor_paths,
        "mask_supported_paths": mask_supported_paths,
        "restored_features": len(restored_positions),
        "restored_length_m": round(sum(
            float(work.iloc[position].geometry.length)
            for position in restored_positions), 3),
        "restored_link_features": sum(
            str(work.iloc[position].get("highway") or "").endswith("_link")
            for position in restored_positions),
        "restored_roles": restored_roles,
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
    role_chain_limits_m = {
        "primary": min(max_chain_length_m, max(
            frame_span * _TEMPLATE_PRIMARY_DANGLING_CHAIN_FRAME_FRACTION,
            _TEMPLATE_PRIMARY_DANGLING_CHAIN_TARGET_M,
        )),
        "secondary": min(max_chain_length_m, max(
            frame_span * _TEMPLATE_SECONDARY_DANGLING_CHAIN_FRAME_FRACTION,
            _TEMPLATE_SECONDARY_DANGLING_CHAIN_TARGET_M,
        )),
        "context": max_chain_length_m,
        "connector": max_chain_length_m,
    }
    endpoint_snap_m = max(0.5, min(2.0, frame_span * 0.00004))
    frame_margin_m = max(endpoint_snap_m * 2.0, frame_span * 0.0025)
    base_evidence = {
        "method": "selected_osm_graph_leaf_chain_v2",
        "max_chain_length_m": round(max_chain_length_m, 3),
        "role_chain_limits_m": {
            role: round(value, 3)
            for role, value in role_chain_limits_m.items()
        },
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
        chain_limit_m = max_chain_length_m
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
            source_position = selected_positions[current]
            role = role_by_position[source_position]
            chain_limit_m = min(
                chain_limit_m,
                role_chain_limits_m.get(role, max_chain_length_m),
            )
            geometry = selected.iloc[current].geometry
            chain_length_m += float(geometry.length)
            if chain_length_m > chain_limit_m:
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
    provisional_roles: dict[int, str] = {}
    support_by_position: dict[int, dict[str, float]] = {}
    identity_stats: dict[str, dict[str, float]] = {}

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
        identity = _template_identity_for_row(row)
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
    seed_roles: dict[int, str] = {}
    for pos, role in provisional_roles.items():
        row = work.iloc[pos]
        geometry = row.geometry
        length_m = float(geometry.length)
        if length_m >= minimum_length_m:
            seed_roles[pos] = role
            continue
        identity = _template_identity_for_row(row)
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
            seed_roles[pos] = role

    # The raster match above supplies evidence seeds, never the final visible
    # selection.  Match those seeds to complete semantic OSM corridors in one
    # pass so palette gaps do not produce a collection of fragments that then
    # needs hundreds of local continuity patches.
    role_by_position, corridor_matching_evidence = _match_template_corridors(
        work,
        seed_roles,
        support_by_position,
        bbox_local=bbox_local,
        nozzle_real_m=nozzle_real_m,
    )
    connector_positions = {
        position for position, role in role_by_position.items()
        if role == "connector"
    }

    continuity_evidence = {
        "method": "disabled_by_corridor_first_selection_v1",
        "eligible_missing_features": 0,
        "anchor_nodes": 0,
        "restored_paths": 0,
        "component_bridge_paths": 0,
        "same_component_corridor_paths": 0,
        "semantic_paths": 0,
        "proximity_corridor_paths": 0,
        "inferred_corridor_paths": 0,
        "mask_supported_paths": 0,
        "restored_features": 0,
        "restored_length_m": 0.0,
        "restored_link_features": 0,
        "restored_roles": {},
    }

    # Corridor-level skeleton pruning already rejected decorative leaves as
    # whole paths.  Running the legacy feature-level pruner here would punch
    # fresh gaps into those complete paths and undo the central v12 contract.
    dangling_chain_evidence = {
        "method": "disabled_by_complete_corridor_leaf_graph_v1",
        "candidate_features": len(role_by_position),
        "traced_leaf_chains": 0,
        "removed_features": 0,
        "removed_length_m": 0.0,
        "removed_roles": {},
    }
    continuity_evidence["retained_after_pruning"] = 0

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
        "method": "amap_physical_corridor_skeleton_to_osm_roles_v6",
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
        "method": "amap_physical_corridor_skeleton_existing_osm_v6",
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
        "corridor_matching": corridor_matching_evidence,
        "continuity_restoration": continuity_evidence,
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
