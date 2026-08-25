"""Deterministic city-identity metrics for framing and showcase ranking.

The scores describe visible, large-scale structure: river/coast contrast,
ring roads, radial avenues, orthogonal grids, relief and density gradients.
They only assess/select a composition and never control mesh geometry, Z
values, booleans or material assignment.
"""
from __future__ import annotations

import math
from typing import Iterable

import numpy as np


_MAJOR_HIGHWAYS = {
    "motorway", "motorway_link", "trunk", "trunk_link",
    "primary", "primary_link", "secondary", "secondary_link",
}
_ROAD_SAMPLE_LIMIT = 30_000
_SEGMENT_SAMPLE_LIMIT = 120_000


def _empty_topology() -> dict:
    return {
        "ring_score": 0.0,
        "radial_score": 0.0,
        "grid_score": 0.0,
        "road_signature_score": 0.0,
        "sampled_road_features": 0,
        "sampled_segments": 0,
        "traits": [],
    }


def _iter_lines(geometry) -> Iterable[object]:
    if geometry is None or geometry.is_empty:
        return
    if geometry.geom_type == "LineString":
        yield geometry
    elif geometry.geom_type == "MultiLineString":
        yield from (line for line in geometry.geoms if not line.is_empty)


def _sample_major_roads(roads_gdf):
    if roads_gdf is None or len(roads_gdf) == 0:
        return None
    roads = roads_gdf
    if "highway" in roads.columns:
        tags = roads["highway"].fillna("").astype(str)
        major = roads.loc[tags.isin(_MAJOR_HIGHWAYS)]
        if len(major) >= 4:
            roads = major
    roads = roads.loc[roads.geometry.geom_type.isin(
        ("LineString", "MultiLineString"))]
    if len(roads) > _ROAD_SAMPLE_LIMIT:
        positions = np.linspace(
            0, len(roads) - 1, _ROAD_SAMPLE_LIMIT).astype(int)
        roads = roads.iloc[positions]
    return roads


