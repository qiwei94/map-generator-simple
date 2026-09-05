#!/usr/bin/env python3
"""Read-only observer for the block grammar of reference city 3MFs.

The observer measures compact white-relief components without importing their
geometry into the generator.  It reports raw distributions first and adds a
small, explicitly provisional morphology label for comparison.  The label is
not a generation instruction and is never consumed by the production pipeline.

Measured dimensions:

* spatial fullness and continuity across a fixed model-space grid;
* component short-axis granularity and component density;
* single-axis and orthogonal orientation coherence;
* solidity, rectangularity, perimeter excess and meaningful outline turns;
* a centroid/size based nearest-neighbour gap proxy;
* distinct quiet and standard white-relief height tiers.

Semantic boundary: Bambu/Orca material channels identify white relief, not
"buildings" with certainty.  Full-frame carriers, very elongated linework and
tiny top surfaces are excluded before measuring the compact-mass population.
"""

from __future__ import annotations

import argparse
import csv
from io import StringIO
import json
import math
from pathlib import Path
import textwrap
from typing import Iterable, Mapping, Sequence
import zipfile
from xml.etree import ElementTree as ET

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy.spatial import cKDTree
from shapely.geometry import Polygon
from shapely.ops import unary_union

from tools.analyze_reference_3mf_shapes import (
    _component_labels,
    silhouette_metrics,
)
from tools.analyze_reference_3mf_style import (
    _city_model_scale,
    _local_name,
    _material_role,
    _percentiles,
    _project_settings,
    _volume_settings,
)


SCHEMA_VERSION = "reference-block-grammar-observer-v1"


def _rounded(value, digits: int = 5):
    if value is None:
        return None
    return round(float(value), digits)


def _safe_percentiles(values: Iterable[float]) -> dict:
    finite = [float(value) for value in values if math.isfinite(float(value))]
    return _percentiles(finite)


def _rotated_geometry_metrics(geometry) -> dict | None:
    if geometry is None or geometry.is_empty or geometry.area <= 1e-9:
        return None
    rectangle = geometry.minimum_rotated_rectangle
    coords = np.asarray(rectangle.exterior.coords[:-1], dtype=float)
    if len(coords) != 4:
        return None
    vectors = np.roll(coords, -1, axis=0) - coords
    lengths = np.linalg.norm(vectors, axis=1)
    if not np.all(np.isfinite(lengths)) or float(lengths.max()) <= 1e-9:
        return None
    long_index = int(np.argmax(lengths))
    long_axis = float(lengths[long_index])
    short_axis = float(np.min(lengths))
    vector = vectors[long_index]
    angle = math.degrees(math.atan2(float(vector[1]), float(vector[0]))) % 180.0
    rectangle_area = max(short_axis * long_axis, 1e-9)
    return {
        "short_axis_mm": short_axis,
        "long_axis_mm": long_axis,
        "aspect_ratio": long_axis / max(short_axis, 1e-9),
        "orientation_deg": angle,
        "rectangularity": float(geometry.area / rectangle_area),
        "centroid_x_mm": float(geometry.centroid.x),
        "centroid_y_mm": float(geometry.centroid.y),
    }


def orientation_metrics(samples: Sequence[Mapping]) -> dict:
    """Return rotation-invariant alignment evidence for compact components."""

    angles = []
    weights = []
    for sample in samples:
        angle = sample.get("orientation_deg")
        aspect = float(sample.get("aspect_ratio") or 1.0)
        area = float(sample.get("area_mm2") or 0.0)
        if angle is None or not math.isfinite(float(angle)) or area <= 0:
            continue
        # Nearly square components do not carry reliable orientation.  Keep
        # the decision continuous instead of using a hard aspect cutoff.
        confidence = min(1.0, max(0.0, (aspect - 1.05) / 0.75))
        weight = area * confidence
        if weight <= 0:
            continue
        angles.append(math.radians(float(angle)))
        weights.append(weight)
    if not weights:
        return {
            "oriented_sample_count": 0,
            "single_axis_coherence": None,
            "orthogonal_coherence": None,
            "orientation_entropy_mod_90": None,
            "dominant_orientation_mod_90_deg": None,
        }
    theta = np.asarray(angles, dtype=float)
    weight_array = np.asarray(weights, dtype=float)
    weight_array /= float(weight_array.sum())
    single_vector = np.sum(weight_array * np.exp(2j * theta))
    orthogonal_vector = np.sum(weight_array * np.exp(4j * theta))
    mod_90 = np.mod(np.degrees(theta), 90.0)
    histogram, _ = np.histogram(
        mod_90, bins=np.linspace(0.0, 90.0, 13), weights=weight_array)
    nonzero = histogram[histogram > 0]
    entropy = -float(np.sum(nonzero * np.log(nonzero))) / math.log(12.0)
    dominant = (math.degrees(np.angle(orthogonal_vector)) / 4.0) % 90.0
    return {
        "oriented_sample_count": len(weights),
        "single_axis_coherence": _rounded(abs(single_vector)),
        "orthogonal_coherence": _rounded(abs(orthogonal_vector)),
        "orientation_entropy_mod_90": _rounded(entropy),
        "dominant_orientation_mod_90_deg": _rounded(dominant, 3),
    }


