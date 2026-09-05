"""Local building-data quality and representation strategy evidence.

The resolver consumes already measured scene cells.  It does not inspect a
city name and never emits geometry, mesh, Z or Boolean instructions.  Its job
is to distinguish three very different cases which a frame-wide building
density cannot separate:

* well mapped but sub-nozzle urban fabric, which should become coherent mass;
* road-supported urban cells with weak building evidence, where Block base is
  a cautious carrier for likely missing data;
* genuinely quiet/open cells, which must not be filled merely because their
  building count is low.

The output is deliberately an auditable strategy map.  External map imagery
may raise confidence in an urban-gap candidate, but it is never accepted as
replacement building geometry.
"""
from __future__ import annotations

from collections import Counter
import math
from typing import Mapping, Sequence

import numpy as np


QUALITY_VERSION = "local-building-quality-v1"

_STRATEGY_WEIGHTS = {
    "block_base_support": {
        "block_base": 0.85, "neighborhood_mass": 0.15,
        "literal_footprints": 0.0,
    },
    "hybrid_mass": {
        "block_base": 0.40, "neighborhood_mass": 0.50,
        "literal_footprints": 0.10,
    },
    "neighborhood_mass": {
        "block_base": 0.10, "neighborhood_mass": 0.80,
        "literal_footprints": 0.10,
    },
    "preserve_printable_footprints": {
        "block_base": 0.05, "neighborhood_mass": 0.15,
        "literal_footprints": 0.80,
    },
    "open_space_preserve": {
        "block_base": 0.0, "neighborhood_mass": 0.05,
        "literal_footprints": 0.10,
    },
    "water": {
        "block_base": 0.0, "neighborhood_mass": 0.0,
        "literal_footprints": 0.0,
    },
}


