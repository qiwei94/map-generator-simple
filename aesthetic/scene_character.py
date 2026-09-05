"""Local, auditable scene-character analysis for printable city maps.

The analyzer describes evidence; it does not choose mesh geometry, Z values,
materials or booleans.  Its grid-relative labels are intended for diagnostic
review before any adaptive rendering policy consumes them.
"""
from __future__ import annotations

from collections import defaultdict
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Iterable, Mapping

import numpy as np
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from aesthetic.landform_character import analyze_landform_character
from aesthetic.building_data_quality import resolve_local_building_quality
from aesthetic.block_grammar import measure_source_block_grammar

SCENE_ANALYSIS_VERSION = "scene-character-v6"

_RING_IDENTITY_RE = re.compile(
    r"(?:[一二三四五六七八九十\d]+\s*环|环路|环线|"
    r"\b(?:inner|outer|\d+(?:st|nd|rd|th)?)?\s*ring(?:\s+road)?\b|"
    r"\bbeltway\b|\borbital\b|\bp[eé]riph[eé]rique\b)",
    re.IGNORECASE,
)

_STRUCTURAL_HIGHWAYS = {
    "motorway", "motorway_link", "trunk", "trunk_link",
    "primary", "primary_link", "secondary", "secondary_link",
    "tertiary", "tertiary_link", "residential", "unclassified",
    "living_street",
}
_MAJOR_HIGHWAYS = {
    "motorway", "motorway_link", "trunk", "trunk_link",
    "primary", "primary_link", "secondary", "secondary_link",
}
_LANDMARK_BUILDINGS = {
    "stadium", "university", "college", "hospital", "train_station",
    "mall", "public", "government", "museum", "cathedral", "church",
    "temple", "mosque", "synagogue", "civic", "library", "pagoda",
    "shrine", "chapel", "monastery", "convent", "abbey",
}
_LANDMARK_AMENITIES = {
    "university", "hospital", "mall", "theatre", "cinema",
    "place_of_worship", "library", "townhall", "courthouse", "college",
}
_LANDMARK_TOURISM = {
    "museum", "gallery", "attraction", "theme_park", "aquarium",
}
_LANDMARK_MAN_MADE = {
    "tower", "lighthouse", "water_tower", "obelisk",
}


def _iter_lines(geometry) -> Iterable[object]:
    if geometry is None or geometry.is_empty:
        return
    if geometry.geom_type == "LineString":
        yield geometry
    elif geometry.geom_type == "MultiLineString":
        yield from (part for part in geometry.geoms if not part.is_empty)


def _cell_indexes(x, y, bbox_local, grid_size: int):
    xmin, ymin, xmax, ymax = bbox_local
    gx = np.floor((np.asarray(x) - xmin) / max(xmax - xmin, 1.0)
                  * grid_size).astype(int)
    gy = np.floor((np.asarray(y) - ymin) / max(ymax - ymin, 1.0)
                  * grid_size).astype(int)
    return (np.clip(gx, 0, grid_size - 1),
            np.clip(gy, 0, grid_size - 1))


def _line_metrics(roads, bbox_local, grid_size: int) -> dict:
    count = grid_size * grid_size
    length_m = np.zeros(count, dtype=float)
    structural_length_m = np.zeros(count, dtype=float)
    major_length_m = np.zeros(count, dtype=float)
    orientation_hist = np.zeros((count, 18), dtype=float)
    direction_hist = np.zeros((count, 18), dtype=float)
    node_degree = defaultdict(int)
    radial_weight = 0.0
    radial_alignment_weight = 0.0
    ring_identities = set()
    closed_ring_candidates = 0

    xmin, ymin, xmax, ymax = bbox_local
    frame_span = max(xmax - xmin, ymax - ymin, 1.0)
    frame_center = np.asarray(((xmin + xmax) * 0.5,
                               (ymin + ymax) * 0.5), dtype=float)

    if roads is None or len(roads) == 0:
        return {
            "length_m": length_m,
            "structural_length_m": structural_length_m,
            "major_length_m": major_length_m,
            "orientation_concentration": np.zeros(count),
            "orientation_entropy": np.zeros(count),
            "junction_count": np.zeros(count),
            "global_direction_entropy": 0.0,
            "radial_alignment": 0.0,
            "ring_identity_count": 0,
            "closed_ring_candidates": 0,
        }

    highway_values = (roads["highway"].fillna("").astype(str).str.lower()
                      if "highway" in roads.columns
                      else [""] * len(roads))
    for geometry, highway in zip(roads.geometry, highway_values):
        structural = highway in _STRUCTURAL_HIGHWAYS
        major = highway in _MAJOR_HIGHWAYS
        for line in _iter_lines(geometry) or ():
            coords = np.asarray(line.coords, dtype=float)
            if len(coords) < 2:
                continue
            delta = coords[1:] - coords[:-1]
            lengths = np.hypot(delta[:, 0], delta[:, 1])
            valid = np.isfinite(lengths) & (lengths >= 1.0)
            if not valid.any():
                continue
            mids = (coords[1:] + coords[:-1]) * 0.5
            gx, gy = _cell_indexes(
                mids[valid, 0], mids[valid, 1], bbox_local, grid_size)
            flat = gy * grid_size + gx
            np.add.at(length_m, flat, lengths[valid])
            if structural:
                np.add.at(structural_length_m, flat, lengths[valid])
                folded = np.arctan2(
                    delta[valid, 1], delta[valid, 0]) % (math.pi / 2)
                bins = np.floor(folded / (math.pi / 2) * 18).astype(int) % 18
                np.add.at(orientation_hist, (flat, bins), lengths[valid])
                directions = np.arctan2(
                    delta[valid, 1], delta[valid, 0]) % math.pi
                direction_bins = np.floor(
                    directions / math.pi * 18).astype(int) % 18
                np.add.at(direction_hist, (flat, direction_bins), lengths[valid])
                radial_vectors = frame_center - mids[valid]
                radial_norm = np.hypot(
                    radial_vectors[:, 0], radial_vectors[:, 1])
                eligible_radial = radial_norm >= frame_span * 0.04
                if eligible_radial.any():
                    radial_angles = np.arctan2(
                        radial_vectors[eligible_radial, 1],
                        radial_vectors[eligible_radial, 0])
                    alignment = np.abs(np.cos(
                        directions[eligible_radial] - radial_angles))
                    weights = lengths[valid][eligible_radial]
                    radial_alignment_weight += float(np.sum(alignment * weights))
                    radial_weight += float(np.sum(weights))
                # Segment incidence, not feature count: a bend has degree 2,
                # a T node degree 3 and a planar crossing degree 4.
                quantized = np.rint(coords / 2.0).astype(np.int64)
                for index, key in enumerate(map(tuple, quantized)):
                    node_degree[key] += 1 if index in (0, len(coords) - 1) else 2
            if major:
                np.add.at(major_length_m, flat, lengths[valid])

            if structural:
                coords_span = np.ptp(coords, axis=0)
                if (line.is_ring
                        and max(coords_span) >= frame_span * 0.12
                        and max(coords_span) <= frame_span * 0.90):
                    closed_ring_candidates += 1

    orientation = np.zeros(count, dtype=float)
    orientation_entropy = np.zeros(count, dtype=float)
    totals = orientation_hist.sum(axis=1)
    for index in np.flatnonzero(totals > 0):
        hist = orientation_hist[index]
        smooth = hist + np.roll(hist, 1) + np.roll(hist, -1)
        peak_share = smooth.max() / max(totals[index] * 3.0, 1e-9)
        orientation[index] = np.clip((peak_share - 0.08) / 0.30, 0.0, 1.0)
        probabilities = direction_hist[index]
        probabilities = probabilities[probabilities > 0]
        probabilities = probabilities / max(probabilities.sum(), 1e-9)
        orientation_entropy[index] = float(
            -np.sum(probabilities * np.log(probabilities)) / math.log(18))

    all_directions = direction_hist.sum(axis=0)
    nonzero_directions = all_directions[all_directions > 0]
    if nonzero_directions.size:
        probabilities = nonzero_directions / nonzero_directions.sum()
        global_direction_entropy = float(
            -np.sum(probabilities * np.log(probabilities)) / math.log(18))
    else:
        global_direction_entropy = 0.0

    # Name/ref evidence catches multi-feature rings that do not arrive as one
    # closed LineString. It is deliberately semantic evidence, not a geometry
    # instruction.
    for _, row in roads.iterrows():
        raw_highway = row.get("highway")
        highway = str(raw_highway if raw_highway is not None else "").lower()
        if highway not in _STRUCTURAL_HIGHWAYS:
            continue
        identity_text = " ".join(
            str(value if value is not None else "")
            for value in (row.get("name"), row.get("name:en"), row.get("ref")))
        match = _RING_IDENTITY_RE.search(identity_text)
        if match:
            ring_identities.add(match.group(0).strip().casefold())

    junction_count = np.zeros(count, dtype=float)
    if node_degree:
        junctions = np.asarray(
            [(key[0] * 2.0, key[1] * 2.0)
             for key, degree in node_degree.items() if degree >= 3],
            dtype=float,
        )
        if junctions.size:
            gx, gy = _cell_indexes(
                junctions[:, 0], junctions[:, 1], bbox_local, grid_size)
            np.add.at(junction_count, gy * grid_size + gx, 1)

    return {
        "length_m": length_m,
        "structural_length_m": structural_length_m,
        "major_length_m": major_length_m,
        "orientation_concentration": orientation,
        "orientation_entropy": orientation_entropy,
        "junction_count": junction_count,
        "global_direction_entropy": global_direction_entropy,
        "radial_alignment": (
            radial_alignment_weight / radial_weight if radial_weight else 0.0),
        "ring_identity_count": len(ring_identities),
        "closed_ring_candidates": closed_ring_candidates,
    }