def _largest_grid_cluster(occupied: np.ndarray) -> int:
    seen = np.zeros_like(occupied, dtype=bool)
    largest = 0
    height, width = occupied.shape
    for row in range(height):
        for column in range(width):
            if not occupied[row, column] or seen[row, column]:
                continue
            stack = [(row, column)]
            seen[row, column] = True
            size = 0
            while stack:
                current_row, current_column = stack.pop()
                size += 1
                for next_row, next_column in (
                    (current_row - 1, current_column),
                    (current_row + 1, current_column),
                    (current_row, current_column - 1),
                    (current_row, current_column + 1),
                ):
                    if (0 <= next_row < height and 0 <= next_column < width
                            and occupied[next_row, next_column]
                            and not seen[next_row, next_column]):
                        seen[next_row, next_column] = True
                        stack.append((next_row, next_column))
            largest = max(largest, size)
    return largest


def spatial_metrics(
    population: Sequence[Mapping],
    frame_bounds: Sequence[float],
    *,
    grid_size: int = 8,
) -> tuple[dict, np.ndarray]:
    """Measure distribution using all cheap component population records."""

    if len(frame_bounds) != 4 or grid_size < 2:
        raise ValueError("frame_bounds and grid_size are invalid")
    minx, miny, maxx, maxy = [float(value) for value in frame_bounds]
    span_x = max(maxx - minx, 1e-9)
    span_y = max(maxy - miny, 1e-9)
    grid = np.zeros((grid_size, grid_size), dtype=float)
    centers = []
    radii = []
    for component in population:
        x = float(component["center_x_mm"])
        y = float(component["center_y_mm"])
        area = max(float(component.get("top_area_mm2") or 0.0), 0.0)
        column = min(grid_size - 1, max(
            0, int((x - minx) / span_x * grid_size)))
        row = min(grid_size - 1, max(
            0, int((y - miny) / span_y * grid_size)))
        grid[row, column] += area
        centers.append((x, y))
        radii.append(0.5 * math.sqrt(max(area, 0.0) / math.pi))

    occupied = grid > 1e-9
    occupied_count = int(occupied.sum())
    total_cells = grid_size * grid_size
    total_area = float(grid.sum())
    frame_area = span_x * span_y
    probabilities = grid.ravel() / total_area if total_area > 0 else np.zeros(
        total_cells, dtype=float)
    nonzero = probabilities[probabilities > 0]
    entropy = (
        -float(np.sum(nonzero * np.log(nonzero))) / math.log(total_cells)
        if len(nonzero) else None)
    positive_loads = grid[occupied]
    load_cv = (
        float(np.std(positive_loads) / max(np.mean(positive_loads), 1e-9))
        if len(positive_loads) else None)
    boundary_mask = np.zeros_like(occupied)
    boundary_mask[[0, -1], :] = True
    boundary_mask[:, [0, -1]] = True
    boundary_count = int(boundary_mask.sum())

    gap_values = []
    if len(centers) >= 2:
        center_array = np.asarray(centers, dtype=float)
        tree = cKDTree(center_array)
        distances, indices = tree.query(center_array, k=2)
        radius_array = np.asarray(radii, dtype=float)
        nearest = indices[:, 1]
        gap_values = np.maximum(
            distances[:, 1] - radius_array - radius_array[nearest], 0.0)

    largest = _largest_grid_cluster(occupied)
    metrics = {
        "grid_size": grid_size,
        "component_population_count": len(population),
        "occupied_cell_fraction": _rounded(occupied_count / total_cells),
        "largest_occupied_cluster_fraction": _rounded(
            largest / occupied_count) if occupied_count else None,
        "boundary_cell_occupancy_fraction": _rounded(
            int(np.logical_and(occupied, boundary_mask).sum()) / boundary_count),
        "cell_load_entropy": _rounded(entropy),
        "occupied_cell_load_cv": _rounded(load_cv),
        "white_top_area_proxy_fraction_of_frame": _rounded(
            total_area / frame_area),
        "nearest_neighbour_edge_gap_proxy_mm": _safe_percentiles(gap_values),
        "interpretation_boundary": (
            "grid load uses projected compact-white top area; nearest-neighbour "
            "gap subtracts equivalent-area radii and is a comparison proxy, "
            "not a slicer clearance measurement"),
    }
    return metrics, grid


