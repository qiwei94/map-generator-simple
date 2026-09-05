"""Region-first height emphasis for printable city mass.

The accepted pipeline historically selected individual source buildings as
height heroes before neighbourhood mass aggregation.  That is useful when
source identity is trustworthy, but it also produces isolated needles and
prevents the selected footprints from participating in coherent city mass.

This experimental policy deliberately reverses that order:

1. source building heroes are returned to the low building channel;
2. the existing building-mass stage aggregates them inside road/water blocks;
3. a few measured urban cells are selected as height-emphasis zones;
4. already-aggregated, printable mass components are promoted unchanged.

It never authors footprints, merges across topology blocks, changes roads or
water, sets absolute Z, or performs mesh Boolean operations.
"""

from __future__ import annotations

from collections import Counter
import math
from typing import Mapping, Sequence

import numpy as np
from shapely.geometry import box


POLICY_VERSION = "height-emphasis-zones-v1"
_TRUSTED_HEIGHT_SOURCES = {
    "osm_height", "osm_levels", "wikidata", "overture",
}


def _quantized_layers(layers: int, layer_height_mm: float) -> float:
    return round(int(layers) * float(layer_height_mm), 10)


def _rotated_axes(polygon) -> tuple[float, float]:
    rectangle = polygon.minimum_rotated_rectangle
    coords = list(rectangle.exterior.coords)
    if len(coords) < 5:
        return 0.0, 0.0
    lengths = sorted(
        math.hypot(
            coords[index + 1][0] - coords[index][0],
            coords[index + 1][1] - coords[index][1],
        )
        for index in range(4)
    )
    return float(lengths[0]), float(lengths[-1])


def _safe_float(value, default: float = 0.0) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    return numeric if math.isfinite(numeric) else default


def prepare_region_first_height_roles(layers) -> dict:
    """Return all source BL footprints to BO before mass aggregation.

    Geometry is moved, not copied or modified.  The building-mass stage that
    follows becomes the sole author of quiet/mass BO heights.
    """

    source = list(getattr(layers, "BL", ()) or ())
    roles = list(getattr(layers, "BL_height_roles", ()) or ())
    if len(roles) != len(source):
        roles = ["legacy_unspecified"] * len(source)
    categories = list(getattr(layers, "BL_categories", ()) or ())
    if len(categories) != len(source):
        categories = [None] * len(source)

    prior_bo = list(getattr(layers, "BO", ()) or ())
    prior_heights = list(getattr(layers, "BO_heights", ()) or ())
    layers.BO = prior_bo + [polygon for polygon, _height in source]
    # Mass composition replaces these values.  Keeping a partial parallel
    # array here would be more dangerous than explicitly clearing it.
    if prior_heights and len(prior_heights) == len(prior_bo):
        minimum = min(prior_heights)
        layers.BO_heights = prior_heights + [minimum] * len(source)
    else:
        layers.BO_heights = []
    layers.BL = []
    layers.BL_height_roles = []
    layers.BL_categories = []

    return {
        "policy_version": POLICY_VERSION,
        "status": "prepared",
        "demoted_source_heroes": len(source),
        "demoted_role_counts": dict(sorted(Counter(roles).items())),
        "geometry_changed": False,
        "geometry_moved_between_semantic_channels": bool(source),
        "source_footprints_preserved": True,
        "forbidden_controls_untouched": [
            "terrain mesh", "global Z", "mesh booleans",
            "road geometry", "water geometry",
        ],
    }


def _trusted_heights_by_cell(buildings, cells: Sequence[Mapping]) -> list[list[float]]:
    result = [[] for _ in cells]
    if buildings is None or len(buildings) == 0 or not cells:
        return result
    if "height_source" not in buildings.columns:
        return result
    height_column = next(
        (name for name in ("est_height", "height_m", "height")
         if name in buildings.columns),
        None,
    )
    if height_column is None:
        return result

    cell_boxes = [box(*cell["bounds"]) for cell in cells]
    for _, row in buildings.iterrows():
        source = str(row.get("height_source") or "").lower()
        if source not in _TRUSTED_HEIGHT_SOURCES:
            continue
        height = _safe_float(row.get(height_column), default=float("nan"))
        geometry = row.get("geometry")
        if not math.isfinite(height) or height <= 0 or geometry is None or geometry.is_empty:
            continue
        point = geometry.centroid
        for index, cell_box in enumerate(cell_boxes):
            if cell_box.covers(point):
                result[index].append(height)
                break
    return result