def _number(value, default=0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float(default)
    return result if math.isfinite(result) else float(default)


def _clamp01(value) -> float:
    return float(np.clip(_number(value), 0.0, 1.0))


def _percentile(values: np.ndarray, percentile: float,
                default: float) -> float:
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    return float(np.percentile(values, percentile)) if values.size else default


def _neighbor_mean(values: np.ndarray, row: int, column: int,
                   grid_size: int) -> float:
    result = []
    for other_row in range(max(0, row - 1), min(grid_size, row + 2)):
        for other_column in range(max(0, column - 1),
                                  min(grid_size, column + 2)):
            if other_row == row and other_column == column:
                continue
            result.append(values[other_row * grid_size + other_column])
    return float(np.mean(result)) if result else 0.0


def _external_support(external_urban: Mapping | None) -> float:
    evidence = external_urban if isinstance(external_urban, Mapping) else {}
    if evidence.get("status") != "evidence_only":
        return 0.0
    return _clamp01(
        0.65 * _number(evidence.get("urban_network_support"))
        + 0.35 * _number(evidence.get("road_presence_cell_fraction")))


def resolve_local_building_quality(
    cells: Sequence[Mapping],
    *,
    grid_size: int,
    building_metrics: Mapping | None = None,
    terrain_metrics: Mapping | None = None,
    external_urban: Mapping | None = None,
) -> dict:
    """Return a deterministic per-cell representation strategy map.

    The thresholds are normalized against the current crop and retain modest
    absolute floors.  This makes the decision sensitive to local distribution
    without allowing a uniformly empty frame to call itself a complete city.
    """

    if grid_size < 3 or grid_size > 16:
        raise ValueError("grid_size must be between 3 and 16")
    expected = grid_size * grid_size
    if len(cells) != expected:
        raise ValueError(
            f"building quality expects {expected} cells, got {len(cells)}")

    ordered = sorted(cells, key=lambda item: (
        int(item.get("row", -1)), int(item.get("column", -1))))
    if [(int(item.get("row", -1)), int(item.get("column", -1)))
            for item in ordered] != [
                (row, column) for row in range(grid_size)
                for column in range(grid_size)]:
        raise ValueError("cells must cover each grid position exactly once")

    coverage = np.asarray([
        max(0.0, _number(cell.get("building_coverage"))) for cell in ordered
    ])
    counts = np.asarray([
        max(0.0, _number(cell.get("building_count"))) for cell in ordered
    ])
    roads = np.asarray([
        max(0.0, _number(cell.get("road_density_km_km2")))
        for cell in ordered
    ])
    major = np.asarray([
        max(0.0, _number(cell.get("major_road_density_km_km2")))
        for cell in ordered
    ])
    junctions = np.asarray([
        max(0.0, _number(cell.get("junction_density_km2")))
        for cell in ordered
    ])
    water = np.asarray([
        _clamp01(cell.get("water_fraction")) for cell in ordered
    ])
    land = water < 0.65

    def land_reference(values, percentile, floor):
        return max(_percentile(values[land], percentile, floor), floor)

    road_reference = land_reference(roads, 75, 8.0)
    major_reference = land_reference(major, 75, 2.0)
    junction_reference = land_reference(junctions, 75, 20.0)
    coverage_reference = land_reference(coverage, 75, 0.06)
    count_reference = land_reference(counts, 75, 20.0)

    road_support = np.clip(
        0.55 * roads / road_reference
        + 0.20 * major / major_reference
        + 0.25 * junctions / junction_reference,
        0.0, 1.0,
    )
    building_support = np.clip(
        0.65 * coverage / coverage_reference
        + 0.35 * np.log1p(counts) / max(math.log1p(count_reference), 1.0),
        0.0, 1.0,
    )
    neighbor_building = np.asarray([
        _neighbor_mean(building_support, int(cell["row"]),
                       int(cell["column"]), grid_size)
        for cell in ordered
    ])
    continuity = np.clip(
        0.65 * building_support + 0.35 * neighbor_building, 0.0, 1.0)
    completeness = np.clip(
        0.55 * building_support
        + 0.25 * neighbor_building
        + 0.20 * (counts > 0),
        0.0, 1.0,
    )

    building_metrics = (building_metrics
                        if isinstance(building_metrics, Mapping) else {})
    terrain_metrics = (terrain_metrics
                       if isinstance(terrain_metrics, Mapping) else {})
    pressure = _clamp01(building_metrics.get("regularization_pressure"))
    survival = _clamp01(
        building_metrics.get("independent_survival_fraction"))
    terrain_strength = 0.0
    if terrain_metrics.get("status") == "ready":
        terrain_strength = _clamp01(
            0.60 * _number(terrain_metrics.get("rugged_fraction")) / 0.25
            + 0.40 * _number(terrain_metrics.get("relief_to_span")) / 0.02)
    external_support = _external_support(external_urban)

    # A road/building contradiction is strongest when a cell has an urban
    # network but weak footprints while its neighbours or an independent map
    # still support urban continuity.  Without that corroboration it remains
    # open space, not an excuse to fill arbitrary land.
    urban_context = np.maximum(neighbor_building, external_support)
    contradiction = np.clip(
        road_support * (1.0 - building_support)
        * (0.55 + 0.45 * urban_context),
        0.0, 1.0,
    )

    resolved_cells = []
    counts_by_strategy = Counter()
    for index, cell in enumerate(ordered):
        if water[index] >= 0.65:
            strategy = "water"
            confidence = 1.0
            reason = "water occupies most of the cell"
        elif (contradiction[index] >= 0.42
              and road_support[index] >= 0.52
              and urban_context[index] >= 0.30):
            strategy = "block_base_support"
            confidence = max(contradiction[index], urban_context[index])
            reason = "urban road support conflicts with weak building evidence"
        elif completeness[index] >= 0.62:
            if pressure >= 0.30:
                strategy = "neighborhood_mass"
                reason = "building evidence is continuous but printer pressure is high"
            else:
                strategy = "preserve_printable_footprints"
                reason = "building evidence is continuous and bodies are printable"
            confidence = completeness[index]
        elif (completeness[index] >= 0.30
              or (road_support[index] >= 0.38
                  and building_support[index] >= 0.15)):
            strategy = "hybrid_mass"
            confidence = max(completeness[index], road_support[index] * 0.75)
            reason = "partial building evidence needs a mixed carrier"
        else:
            strategy = "open_space_preserve"
            confidence = max(
                1.0 - max(road_support[index], building_support[index]),
                terrain_strength * 0.75,
            )
            reason = "insufficient corroborated urban evidence for filling"

        counts_by_strategy[strategy] += 1
        resolved_cells.append({
            "row": int(cell["row"]),
            "column": int(cell["column"]),
            "bounds": list(cell.get("bounds") or []),
            "strategy": strategy,
            "confidence": round(_clamp01(confidence), 4),
            "reason": reason,
            "scores": {
                "road_support": round(float(road_support[index]), 4),
                "building_support": round(float(building_support[index]), 4),
                "neighbor_building_support": round(
                    float(neighbor_building[index]), 4),
                "distribution_continuity": round(float(continuity[index]), 4),
                "data_completeness": round(float(completeness[index]), 4),
                "road_building_contradiction": round(
                    float(contradiction[index]), 4),
            },
            "weights": dict(_STRATEGY_WEIGHTS[strategy]),
        })

    land_count = max(int(np.count_nonzero(land)), 1)
    urban_supported = land & (road_support >= 0.38)
    urban_count = max(int(np.count_nonzero(urban_supported)), 1)
    land_coverage = coverage[land]
    positive_coverage = land_coverage[land_coverage > 0]
    coverage_cv = (
        float(np.std(positive_coverage) / max(np.mean(positive_coverage), 1e-9))
        if positive_coverage.size else None)
    strategy_fractions = {
        key: round(value / land_count, 4)
        for key, value in sorted(counts_by_strategy.items())
        if key != "water"
    }
    suspected_gap_cells = counts_by_strategy["block_base_support"]
    completeness_score = (
        float(np.mean(completeness[urban_supported]))
        if np.any(urban_supported) else 0.0)
    distribution_score = (
        float(np.mean(continuity[urban_supported]))
        if np.any(urban_supported) else 0.0)
    gap_fraction = suspected_gap_cells / urban_count
    open_fraction = strategy_fractions.get("open_space_preserve", 0.0)
    hybrid_fraction = strategy_fractions.get("hybrid_mass", 0.0)
    if (completeness_score >= 0.82 and distribution_score >= 0.78
            and gap_fraction <= 0.03):
        if open_fraction >= 0.12:
            distribution_profile = "continuous_urban_with_large_open_space"
            recommended_representation = (
                "neighborhood_mass_preserve_open_space_and_crosscheck_water")
        else:
            distribution_profile = "continuous_urban_fabric"
            recommended_representation = (
                "neighborhood_mass_with_selective_hero_footprints")
    elif (gap_fraction >= 0.12 or completeness_score < 0.68
          or distribution_score < 0.58):
        distribution_profile = "fragmented_or_incomplete_urban_evidence"
        recommended_representation = (
            "cross_source_gap_review_then_local_block_support")
    elif (gap_fraction >= 0.06 or hybrid_fraction >= 0.30
          or (coverage_cv is not None and coverage_cv >= 0.85)):
        distribution_profile = "heterogeneous_urban_mosaic"
        recommended_representation = (
            "hybrid_mass_with_local_block_base_support")
    else:
        distribution_profile = "mixed_complete_urban_fabric"
        recommended_representation = (
            "neighborhood_mass_with_hybrid_edges")
    return {
        "version": QUALITY_VERSION,
        "status": "ready",
        "grid_size": grid_size,
        "city_name_lookup": False,
        "geometry_authority": "source vectors only",
        "external_evidence_role": (
            "confidence_only_never_geometry" if external_support > 0
            else "not_available"),
        "printer_context": {
            "regularization_pressure": round(pressure, 5),
            "independent_survival_fraction": round(survival, 5),
        },
        "references": {
            "road_density_p75_floor": round(road_reference, 5),
            "major_road_density_p75_floor": round(major_reference, 5),
            "junction_density_p75_floor": round(junction_reference, 5),
            "building_coverage_p75_floor": round(coverage_reference, 5),
            "building_count_p75_floor": round(count_reference, 5),
        },
        "summary": {
            "urban_supported_cells": int(np.count_nonzero(urban_supported)),
            "suspected_building_gap_cells": suspected_gap_cells,
            "suspected_building_gap_fraction_of_urban": round(
                suspected_gap_cells / urban_count, 4),
            "local_data_completeness_score": round(completeness_score, 4),
            "building_distribution_continuity_score": round(
                distribution_score, 4),
            "positive_building_coverage_cv": (
                round(coverage_cv, 4) if coverage_cv is not None else None),
            "strategy_counts": dict(sorted(counts_by_strategy.items())),
            "strategy_fractions_of_land": strategy_fractions,
            "distribution_profile": distribution_profile,
            "recommended_representation": recommended_representation,
        },
        "cells": resolved_cells,
        "contract": [
            "low building density alone never authorizes Block base",
            "Block base support requires road/building contradiction plus urban context",
            "complete sub-nozzle footprints become neighbourhood mass, not literal needles",
            "this report does not modify geometry, mesh, global Z or booleans",
        ],
    }