def _population_and_samples(
    vertices: np.ndarray,
    triangles: np.ndarray,
    *,
    model_scale: float,
    line_width_mm: float,
    sample_limit: int,
) -> tuple[list[dict], list[dict], dict]:
    if len(vertices) == 0 or len(triangles) == 0:
        return [], [], {"component_height_mm": _safe_percentiles([])}
    labels = _component_labels(len(vertices), triangles)
    component_count = int(labels.max()) + 1 if len(labels) else 0
    minima = np.full((component_count, 3), np.inf, dtype=float)
    maxima = np.full((component_count, 3), -np.inf, dtype=float)
    for axis in range(3):
        np.minimum.at(minima[:, axis], labels, vertices[:, axis])
        np.maximum.at(maxima[:, axis], labels, vertices[:, axis])
    spans = (maxima - minima) * float(model_scale)
    centers = (minima[:, :2] + maxima[:, :2]) * 0.5 * float(model_scale)

    points = vertices[triangles]
    cross_z = (
        (points[:, 1, 0] - points[:, 0, 0])
        * (points[:, 2, 1] - points[:, 0, 1])
        - (points[:, 1, 1] - points[:, 0, 1])
        * (points[:, 2, 0] - points[:, 0, 0])
    )
    top_mask = cross_z > 1e-10
    triangle_labels = labels[triangles[:, 0]]
    top_area = np.bincount(
        triangle_labels[top_mask],
        weights=cross_z[top_mask] * 0.5 * model_scale * model_scale,
        minlength=component_count,
    )
    minimum_axis = np.min(spans[:, :2], axis=1)
    maximum_axis = np.max(spans[:, :2], axis=1)
    aspect = maximum_axis / np.maximum(minimum_axis, 1e-9)
    floor = max(0.15, float(line_width_mm) * 0.5)
    eligible = np.flatnonzero(
        (minimum_axis >= floor)
        & (minimum_axis <= 16.0)
        & (maximum_axis <= 24.0)
        & (aspect <= 4.0)
        & (spans[:, 2] >= 0.01)
        & (top_area >= 0.04)
    )
    population = [{
        "component_id": int(component),
        "center_x_mm": float(centers[component, 0]),
        "center_y_mm": float(centers[component, 1]),
        "top_area_mm2": float(top_area[component]),
        "aabb_short_axis_mm": float(minimum_axis[component]),
        "height_mm": float(spans[component, 2]),
    } for component in eligible]

    selected = eligible
    if len(selected) > sample_limit:
        ordered = selected[np.lexsort((
            selected, top_area[selected], minimum_axis[selected]))]
        positions = np.linspace(0, len(ordered) - 1, sample_limit)
        selected = ordered[np.rint(positions).astype(int)]

    top_indices = np.flatnonzero(top_mask)
    top_labels = triangle_labels[top_mask]
    order = np.argsort(top_labels, kind="stable")
    sorted_labels = top_labels[order]
    sorted_triangle_indices = top_indices[order]
    starts = np.searchsorted(sorted_labels, selected, side="left")
    ends = np.searchsorted(sorted_labels, selected, side="right")

    samples = []
    simplify = max(0.02, line_width_mm * 0.12)
    for component, start, end in zip(selected, starts, ends):
        component_triangles = sorted_triangle_indices[start:end]
        polygons = []
        for triangle_index in component_triangles:
            xy = points[triangle_index, :, :2] * float(model_scale)
            polygon = Polygon(xy)
            if polygon.area > 1e-10:
                polygons.append(polygon)
        if not polygons:
            continue
        footprint = unary_union(polygons)
        silhouette = silhouette_metrics(footprint, simplify_mm=simplify)
        rotated = _rotated_geometry_metrics(footprint)
        if silhouette is None or rotated is None:
            continue
        samples.append({
            "component_id": int(component),
            "height_mm": float(spans[component, 2]),
            **silhouette,
            **rotated,
        })
    return population, samples, {
        "eligible_component_count": int(len(eligible)),
        "sample_count": len(samples),
        "component_height_mm": _safe_percentiles(
            spans[eligible, 2] if len(eligible) else []),
    }