def _truthy_text(values) -> np.ndarray:
    text = values.fillna("").astype(str).str.strip().str.lower()
    return ~text.isin(("", "nan", "none", "false", "0"))


def _building_metrics(buildings, bbox_local, grid_size: int) -> dict:
    count = grid_size * grid_size
    area_m2 = np.zeros(count, dtype=float)
    building_count = np.zeros(count, dtype=float)
    landmark_count = np.zeros(count, dtype=float)
    landmark_area_m2 = np.zeros(count, dtype=float)
    if buildings is None or len(buildings) == 0:
        return {
            "area_m2": area_m2,
            "count": building_count,
            "landmark_count": landmark_count,
            "landmark_area_m2": landmark_area_m2,
        }

    polygon_mask = buildings.geometry.geom_type.isin(("Polygon", "MultiPolygon"))
    work = buildings.loc[polygon_mask]
    if len(work) == 0:
        return {
            "area_m2": area_m2,
            "count": building_count,
            "landmark_count": landmark_count,
            "landmark_area_m2": landmark_area_m2,
        }
    areas = work.geometry.area.to_numpy(dtype=float)
    centroids = work.geometry.centroid
    gx, gy = _cell_indexes(
        centroids.x.to_numpy(), centroids.y.to_numpy(), bbox_local, grid_size)
    flat = gy * grid_size + gx
    np.add.at(area_m2, flat, areas)
    np.add.at(building_count, flat, 1)

    landmark = np.zeros(len(work), dtype=bool)
    for column in ("wikidata", "wikipedia", "historic", "heritage"):
        if column in work.columns:
            landmark |= _truthy_text(work[column]).to_numpy()
    for column, allowed in (
        ("building", _LANDMARK_BUILDINGS),
        ("amenity", _LANDMARK_AMENITIES),
        ("tourism", _LANDMARK_TOURISM),
        ("man_made", _LANDMARK_MAN_MADE),
    ):
        if column in work.columns:
            landmark |= work[column].fillna("").astype(str).str.lower().isin(
                allowed).to_numpy()
    finite_areas = areas[np.isfinite(areas) & (areas > 0)]
    large_cutoff = (max(5000.0, float(np.percentile(finite_areas, 99.9)))
                    if finite_areas.size else math.inf)
    # Strict comparison avoids labelling every building when a synthetic or
    # highly regular source has one repeated footprint size.
    landmark |= areas > large_cutoff
    if landmark.any():
        np.add.at(landmark_count, flat[landmark], 1)
        np.add.at(landmark_area_m2, flat[landmark], areas[landmark])
    return {
        "area_m2": area_m2,
        "count": building_count,
        "landmark_count": landmark_count,
        "landmark_area_m2": landmark_area_m2,
    }


def _water_metrics(water, bbox_local, grid_size: int) -> dict:
    count = grid_size * grid_size
    water_area_m2 = np.zeros(count, dtype=float)
    shoreline_m = np.zeros(count, dtype=float)
    waterway_m = np.zeros(count, dtype=float)
    if water is None or len(water) == 0:
        return {
            "area_m2": water_area_m2,
            "shoreline_m": shoreline_m,
            "waterway_m": waterway_m,
        }

    polygons = water.loc[water.geometry.geom_type.isin(
        ("Polygon", "MultiPolygon"))]
    water_union = None
    if len(polygons):
        try:
            water_union = unary_union(
                [geometry for geometry in polygons.geometry
                 if geometry is not None and not geometry.is_empty])
        except Exception:
            water_union = None

    xmin, ymin, xmax, ymax = bbox_local
    cell_width = (xmax - xmin) / grid_size
    cell_height = (ymax - ymin) / grid_size
    frame_boundary = box(xmin, ymin, xmax, ymax).boundary.buffer(1.0)
    shoreline = None
    if water_union is not None and not water_union.is_empty:
        try:
            shoreline = water_union.boundary.difference(frame_boundary)
        except Exception:
            shoreline = water_union.boundary
    for row in range(grid_size):
        for column in range(grid_size):
            index = row * grid_size + column
            cell = box(
                xmin + column * cell_width,
                ymin + row * cell_height,
                xmin + (column + 1) * cell_width,
                ymin + (row + 1) * cell_height,
            )
            if water_union is not None and not water_union.is_empty:
                try:
                    water_area_m2[index] = water_union.intersection(cell).area
                except Exception:
                    pass
            if shoreline is not None and not shoreline.is_empty:
                try:
                    shoreline_m[index] = shoreline.intersection(cell).length
                except Exception:
                    pass

    linework = water.loc[water.geometry.geom_type.isin(
        ("LineString", "MultiLineString"))]
    for geometry in linework.geometry:
        for line in _iter_lines(geometry) or ():
            coords = np.asarray(line.coords, dtype=float)
            if len(coords) < 2:
                continue
            delta = coords[1:] - coords[:-1]
            lengths = np.hypot(delta[:, 0], delta[:, 1])
            valid = np.isfinite(lengths) & (lengths >= 1.0)
            if not valid.any():
                continue
            mids = (coords[1:] + coords[:-1]) * 0.5
            gx, gy = _cell_indexes(
                mids[valid, 0], mids[valid, 1], bbox_local, grid_size)
            np.add.at(waterway_m, gy * grid_size + gx, lengths[valid])
    return {
        "area_m2": water_area_m2,
        "shoreline_m": shoreline_m,
        "waterway_m": waterway_m,
    }


