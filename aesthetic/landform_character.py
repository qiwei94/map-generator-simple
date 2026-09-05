"""Deterministic DEM evidence for natural-landscape scene policy.

This module measures a height field; it does not modify terrain, choose global
Z, construct meshes, or run booleans.  The scores are crop-relative evidence
used by :mod:`aesthetic.scene_policy` while that policy remains audit-only.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from scipy.ndimage import (
    gaussian_filter,
    label,
    maximum_filter,
    minimum_filter,
)


LANDFORM_ANALYSIS_VERSION = "landform-character-v1"


def _clamp01(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))


def _round(value: Any, digits: int = 5):
    if value is None:
        return None
    return round(float(value), digits)


def _elongation(mask: np.ndarray, weights: np.ndarray | None = None) -> float:
    rows, columns = np.nonzero(mask)
    if len(rows) < 4:
        return 0.0
    points = np.column_stack((columns, rows)).astype(float)
    if weights is None:
        covariance = np.cov(points, rowvar=False)
    else:
        selected = np.asarray(weights, dtype=float)[mask]
        selected = np.maximum(selected, 1e-6)
        center = np.average(points, axis=0, weights=selected)
        centered = points - center
        covariance = ((centered * selected[:, None]).T @ centered
                      / max(float(selected.sum()), 1e-9))
    eigenvalues = np.linalg.eigvalsh(covariance)
    if eigenvalues[-1] <= 1e-12:
        return 0.0
    axis_ratio = math.sqrt(max(eigenvalues[0], 0.0) / eigenvalues[-1])
    return _clamp01(1.0 - axis_ratio)


def _largest_component_share(mask: np.ndarray) -> float:
    if not mask.any():
        return 0.0
    components, count = label(mask)
    if count == 0:
        return 0.0
    sizes = np.bincount(components.ravel())[1:]
    return float(sizes.max() / max(sizes.sum(), 1))


def _crater_evidence(surface: np.ndarray) -> dict:
    """Find a crop-relative closed rim around a local depression."""

    rows, columns = surface.shape
    span = min(rows, columns)
    yy, xx = np.indices(surface.shape)
    local_minima = surface == minimum_filter(
        surface, size=max(3, int(round(span * 0.08)) | 1), mode="nearest")
    margin = max(3, int(round(span * 0.10)))
    eligible = local_minima.copy()
    eligible[:margin, :] = False
    eligible[-margin:, :] = False
    eligible[:, :margin] = False
    eligible[:, -margin:] = False
    eligible &= surface <= np.percentile(surface, 45)
    candidates = np.column_stack(np.nonzero(eligible))
    if len(candidates) == 0:
        return {
            "crater_rim_score": 0.0,
            "rim_closure": 0.0,
            "rim_relief_fraction": 0.0,
            "basin_score": 0.0,
            "center_normalized": None,
            "radius_frame_fraction": None,
        }
    candidates = sorted(
        candidates.tolist(), key=lambda item: surface[item[0], item[1]])[:48]
    radii = np.unique(np.linspace(
        max(4, span * 0.055), span * 0.27, 9).astype(int))
    best = None
    for row, column in candidates:
        distance = np.hypot(yy - row, xx - column)
        for radius in radii:
            inner = distance <= max(2.0, radius * 0.36)
            annulus = ((distance >= radius * 0.78)
                       & (distance <= radius * 1.18))
            if inner.sum() < 4 or annulus.sum() < 16:
                continue
            center_level = float(np.median(surface[inner]))
            ring_level = float(np.percentile(surface[annulus], 55))
            relief = max(0.0, ring_level - center_level)
            angles = (np.arctan2(yy - row, xx - column) + 2 * math.pi) % (
                2 * math.pi)
            sector_levels = []
            for sector in range(24):
                sector_mask = annulus & (
                    (angles >= sector * 2 * math.pi / 24)
                    & (angles < (sector + 1) * 2 * math.pi / 24))
                if sector_mask.any():
                    sector_levels.append(float(np.median(surface[sector_mask])))
            closure = (sum(level >= center_level + 0.055
                           for level in sector_levels)
                       / max(len(sector_levels), 1))
            weakest_clearance = max(
                0.0, min(sector_levels, default=center_level) - center_level)
            depression = _clamp01(relief / 0.18)
            closure_score = _clamp01((closure - 0.82) / 0.18)
            weakest_sector_score = _clamp01(weakest_clearance / 0.065)
            radius_score = _clamp01(
                1.0 - abs(radius / span - 0.15) / 0.15)
            score = (depression
                     * (0.58 * closure_score + 0.22 * weakest_sector_score
                        + 0.20 * radius_score)
                     * (0.30 + 0.70 * weakest_sector_score))
            candidate = (score, closure, relief, row, column, radius)
            if best is None or candidate > best:
                best = candidate
    if best is None:
        return {
            "crater_rim_score": 0.0,
            "rim_closure": 0.0,
            "rim_relief_fraction": 0.0,
            "basin_score": 0.0,
            "center_normalized": None,
            "radius_frame_fraction": None,
        }
    score, closure, relief, row, column, radius = best
    return {
        "crater_rim_score": _round(score),
        "rim_closure": _round(closure),
        "rim_relief_fraction": _round(relief),
        "basin_score": _round(
            _clamp01(relief / 0.12) * _clamp01((closure - 0.30) / 0.55)),
        "center_normalized": [
            _round(column / max(columns - 1, 1), 4),
            _round(row / max(rows - 1, 1), 4),
        ],
        "radius_frame_fraction": _round(radius / span, 4),
    }


def unavailable_landform_character(reason: str = "DEM unavailable") -> dict:
    return {
        "version": LANDFORM_ANALYSIS_VERSION,
        "status": "unavailable",
        "reason": reason,
        "scores": {
            "isolated_prominence": 0.0,
            "iconic_peak": 0.0,
            "ridge_network": 0.0,
            "crater_rim": 0.0,
            "basin": 0.0,
            "canyon_valley": 0.0,
            "repeated_cones": 0.0,
            "open_plain": 0.0,
        },
    }


def analyze_landform_character(elevation_grid, bbox_local) -> dict:
    """Measure landform identity evidence from a projected DEM crop.

    Scores are intentionally relative to the current frame.  They help choose
    an archetype and framing review; they are not a geological classification.
    """

    if elevation_grid is None:
        return unavailable_landform_character()
    try:
        values = np.asarray(elevation_grid, dtype=float)
    except Exception:
        return unavailable_landform_character("DEM cannot be converted to an array")
    if values.ndim != 2 or min(values.shape) < 9:
        return unavailable_landform_character("DEM grid is smaller than 9x9")
    finite = np.isfinite(values)
    if finite.mean() < 0.80:
        return unavailable_landform_character("DEM has too many missing samples")
    filled = values.copy()
    filled[~finite] = float(np.median(values[finite]))

    p02, p05, p50, p95, p98 = np.percentile(
        filled, [2, 5, 50, 95, 98]).astype(float)
    relief = max(p95 - p05, 0.0)
    robust_range = max(p98 - p02, 1e-9)
    normalized = np.clip((filled - p02) / robust_range, 0.0, 1.0)
    span_cells = min(values.shape)
    fine = gaussian_filter(normalized, sigma=max(0.8, span_cells / 160.0))
    broad = gaussian_filter(normalized, sigma=max(2.0, span_cells / 10.0))
    topographic_position = fine - broad

    xmin, ymin, xmax, ymax = (float(value) for value in bbox_local)
    frame_width = max(xmax - xmin, 1.0)
    frame_height = max(ymax - ymin, 1.0)
    frame_span = max(frame_width, frame_height)
    cell_x = frame_width / max(values.shape[1] - 1, 1)
    cell_y = frame_height / max(values.shape[0] - 1, 1)
    gradient_y, gradient_x = np.gradient(filled, cell_y, cell_x)
    slope = np.degrees(np.arctan(np.hypot(gradient_x, gradient_y)))
    relief_to_span = relief / frame_span
    relief_strength = _clamp01(relief_to_span / 0.018)

    peak_window = max(3, int(round(span_cells * 0.10)) | 1)
    maxima = fine == maximum_filter(fine, size=peak_window, mode="nearest")
    peak_threshold = max(0.055, float(np.percentile(topographic_position, 94)))
    peak_candidates = maxima & (topographic_position >= peak_threshold)
    peak_candidates &= fine >= np.percentile(fine, 72)
    peak_regions, peak_region_count = label(peak_candidates)
    selected_peaks = []
    for region in range(1, peak_region_count + 1):
        region_rows, region_columns = np.nonzero(peak_regions == region)
        if len(region_rows) == 0:
            continue
        region_values = topographic_position[region_rows, region_columns]
        best_index = int(np.argmax(region_values))
        selected_peaks.append((
            float(region_values[best_index]),
            int(region_rows[best_index]),
            int(region_columns[best_index]),
        ))
    selected_peaks.sort(reverse=True)
    peak_prominences = np.asarray(
        [item[0] for item in selected_peaks], dtype=float)
    peak_rows = np.asarray([item[1] for item in selected_peaks], dtype=int)
    peak_columns = np.asarray([item[2] for item in selected_peaks], dtype=int)
    significant_peak_count = int(len(peak_prominences))
    strongest_prominence = float(peak_prominences[0]) if significant_peak_count else 0.0
    second_prominence = float(peak_prominences[1]) if significant_peak_count > 1 else 0.0
    peak_dominance = _clamp01(
        (strongest_prominence - second_prominence * 0.65) / 0.16)

    if significant_peak_count:
        peak_row, peak_column = int(peak_rows[0]), int(peak_columns[0])
        center_distance = math.hypot(
            peak_column / max(values.shape[1] - 1, 1) - 0.5,
            peak_row / max(values.shape[0] - 1, 1) - 0.5,
        ) / math.sqrt(0.5)
        peak_centering = _clamp01(1.0 - center_distance)
        peak_location = [
            _round(peak_column / max(values.shape[1] - 1, 1), 4),
            _round(peak_row / max(values.shape[0] - 1, 1), 4),
        ]
    else:
        peak_centering = 0.0
        peak_location = None

    yy, xx = np.indices(values.shape)
    edge_distance = np.minimum.reduce((
        yy, xx, values.shape[0] - 1 - yy, values.shape[1] - 1 - xx))
    peripheral = edge_distance <= max(2, int(round(span_cells * 0.13)))
    slope_p90 = float(np.percentile(slope, 90))
    peripheral_flatness = _clamp01(
        1.0 - float(np.percentile(slope[peripheral], 75)) / 12.0)
    multi_peak_factor = _clamp01((significant_peak_count - 1) / 5.0)

    highland = fine >= np.percentile(fine, 78)
    highland_elongation = _elongation(highland, np.maximum(fine - 0.5, 0.0))
    highland_continuity = _largest_component_share(highland)
    rugged_fraction = float(np.mean(slope >= 15.0))
    prominence_strength = _clamp01(strongest_prominence / 0.24)
    isolated_prominence = (
        relief_strength * prominence_strength
        * (0.35 + 0.65 * peak_dominance)
        * (0.25 + 0.75 * peripheral_flatness)
        * (1.0 - 0.55 * highland_elongation)
        * (1.0 - 0.65 * multi_peak_factor)
    )
    iconic_peak = (
        relief_strength * prominence_strength
        * (0.55 + 0.25 * peak_dominance + 0.20 * peak_centering)
        * (1.0 - 0.35 * highland_elongation)
        * (1.0 - 0.40 * multi_peak_factor)
    )
    ridge_network = (
        relief_strength * _clamp01((highland_elongation - 0.12) / 0.72)
        * (0.55 + 0.45 * highland_continuity)
    )

    valley_threshold = float(np.percentile(topographic_position, 12))
    valley_mask = topographic_position <= min(-0.035, valley_threshold)
    valley_depth = max(0.0, -float(np.percentile(topographic_position, 4)))
    valley_elongation = _elongation(
        valley_mask, np.maximum(-topographic_position, 0.0))
    valley_continuity = _largest_component_share(valley_mask)
    canyon_valley = (
        relief_strength * _clamp01(valley_depth / 0.18)
        * _clamp01((valley_elongation - 0.15) / 0.70)
        * (0.55 + 0.45 * valley_continuity)
    )

    crater = _crater_evidence(fine)
    crater["crater_rim_score"] = _round(
        float(crater["crater_rim_score"]) * relief_strength)
    crater["basin_score"] = _round(
        float(crater["basin_score"]) * relief_strength)
    repeated_cones = (
        relief_strength
        * multi_peak_factor
        * _clamp01(strongest_prominence / 0.16)
        * (0.75 + 0.25 * peripheral_flatness)
    )
    open_plain = (
        _clamp01(1.0 - relief_to_span / 0.008)
        * _clamp01(1.0 - slope_p90 / 7.0)
    )

    scores = {
        "isolated_prominence": _round(_clamp01(isolated_prominence), 4),
        "iconic_peak": _round(_clamp01(iconic_peak), 4),
        "ridge_network": _round(_clamp01(ridge_network), 4),
        "crater_rim": _round(crater["crater_rim_score"], 4),
        "basin": _round(crater["basin_score"], 4),
        "canyon_valley": _round(_clamp01(canyon_valley), 4),
        "repeated_cones": _round(_clamp01(repeated_cones), 4),
        "open_plain": _round(_clamp01(open_plain), 4),
    }
    landform_priority = (
        "crater_rim", "isolated_prominence", "iconic_peak",
        "canyon_valley", "repeated_cones", "ridge_network", "open_plain",
        "basin",
    )
    ranked = sorted(
        scores,
        key=lambda key: (-scores[key], landform_priority.index(key)),
    )
    return {
        "version": LANDFORM_ANALYSIS_VERSION,
        "status": "ready",
        "measurement_scope": "current_crop_relative",
        "elevation": {
            "p02_m": _round(p02, 3),
            "p05_m": _round(p05, 3),
            "median_m": _round(p50, 3),
            "p95_m": _round(p95, 3),
            "p98_m": _round(p98, 3),
            "robust_relief_m": _round(relief, 3),
            "relief_to_span": _round(relief_to_span, 6),
            "slope_p90_deg": _round(slope_p90, 3),
            "rugged_fraction": _round(rugged_fraction),
            "peripheral_flatness": _round(peripheral_flatness),
        },
        "peaks": {
            "significant_count": significant_peak_count,
            "strongest_prominence_fraction": _round(strongest_prominence),
            "dominance": _round(peak_dominance),
            "primary_centering": _round(peak_centering),
            "primary_location_normalized": peak_location,
        },
        "ridges": {
            "highland_elongation": _round(highland_elongation),
            "highland_continuity": _round(highland_continuity),
        },
        "depressions": {
            **crater,
            "valley_depth_fraction": _round(valley_depth),
            "valley_elongation": _round(valley_elongation),
            "valley_continuity": _round(valley_continuity),
        },
        "scores": scores,
        "dominant_landform": (
            ranked[0] if scores[ranked[0]] >= 0.35 else "undetermined"),
        "warning": (
            "Landform scores are deterministic crop-relative evidence, not "
            "geological labels; framing and source resolution must be reviewed."
        ),
    }