def _aggregate_shape(samples: Sequence[Mapping]) -> dict:
    fields = (
        "area_mm2", "short_axis_mm", "long_axis_mm", "aspect_ratio",
        "solidity", "rectangularity", "perimeter_excess", "compactness",
        "concavity_depth_fraction", "outline_vertices",
        "reflex_vertex_fraction", "sharp_convex_vertex_fraction",
        "height_mm",
    )
    return {
        "sample_count": len(samples),
        "metrics": {
            field: _safe_percentiles(sample[field] for sample in samples)
            for field in fields
        },
        "fraction_with_holes": _rounded(np.mean([
            bool(sample.get("hole_count")) for sample in samples
        ])) if samples else None,
        "orientation": orientation_metrics(samples),
    }


def _provisional_profile(shape: Mapping, spatial: Mapping) -> dict:
    """Assign an interpretable comparison label without affecting geometry."""

    occupied = float(spatial.get("occupied_cell_fraction") or 0.0)
    continuity = float(spatial.get("largest_occupied_cluster_fraction") or 0.0)
    coherence = float(
        shape.get("orientation", {}).get("orthogonal_coherence") or 0.0)
    solidity = float(
        shape.get("metrics", {}).get("solidity", {}).get("p50") or 0.0)
    rectangularity = float(
        shape.get("metrics", {}).get("rectangularity", {}).get("p50") or 0.0)

    if occupied >= 0.68 and continuity >= 0.85 and coherence >= 0.55:
        label = "continuous_orthogonal_urban_carpet"
        reason = "widely distributed compact relief with a coherent grid axis"
    elif occupied >= 0.68 and continuity >= 0.85:
        label = "continuous_mixed_orientation_urban_carpet"
        reason = "widely distributed compact relief with mixed local directions"
    elif occupied >= 0.45 and coherence >= 0.55:
        label = "distributed_aligned_compact_masses"
        reason = "moderate coverage and a strong orthogonal direction system"
    elif occupied >= 0.45:
        label = "distributed_organic_compact_masses"
        reason = "moderate coverage with mixed or curved local structure"
    else:
        label = "clustered_or_sparse_relief"
        reason = "compact relief occupies fewer than half of the observation cells"

    return {
        "label": label,
        "reason": reason,
        "compact_silhouette_supported": bool(
            solidity >= 0.97 and rectangularity >= 0.78),
        "status": "provisional_observation_only",
        "not_a_generation_instruction": True,
    }


