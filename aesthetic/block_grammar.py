"""Printer-scaled source block-grammar measurements and bounded style intent.

The module measures projected source building footprints.  It does not mutate
geometry and it does not load reference 3MF files at generation time.  The
versioned reference envelope below contains only aggregate audit statistics;
it is a soft visual prior subordinate to source truth, topology boundaries and
the selected printer profile.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping

import numpy as np


MEASUREMENT_VERSION = "source-block-grammar-v1"
STRATEGY_VERSION = "block-grammar-strategy-v2"
REFERENCE_ENVELOPE_VERSION = "reference-demo-eight-city-envelope-v1"

# Eight-city reference observation, intentionally expressed as a broad soft
# envelope.  It is not a city lookup and none of these values is a hard mesh
# acceptance threshold.
REFERENCE_ENVELOPE = {
    "short_axis_p50_mm": {"low": 0.95, "high": 1.70},
    "solidity_p50_floor": 0.97,
    "rectangularity_p50_floor": 0.81,
    "perimeter_excess_p50_ceiling": 1.05,
}


def _clamp01(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return float(np.clip(number, 0.0, 1.0))


def _percentiles(values: Iterable[float]) -> dict:
    array = np.asarray([
        float(value) for value in values if math.isfinite(float(value))
    ], dtype=float)
    if not len(array):
        return {key: None for key in ("p10", "p50", "p90")}
    result = np.percentile(array, [10, 50, 90])
    return {
        key: round(float(value), 5)
        for key, value in zip(("p10", "p50", "p90"), result)
    }


def _polygon_parts(geometry):
    if geometry is None or geometry.is_empty:
        return []
    if geometry.geom_type == "Polygon":
        return [geometry]
    if geometry.geom_type in {"MultiPolygon", "GeometryCollection"}:
        result = []
        for child in geometry.geoms:
            result.extend(_polygon_parts(child))
        return result
    return []


def _shape_record(polygon, scale_mm_per_m: float,
                  simplify_model_mm: float) -> dict | None:
    if polygon is None or polygon.is_empty or polygon.area <= 0:
        return None
    try:
        rectangle = polygon.minimum_rotated_rectangle
        coords = np.asarray(rectangle.exterior.coords[:-1], dtype=float)
    except (AttributeError, TypeError, ValueError):
        return None
    if len(coords) != 4:
        return None
    vectors = np.roll(coords, -1, axis=0) - coords
    lengths = np.linalg.norm(vectors, axis=1)
    if not np.all(np.isfinite(lengths)) or float(lengths.max()) <= 0:
        return None
    long_index = int(np.argmax(lengths))
    short_m = float(lengths.min())
    long_m = float(lengths.max())
    hull = polygon.convex_hull
    if hull.is_empty or hull.area <= 0 or hull.length <= 0:
        return None
    rectangle_area = max(short_m * long_m, 1e-9)
    vector = vectors[long_index]
    angle = math.degrees(math.atan2(
        float(vector[1]), float(vector[0]))) % 180.0
    simplified = polygon.simplify(
        simplify_model_mm / max(scale_mm_per_m, 1e-12),
        preserve_topology=True)
    parts = _polygon_parts(simplified)
    outline_vertices = sum(max(0, len(part.exterior.coords) - 1)
                           for part in parts)
    return {
        "area_mm2": float(polygon.area) * scale_mm_per_m ** 2,
        "short_axis_mm": short_m * scale_mm_per_m,
        "long_axis_mm": long_m * scale_mm_per_m,
        "aspect_ratio": long_m / max(short_m, 1e-9),
        "solidity": float(polygon.area / hull.area),
        "rectangularity": float(polygon.area / rectangle_area),
        "perimeter_excess": float(polygon.length / hull.length),
        "outline_vertices": int(outline_vertices),
        "orientation_deg": angle,
    }


def _orientation(records: list[dict]) -> dict:
    angles = []
    weights = []
    for record in records:
        aspect = float(record["aspect_ratio"])
        confidence = min(1.0, max(0.0, (aspect - 1.05) / 0.75))
        weight = float(record["area_mm2"]) * confidence
        if weight <= 0:
            continue
        angles.append(math.radians(float(record["orientation_deg"])))
        weights.append(weight)
    if not weights:
        return {
            "oriented_sample_count": 0,
            "single_axis_coherence": None,
            "orthogonal_coherence": None,
        }
    theta = np.asarray(angles, dtype=float)
    weight_array = np.asarray(weights, dtype=float)
    weight_array /= max(float(weight_array.sum()), 1e-12)
    return {
        "oriented_sample_count": len(weights),
        "single_axis_coherence": round(float(abs(np.sum(
            weight_array * np.exp(2j * theta)))), 5),
        "orthogonal_coherence": round(float(abs(np.sum(
            weight_array * np.exp(4j * theta)))), 5),
    }


def _summary(records: list[dict]) -> dict:
    keys = (
        "area_mm2", "short_axis_mm", "long_axis_mm", "aspect_ratio",
        "solidity", "rectangularity", "perimeter_excess",
        "outline_vertices",
    )
    return {
        "sample_count": len(records),
        "metrics": {
            key: _percentiles(record[key] for record in records)
            for key in keys
        },
        "orientation": _orientation(records),
    }


def measure_source_block_grammar(
    buildings,
    bbox_local,
    *,
    model_span_mm: float | None,
    nozzle_real_m: float | None,
    maximum_samples: int = 4_000,
    footprint_frame_coverage: float | None = None,
    occupied_cell_fraction: float | None = None,
) -> dict:
    """Measure raw footprint morphology in final-model millimetres."""

    if model_span_mm is None or not math.isfinite(float(model_span_mm)):
        return {
            "version": MEASUREMENT_VERSION,
            "status": "unavailable",
            "reason": "model span is unavailable",
        }
    xmin, ymin, xmax, ymax = [float(value) for value in bbox_local]
    frame_span_m = max(xmax - xmin, ymax - ymin)
    if frame_span_m <= 0 or float(model_span_mm) <= 0:
        raise ValueError("block-grammar frame and model span must be positive")
    if buildings is None or len(buildings) == 0:
        return {
            "version": MEASUREMENT_VERSION,
            "status": "unavailable",
            "reason": "building source is empty",
        }
    polygon_mask = buildings.geometry.geom_type.isin(
        ("Polygon", "MultiPolygon"))
    work = buildings.loc[polygon_mask]
    if not len(work):
        return {
            "version": MEASUREMENT_VERSION,
            "status": "unavailable",
            "reason": "building source has no polygonal footprints",
        }
    scale = float(model_span_mm) / frame_span_m
    if len(work) > maximum_samples:
        rng = np.random.default_rng(20260830)
        indices = np.sort(rng.choice(
            len(work), size=int(maximum_samples), replace=False))
        sample = work.iloc[indices]
    else:
        sample = work
    nozzle_model_mm = (
        float(nozzle_real_m) * scale
        if nozzle_real_m is not None and float(nozzle_real_m) > 0 else 0.4)
    compact_floor_mm = max(0.15, nozzle_model_mm * 0.5)
    records = []
    for geometry in sample.geometry:
        for polygon in _polygon_parts(geometry):
            record = _shape_record(
                polygon, scale, simplify_model_mm=max(0.035, 0.18 * compact_floor_mm))
            if record is not None:
                records.append(record)
    comparable = [record for record in records if (
        record["short_axis_mm"] >= compact_floor_mm
        and record["short_axis_mm"] <= 16.0
        and record["long_axis_mm"] <= 24.0
        and record["aspect_ratio"] <= 4.0
        and record["area_mm2"] >= 0.04
    )]
    comparable_fraction = len(comparable) / max(len(records), 1)
    return {
        "version": MEASUREMENT_VERSION,
        "status": "ready",
        "model_span_mm": round(float(model_span_mm), 5),
        "scale_mm_per_m": round(scale, 9),
        "nozzle_model_mm": round(nozzle_model_mm, 5),
        "source_footprint_count": int(len(work)),
        "spatial_fullness": {
            "footprint_frame_coverage": (
                round(float(footprint_frame_coverage), 5)
                if footprint_frame_coverage is not None else None),
            "occupied_cell_fraction": (
                round(float(occupied_cell_fraction), 5)
                if occupied_cell_fraction is not None else None),
        },
        "all_source_sample": _summary(records),
        "printable_comparable_sample": {
            **_summary(comparable),
            "sample_fraction": round(comparable_fraction, 5),
            "estimated_population_count": int(round(
                comparable_fraction * len(work))),
            "selection": {
                "short_axis_floor_mm": round(compact_floor_mm, 5),
                "maximum_short_axis_mm": 16.0,
                "maximum_long_axis_mm": 24.0,
                "maximum_aspect": 4.0,
                "minimum_area_mm2": 0.04,
            },
        },
        "reference_envelope": {
            "version": REFERENCE_ENVELOPE_VERSION,
            **REFERENCE_ENVELOPE,
            "authority": "soft aggregate audit prior only",
        },
        "constraints": [
            "measurement only", "no city-name lookup", "no geometry output",
            "printer profile remains hard authority",
        ],
    }


def resolve_block_grammar_strategy(
    measurement: Mapping | None,
    *,
    local_completeness: float | None,
    local_continuity: float | None,
    block_base_support_fraction: float | None,
    external_urban_support: float | None,
    grid_score: float | None,
    water_network_score: float | None,
) -> dict:
    """Convert measurements to bounded, semantic building-policy pressures."""

    if not isinstance(measurement, Mapping) or measurement.get("status") != "ready":
        return {
            "version": STRATEGY_VERSION,
            "status": "unavailable",
            "reason": "source block-grammar measurement is unavailable",
        }
    comparable = measurement.get("printable_comparable_sample", {}) or {}
    metrics = comparable.get("metrics", {}) or {}
    spatial = measurement.get("spatial_fullness", {}) or {}

    def p50(name):
        value = (metrics.get(name, {}) or {}).get("p50")
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    completeness = _clamp01(local_completeness)
    continuity = _clamp01(local_continuity)
    support = max(
        _clamp01(external_urban_support),
        _clamp01(completeness * continuity),
    )
    grid = _clamp01(grid_score)
    water_network = _clamp01(water_network_score)
    coverage = _clamp01(spatial.get("footprint_frame_coverage"))

    # A broad style target changes by measured continuity and composition.
    # Complete orthogonal cities stay finer; heterogeneous water-led cities
    # receive a slightly coarser quiet mass.  The result remains within the
    # observed multi-city envelope and is only a soft clustering target.
    desired_short_axis = float(np.clip(
        1.12
        + 0.30 * (1.0 - continuity)
        + 0.16 * _clamp01(block_base_support_fraction)
        + 0.10 * water_network
        - 0.10 * grid * completeness,
        REFERENCE_ENVELOPE["short_axis_p50_mm"]["low"],
        1.45,
    ))
    source_width = p50("short_axis_mm")
    width_ratio = (
        desired_short_axis / max(source_width, 1e-9)
        if source_width is not None else 1.0)
    aggregation_pressure = _clamp01((width_ratio - 1.0) / 3.5)
    solidity = p50("solidity")
    rectangularity = p50("rectangularity")
    perimeter = p50("perimeter_excess")
    outline_pressure = _clamp01(
        0.40 * max(0.0, REFERENCE_ENVELOPE["solidity_p50_floor"]
                   - float(solidity or 0.0)) / 0.25
        + 0.35 * max(0.0, REFERENCE_ENVELOPE["rectangularity_p50_floor"]
                     - float(rectangularity or 0.0)) / 0.30
        + 0.25 * max(0.0, float(perimeter or 1.0)
                     - REFERENCE_ENVELOPE["perimeter_excess_p50_ceiling"]) / 0.30
    )
    orthogonal = _clamp01(
        ((comparable.get("orientation") or {}).get("orthogonal_coherence")))
    texture_promotion = _clamp01(
        max(0.0, 0.10 - coverage) / 0.10
        * max(continuity, _clamp01(external_urban_support)))
    selection_pressure = _clamp01(
        float(measurement.get("source_footprint_count") or 0) / 250_000.0
        * max(continuity, completeness))

    if texture_promotion >= 0.45:
        mode = "promote_sub_nozzle_texture_into_bounded_blocks"
    elif selection_pressure >= 0.45:
        mode = "select_and_aggregate_complete_dense_source"
    elif aggregation_pressure >= 0.40:
        mode = "aggregate_and_regularize"
    else:
        mode = "preserve_compact_source_with_light_regularization"
    return {
        "version": STRATEGY_VERSION,
        "status": "ready",
        "mode": mode,
        "soft_target_short_axis_p50_mm": round(desired_short_axis, 5),
        "soft_target_short_axis_band_mm": [
            round(max(0.75, desired_short_axis * 0.82), 5),
            round(min(2.30, desired_short_axis * 1.55), 5),
        ],
        "source_short_axis_p50_mm": (
            round(source_width, 5) if source_width is not None else None),
        "source_to_target_width_ratio": round(width_ratio, 5),
        "aggregation_pressure": round(aggregation_pressure, 5),
        "outline_regularization_pressure": round(outline_pressure, 5),
        "orientation_preservation_pressure": round(orthogonal, 5),
        "texture_promotion_pressure": round(texture_promotion, 5),
        "selection_pressure": round(selection_pressure, 5),
        "evidence": {
            "local_completeness": round(completeness, 5),
            "local_continuity": round(continuity, 5),
            "urban_support": round(support, 5),
            "footprint_frame_coverage": round(coverage, 5),
            "grid_score": round(grid, 5),
            "water_network_score": round(water_network, 5),
        },
        "hard_boundaries": [
            "source footprint support", "road/water topology blocks",
            "printer width floor", "candidate area-growth guards",
        ],
        "forbidden_controls": [
            "mesh vertices", "global Z", "boolean operations",
            "replacement road/water geometry",
        ],
    }