def _score_cells(cells: Sequence[Mapping]) -> list[dict]:
    coverages = np.asarray([
        _safe_float(cell.get("building_coverage")) for cell in cells
    ], dtype=float)
    positive = coverages[coverages > 0]
    coverage_p90 = float(np.percentile(positive, 90)) if positive.size else 1.0
    scored = []
    for index, cell in enumerate(cells):
        bounds = [float(value) for value in cell["bounds"]]
        water = _safe_float(cell.get("water_fraction"))
        count = int(cell.get("building_count") or 0)
        urban = _safe_float(cell.get("urban_signal"))
        if water >= 0.75 or count <= 0 or urban < 0.45:
            continue
        coverage = min(1.0, _safe_float(cell.get("building_coverage")) /
                       max(coverage_p90, 1e-9))
        landmark = min(1.0, _safe_float(cell.get("landmark_focus_score")) /
                       0.30)
        neighbour = _safe_float(cell.get("neighbor_urban_signal"))
        # This score decides attention, never geometry.  Local density and
        # continuity dominate; landmark evidence is a bounded tie-breaker.
        score = (
            0.38 * coverage
            + 0.27 * urban
            + 0.20 * neighbour
            + 0.15 * landmark
        )
        scored.append({
            "cell_index": index,
            "row": int(cell.get("row") or 0),
            "column": int(cell.get("column") or 0),
            "bounds": bounds,
            "score": round(float(score), 6),
            "building_count": count,
            "building_coverage": round(_safe_float(
                cell.get("building_coverage")), 6),
            "urban_signal": round(urban, 6),
            "landmark_focus_score": round(_safe_float(
                cell.get("landmark_focus_score")), 6),
        })
    return sorted(scored, key=lambda item: (-item["score"], item["row"], item["column"]))


def _select_zones(scored: Sequence[Mapping], maximum: int) -> list[dict]:
    selected: list[dict] = []
    for candidate in scored:
        # Suppress direct grid neighbours so one dense downtown does not spend
        # the entire scene's height budget.  Diagonal cells are also adjacent.
        if any(
            abs(candidate["row"] - existing["row"]) <= 1
            and abs(candidate["column"] - existing["column"]) <= 1
            for existing in selected
        ):
            continue
        selected.append(dict(candidate))
        if len(selected) >= maximum:
            break
    return selected