def _parse_reference(path: Path, *, sample_limit: int, grid_size: int) -> dict:
    with zipfile.ZipFile(path) as archive:
        settings = _project_settings(archive)
        volumes = _volume_settings(archive)
        scale = _city_model_scale(archive)
        colors = settings["filament_colors"]
        line_width = float(settings["line_width_mm"] or 0.42)
        white_volumes = []
        global_min = np.full(2, np.inf, dtype=float)
        global_max = np.full(2, -np.inf, dtype=float)
        backing_bounds = None

        with archive.open("3D/Objects/object_1.model") as stream:
            current_id = None
            vertices = []
            triangles = []
            for event, element in ET.iterparse(stream, events=("start", "end")):
                kind = _local_name(element.tag)
                if event == "start" and kind == "object":
                    current_id = int(element.attrib.get("id", 0))
                    vertices = []
                    triangles = []
                elif event == "end" and kind == "vertex":
                    vertices.append(tuple(
                        float(element.attrib[axis]) for axis in ("x", "y", "z")))
                    element.clear()
                elif event == "end" and kind == "triangle":
                    triangles.append(tuple(
                        int(element.attrib[axis]) for axis in ("v1", "v2", "v3")))
                    element.clear()
                elif event == "end" and kind == "object":
                    vertex_array = np.asarray(vertices, dtype=float)
                    triangle_array = np.asarray(triangles, dtype=np.int32)
                    volume = volumes.get(current_id, {})
                    extruder = int(volume.get("extruder") or 1)
                    color = colors[extruder - 1] if extruder <= len(colors) else None
                    role = _material_role(color)
                    if len(vertex_array):
                        bounds_min = vertex_array[:, :2].min(axis=0) * scale
                        bounds_max = vertex_array[:, :2].max(axis=0) * scale
                        global_min = np.minimum(global_min, bounds_min)
                        global_max = np.maximum(global_max, bounds_max)
                        spans = bounds_max - bounds_min
                        if (role == "black_negative_backing"
                                and min(spans) >= 150.0):
                            backing_bounds = [
                                float(bounds_min[0]), float(bounds_min[1]),
                                float(bounds_max[0]), float(bounds_max[1]),
                            ]
                    if role == "white_relief":
                        population, samples, summary = _population_and_samples(
                            vertex_array,
                            triangle_array,
                            model_scale=scale,
                            line_width_mm=line_width,
                            sample_limit=sample_limit,
                        )
                        if population:
                            white_volumes.append({
                                "object_id": current_id,
                                "name": volume.get("name", f"Object_{current_id}"),
                                "population": population,
                                "samples": samples,
                                **summary,
                            })
                    current_id = None
                    vertices = []
                    triangles = []
                    element.clear()

    if backing_bounds is None:
        if not np.all(np.isfinite(global_min)) or not np.all(np.isfinite(global_max)):
            raise ValueError(f"no measurable geometry in {path}")
        backing_bounds = [
            float(global_min[0]), float(global_min[1]),
            float(global_max[0]), float(global_max[1]),
        ]

    # Relief tiers are measured, not inferred from names: the lower median Z
    # span is quiet texture; the next is standard urban mass.
    white_volumes.sort(key=lambda volume: (
        volume["component_height_mm"].get("p50") or math.inf,
        volume["object_id"],
    ))
    tier_names = ["quiet_relief", "standard_relief"]
    all_population = []
    all_samples = []
    tiers = []
    for index, volume in enumerate(white_volumes):
        tier = tier_names[index] if index < len(tier_names) else f"white_tier_{index + 1}"
        for item in volume["population"]:
            item["tier"] = tier
        for item in volume["samples"]:
            item["tier"] = tier
        all_population.extend(volume["population"])
        all_samples.extend(volume["samples"])
        tiers.append({
            "tier": tier,
            "object_id": volume["object_id"],
            "name": volume["name"],
            "eligible_component_count": volume["eligible_component_count"],
            "sample_count": volume["sample_count"],
            "component_height_mm": volume["component_height_mm"],
            "shape": _aggregate_shape(volume["samples"]),
        })

    shape = _aggregate_shape(all_samples)
    spatial, grid = spatial_metrics(
        all_population, backing_bounds, grid_size=grid_size)
    profile = _provisional_profile(shape, spatial)
    city = path.parent.name if path.parent.name else path.stem
    return {
        "schema_version": SCHEMA_VERSION,
        "purpose": "diagnostic",
        "scope": {
            "city": city,
            "source_3mf": str(path),
            "archive_bytes": path.stat().st_size,
            "model_scale": _rounded(scale, 8),
            "frame_bounds_mm": [_rounded(value, 4) for value in backing_bounds],
            "comparability": "reference_family_morphology_only",
        },
        "printer": settings,
        "selection": {
            "material": "white_relief",
            "compact_component_floor_mm": _rounded(max(0.15, line_width * 0.5)),
            "maximum_short_axis_mm": 16.0,
            "maximum_long_axis_mm": 24.0,
            "maximum_aabb_aspect": 4.0,
            "minimum_projected_top_area_mm2": 0.04,
            "semantic_boundary": (
                "compact white relief is a building/block-texture proxy; "
                "material membership does not prove semantic ownership"),
        },
        "population": {
            "eligible_component_count": len(all_population),
            "measured_shape_sample_count": len(all_samples),
            "tiers": tiers,
        },
        "block_grammar": {
            "spatial_fullness": spatial,
            "component_shape": shape,
            "provisional_profile": profile,
        },
        "raw_diagnostic": {
            "grid_top_area_mm2": [[_rounded(value, 4) for value in row]
                                  for row in grid.tolist()],
            "sample_components": all_samples,
        },
        "limitations": [
            "No visual-aesthetic pass/fail is inferred from these measurements.",
            "Road gaps are represented only by a compact-relief spacing proxy; "
            "the observer does not claim slicer clearance.",
            "Cross-city values are comparable within this reference package "
            "family, not automatically transferable to arbitrary 3MF producers.",
        ],
    }


def _font(size: int, *, bold: bool = False):
    # Prefer a CJK-capable system font so city names remain evidence instead
    # of turning into tofu boxes on the controller Mac.  The fallbacks retain
    # portability on Linux/Windows test nodes.
    candidates = (
        (
            "/System/Library/Fonts/STHeiti Medium.ttc",
            "/System/Library/Fonts/Hiragino Sans GB.ttc",
            "Arial Bold.ttf",
            "DejaVuSans-Bold.ttf",
        ) if bold else (
            "/System/Library/Fonts/STHeiti Light.ttc",
            "/System/Library/Fonts/Hiragino Sans GB.ttc",
            "Arial.ttf",
            "DejaVuSans.ttf",
        )
    )
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _metric(report: Mapping, path: Sequence[str]):
    value = report
    for key in path:
        value = value.get(key, {}) if isinstance(value, Mapping) else {}
    return None if value == {} else value