def _percentile(values, percentile: float, default=0.0) -> float:
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    return float(np.percentile(values, percentile)) if values.size else default


def _neighbor_mean(values: np.ndarray, row: int, column: int,
                   grid_size: int) -> float:
    neighbors = []
    for other_row in range(max(0, row - 1), min(grid_size, row + 2)):
        for other_column in range(max(0, column - 1), min(grid_size, column + 2)):
            if other_row == row and other_column == column:
                continue
            neighbors.append(values[other_row * grid_size + other_column])
    return float(np.mean(neighbors)) if neighbors else 0.0


def _clamp01(value: float) -> float:
    return float(np.clip(value, 0.0, 1.0))


def _polygon_parts(geometry) -> list:
    if geometry is None or geometry.is_empty:
        return []
    if geometry.geom_type == "Polygon":
        return [geometry]
    if geometry.geom_type in {"MultiPolygon", "GeometryCollection"}:
        parts = []
        for child in geometry.geoms:
            parts.extend(_polygon_parts(child))
        return parts
    return []


def _water_topology_metrics(water, bbox_local) -> dict:
    """Measure water composition without assigning replacement geometry."""

    default = {
        "polygon_components": 0,
        "largest_component_frame_fraction": 0.0,
        "largest_component_share": 0.0,
        "largest_component_elongation": 0.0,
        "largest_component_compactness": 0.0,
        "frame_edge_contacts": [],
        "frame_edge_contact_count": 0,
        "interior_hole_count": 0,
        "large_interior_hole_count": 0,
        "interior_hole_area_fraction": 0.0,
        "waterway_length_km": 0.0,
        "waterway_branch_nodes": 0,
        "coast_score": 0.0,
        "river_axis_score": 0.0,
        "water_network_score": 0.0,
        "island_field_score": 0.0,
        "confluence_score": 0.0,
    }
    if water is None or len(water) == 0:
        return default

    xmin, ymin, xmax, ymax = bbox_local
    width = max(xmax - xmin, 1.0)
    height = max(ymax - ymin, 1.0)
    frame_area = width * height
    frame = box(xmin, ymin, xmax, ymax)
    polygons = water.loc[water.geometry.geom_type.isin(
        ("Polygon", "MultiPolygon"))]
    parts = []
    if len(polygons):
        try:
            merged = unary_union([
                geometry for geometry in polygons.geometry
                if geometry is not None and not geometry.is_empty
            ]).intersection(frame)
            parts = sorted(
                _polygon_parts(merged), key=lambda item: item.area,
                reverse=True)
        except Exception:
            parts = []

    total_area = float(sum(part.area for part in parts))
    water_fraction = total_area / frame_area
    largest = parts[0] if parts else None
    largest_area = float(largest.area) if largest is not None else 0.0
    largest_share = largest_area / max(total_area, 1.0)
    elongation = 0.0
    compactness = 0.0
    edge_contacts = []
    holes = 0
    large_holes = 0
    hole_area = 0.0
    if largest is not None:
        lxmin, lymin, lxmax, lymax = largest.bounds
        component_width = max(lxmax - lxmin, 1.0)
        component_height = max(lymax - lymin, 1.0)
        elongation = max(component_width, component_height) / min(
            component_width, component_height)
        compactness = 4.0 * math.pi * largest.area / max(
            largest.length ** 2, 1.0)
        strip = max(min(width, height) * 0.002, 1.0)
        edge_strips = {
            "west": box(xmin, ymin, xmin + strip, ymax),
            "east": box(xmax - strip, ymin, xmax, ymax),
            "south": box(xmin, ymin, xmax, ymin + strip),
            "north": box(xmin, ymax - strip, xmax, ymax),
        }
        edge_contacts = [
            name for name, edge in edge_strips.items()
            if largest.intersects(edge)
        ]
    for part in parts:
        holes += len(part.interiors)
        for ring in part.interiors:
            try:
                area = float(Polygon(ring).area)
            except Exception:
                area = 0.0
            hole_area += area
            if area >= frame_area * 0.001:
                large_holes += 1

    line_length = 0.0
    endpoint_degree = defaultdict(int)
    quantization = max(min(width, height) / 2000.0, 2.0)
    lines = water.loc[water.geometry.geom_type.isin(
        ("LineString", "MultiLineString"))]
    for geometry in lines.geometry:
        for line in _iter_lines(geometry) or ():
            line_length += float(line.length)
            coords = np.asarray(line.coords, dtype=float)
            if len(coords) >= 2:
                for point in (coords[0], coords[-1]):
                    endpoint_degree[tuple(np.rint(
                        point / quantization).astype(np.int64))] += 1
    branch_nodes = sum(degree >= 3 for degree in endpoint_degree.values())

    edge_count = len(edge_contacts)
    coast_score = (
        _clamp01(water_fraction / 0.18) * 0.45
        + _clamp01(edge_count / 2.0) * 0.25
        + _clamp01(largest_share) * 0.30
    ) if edge_count else 0.0
    river_axis_score = (
        _clamp01((elongation - 1.3) / 4.0) * 0.35
        + _clamp01(edge_count / 2.0) * 0.25
        + _clamp01(water_fraction / 0.10) * 0.25
        + _clamp01(largest_share) * 0.15
    ) if 0.003 <= water_fraction <= 0.30 else 0.0
    network_density = line_length / max(math.sqrt(frame_area), 1.0)
    component_diversity = _clamp01(1.0 - largest_share)
    water_network_score = (
        component_diversity * _clamp01((len(parts) - 1) / 8.0) * 0.45
        + _clamp01(network_density / 25.0) * 0.35
        + _clamp01(branch_nodes / 40.0) * 0.20
    )
    island_field_score = (
        _clamp01(water_fraction / 0.30) * 0.30
        + _clamp01(large_holes / 3.0) * 0.45
        + _clamp01(hole_area / max(frame_area * 0.12, 1.0)) * 0.25
    )
    branch_focus = (
        _clamp01(branch_nodes / 2.0)
        * math.exp(-max(0, branch_nodes - 6) / 8.0)
    )
    confluence_score = branch_focus * _clamp01(largest_share)
    return {
        "polygon_components": len(parts),
        "largest_component_frame_fraction": round(
            largest_area / frame_area, 5),
        "largest_component_share": round(largest_share, 5),
        "largest_component_elongation": round(elongation, 4),
        "largest_component_compactness": round(compactness, 5),
        "frame_edge_contacts": edge_contacts,
        "frame_edge_contact_count": edge_count,
        "interior_hole_count": holes,
        "large_interior_hole_count": large_holes,
        "interior_hole_area_fraction": round(hole_area / frame_area, 5),
        "waterway_length_km": round(line_length / 1000.0, 4),
        "waterway_branch_nodes": branch_nodes,
        "coast_score": round(_clamp01(coast_score), 4),
        "river_axis_score": round(_clamp01(river_axis_score), 4),
        "water_network_score": round(_clamp01(water_network_score), 4),
        "island_field_score": round(_clamp01(island_field_score), 4),
        "confluence_score": round(_clamp01(confluence_score), 4),
    }