def _road_segments(roads) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return segment midpoints, orientations and length weights."""
    if roads is None or len(roads) == 0:
        return (np.empty((0, 2)), np.empty(0), np.empty(0))
    midpoints = []
    angles = []
    weights = []
    per_feature_limit = max(2, _SEGMENT_SAMPLE_LIMIT // max(1, len(roads)))
    for geometry in roads.geometry:
        for line in _iter_lines(geometry) or ():
            coords = np.asarray(line.coords, dtype=float)
            if len(coords) < 2:
                continue
            first, second = coords[:-1], coords[1:]
            delta = second - first
            length = np.hypot(delta[:, 0], delta[:, 1])
            valid = length >= 20.0
            if not valid.any():
                continue
            first, second, delta, length = (
                value[valid] for value in (first, second, delta, length))
            if len(length) > per_feature_limit:
                sample = np.linspace(
                    0, len(length) - 1, per_feature_limit).astype(int)
                first, second, delta, length = (
                    value[sample] for value in (first, second, delta, length))
            midpoints.extend((first + second) * 0.5)
            angles.extend(np.arctan2(delta[:, 1], delta[:, 0]))
            # One unusually long motorway segment must not dominate a score.
            weights.extend(np.minimum(length, 1500.0))
    if not weights:
        return (np.empty((0, 2)), np.empty(0), np.empty(0))
    if len(weights) > _SEGMENT_SAMPLE_LIMIT:
        sample = np.linspace(
            0, len(weights) - 1, _SEGMENT_SAMPLE_LIMIT).astype(int)
        return (np.asarray(midpoints)[sample], np.asarray(angles)[sample],
                np.asarray(weights)[sample])
    return np.asarray(midpoints), np.asarray(angles), np.asarray(weights)


def _angular_coverage(angles: np.ndarray, weights: np.ndarray,
                      radius: float, ideal_bins: int) -> float:
    if not len(angles) or float(weights.sum()) <= 0:
        return 0.0
    bins = 16
    indexes = np.floor((angles % (2 * math.pi)) / (2 * math.pi) * bins)
    indexes = np.clip(indexes.astype(int), 0, bins - 1)
    totals = np.bincount(indexes, weights=weights, minlength=bins)
    occupied = int((totals >= max(radius * 0.025,
                                  float(weights.sum()) * 0.012)).sum())
    return min(1.0, occupied / max(1, ideal_bins))


def _grid_score(angles: np.ndarray, midpoints: np.ndarray,
                weights: np.ndarray, bbox_local) -> float:
    if len(angles) < 12 or float(weights.sum()) <= 0:
        return 0.0
    # Orthogonal directions collapse to one bearing modulo 90 degrees.
    folded = angles % (math.pi / 2)
    bins = 18
    indexes = np.floor(folded / (math.pi / 2) * bins).astype(int) % bins
    hist = np.bincount(indexes, weights=weights, minlength=bins)
    smooth = hist + np.roll(hist, 1) + np.roll(hist, -1)
    peak_share = float(smooth.max() / max(hist.sum() * 3.0, 1e-9))
    orientation = float(np.clip((peak_share - 0.08) / 0.30, 0.0, 1.0))

    minx, miny, maxx, maxy = bbox_local
    gx = np.clip(((midpoints[:, 0] - minx) / max(maxx - minx, 1.0) * 8)
                 .astype(int), 0, 7)
    gy = np.clip(((midpoints[:, 1] - miny) / max(maxy - miny, 1.0) * 8)
                 .astype(int), 0, 7)
    spatial = min(1.0, len(set((gx * 8 + gy).tolist())) / 32.0)
    return float(np.clip(orientation * math.sqrt(spatial), 0.0, 1.0))


def analyze_road_topology(roads_gdf, bbox_local) -> dict:
    """Measure metro-scale ring, radial and grid structure in projected roads."""
    roads = _sample_major_roads(roads_gdf)
    if roads is None or len(roads) == 0:
        return _empty_topology()
    midpoints, angles, weights = _road_segments(roads)
    if len(weights) < 4:
        result = _empty_topology()
        result["sampled_road_features"] = len(roads)
        result["sampled_segments"] = len(weights)
        return result

    minx, miny, maxx, maxy = bbox_local
    cx, cy = (minx + maxx) * 0.5, (miny + maxy) * 0.5
    radius = max(1.0, min(maxx - minx, maxy - miny) * 0.5)
    offset = midpoints - np.array([cx, cy])
    distance = np.hypot(offset[:, 0], offset[:, 1])
    position_angle = np.arctan2(offset[:, 1], offset[:, 0])
    difference = np.abs(
        (angles - position_angle + math.pi / 2) % math.pi - math.pi / 2)

    ring_zone = (distance >= radius * 0.16) & (distance <= radius * 0.62)
    tangent = ring_zone & (difference >= math.radians(58))
    ring_base = float(weights[ring_zone].sum()) if ring_zone.any() else 0.0
    tangent_weight = float(weights[tangent].sum()) if tangent.any() else 0.0
    ring_alignment = tangent_weight / max(ring_base, 1e-9)
    ring_coverage = _angular_coverage(
        position_angle[tangent], weights[tangent], radius, ideal_bins=10)
    ring_length = min(
        1.0, tangent_weight / max(2 * math.pi * radius * 0.32, 1.0))
    ring_score = ring_alignment * math.sqrt(ring_coverage * ring_length)

    radial_zone = (distance >= radius * 0.05) & (distance <= radius * 0.78)
    radial = radial_zone & (difference <= math.radians(28))
    radial_base = float(weights[radial_zone].sum()) if radial_zone.any() else 0.0
    radial_weight = float(weights[radial].sum()) if radial.any() else 0.0
    radial_alignment = radial_weight / max(radial_base, 1e-9)
    radial_coverage = _angular_coverage(
        position_angle[radial], weights[radial], radius, ideal_bins=8)
    # Four to eight long spokes are already a strong metro-scale signature;
    # do not require their capped sample weights to equal several full radii.
    radial_length = min(1.0, radial_weight / max(radius * 1.5, 1.0))
    radial_score = radial_alignment * math.sqrt(
        radial_coverage * radial_length)

    grid_score = _grid_score(angles, midpoints, weights, bbox_local)
    signature = max(ring_score, radial_score, grid_score * 0.85)
    traits = []
    if ring_score >= 0.45:
        traits.append("ring")
    if radial_score >= 0.45:
        traits.append("radial")
    if grid_score >= 0.55:
        traits.append("grid")
    return {
        "ring_score": round(float(np.clip(ring_score, 0.0, 1.0)), 4),
        "radial_score": round(float(np.clip(radial_score, 0.0, 1.0)), 4),
        "grid_score": round(float(np.clip(grid_score, 0.0, 1.0)), 4),
        "road_signature_score": round(float(np.clip(signature, 0.0, 1.0)), 4),
        "sampled_road_features": len(roads),
        "sampled_segments": len(weights),
        "traits": traits,
    }


def compare_visible_road_signature(source_roads, visible_roads,
                                   bbox_local) -> dict:
    """Check that road filtering preserves the source frame's main trait.

    A visible network is not allowed to substitute a different attractive
    pattern: if the source frame is ring-led, retaining an unrelated grid does
    not count as preserving identity.  Weak/ambiguous source frames remain
    non-blocking and are reported explicitly.
    """

    source = analyze_road_topology(source_roads, bbox_local)
    visible = analyze_road_topology(visible_roads, bbox_local)

    def strengths(topology):
        return {
            "ring": float(topology["ring_score"]),
            "radial": float(topology["radial_score"]),
            # Match the weighting used by road_signature_score.
            "grid": float(topology["grid_score"]) * 0.85,
        }

    source_strengths = strengths(source)
    visible_strengths = strengths(visible)
    dominant_trait = max(source_strengths, key=source_strengths.get)
    source_strength = source_strengths[dominant_trait]
    visible_strength = visible_strengths[dominant_trait]

    source_is_distinctive = source_strength >= 0.35
    required_traits = []
    if source_is_distinctive:
        # A city can genuinely be ring+radial (Beijing) or grid+ring
        # (Chicago).  Treat strengths within five points of the leader as one
        # composition contract; preserving only one can still draw the city
        # incorrectly while the old single-score check reports success.
        required_traits = [
            trait for trait, strength in source_strengths.items()
            if strength >= 0.35 and source_strength - strength <= 0.05
        ]
    trait_results = {}
    for trait in required_traits:
        required = max(0.22, source_strengths[trait] * 0.60)
        retained = visible_strengths[trait] >= required
        trait_results[trait] = {
            "source_strength": round(source_strengths[trait], 4),
            "visible_strength": round(visible_strengths[trait], 4),
            "required_visible_strength": round(required, 4),
            "passed": bool(retained),
        }
    passed = (not source_is_distinctive
              or all(result["passed"] for result in trait_results.values()))
    required_visible_strength = max(0.22, source_strength * 0.60)
    if not source_is_distinctive:
        reason = "source frame has no dominant road signature"
    elif passed:
        reason = ("visible network preserves source traits: "
                  + ", ".join(required_traits))
    else:
        lost = [trait for trait, result in trait_results.items()
                if not result["passed"]]
        reason = "visible network lost source traits: " + ", ".join(lost)
    return {
        "passed": bool(passed),
        "dominant_trait": dominant_trait,
        "required_traits": required_traits,
        "trait_results": trait_results,
        "source_is_distinctive": bool(source_is_distinctive),
        "source_strength": round(source_strength, 4),
        "visible_strength": round(visible_strength, 4),
        "required_visible_strength": round(required_visible_strength, 4),
        "reason": reason,
        "source": source,
        "visible": visible,
    }


def _profile_value(profile, name: str, default=0.0):
    if isinstance(profile, dict):
        return profile.get(name, default)
    return getattr(profile, name, default)


def _center_concentration(buildings_gdf, bbox_local) -> float:
    if buildings_gdf is None or len(buildings_gdf) < 20:
        return 0.0
    buildings = buildings_gdf
    if len(buildings) > 50_000:
        positions = np.linspace(0, len(buildings) - 1, 50_000).astype(int)
        buildings = buildings.iloc[positions]
    centers = buildings.geometry.centroid
    points = np.column_stack((centers.x.to_numpy(), centers.y.to_numpy()))
    minx, miny, maxx, maxy = bbox_local
    cx, cy = (minx + maxx) * 0.5, (miny + maxy) * 0.5
    radius = max(1.0, min(maxx - minx, maxy - miny) * 0.5)
    distance = np.hypot(points[:, 0] - cx, points[:, 1] - cy)
    inner_r, outer_r = radius * 0.22, radius * 0.62
    inner_count = int((distance <= inner_r).sum())
    outer_count = int(((distance > inner_r) & (distance <= outer_r)).sum())
    if inner_count < 10 or outer_count < 10:
        return 0.0
    inner_density = inner_count / (math.pi * inner_r ** 2)
    outer_density = outer_count / (
        math.pi * (outer_r ** 2 - inner_r ** 2))
    ratio = inner_density / max(outer_density, 1e-12)
    return float(np.clip((ratio - 1.0) / 3.0, 0.0, 1.0))


def analyze_city_signature(roads_gdf, buildings_gdf, bbox_local, profile,
                           water_framing: dict, scene_type: str) -> dict:
    """Combine characteristic points without letting one signal hide bad data."""
    topology = analyze_road_topology(roads_gdf, bbox_local)
    center_score = _center_concentration(buildings_gdf, bbox_local)
    water_ratio = float(_profile_value(profile, "water_ratio", 0.0) or 0.0)
    # A high framing score from a few tiny ponds must not label a dry city as
    # water-led.  Ramp from no evidence below 0.2% of the frame to full
    # evidence at 1%; a genuinely frame-defining river, lake or coast easily
    # clears that threshold at showcase scale.
    water_presence = float(np.clip((water_ratio - 0.002) / 0.008, 0.0, 1.0))
    water_score = (float(water_framing.get("narrative_score") or 0.0)
                   * water_presence)
    relief_score = min(
        1.0, float(_profile_value(profile, "elevation_range_m", 0.0)) / 250.0)
    geography_score = max(water_score, relief_score)

    signals = [
        geography_score,
        topology["ring_score"],
        topology["radial_score"],
        topology["grid_score"] * 0.85,
        center_score * 0.70,
    ]
    strongest = sorted(signals, reverse=True)
    feature_score = 0.70 * strongest[0] + 0.30 * strongest[1]

    quality = str(_profile_value(profile, "osm_quality", "poor"))
    quality_base = {"good": 1.0, "fair": 0.72, "poor": 0.25}.get(
        quality, 0.25)
    road_density = float(_profile_value(
        profile, "road_density_km_per_km2", 0.0) or 0.0)
    building_density = float(_profile_value(
        profile, "building_density", 0.0) or 0.0)
    if scene_type == "urban":
        evidence = min(1.0, 0.55 * min(1.0, road_density / 5.0)
                       + 0.45 * min(1.0, building_density / 200.0))
    else:
        evidence = max(
            min(1.0, road_density / 2.0),
            min(1.0, water_ratio / 0.05),
            relief_score,
        )
    confidence = 0.55 * quality_base + 0.45 * evidence

    hard_failures = []
    if scene_type == "urban" and road_density <= 0:
        hard_failures.append("urban road extraction returned zero density")
    if scene_type == "urban" and building_density <= 0:
        hard_failures.append("urban building extraction returned zero density")
    if water_ratio >= 0.92 and building_density >= 20:
        hard_failures.append("implausible water coverage for populated frame")

    overall = feature_score * confidence
    traits = list(topology["traits"])
    if water_score >= 0.45:
        traits.append("water")
    if relief_score >= 0.45:
        traits.append("relief")
    if center_score >= 0.45:
        traits.append("urban_core")
    return {
        "overall": round(float(np.clip(overall, 0.0, 1.0)), 4),
        "feature_score": round(float(np.clip(feature_score, 0.0, 1.0)), 4),
        "confidence": round(float(np.clip(confidence, 0.0, 1.0)), 4),
        "geography_score": round(float(geography_score), 4),
        "center_concentration_score": round(float(center_score), 4),
        "road_topology": topology,
        "traits": traits,
        "hard_failures": hard_failures,
        "showcase_candidate": not hard_failures and overall >= 0.22,
    }