def apply_height_emphasis_zones(
    layers,
    buildings,
    scene_character: Mapping,
    scene_policy: Mapping,
    *,
    printer_profile,
    scale_mm_per_m: float,
    building_mass_evidence: Mapping,
) -> dict:
    """Promote a few unchanged aggregate BO components into height roles."""

    evidence = {
        "policy_version": POLICY_VERSION,
        "status": "inactive",
        "geometry_authorship": "existing aggregated building-mass components",
        "new_footprints_created": 0,
        "cross_topology_merges": 0,
        "absolute_z_control": False,
    }
    if scene_policy.get("activation") != "active":
        return {**evidence, "reason": "scene policy is audit-only"}
    if building_mass_evidence.get("status") != "active":
        return {**evidence, "reason": "building mass is not active"}
    cells = list(scene_character.get("cells", ()) or ())
    if not cells:
        return {**evidence, "reason": "scene character cells unavailable"}

    bo = list(getattr(layers, "BO", ()) or ())
    heights = list(getattr(layers, "BO_heights", ()) or ())
    if not bo or len(heights) != len(bo):
        return {**evidence, "reason": "building mass heights are unavailable"}

    maximum_zones = max(1, min(4, int(
        (scene_character.get("summary", {}) or {}).get(
            "landmark_focus_cell_limit", 4) or 4)))
    selected = _select_zones(_score_cells(cells), maximum_zones)
    if not selected:
        return {**evidence, "reason": "no eligible urban cells"}

    trusted = _trusted_heights_by_cell(buildings, cells)
    global_trusted = [value for values in trusted for value in values]
    global_p50 = float(np.percentile(global_trusted, 50)) if global_trusted else None
    global_p90 = float(np.percentile(global_trusted, 90)) if global_trusted else None

    urban_mass_height = max(float(value) for value in heights)
    layer_mm = float(printer_profile.layer_height_mm)
    min_width_mm = float(printer_profile.min_colored_strip_mm)
    target_width_mm = max(1.30, 2.25 * min_width_mm)
    maximum_width_mm = max(3.0, 2.25 * target_width_mm)
    promoted_indexes: list[int] = []
    promoted_records: list[dict] = []

    for zone in selected:
        cell_index = int(zone["cell_index"])
        zone_box = box(*zone["bounds"])
        values = trusted[cell_index]
        p75 = float(np.percentile(values, 75)) if values else None
        height_signal = (
            min(1.0, p75 / max(global_p90 or p75 or 1.0, 1.0))
            if p75 is not None else 0.0)
        combined_signal = 0.70 * float(zone["score"]) + 0.30 * height_signal
        tier_layers = 10 if combined_signal < 0.62 else 12
        if combined_signal >= 0.80:
            tier_layers = 14
        requested_height = max(
            urban_mass_height + layer_mm,
            _quantized_layers(tier_layers, layer_mm),
        )

        candidates = []
        for index, (polygon, current_height) in enumerate(zip(bo, heights)):
            if index in promoted_indexes:
                continue
            if abs(float(current_height) - urban_mass_height) > 1e-8:
                continue
            if not zone_box.covers(polygon.centroid):
                continue
            short_m, long_m = _rotated_axes(polygon)
            short_mm = short_m * float(scale_mm_per_m)
            long_mm = long_m * float(scale_mm_per_m)
            aspect = long_mm / max(short_mm, 1e-9)
            if short_mm + 1e-9 < min_width_mm or aspect > 4.0:
                continue
            if short_mm > maximum_width_mm:
                continue
            compactness = 4.0 * math.pi * float(polygon.area) / max(
                float(polygon.length) ** 2, 1e-9)
            width_fit = math.exp(-abs(short_mm - target_width_mm) /
                                 max(target_width_mm, 1e-9))
            center_distance = polygon.centroid.distance(zone_box.centroid)
            diagonal = math.hypot(
                zone["bounds"][2] - zone["bounds"][0],
                zone["bounds"][3] - zone["bounds"][1],
            )
            centrality = max(0.0, 1.0 - center_distance / max(diagonal, 1.0))
            rank = 0.45 * centrality + 0.35 * width_fit + 0.20 * compactness
            candidates.append((rank, -index, index, short_mm, long_mm, aspect))
        candidates.sort(reverse=True)
        chosen = candidates[:3]
        zone["trusted_height_count"] = len(values)
        zone["trusted_height_p75_m"] = round(p75, 3) if p75 is not None else None
        zone["requested_height_mm"] = round(requested_height, 3)
        zone["promoted_components"] = len(chosen)
        for _rank, _stable, index, short_mm, long_mm, aspect in chosen:
            promoted_indexes.append(index)
            promoted_records.append({
                "source_bo_index": index,
                "cell_index": cell_index,
                "requested_height_mm": round(requested_height, 3),
                "short_axis_mm": round(short_mm, 4),
                "long_axis_mm": round(long_mm, 4),
                "aspect_ratio": round(aspect, 4),
                "area_m2": round(float(bo[index].area), 3),
            })

    if not promoted_indexes:
        return {
            **evidence,
            "reason": "eligible cells contain no printable urban-mass components",
            "selected_zones": selected,
        }

    promoted_set = set(promoted_indexes)
    retained_bo = []
    retained_heights = []
    promoted_by_index = {item["source_bo_index"]: item for item in promoted_records}
    for index, (polygon, height) in enumerate(zip(bo, heights)):
        if index in promoted_set:
            record = promoted_by_index[index]
            layers.BL.append((polygon, float(record["requested_height_mm"])))
            layers.BL_height_roles.append("zone_height_mass")
            layers.BL_categories.append(None)
        else:
            retained_bo.append(polygon)
            retained_heights.append(float(height))
    layers.BO = retained_bo
    layers.BO_heights = retained_heights

    mapping = dict(getattr(layers, "building_height_evidence", {}) or {})
    mapping.update({
        "height_emphasis_policy_version": POLICY_VERSION,
        "height_emphasis_zone_count": len(selected),
        "height_emphasis_component_count": len(promoted_records),
        "height_emphasis_role": "zone_height_mass",
    })
    layers.building_height_evidence = mapping

    return {
        **evidence,
        "status": "active",
        "selected_zone_count": len(selected),
        "promoted_component_count": len(promoted_records),
        "urban_mass_height_mm": round(urban_mass_height, 3),
        "global_trusted_height_count": len(global_trusted),
        "global_trusted_height_p50_m": (
            round(global_p50, 3) if global_p50 is not None else None),
        "global_trusted_height_p90_m": (
            round(global_p90, 3) if global_p90 is not None else None),
        "selected_zones": selected,
        "promoted_components": promoted_records,
        "minimum_width_mm": min_width_mm,
        "maximum_aspect_ratio": 4.0,
        "road_water_topology_preserved": True,
        "forbidden_controls_untouched": [
            "terrain mesh", "global Z", "mesh booleans",
            "road geometry", "water geometry",
        ],
    }