def _building_shape_metrics(buildings, bbox_local, grid_size: int,
                            nozzle_real_m: float | None,
                            model_span_mm: float | None) -> dict:
    default = {
        "footprint_count": 0,
        "footprint_frame_coverage": 0.0,
        "axis_width_p10_m": None,
        "axis_width_p50_m": None,
        "aspect_p90": None,
        "slender_footprint_fraction": None,
        "compactness_p10": None,
        "compactness_p50": None,
        "sub_nozzle_fraction": None,
        "sub_nozzle_area_fraction": None,
        "independent_survival_fraction": None,
        "independent_survival_area_fraction": None,
        "regularization_pressure": None,
        "occupied_cell_fraction": 0.0,
        "footprints_per_occupied_cell_p50": None,
        "footprints_per_occupied_cell_p90": None,
        "trusted_height_coverage": 0.0,
        "height_mass_concentration": 0.0,
        "significant_height_cells": 0,
        "block_grammar": {
            "version": "source-block-grammar-v1",
            "status": "unavailable",
            "reason": "building source is empty",
        },
    }
    if buildings is None or len(buildings) == 0:
        return default
    polygon_mask = buildings.geometry.geom_type.isin(("Polygon", "MultiPolygon"))
    work = buildings.loc[polygon_mask]
    if len(work) == 0:
        return default

    bounds = work.geometry.bounds
    widths_x = (bounds.maxx - bounds.minx).to_numpy(dtype=float)
    widths_y = (bounds.maxy - bounds.miny).to_numpy(dtype=float)
    minimum_width = np.minimum(widths_x, widths_y)
    maximum_width = np.maximum(widths_x, widths_y)
    valid_width = np.isfinite(minimum_width) & (minimum_width > 0)
    aspect = maximum_width[valid_width] / minimum_width[valid_width]
    areas = work.geometry.area.to_numpy(dtype=float)
    perimeters = work.geometry.length.to_numpy(dtype=float)
    compactness = (
        4.0 * math.pi * np.maximum(areas, 0.0)
        / np.maximum(perimeters * perimeters, 1.0)
    )
    xmin, ymin, xmax, ymax = bbox_local
    frame_area = max((xmax - xmin) * (ymax - ymin), 1.0)

    centroids = work.geometry.centroid
    cell_x, cell_y = _cell_indexes(
        centroids.x.to_numpy(), centroids.y.to_numpy(),
        bbox_local, grid_size)
    cell_counts = np.zeros(grid_size * grid_size, dtype=int)
    np.add.at(cell_counts, cell_y * grid_size + cell_x, 1)
    occupied_counts = cell_counts[cell_counts > 0]

    sub_nozzle_fraction = None
    sub_nozzle_area_fraction = None
    independent_survival_fraction = None
    independent_survival_area_fraction = None
    regularization_pressure = None
    slender_fraction = (
        float(np.mean(aspect > 4.0)) if aspect.size else None)
    if valid_width.any() and nozzle_real_m and nozzle_real_m > 0:
        printable_width = minimum_width[valid_width] >= float(nozzle_real_m)
        compact_enough = aspect <= 4.0
        independent = printable_width & compact_enough
        valid_areas = np.maximum(areas[valid_width], 0.0)
        total_valid_area = max(float(valid_areas.sum()), 1.0)
        sub_nozzle = ~printable_width
        sub_nozzle_fraction = float(np.mean(sub_nozzle))
        sub_nozzle_area_fraction = float(
            valid_areas[sub_nozzle].sum() / total_valid_area)
        independent_survival_fraction = float(np.mean(independent))
        independent_survival_area_fraction = float(
            valid_areas[independent].sum() / total_valid_area)
        # Count pressure catches a field of needles, area pressure prevents a
        # few large but narrow slabs from disappearing in the count statistic,
        # and aspect pressure catches long splinters even when their bounding
        # width barely clears the nozzle floor.  This is diagnostic evidence,
        # not a geometry mutation threshold.
        regularization_pressure = (
            0.55 * sub_nozzle_fraction
            + 0.25 * sub_nozzle_area_fraction
            + 0.20 * float(slender_fraction or 0.0)
        )

    trusted_sources = {"osm_height", "osm_levels", "wikidata", "overture"}
    if "height_source" in work.columns:
        trusted = work["height_source"].fillna("").astype(str).str.lower().isin(
            trusted_sources).to_numpy()
    else:
        trusted = np.zeros(len(work), dtype=bool)

    numeric_height = np.full(len(work), np.nan, dtype=float)
    for column in ("est_height", "height_m", "height"):
        if column not in work.columns:
            continue
        values = []
        for value in work[column]:
            try:
                values.append(float(value))
            except (TypeError, ValueError):
                values.append(float("nan"))
        candidate = np.asarray(values, dtype=float)
        fill = ~np.isfinite(numeric_height) & np.isfinite(candidate)
        numeric_height[fill] = candidate[fill]

    height_mass_concentration = 0.0
    significant_height_cells = 0
    valid_height = trusted & np.isfinite(numeric_height) & (numeric_height > 0)
    if valid_height.any():
        centroids = work.geometry.centroid
        gx, gy = _cell_indexes(
            centroids.x.to_numpy(), centroids.y.to_numpy(),
            bbox_local, grid_size)
        mass = np.zeros(grid_size * grid_size, dtype=float)
        weighted = numeric_height[valid_height] * np.maximum(
            areas[valid_height], 1.0)
        np.add.at(mass, (gy * grid_size + gx)[valid_height], weighted)
        height_mass_concentration = float(mass.max() / max(mass.sum(), 1.0))
        positive = mass[mass > 0]
        if positive.size:
            threshold = np.percentile(positive, 75)
            significant_height_cells = int(np.sum(mass >= threshold))

    footprint_frame_coverage = float(np.nansum(areas)) / frame_area
    occupied_cell_fraction = (
        float(occupied_counts.size) / max(grid_size * grid_size, 1))
    result = {
        "footprint_count": len(work),
        "footprint_frame_coverage": round(footprint_frame_coverage, 5),
        "axis_width_p10_m": (
            round(_percentile(minimum_width[valid_width], 10), 3)
            if valid_width.any() else None),
        "axis_width_p50_m": (
            round(_percentile(minimum_width[valid_width], 50), 3)
            if valid_width.any() else None),
        "aspect_p90": (
            round(_percentile(aspect, 90), 3) if aspect.size else None),
        "slender_footprint_fraction": (
            round(slender_fraction, 5)
            if slender_fraction is not None else None),
        "compactness_p10": (
            round(_percentile(compactness, 10), 5)
            if compactness.size else None),
        "compactness_p50": (
            round(_percentile(compactness, 50), 5)
            if compactness.size else None),
        "sub_nozzle_fraction": (
            round(sub_nozzle_fraction, 5)
            if sub_nozzle_fraction is not None else None),
        "sub_nozzle_area_fraction": (
            round(sub_nozzle_area_fraction, 5)
            if sub_nozzle_area_fraction is not None else None),
        "independent_survival_fraction": (
            round(independent_survival_fraction, 5)
            if independent_survival_fraction is not None else None),
        "independent_survival_area_fraction": (
            round(independent_survival_area_fraction, 5)
            if independent_survival_area_fraction is not None else None),
        "regularization_pressure": (
            round(_clamp01(regularization_pressure), 5)
            if regularization_pressure is not None else None),
        "occupied_cell_fraction": round(occupied_cell_fraction, 5),
        "footprints_per_occupied_cell_p50": (
            round(_percentile(occupied_counts, 50), 3)
            if occupied_counts.size else None),
        "footprints_per_occupied_cell_p90": (
            round(_percentile(occupied_counts, 90), 3)
            if occupied_counts.size else None),
        "trusted_height_coverage": round(float(np.mean(trusted)), 5),
        "height_mass_concentration": round(height_mass_concentration, 5),
        "significant_height_cells": significant_height_cells,
    }
    result["block_grammar"] = measure_source_block_grammar(
        work,
        bbox_local,
        model_span_mm=model_span_mm,
        nozzle_real_m=nozzle_real_m,
        footprint_frame_coverage=footprint_frame_coverage,
        occupied_cell_fraction=occupied_cell_fraction,
    )
    return result