_PROFILE_LABELS_ZH = {
    "continuous_orthogonal_urban_carpet": "连续正交城市肌理",
    "continuous_mixed_orientation_urban_carpet": "连续混合方向城市肌理",
    "distributed_aligned_compact_masses": "分布式规则紧凑街坊",
    "distributed_organic_compact_masses": "分布式有机紧凑街坊",
    "clustered_or_sparse_relief": "聚集或稀疏浮雕",
}

_PROFILE_REASONS_ZH = {
    "continuous_orthogonal_urban_carpet": (
        "紧凑浮雕分布广泛，并具有清晰一致的正交网格方向"),
    "continuous_mixed_orientation_urban_carpet": (
        "紧凑浮雕分布广泛，但各片区具有混合的局部方向"),
    "distributed_aligned_compact_masses": (
        "覆盖范围适中，同时具有较强的正交方向体系"),
    "distributed_organic_compact_masses": (
        "覆盖范围适中，局部结构呈混合或有机曲线方向"),
    "clustered_or_sparse_relief": (
        "紧凑浮雕覆盖不足一半观测网格，呈聚集或稀疏分布"),
}


def render_diagnostic(report: Mapping, output_path: Path) -> Path:
    width, height = 1600, 940
    image = Image.new("RGB", (width, height), (246, 245, 241))
    draw = ImageDraw.Draw(image)
    title_font = _font(34, bold=True)
    heading_font = _font(24, bold=True)
    body_font = _font(19)
    small_font = _font(16)
    city = report["scope"]["city"]
    profile = _metric(report, ("block_grammar", "provisional_profile", "label"))
    profile_zh = _PROFILE_LABELS_ZH.get(str(profile), str(profile))
    draw.rectangle((0, 0, width, 90), fill=(37, 39, 37))
    draw.text((34, 22), f"{city} · 参考街坊语法观测",
              font=title_font, fill=(250, 249, 246))
    draw.text((1120, 31), profile_zh, font=body_font, fill=(211, 207, 197))

    # Panel 1: spatial fullness heat map.
    draw.text((34, 120), "1  空间饱满度", font=heading_font,
              fill=(45, 45, 42))
    grid = np.asarray(report["raw_diagnostic"]["grid_top_area_mm2"], dtype=float)
    max_value = max(float(grid.max()), 1e-9)
    left, top, cell = 34, 164, 68
    for row in range(grid.shape[0] - 1, -1, -1):
        for column in range(grid.shape[1]):
            ratio = math.sqrt(float(grid[row, column]) / max_value)
            shade = int(239 - ratio * 176)
            y = top + (grid.shape[0] - 1 - row) * cell
            x = left + column * cell
            draw.rectangle((x, y, x + cell - 3, y + cell - 3),
                           fill=(shade, shade, shade))
    spatial = report["block_grammar"]["spatial_fullness"]
    lines = [
        f"已占网格比例      {spatial['occupied_cell_fraction']:.3f}",
        f"最大连续区比例    {spatial['largest_occupied_cluster_fraction']:.3f}",
        f"空间分布熵        {spatial['cell_load_entropy']:.3f}",
        f"白色肌理覆盖率    {spatial['white_top_area_proxy_fraction_of_frame']:.3f}",
        f"有效组件数量      {spatial['component_population_count']:,}",
    ]
    for index, line in enumerate(lines):
        draw.text((34, 730 + index * 30), line, font=body_font,
                  fill=(65, 64, 60))

    samples = report["raw_diagnostic"]["sample_components"]
    shape = report["block_grammar"]["component_shape"]

    # Panel 2: short-axis histogram.
    panel_x = 650
    draw.text((panel_x, 120), "2  单体粒度", font=heading_font,
              fill=(45, 45, 42))
    axes = np.asarray([sample["short_axis_mm"] for sample in samples], dtype=float)
    bins = np.linspace(0.0, min(8.0, max(4.0, float(np.percentile(axes, 98))
                                      if len(axes) else 4.0)), 21)
    histogram, _ = np.histogram(axes, bins=bins)
    chart_left, chart_top, chart_width, chart_height = panel_x, 178, 390, 230
    max_count = max(int(histogram.max()) if len(histogram) else 0, 1)
    for index, count in enumerate(histogram):
        x0 = chart_left + index * chart_width / len(histogram)
        x1 = chart_left + (index + 1) * chart_width / len(histogram) - 2
        bar_height = chart_height * count / max_count
        draw.rectangle((x0, chart_top + chart_height - bar_height,
                        x1, chart_top + chart_height), fill=(91, 94, 90))
    draw.line((chart_left, chart_top + chart_height,
               chart_left + chart_width, chart_top + chart_height),
              fill=(45, 45, 42), width=2)
    axis_distribution = shape["metrics"]["short_axis_mm"]
    draw.text((panel_x, 430),
              f"短轴：p10 {axis_distribution['p10']:.2f}  ·  "
              f"p50 {axis_distribution['p50']:.2f}  ·  "
              f"p90 {axis_distribution['p90']:.2f} 毫米",
              font=body_font, fill=(65, 64, 60))

    # Panel 3: orientation histogram modulo 90 degrees.
    draw.text((panel_x, 510), "3  方向系统", font=heading_font,
              fill=(45, 45, 42))
    angles = np.asarray([
        sample["orientation_deg"] % 90.0 for sample in samples
        if sample.get("aspect_ratio", 1.0) >= 1.15
    ], dtype=float)
    orientation_hist, _ = np.histogram(angles, bins=np.linspace(0, 90, 13))
    max_orientation = max(int(orientation_hist.max())
                          if len(orientation_hist) else 0, 1)
    for index, count in enumerate(orientation_hist):
        x0 = panel_x + index * chart_width / len(orientation_hist)
        x1 = panel_x + (index + 1) * chart_width / len(orientation_hist) - 3
        bar_height = 160 * count / max_orientation
        draw.rectangle((x0, 710 - bar_height, x1, 710),
                       fill=(178, 88, 53))
    orientation = shape["orientation"]
    draw.text((panel_x, 735),
              f"正交一致性    {orientation['orthogonal_coherence']:.3f}",
              font=body_font, fill=(65, 64, 60))
    draw.text((panel_x, 765),
              f"单轴一致性    {orientation['single_axis_coherence']:.3f}",
              font=body_font, fill=(65, 64, 60))
    draw.text((panel_x, 795),
              f"90°方向熵     {orientation['orientation_entropy_mod_90']:.3f}",
              font=body_font, fill=(65, 64, 60))

    # Panel 4: calm-silhouette evidence and Z tiers.
    panel_x = 1090
    draw.text((panel_x, 120), "4  轮廓与高度层级", font=heading_font,
              fill=(45, 45, 42))
    metric_rows = (
        ("完整度 p50", shape["metrics"]["solidity"]["p50"]),
        ("矩形填充度 p50", shape["metrics"]["rectangularity"]["p50"]),
        ("周长冗余 p90", shape["metrics"]["perimeter_excess"]["p90"]),
        ("轮廓转折数 p75", shape["metrics"]["outline_vertices"]["p75"]),
        ("相对凹陷深度 p90", shape["metrics"]["concavity_depth_fraction"]["p90"]),
    )
    for index, (label, value) in enumerate(metric_rows):
        y = 178 + index * 52
        draw.text((panel_x, y), label, font=body_font, fill=(80, 78, 73))
        draw.text((1460, y), f"{value:.3f}", font=body_font,
                  fill=(38, 39, 37), anchor="ra")
    draw.line((panel_x, 455, 1548, 455), fill=(205, 201, 192), width=2)
    draw.text((panel_x, 478), "实测白色浮雕层级", font=small_font,
              fill=(110, 106, 99))
    tiers = report["population"]["tiers"]
    tier_labels = {
        "quiet_relief": "低浮雕纹理",
        "standard_relief": "标准城市肌理",
    }
    for index, tier in enumerate(tiers):
        y = 520 + index * 90
        height_distribution = tier["component_height_mm"]
        draw.text((panel_x, y), tier_labels.get(tier["tier"], tier["tier"]),
                  font=heading_font,
                  fill=(48, 48, 45))
        draw.text((panel_x, y + 35),
                  f"高度 p50：{height_distribution['p50']:.3f} 毫米  ·  "
                  f"{tier['eligible_component_count']:,} 个组件",
                  font=body_font, fill=(80, 78, 73))
    profile_data = report["block_grammar"]["provisional_profile"]
    draw.rounded_rectangle((1082, 755, 1550, 875), radius=10,
                           fill=(229, 226, 218))
    draw.text((1102, 773), "暂定形态判断", font=small_font,
              fill=(105, 100, 91))
    draw.text((1102, 805), profile_zh, font=body_font,
              fill=(39, 40, 38))
    reason = _PROFILE_REASONS_ZH.get(profile_data["label"],
                                     profile_data["reason"])
    for index, line in enumerate(textwrap.wrap(reason, width=23)[:2]):
        draw.text((1102, 838 + index * 20), line, font=small_font,
                  fill=(75, 73, 68))

    draw.text((34, 910),
              "只读形态观测 · 不代表审美验收通过 · 不直接控制生成参数",
              font=small_font, fill=(105, 101, 94))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path)
    return output_path


