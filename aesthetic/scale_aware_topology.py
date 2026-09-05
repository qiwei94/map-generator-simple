"""Scale-aware city-block topology for fixed-size printable models.

The physical model and printer define the visible XY floor.  When a wider
geographic crop is mapped to the same model span, more real-world metres fit
inside every model millimetre.  Low-order road cuts that leave no printable
urban core must therefore stop splitting independent building masses.

This module performs that bounded topology decision.  It never invents a
road, changes visible road geometry, controls Z, or touches mesh booleans.
Major/visible roads and water boundaries are immutable separators; only
existing low-order road boundaries may be dissolved.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable, Sequence

import numpy as np
from shapely.errors import GEOSException
from shapely.geometry import (
    GeometryCollection, LineString, MultiLineString, MultiPolygon, Polygon,
)
from shapely.ops import unary_union
from shapely.strtree import STRtree


POLICY_VERSION = "scale-aware-block-topology-v2"


@dataclass
class _BlockRecord:
    geometry: Polygon
    building_count: int


def _polygon_parts(geometry) -> list[Polygon]:
    if geometry is None or geometry.is_empty:
        return []
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, (MultiPolygon, GeometryCollection)):
        result: list[Polygon] = []
        for child in geometry.geoms:
            result.extend(_polygon_parts(child))
        return result
    return []


def _line_parts(geometry) -> list[LineString]:
    if geometry is None or geometry.is_empty:
        return []
    if isinstance(geometry, LineString):
        return [geometry]
    if isinstance(geometry, (MultiLineString, GeometryCollection)):
        result: list[LineString] = []
        for child in geometry.geoms:
            result.extend(_line_parts(child))
        return result
    return []


def _geometry_iter(source) -> Iterable:
    if source is None:
        return ()
    geometry = source.geometry if hasattr(source, "geometry") else source
    return geometry if geometry is not None else ()


def _safe_union(geometries: Iterable):
    clean = [geometry for geometry in geometries
             if geometry is not None and not geometry.is_empty]
    if not clean:
        return GeometryCollection()
    try:
        return unary_union(clean)
    except GEOSException:
        repaired = []
        for geometry in clean:
            try:
                candidate = geometry if geometry.is_valid else geometry.buffer(0)
            except GEOSException:
                continue
            if candidate is not None and not candidate.is_empty:
                repaired.append(candidate)
        try:
            return unary_union(repaired) if repaired else GeometryCollection()
        except GEOSException:
            return GeometryCollection()


def _minimum_rotated_axis(polygon: Polygon) -> float:
    try:
        rectangle = polygon.minimum_rotated_rectangle
        coordinates = list(rectangle.exterior.coords)
        lengths = [
            math.hypot(second[0] - first[0], second[1] - first[1])
            for first, second in zip(coordinates, coordinates[1:])
        ]
        positive = [value for value in lengths if value > 1e-9]
        return min(positive) if positive else 0.0
    except (AttributeError, GEOSException, ValueError):
        return 0.0


def _usable_short_axis_model_mm(
    polygon: Polygon,
    *,
    scale_mm_per_m: float,
    boundary_inset_model_mm: float,
) -> float:
    """Return printable core width after the two road-side clearances."""

    inset_real_m = boundary_inset_model_mm / scale_mm_per_m
    try:
        core = polygon.buffer(-inset_real_m, join_style=1)
    except GEOSException:
        core = GeometryCollection()
    parts = _polygon_parts(core)
    if not parts:
        return 0.0
    return max(_minimum_rotated_axis(part) for part in parts) * scale_mm_per_m


def _building_counts(blocks: Sequence[Polygon], buildings) -> list[int]:
    counts = [0] * len(blocks)
    sources = [
        geometry for geometry in _geometry_iter(buildings)
        if geometry is not None and not geometry.is_empty
    ]
    if not blocks or not sources:
        return counts
    centroids = np.asarray([geometry.centroid for geometry in sources], dtype=object)
    # STRtree prepares the *query* geometry for a predicate.  Keep the cheap
    # Points in the tree and query with the complex city blocks so GEOS can
    # prepare each block once.  The inverse point.within(block) formulation
    # repeatedly rebuilt block topology and took tens of minutes for Paris.
    tree = STRtree(centroids)
    block_batch_size = 10_000
    try:
        for start in range(0, len(blocks), block_batch_size):
            stop = min(len(blocks), start + block_batch_size)
            block_batch = np.asarray(blocks[start:stop], dtype=object)
            pairs = tree.query(block_batch, predicate="contains")
            if pairs.size:
                for local_block_index in pairs[0]:
                    counts[start + int(local_block_index)] += 1
        return counts
    except (TypeError, ValueError, GEOSException):
        for block_index, block in enumerate(blocks):
            try:
                candidates = tree.query(block)
            except GEOSException:
                continue
            for centroid_index in candidates:
                try:
                    if block.contains(centroids[int(centroid_index)]):
                        counts[block_index] += 1
                except GEOSException:
                    continue
        return counts


def _water_boundaries(water) -> list[LineString]:
    result: list[LineString] = []
    for geometry in _geometry_iter(water):
        if geometry is None or geometry.is_empty:
            continue
        if isinstance(geometry, Polygon):
            result.extend(_line_parts(geometry.boundary))
        elif isinstance(geometry, MultiPolygon):
            for polygon in geometry.geoms:
                result.extend(_line_parts(polygon.boundary))
        else:
            result.extend(_line_parts(geometry))
    return result


def _adjacent_pairs(polygons: Sequence[Polygon]) -> list[tuple[int, int]]:
    if len(polygons) < 2:
        return []
    tree = STRtree(polygons)
    values = np.asarray(polygons, dtype=object)
    try:
        # Return bbox candidates only.  The caller immediately computes the
        # exact shared boundary and rejects zero-length contacts, so applying
        # ``touches`` here repeats the expensive topology predicate.  A large
        # remainder polygon in a nested-face partition can touch ten thousand
        # blocks; avoiding the duplicate GEOSPreparedTouches is material.
        pairs = tree.query(values)
        return [
            (int(left), int(right))
            for left, right in zip(pairs[0], pairs[1])
            if int(left) < int(right)
        ]
    except (TypeError, ValueError, GEOSException):
        result = []
        for left, polygon in enumerate(polygons):
            try:
                candidates = tree.query(polygon)
            except GEOSException:
                continue
            for right_value in candidates:
                right = int(right_value)
                if right <= left:
                    continue
                result.append((left, right))
        return result


def _block_statistics(
    records: Sequence[_BlockRecord],
    *,
    scale_mm_per_m: float,
    boundary_inset_model_mm: float,
    target_min_model_mm: float,
    hard_floor_model_mm: float,
) -> tuple[list[float], dict]:
    widths = [
        _usable_short_axis_model_mm(
            record.geometry,
            scale_mm_per_m=scale_mm_per_m,
            boundary_inset_model_mm=boundary_inset_model_mm,
        )
        for record in records
    ]
    occupied = [width for width, record in zip(widths, records)
                if record.building_count > 0]
    return widths, {
        "occupied_blocks": len(occupied),
        "occupied_core_short_axis_p50_model_mm": (
            round(float(np.percentile(occupied, 50)), 5)
            if occupied else None),
        "occupied_below_target": int(sum(
            width < target_min_model_mm for width in occupied)),
        "occupied_below_hard_floor": int(sum(
            width < hard_floor_model_mm for width in occupied)),
    }


def _retained_cut_lines(
    cut_lines,
    final_blocks: Sequence[Polygon],
    *,
    tolerance_real_m: float,
) -> list[LineString]:
    source_lines = [
        part for geometry in _geometry_iter(cut_lines)
        for part in _line_parts(geometry)
        if part.length > 0
    ]
    if not source_lines or not final_blocks:
        return []
    road_union = _safe_union(source_lines)
    final_boundary = _safe_union(
        block.boundary for block in final_blocks)
    # Final block boundaries are assembled from these exact source roads, so
    # an exact line/line intersection is the correct authority.  Buffering the
    # complete 25 km city boundary created a huge polygon and dominated the
    # full run without adding semantic evidence.
    try:
        retained = road_union.intersection(final_boundary)
    except GEOSException:
        return source_lines
    # Intersections at a removed road's endpoint create tiny buffered crumbs;
    # they are not retained structural seams.  A four-tolerance floor keeps
    # genuine boundary runs while removing those point-contact artifacts.
    minimum_length = max(tolerance_real_m * 4.0, 0.05)
    return [line for line in _line_parts(retained)
            if line.length >= minimum_length]


def coarsen_city_blocks_for_print(
    blocks: Sequence[Polygon],
    *,
    buildings,
    cut_lines,
    protected_cut_lines=(),
    water=None,
    scale_mm_per_m: float,
    target_min_model_mm: float,
    hard_floor_model_mm: float,
    boundary_inset_model_mm: float,
    max_passes: int = 10,
    max_merge_fraction_per_pass: float = 0.35,
) -> tuple[list[Polygon], list[LineString], dict]:
    """Dissolve only low-order cuts that create sub-target occupied blocks.

    The target is a median distribution guard, not a minimum imposed on every
    city block.  Hard-floor violations are always attempted; softer merges run
    only while the occupied-block median remains below the requested visual
    target.  Each pass is bounded to avoid collapsing a dense city into a few
    giant slabs.
    """

    for name, value in (
        ("scale_mm_per_m", scale_mm_per_m),
        ("target_min_model_mm", target_min_model_mm),
        ("hard_floor_model_mm", hard_floor_model_mm),
    ):
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be finite and positive")
    if not math.isfinite(boundary_inset_model_mm) or boundary_inset_model_mm < 0:
        raise ValueError("boundary_inset_model_mm must be finite and non-negative")
    if isinstance(max_passes, bool) or max_passes < 0:
        raise ValueError("max_passes must be a non-negative integer")
    if not 0 < max_merge_fraction_per_pass <= 0.5:
        raise ValueError("max_merge_fraction_per_pass must be in (0, 0.5]")

    clean_blocks = [
        block for geometry in blocks for block in _polygon_parts(geometry)
        if block.area > 0
    ]
    counts = _building_counts(clean_blocks, buildings)
    records = [
        _BlockRecord(block, count)
        for block, count in zip(clean_blocks, counts)
    ]
    initial_widths, before = _block_statistics(
        records,
        scale_mm_per_m=scale_mm_per_m,
        boundary_inset_model_mm=boundary_inset_model_mm,
        target_min_model_mm=target_min_model_mm,
        hard_floor_model_mm=hard_floor_model_mm,
    )

    protected_geometries = [
        part for geometry in _geometry_iter(protected_cut_lines)
        for part in _line_parts(geometry)
    ] + _water_boundaries(water)
    protection_tolerance_real_m = max(
        0.05, hard_floor_model_mm / scale_mm_per_m * 0.01)
    # Do not union/buffer the complete protected network.  Intersecting every
    # shared block edge with that city-wide geometry dominated a 25 km run.
    # Query a spatial index and build only the handful of local supports that
    # can protect the current edge.
    protected_tree = (
        STRtree(protected_geometries) if protected_geometries else None)

    total_merges = 0
    protected_edge_rejections = 0
    passes = []
    stop_reason = "target_already_met"
    for pass_index in range(int(max_passes)):
        widths, stats = _block_statistics(
            records,
            scale_mm_per_m=scale_mm_per_m,
            boundary_inset_model_mm=boundary_inset_model_mm,
            target_min_model_mm=target_min_model_mm,
            hard_floor_model_mm=hard_floor_model_mm,
        )
        occupied_indices = [
            index for index, record in enumerate(records)
            if record.building_count > 0
        ]
        hard = [index for index in occupied_indices
                if widths[index] < hard_floor_model_mm]
        median = stats["occupied_core_short_axis_p50_model_mm"]
        soft = [
            index for index in occupied_indices
            if widths[index] < target_min_model_mm
        ] if median is not None and median < target_min_model_mm else []
        candidates = sorted(
            set(hard + soft), key=lambda index: (widths[index], index))
        if not candidates:
            stop_reason = (
                "target_met" if median is not None else "no_occupied_blocks")
            break

        polygons = [record.geometry for record in records]
        adjacency: dict[int, list[tuple[int, float]]] = {}
        for left, right in _adjacent_pairs(polygons):
            if left not in candidates and right not in candidates:
                continue
            try:
                shared = polygons[left].boundary.intersection(
                    polygons[right].boundary)
                shared_length = float(shared.length)
            except GEOSException:
                continue
            if shared_length <= protection_tolerance_real_m:
                continue
            protected_fraction = 0.0
            if protected_tree is not None:
                try:
                    probe = shared.buffer(
                        protection_tolerance_real_m,
                        cap_style=2, join_style=2)
                    candidate_indices = protected_tree.query(
                        probe, predicate="intersects")
                    local_lines = [
                        protected_geometries[int(index)]
                        for index in candidate_indices
                    ]
                    if local_lines:
                        local_support = _safe_union(local_lines).buffer(
                            protection_tolerance_real_m,
                            cap_style=2, join_style=2)
                        protected_fraction = float(
                            shared.intersection(local_support).length
                            / shared_length)
                except (GEOSException, TypeError, ValueError):
                    protected_fraction = 1.0
            if protected_fraction >= 0.55:
                protected_edge_rejections += 1
                continue
            adjacency.setdefault(left, []).append((right, shared_length))
            adjacency.setdefault(right, []).append((left, shared_length))

        maximum_merges = max(1, int(math.ceil(
            len(occupied_indices) * max_merge_fraction_per_pass)))
        used: set[int] = set()
        pairs: list[tuple[int, int]] = []
        for index in candidates:
            if index in used or len(pairs) >= maximum_merges:
                continue
            options = []
            for neighbour, shared_length in adjacency.get(index, ()):
                if neighbour in used:
                    continue
                try:
                    merged = records[index].geometry.union(
                        records[neighbour].geometry)
                except GEOSException:
                    continue
                merged_parts = _polygon_parts(merged)
                if len(merged_parts) != 1:
                    continue
                merged_width = _usable_short_axis_model_mm(
                    merged_parts[0],
                    scale_mm_per_m=scale_mm_per_m,
                    boundary_inset_model_mm=boundary_inset_model_mm,
                )
                neighbour_occupied = records[neighbour].building_count > 0
                reaches_target = merged_width >= target_min_model_mm
                options.append((
                    bool(reaches_target), bool(neighbour_occupied),
                    merged_width, shared_length, -neighbour,
                    neighbour, merged_parts[0],
                ))
            if not options:
                continue
            choice = max(options, key=lambda item: item[:5])
            neighbour = int(choice[5])
            pairs.append((index, neighbour))
            used.add(index)
            used.add(neighbour)

        if not pairs:
            stop_reason = "protected_or_disconnected_topology"
            break
        merged_by_first = {left: (right, None) for left, right in pairs}
        second_indices = {right for _, right in pairs}
        next_records: list[_BlockRecord] = []
        for index, record in enumerate(records):
            if index in second_indices:
                continue
            if index in merged_by_first:
                right = merged_by_first[index][0]
                try:
                    geometry = record.geometry.union(records[right].geometry)
                except GEOSException:
                    geometry = _safe_union(
                        [record.geometry, records[right].geometry])
                parts = _polygon_parts(geometry)
                if len(parts) == 1:
                    next_records.append(_BlockRecord(
                        parts[0],
                        record.building_count + records[right].building_count,
                    ))
                else:
                    next_records.append(record)
                    next_records.append(records[right])
            else:
                next_records.append(record)
        records = next_records
        total_merges += len(pairs)
        passes.append({
            "pass": pass_index + 1,
            "candidate_blocks": len(candidates),
            "merged_pairs": len(pairs),
            "blocks_after": len(records),
        })
        stop_reason = "maximum_passes_reached"

    final_blocks = [record.geometry for record in records]
    _, after = _block_statistics(
        records,
        scale_mm_per_m=scale_mm_per_m,
        boundary_inset_model_mm=boundary_inset_model_mm,
        target_min_model_mm=target_min_model_mm,
        hard_floor_model_mm=hard_floor_model_mm,
    )
    retained_lines = _retained_cut_lines(
        cut_lines,
        final_blocks,
        tolerance_real_m=protection_tolerance_real_m,
    )
    input_cut_length = sum(
        part.length for geometry in _geometry_iter(cut_lines)
        for part in _line_parts(geometry)
    )
    retained_cut_length = sum(line.length for line in retained_lines)
    target_status = "not_measured"
    final_median = after["occupied_core_short_axis_p50_model_mm"]
    if final_median is not None:
        target_status = (
            "met" if final_median >= target_min_model_mm else "below_target")
        if target_status == "met" and stop_reason == "maximum_passes_reached":
            stop_reason = "target_met_after_final_pass"
    evidence = {
        "policy_version": POLICY_VERSION,
        "status": target_status,
        "stop_reason": stop_reason,
        "scale_mm_per_m": round(float(scale_mm_per_m), 9),
        "real_m_per_model_mm": round(1.0 / scale_mm_per_m, 6),
        "target_min_model_mm": round(float(target_min_model_mm), 5),
        "target_min_real_m": round(
            float(target_min_model_mm / scale_mm_per_m), 5),
        "hard_floor_model_mm": round(float(hard_floor_model_mm), 5),
        "boundary_inset_each_side_model_mm": round(
            float(boundary_inset_model_mm), 5),
        "minimum_target_road_interval_model_mm": round(
            float(target_min_model_mm + 2.0 * boundary_inset_model_mm), 5),
        "initial_blocks": len(clean_blocks),
        "final_blocks": len(final_blocks),
        "merged_blocks": int(total_merges),
        "protected_edge_rejections": int(protected_edge_rejections),
        "input_cut_length_m": round(float(input_cut_length), 3),
        "retained_cut_length_m": round(float(retained_cut_length), 3),
        "retained_cut_fraction": round(
            float(retained_cut_length / max(input_cut_length, 1e-9)), 5),
        "before": before,
        "after": after,
        "passes": passes,
        "invariants": {
            "protected_major_visible_roads_preserved": True,
            "water_boundaries_preserved": True,
            "source_road_geometry_only": True,
            "visible_road_selection_unchanged": True,
            "global_z_untouched": True,
            "mesh_booleans_untouched": True,
        },
    }
    return final_blocks, retained_lines, evidence