def _terrain_metrics(elevation_grid, bbox_local) -> dict:
    default = {
        "status": "unavailable",
        "elevation_p05_m": None,
        "elevation_p95_m": None,
        "relief_p90_m": None,
        "relief_to_span": 0.0,
        "slope_p90_deg": None,
        "rugged_fraction": 0.0,
        "buildable_low_slope_fraction": 0.0,
        "landform": analyze_landform_character(None, bbox_local),
    }
    if elevation_grid is None:
        return default
    try:
        values = np.asarray(elevation_grid, dtype=float)
    except Exception:
        return default
    if values.ndim != 2 or min(values.shape) < 2:
        return default
    finite = np.isfinite(values)
    if not finite.any():
        return default
    filled = values.copy()
    filled[~finite] = float(np.median(values[finite]))
    p05 = float(np.percentile(filled, 5))
    p95 = float(np.percentile(filled, 95))
    relief = max(0.0, p95 - p05)
    xmin, ymin, xmax, ymax = bbox_local
    width = max(xmax - xmin, 1.0)
    height = max(ymax - ymin, 1.0)
    dy = height / max(values.shape[0] - 1, 1)
    dx = width / max(values.shape[1] - 1, 1)
    grad_y, grad_x = np.gradient(filled, dy, dx)
    slope = np.degrees(np.arctan(np.hypot(grad_x, grad_y)))
    return {
        "status": "ready",
        "elevation_p05_m": round(p05, 3),
        "elevation_p95_m": round(p95, 3),
        "relief_p90_m": round(relief, 3),
        "relief_to_span": round(relief / max(width, height), 6),
        "slope_p90_deg": round(float(np.percentile(slope, 90)), 3),
        "rugged_fraction": round(float(np.mean(slope >= 15.0)), 5),
        "buildable_low_slope_fraction": round(float(np.mean(slope <= 5.0)), 5),
        "landform": analyze_landform_character(filled, bbox_local),
    }