def _summary_row(report: Mapping) -> dict:
    shape = report["block_grammar"]["component_shape"]
    spatial = report["block_grammar"]["spatial_fullness"]
    orientation = shape["orientation"]
    metrics = shape["metrics"]
    tiers = report["population"]["tiers"]
    return {
        "city": report["scope"]["city"],
        "profile": report["block_grammar"]["provisional_profile"]["label"],
        "components": report["population"]["eligible_component_count"],
        "occupied_cell_fraction": spatial["occupied_cell_fraction"],
        "largest_cluster_fraction": spatial["largest_occupied_cluster_fraction"],
        "white_area_frame_fraction": spatial[
            "white_top_area_proxy_fraction_of_frame"],
        "short_axis_p50_mm": metrics["short_axis_mm"]["p50"],
        "short_axis_p90_mm": metrics["short_axis_mm"]["p90"],
        "solidity_p50": metrics["solidity"]["p50"],
        "rectangularity_p50": metrics["rectangularity"]["p50"],
        "outline_vertices_p75": metrics["outline_vertices"]["p75"],
        "orthogonal_coherence": orientation["orthogonal_coherence"],
        "orientation_entropy_mod_90": orientation[
            "orientation_entropy_mod_90"],
        "quiet_height_p50_mm": (
            tiers[0]["component_height_mm"]["p50"] if tiers else None),
        "standard_height_p50_mm": (
            tiers[1]["component_height_mm"]["p50"] if len(tiers) > 1 else None),
    }


