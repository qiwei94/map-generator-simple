"""Deterministic framing advice derived from measured water geometry.

This module only recommends a composition size.  It never changes mesh,
height, boolean, or material parameters.
"""
from __future__ import annotations

import math


def _iter_lines(geometry):
    if geometry is None or geometry.is_empty:
        return
    if geometry.geom_type == "LineString":
        yield geometry
    elif geometry.geom_type == "MultiLineString":
        yield from geometry.geoms


def _line_bend_score(line, frame_width: float, frame_height: float) -> float:
    """Score a river centreline for visible span and directional change."""
    coords = list(line.coords)
    if len(coords) < 3 or line.length <= 0:
        return 0.0

    chord = math.hypot(coords[-1][0] - coords[0][0],
                       coords[-1][1] - coords[0][1])
    sinuosity = line.length / max(chord, line.length * 0.05, 1.0)
    sinuosity_score = min(1.0, max(0.0, (sinuosity - 1.0) / 0.45))

    headings = []
    for first, second in zip(coords, coords[1:]):
        dx, dy = second[0] - first[0], second[1] - first[1]
        if math.hypot(dx, dy) >= 1.0:
            headings.append(math.atan2(dy, dx))
    turn = 0.0
    for before, after in zip(headings, headings[1:]):
        delta = (after - before + math.pi) % (2 * math.pi) - math.pi
        turn += abs(delta)
    turn_score = min(1.0, turn / math.pi)

    minx, miny, maxx, maxy = line.bounds
    span = max((maxx - minx) / max(frame_width, 1.0),
               (maxy - miny) / max(frame_height, 1.0))
    span_score = min(1.0, span / 0.55)
    return round((0.45 * max(sinuosity_score, turn_score)
                  + 0.55 * span_score), 4)


def analyze_water_framing(water_gdf, bbox_local,
                          water_ratio: float = 0.0) -> dict:
    """Recommend 15 or 25 km from real water coverage and river curvature.

    A 25 km frame is recommended only when there is measured evidence: a
    long/bending river centreline or enough water area to create a meaningful
    land-water contrast.  Missing geometry falls back to 15 km.
    """
    minx, miny, maxx, maxy = bbox_local
    frame_width, frame_height = maxx - minx, maxy - miny
    line_scores = []
    line_count = 0
    if water_gdf is not None and len(water_gdf) > 0:
        for geometry in water_gdf.geometry:
            for line in _iter_lines(geometry) or ():
                line_count += 1
                line_scores.append(_line_bend_score(
                    line, frame_width, frame_height))

    bend_score = max(line_scores, default=0.0)
    contrast_score = min(1.0, max(0.0, float(water_ratio)) / 0.12)
    narrative_score = round(0.65 * bend_score + 0.35 * contrast_score, 4)
    recommend_25 = bend_score >= 0.45 or (
        contrast_score >= 0.5 and narrative_score >= 0.34)

    if bend_score >= 0.45:
        reason = "检测到贯穿或转折明显的河流，25 km 更能保留水陆反差与城市两岸关系"
    elif contrast_score >= 0.5:
        reason = "水体占比突出，25 km 更适合呈现完整岸线和周边城市纹理"
    else:
        reason = "水系叙事证据较弱，15 km 更利于保留局部细节"
    return {
        "recommended_size_km": 25 if recommend_25 else 15,
        "river_bend_score": bend_score,
        "water_contrast_score": round(contrast_score, 4),
        "narrative_score": narrative_score,
        "water_line_count": line_count,
        "reason": reason,
    }