def analyze_scene_character(roads, buildings, water, bbox_local,
                            *, grid_size: int = 8,
                            elevation_grid=None,
                            nozzle_real_m: float | None = None,
                            model_span_mm: float | None = None,
                            external_urban=None) -> dict:
    """Return transparent local evidence and relative scene roles.

    Inputs must use a projected metric CRS.  Roles are relative to this frame;
    they are evidence for review, not universal land-use truth.
    """

    if grid_size < 3 or grid_size > 16:
        raise ValueError("grid_size must be between 3 and 16")
    xmin, ymin, xmax, ymax = (float(value) for value in bbox_local)
    if xmax <= xmin or ymax <= ymin:
        raise ValueError("bbox_local must have positive width and height")
    bbox_local = (xmin, ymin, xmax, ymax)
    cell_area_m2 = ((xmax - xmin) * (ymax - ymin)
                    / (grid_size * grid_size))

    road = _line_metrics(roads, bbox_local, grid_size)
    building = _building_metrics(buildings, bbox_local, grid_size)
    water_data = _water_metrics(water, bbox_local, grid_size)
    water_topology = _water_topology_metrics(water, bbox_local)
    building_shapes = _building_shape_metrics(
        buildings, bbox_local, grid_size, nozzle_real_m, model_span_mm)
    terrain = _terrain_metrics(elevation_grid, bbox_local)

    water_fraction = np.clip(water_data["area_m2"] / cell_area_m2, 0.0, 1.0)
    # Use a floor so a bridge in a nearly all-water cell does not report an
    # absurd road density; the cell is still classified by its water evidence.
    effective_land_km2 = np.maximum(
        cell_area_m2 * np.maximum(1.0 - water_fraction, 0.25) / 1e6,
        1e-6,
    )
    road_density = road["structural_length_m"] / 1000.0 / effective_land_km2
    major_density = road["major_length_m"] / 1000.0 / effective_land_km2
    junction_density = road["junction_count"] / effective_land_km2
    building_coverage = np.clip(
        building["area_m2"] / np.maximum(
            cell_area_m2 * (1.0 - water_fraction), cell_area_m2 * 0.25),
        0.0, 1.0,
    )
    landmark_score = (
        np.log1p(building["landmark_count"])
        * np.sqrt(np.clip(
            building["landmark_area_m2"] / cell_area_m2, 0.0, 1.0))
    )
    # OSM often maps a campus, palace or station as hundreds of tagged
    # building parts.  "Has one landmark tag" therefore cannot make a cell a
    # focus.  Rank aggregate evidence and keep at most the strongest 5% of
    # frame cells as diagnostic focus candidates.
    landmark_focus = np.zeros(grid_size * grid_size, dtype=bool)
    eligible_landmarks = np.flatnonzero(
        (landmark_score > 0)
        & (building["landmark_area_m2"]
           >= max(1500.0, cell_area_m2 * 0.001))
    )
    landmark_focus_limit = max(1, int(math.ceil(grid_size * grid_size * 0.05)))
    if eligible_landmarks.size:
        ranked = sorted(
            eligible_landmarks.tolist(),
            key=lambda index: (-landmark_score[index], index),
        )[:landmark_focus_limit]
        landmark_focus[ranked] = True

    land = water_fraction < 0.80
    land_values = lambda values: values[land]
    quantiles = {
        "road_density_p25": _percentile(land_values(road_density), 25),
        "road_density_p50": _percentile(land_values(road_density), 50),
        "road_density_p75": _percentile(land_values(road_density), 75),
        "major_density_p75": _percentile(land_values(major_density), 75),
        "building_coverage_p25": _percentile(
            land_values(building_coverage), 25),
        "building_coverage_p50": _percentile(
            land_values(building_coverage), 50),
        "building_coverage_p75": _percentile(
            land_values(building_coverage), 75),
        "junction_density_p60": _percentile(
            land_values(junction_density), 60),
        "junction_density_p75": _percentile(
            land_values(junction_density), 75),
    }

    def normalized(values, reference, floor):
        return np.clip(values / max(reference, floor), 0.0, 1.0)

    urban_signal = (
        0.45 * normalized(
            building_coverage, quantiles["building_coverage_p75"], 0.02)
        + 0.35 * normalized(
            junction_density, quantiles["junction_density_p75"], 1.0)
        + 0.20 * normalized(
            road_density, quantiles["road_density_p75"], 0.1)
    )

    cells = []
    role_counts = defaultdict(int)
    cell_width = (xmax - xmin) / grid_size
    cell_height = (ymax - ymin) / grid_size
    for row in range(grid_size):
        for column in range(grid_size):
            index = row * grid_size + column
            neighbor_urban = _neighbor_mean(
                urban_signal, row, column, grid_size)
            neighbor_water = _neighbor_mean(
                water_fraction, row, column, grid_size)
            roles = []
            if water_fraction[index] >= 0.65:
                roles.append("water")
            if (0.05 <= water_fraction[index] < 0.65
                    or (neighbor_water >= 0.20
                        and water_fraction[index] < 0.65)):
                roles.append("waterfront")
            if landmark_focus[index]:
                roles.append("landmark_focus")
            if (land[index]
                    and building_coverage[index]
                    >= max(0.02, quantiles["building_coverage_p75"])
                    and (junction_density[index]
                         >= max(1.0, quantiles["junction_density_p60"])
                         or road_density[index]
                         >= quantiles["road_density_p75"])):
                roles.append("dense_core")
            if (land[index]
                    and road["orientation_concentration"][index] >= 0.42
                    and road_density[index]
                    >= max(0.1, quantiles["road_density_p50"])):
                roles.append("grid")
            if (land[index]
                    and major_density[index]
                    >= max(0.05, quantiles["major_density_p75"])
                    and major_density[index]
                    >= road_density[index] * 0.20):
                roles.append("arterial")
            possible_gap = (
                land[index]
                and water_fraction[index] < 0.10
                and urban_signal[index] <= 0.18
                and neighbor_urban >= 0.52
            )
            if possible_gap:
                roles.append("possible_data_gap")
            if (land[index] and urban_signal[index] <= 0.25
                    and not possible_gap):
                roles.append("sparse")
            if not roles:
                roles.append("background")

            priority = (
                "possible_data_gap", "water", "landmark_focus", "waterfront",
                "dense_core", "grid", "arterial", "sparse", "background",
            )
            dominant = next(role for role in priority if role in roles)
            role_counts[dominant] += 1
            cells.append({
                "row": row,
                "column": column,
                "bounds": [
                    round(xmin + column * cell_width, 3),
                    round(ymin + row * cell_height, 3),
                    round(xmin + (column + 1) * cell_width, 3),
                    round(ymin + (row + 1) * cell_height, 3),
                ],
                "dominant_role": dominant,
                "roles": roles,
                "road_density_km_km2": round(road_density[index], 3),
                "major_road_density_km_km2": round(major_density[index], 3),
                "junction_density_km2": round(junction_density[index], 3),
                "orientation_concentration": round(
                    road["orientation_concentration"][index], 4),
                "orientation_entropy": round(
                    road["orientation_entropy"][index], 4),
                "building_coverage": round(building_coverage[index], 5),
                "building_count": int(building["count"][index]),
                "landmark_evidence_features": int(
                    building["landmark_count"][index]),
                "landmark_focus_score": round(landmark_score[index], 5),
                "water_fraction": round(water_fraction[index], 5),
                "shoreline_km": round(
                    water_data["shoreline_m"][index] / 1000.0, 3),
                "waterway_density_km_km2": round(
                    water_data["waterway_m"][index] / 1000.0
                    / effective_land_km2[index], 3),
                "urban_signal": round(urban_signal[index], 4),
                "neighbor_urban_signal": round(neighbor_urban, 4),
            })

    building_data_quality = resolve_local_building_quality(
        cells,
        grid_size=grid_size,
        building_metrics=building_shapes,
        terrain_metrics=terrain,
        external_urban=external_urban,
    )

    cell_total = grid_size * grid_size
    role_fractions = {
        role: round(value / cell_total, 4)
        for role, value in sorted(role_counts.items())
    }
    weighted_grid_score = float(np.average(
        road["orientation_concentration"],
        weights=np.maximum(road["structural_length_m"], 1.0),
    )) if cell_total else 0.0
    ring_score = _clamp01(
        road["ring_identity_count"] * 0.28
        + road["closed_ring_candidates"] * 0.45)
    radial_score = _clamp01(
        (road["radial_alignment"] - 0.58) / 0.24
        * _clamp01(road["global_direction_entropy"] / 0.75))
    road_structure = {
        "orthogonal_grid_score": round(weighted_grid_score, 4),
        "global_direction_entropy": round(
            float(road["global_direction_entropy"]), 4),
        "radial_alignment": round(float(road["radial_alignment"]), 4),
        "radial_score": round(radial_score, 4),
        "ring_identity_count": int(road["ring_identity_count"]),
        "closed_ring_candidates": int(road["closed_ring_candidates"]),
        "ring_score": round(ring_score, 4),
        "major_corridor_length_km": round(
            float(np.sum(road["major_length_m"])) / 1000.0, 4),
        "structural_road_length_km": round(
            float(np.sum(road["structural_length_m"])) / 1000.0, 4),
    }
    traits = []
    if float(np.mean(water_fraction)) >= 0.12:
        traits.append("water_led")
    if role_fractions.get("waterfront", 0.0) >= 0.08:
        traits.append("waterfront_composition")
    grid_cell_fraction = sum("grid" in cell["roles"] for cell in cells) / cell_total
    if grid_cell_fraction >= 0.20:
        traits.append("grid_structure")
    dense_cell_fraction = sum(
        "dense_core" in cell["roles"] for cell in cells) / cell_total
    if dense_cell_fraction >= 0.10:
        traits.append("dense_urban_core")
    if sum(building["landmark_count"]) > 0:
        traits.append("landmark_evidence")
    if water_topology["coast_score"] >= 0.60:
        traits.append("coast_structure")
    if water_topology["river_axis_score"] >= 0.55:
        traits.append("river_axis")
    if water_topology["water_network_score"] >= 0.45:
        traits.append("water_network")
    if water_topology["confluence_score"] >= 0.55:
        traits.append("confluence")
    if ring_score >= 0.45:
        traits.append("ring_structure")
    if radial_score >= 0.55:
        traits.append("radial_structure")
    if (terrain["status"] == "ready"
            and (terrain["rugged_fraction"] >= 0.15
                 or terrain["relief_to_span"] >= 0.015)):
        traits.append("terrain_led")
    landform_scores = ((terrain.get("landform") or {}).get("scores") or {})
    for score_key, trait in (
        ("isolated_prominence", "isolated_landform"),
        ("iconic_peak", "iconic_peak"),
        ("ridge_network", "ridge_network"),
        ("crater_rim", "crater_rim"),
        ("canyon_valley", "canyon_valley"),
        ("repeated_cones", "volcanic_field_candidate"),
        ("open_plain", "open_plain"),
    ):
        if float(landform_scores.get(score_key) or 0.0) >= 0.55:
            traits.append(trait)
    if building_shapes["height_mass_concentration"] >= 0.28:
        traits.append("compact_height_core")
    if ((building_shapes.get("regularization_pressure") or 0.0) >= 0.55):
        traits.append("building_mass_regularization_needed")

    local_hole_count = sum(
        cell["dominant_role"] == "possible_data_gap" for cell in cells)
    quality_gap_count = int(
        building_data_quality["summary"]["suspected_building_gap_cells"])
    gap_count = max(local_hole_count, quality_gap_count)
    if buildings is None or len(buildings) == 0:
        confidence = "low"
        confidence_reason = "building evidence is missing"
    elif roads is None or len(roads) == 0:
        confidence = "low"
        confidence_reason = "road evidence is missing"
    elif gap_count / max(1, int(land.sum())) > 0.08:
        confidence = "medium"
        confidence_reason = (
            "local road/building contradictions need external validation")
    else:
        confidence = "high"
        confidence_reason = "roads and buildings are present without many local holes"

    return {
        "version": SCENE_ANALYSIS_VERSION,
        "grid_size": grid_size,
        "bbox_projected_m": [xmin, ymin, xmax, ymax],
        "cell_area_km2": round(cell_area_m2 / 1e6, 5),
        "feature_counts": {
            "roads": 0 if roads is None else len(roads),
            "buildings": 0 if buildings is None else len(buildings),
            "water": 0 if water is None else len(water),
        },
        "summary": {
            "traits": traits,
            "role_fractions": role_fractions,
            "water_fraction": round(float(np.mean(water_fraction)), 5),
            "grid_cell_fraction": round(grid_cell_fraction, 4),
            "dense_core_cell_fraction": round(dense_cell_fraction, 4),
            "possible_data_gap_cells": gap_count,
            "neighbor_hole_cells": local_hole_count,
            "suspected_building_gap_cells": quality_gap_count,
            "landmark_evidence_features": int(
                sum(building["landmark_count"])),
            "landmark_focus_cells": int(landmark_focus.sum()),
            "landmark_focus_cell_limit": landmark_focus_limit,
            "quantiles": {key: round(value, 5)
                          for key, value in quantiles.items()},
            "osm_internal_consistency": confidence,
            "consistency_reason": confidence_reason,
            "warning": (
                "Scene character is deterministic evidence, not an aesthetic "
                "verdict; sparse and incomplete data still require confidence "
                "checks before a generation policy consumes the report."
            ),
        },
        "metrics": {
            "water_topology": water_topology,
            "road_structure": road_structure,
            "buildings": building_shapes,
            "building_data_quality": building_data_quality,
            "terrain": terrain,
            "data_confidence": {
                "osm_internal_consistency": confidence,
                "reason": confidence_reason,
                "possible_data_gap_cells": gap_count,
            },
        },
        "cells": cells,
    }