def _write_summary(rows: Sequence[Mapping], output_dir: Path) -> None:
    (output_dir / "summary.json").write_text(
        json.dumps(list(rows), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    (output_dir / "summary.csv").write_text(buffer.getvalue(), encoding="utf-8")

    columns = (
        "city", "profile", "components", "occupied_cell_fraction",
        "short_axis_p50_mm", "solidity_p50", "rectangularity_p50",
        "orthogonal_coherence", "quiet_height_p50_mm",
        "standard_height_p50_mm",
    )
    lines = [
        "# Reference block-grammar observation",
        "",
        "Read-only morphology evidence. Labels are provisional and do not "
        "control production geometry.",
        "",
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(
            str(row.get(column, "")) for column in columns) + " |")
    lines.extend((
        "",
        "Interpretation limits: compact white relief is a building/block proxy; "
        "road clearance and beauty are not established by these metrics.",
        "",
    ))
    (output_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def _resolve_inputs(inputs: Sequence[Path]) -> list[Path]:
    paths = []
    for source in inputs:
        if source.is_dir():
            paths.extend(sorted(source.rglob("*.3mf")))
        elif source.is_file() and source.suffix.lower() == ".3mf":
            paths.append(source)
        else:
            raise FileNotFoundError(f"3MF input not found: {source}")
    unique = []
    seen = set()
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen:
            unique.append(path)
            seen.add(resolved)
    return unique


def observe_paths(
    inputs: Sequence[Path],
    *,
    output_dir: Path,
    sample_limit: int = 300,
    grid_size: int = 8,
) -> list[dict]:
    paths = _resolve_inputs(inputs)
    if not paths:
        raise ValueError("no 3MF inputs found")
    reports = []
    for path in paths:
        report = _parse_reference(
            path, sample_limit=sample_limit, grid_size=grid_size)
        city = report["scope"]["city"]
        report_path = output_dir / "observations" / f"{city}.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        diagnostic_path = output_dir / "diagnostics" / f"{city}.png"
        render_diagnostic(report, diagnostic_path)
        report["artifacts"] = {
            "observation_json": str(report_path),
            "diagnostic_png": str(diagnostic_path),
        }
        reports.append(report)
    rows = [_summary_row(report) for report in reports]
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_summary(rows, output_dir)
    return reports


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only reference 3MF block-grammar observer")
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--sample-limit", type=int, default=300)
    parser.add_argument("--grid-size", type=int, default=8)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.sample_limit < 20:
        raise SystemExit("--sample-limit must be at least 20")
    if args.grid_size < 4 or args.grid_size > 32:
        raise SystemExit("--grid-size must be between 4 and 32")
    reports = observe_paths(
        args.inputs,
        output_dir=args.output_dir,
        sample_limit=args.sample_limit,
        grid_size=args.grid_size,
    )
    print(json.dumps({
        "schema_version": SCHEMA_VERSION,
        "observed": len(reports),
        "cities": [report["scope"]["city"] for report in reports],
        "output_dir": str(args.output_dir),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