def write_scene_character(output_dir: os.PathLike | str,
                          report: Mapping, *,
                          filename: str = "scene_character.json") -> str:
    """Atomically persist an optionally attempt-scoped scene report."""

    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    if Path(filename).name != filename or not filename.endswith(".json"):
        raise ValueError("scene character filename must be a plain JSON name")
    destination = directory / filename
    payload = json.dumps(report, ensure_ascii=False,
                         indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=directory,
        prefix=".scene_character.", suffix=".tmp", delete=False,
    ) as stream:
        temporary = Path(stream.name)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, destination)
    return str(destination)


def refresh_building_data_quality(report: dict) -> dict:
    """Re-resolve local building quality after external evidence changes.

    Scene analysis initially runs without optional cross-source evidence.  A
    caller may then attach AMap or another evidence-only summary.  This helper
    keeps the local strategy, gap counters and confidence record synchronized
    without re-reading or changing any source geometry.
    """

    metrics = report.get("metrics", {})
    cells = report.get("cells", ())
    quality = resolve_local_building_quality(
        cells,
        grid_size=int(report["grid_size"]),
        building_metrics=metrics.get("buildings"),
        terrain_metrics=metrics.get("terrain"),
        external_urban=metrics.get("external_urban"),
    )
    metrics["building_data_quality"] = quality
    summary = report.setdefault("summary", {})
    quality_gaps = int(
        quality["summary"]["suspected_building_gap_cells"])
    neighbor_gaps = int(summary.get("neighbor_hole_cells") or 0)
    gap_count = max(neighbor_gaps, quality_gaps)
    summary["suspected_building_gap_cells"] = quality_gaps
    summary["possible_data_gap_cells"] = gap_count

    feature_counts = report.get("feature_counts", {})
    land_cells = sum(
        float(cell.get("water_fraction") or 0.0) < 0.65
        for cell in cells)
    if int(feature_counts.get("buildings") or 0) == 0:
        confidence = "low"
        reason = "building evidence is missing"
    elif int(feature_counts.get("roads") or 0) == 0:
        confidence = "low"
        reason = "road evidence is missing"
    elif gap_count / max(1, land_cells) > 0.08:
        confidence = "medium"
        reason = "local road/building contradictions need external validation"
    else:
        confidence = "high"
        reason = "roads and buildings are present without many local holes"
    summary["osm_internal_consistency"] = confidence
    summary["consistency_reason"] = reason
    metrics["data_confidence"] = {
        "osm_internal_consistency": confidence,
        "reason": reason,
        "possible_data_gap_cells": gap_count,
    }
    return quality


def render_scene_character(report: dict, output_path) -> str:
    """Render a four-panel PNG from an analysis report."""

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import ListedColormap

    grid_size = int(report["grid_size"])
    shape = (grid_size, grid_size)
    roles = [
        "background", "sparse", "arterial", "grid", "dense_core",
        "waterfront", "landmark_focus", "water", "possible_data_gap",
    ]
    colors = [
        "#ebe7df", "#d8d3ca", "#d47b4d", "#6d8f82", "#9b4d3f",
        "#4d88a8", "#d7a22a", "#1f3f5b", "#cc2f45",
    ]
    role_index = {role: index for index, role in enumerate(roles)}
    dominant = np.zeros(shape, dtype=int)
    road_density = np.zeros(shape, dtype=float)
    building = np.zeros(shape, dtype=float)
    water = np.zeros(shape, dtype=float)
    gaps = []
    for cell in report["cells"]:
        row, column = int(cell["row"]), int(cell["column"])
        dominant[row, column] = role_index[cell["dominant_role"]]
        road_density[row, column] = cell["road_density_km_km2"]
        building[row, column] = cell["building_coverage"] * 100.0
        water[row, column] = cell["water_fraction"] * 100.0
        if cell["dominant_role"] == "possible_data_gap":
            gaps.append((column, row))

    cross_source = report.get("cross_source_water") or {}
    compact_water_gaps = []
    linear_water_gaps = []
    if cross_source.get("status") == "evidence_only":
        candidate_cells = cross_source.get("candidate_cells", [])
        compact_cells = sorted(
            candidate_cells,
            key=lambda cell: -float(cell.get("compact_gap_area_m2", 0.0)),
        )[:8]
        linear_cells = sorted(
            candidate_cells,
            key=lambda cell: -float(cell.get("linear_gap_area_m2", 0.0)),
        )[:6]
        for cell in compact_cells:
            point = (int(cell["column"]), int(cell["row"]))
            if float(cell.get("compact_gap_area_m2", 0.0)) > 0:
                compact_water_gaps.append(point)
        for cell in linear_cells:
            point = (int(cell["column"]), int(cell["row"]))
            if float(cell.get("linear_gap_area_m2", 0.0)) > 0:
                linear_water_gaps.append(point)

    fig, axes = plt.subplots(2, 2, figsize=(15, 13), constrained_layout=True)
    role_image = axes[0, 0].imshow(
        dominant, origin="lower", cmap=ListedColormap(colors),
        vmin=-0.5, vmax=len(roles) - 0.5)
    axes[0, 0].set_title("Relative local role")
    handles = [plt.Line2D([0], [0], marker="s", linestyle="", color=color,
                          label=role, markersize=9)
               for role, color in zip(roles, colors)
               if role in {cell["dominant_role"] for cell in report["cells"]}]
    axes[0, 0].legend(handles=handles, loc="upper left", fontsize=8,
                      framealpha=0.9)

    road_image = axes[0, 1].imshow(
        np.log1p(road_density), origin="lower", cmap="magma")
    axes[0, 1].set_title("Structural road density (log km / km2)")
    fig.colorbar(road_image, ax=axes[0, 1], fraction=0.046)

    building_image = axes[1, 0].imshow(
        building, origin="lower", cmap="YlOrBr", vmin=0,
        vmax=max(5.0, _percentile(building, 95)))
    axes[1, 0].set_title("Building footprint coverage (%)")
    fig.colorbar(building_image, ax=axes[1, 0], fraction=0.046)

    water_image = axes[1, 1].imshow(
        water, origin="lower", cmap="Blues", vmin=0, vmax=100)
    water_title = "OSM water coverage (%) and gap evidence"
    if cross_source.get("status") == "evidence_only":
        water_title += (
            "\nAMap-only candidates: "
            f"{cross_source.get('compact_gap_count', 0)} compact / "
            f"{cross_source.get('linear_or_noise_gap_count', 0)} linear")
    elif cross_source.get("status") in {"unavailable", "error"}:
        water_title += f"\nAMap cross-check: {cross_source['status']}"
    axes[1, 1].set_title(water_title)
    if gaps:
        axes[1, 1].scatter(
            [x for x, _ in gaps], [y for _, y in gaps],
            marker="x", s=90, linewidths=2.0, color="#cc2f45",
            label="OSM internal urban hole")
    if compact_water_gaps:
        axes[1, 1].scatter(
            [x for x, _ in compact_water_gaps],
            [y for _, y in compact_water_gaps],
            marker="o", facecolors="none", edgecolors="#e58b2a",
            s=125, linewidths=2.2, label="largest AMap-only compact water")
    if linear_water_gaps:
        axes[1, 1].scatter(
            [x for x, _ in linear_water_gaps],
            [y for _, y in linear_water_gaps],
            marker="+", s=125, linewidths=2.2, color="#8d3f8f",
            label="largest AMap-only linear / noise")
    if gaps or compact_water_gaps or linear_water_gaps:
        axes[1, 1].legend(loc="upper left", fontsize=8, framealpha=0.9)
    fig.colorbar(water_image, ax=axes[1, 1], fraction=0.046)

    for axis in axes.flat:
        axis.set_xticks(range(grid_size))
        axis.set_yticks(range(grid_size))
        axis.set_xlabel("west to east")
        axis.set_ylabel("south to north")
        axis.grid(which="major", color="white", linewidth=0.35, alpha=0.45)

    summary = report["summary"]
    fig.suptitle(
        "Scene character diagnostic — "
        + ", ".join(summary["traits"] or ["no strong trait"])
        + f"\nOSM internal consistency: {summary['osm_internal_consistency']}; "
          f"water {summary['water_fraction'] * 100:.1f}%; "
          f"possible gaps {summary['possible_data_gap_cells']}",
        fontsize=15,
    )
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=150)
    plt.close(fig)
    return str(output_path)


def render_building_data_quality(report: Mapping, output_path) -> str:
    """Render the local building representation strategy as audit evidence."""

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import ListedColormap

    quality = (report.get("metrics", {}).get("building_data_quality", {})
               or {})
    if quality.get("status") != "ready":
        raise ValueError("building_data_quality evidence is not ready")
    grid_size = int(quality["grid_size"])
    strategies = [
        "open_space_preserve", "block_base_support", "hybrid_mass",
        "neighborhood_mass", "preserve_printable_footprints", "water",
    ]
    colors = [
        "#ece8df", "#d06b45", "#d5a94f", "#6f9185", "#6d7f9b", "#244e70",
    ]
    indexes = {name: index for index, name in enumerate(strategies)}
    strategy_grid = np.zeros((grid_size, grid_size), dtype=int)
    completeness = np.zeros((grid_size, grid_size), dtype=float)
    contradiction = np.zeros((grid_size, grid_size), dtype=float)
    for cell in quality["cells"]:
        row, column = int(cell["row"]), int(cell["column"])
        strategy_grid[row, column] = indexes[cell["strategy"]]
        completeness[row, column] = cell["scores"]["data_completeness"]
        contradiction[row, column] = cell["scores"][
            "road_building_contradiction"]

    fig, axes = plt.subplots(1, 3, figsize=(18, 6), constrained_layout=True)
    axes[0].imshow(
        strategy_grid, origin="lower", cmap=ListedColormap(colors),
        vmin=-0.5, vmax=len(strategies) - 0.5)
    axes[0].set_title("Local representation strategy")
    present = {cell["strategy"] for cell in quality["cells"]}
    handles = [
        plt.Line2D([0], [0], marker="s", linestyle="", color=color,
                   label=name, markersize=9)
        for name, color in zip(strategies, colors) if name in present
    ]
    axes[0].legend(handles=handles, loc="upper left", fontsize=8,
                   framealpha=0.9)

    complete_image = axes[1].imshow(
        completeness, origin="lower", cmap="YlGn", vmin=0, vmax=1)
    axes[1].set_title("Building data completeness")
    fig.colorbar(complete_image, ax=axes[1], fraction=0.046)

    conflict_image = axes[2].imshow(
        contradiction, origin="lower", cmap="OrRd", vmin=0, vmax=1)
    axes[2].set_title("Road / building contradiction")
    fig.colorbar(conflict_image, ax=axes[2], fraction=0.046)

    for axis in axes:
        axis.set_xticks(range(grid_size))
        axis.set_yticks(range(grid_size))
        axis.set_xlabel("west to east")
        axis.set_ylabel("south to north")
        axis.grid(which="major", color="white", linewidth=0.4, alpha=0.55)
    summary = quality["summary"]
    fig.suptitle(
        "Local building-data strategy — "
        f"completeness {summary['local_data_completeness_score']:.3f}; "
        f"continuity {summary['building_distribution_continuity_score']:.3f}; "
        f"suspected gaps {summary['suspected_building_gap_cells']}",
        fontsize=14,
    )
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=150)
    plt.close(fig)
    return str(output_path)
