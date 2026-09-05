"""Bounded Z hierarchy for scene-adaptive printable city models.

This module never authors terrain or building geometry.  It only reduces
already-resolved relative building heights, on printer layers, when measured
landform evidence says terrain must own the scene's highest Z role.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Mapping

import numpy as np

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.terrain import (
    sample_terrain_z,
)


POLICY_VERSION = "terrain-owned-building-z-v2"

_TERRAIN_OWNED_ARCHETYPES = {
    "water_terrain_garden_city",
    "mountain_harbour_clustered",
    "terrain_confluence",
    "natural_landscape",
}


def _floor_to_layer(value_mm: float, layer_height_mm: float) -> float:
    layers = math.floor(float(value_mm) / float(layer_height_mm) + 1e-9)
    return round(max(0, layers) * float(layer_height_mm), 10)


def _top_surface_z(vertices: np.ndarray) -> np.ndarray:
    """Return the highest vertex Z at every XY sample.

    A watertight terrain mesh also contains its vertical skirt and bottom.
    Counting those vertices as landform relief makes even a perfectly flat
    terrain appear to have ``base_thickness`` relief.  Height ownership must
    be based on the sampled top surface only.
    """
    xy = np.round(np.asarray(vertices[:, :2], dtype=float), 8)
    order = np.lexsort((xy[:, 1], xy[:, 0]))
    xy_sorted = xy[order]
    z_sorted = np.asarray(vertices[order, 2], dtype=float)
    starts = np.r_[0, np.flatnonzero(np.any(np.diff(xy_sorted, axis=0), axis=1)) + 1]
    return np.maximum.reduceat(z_sorted, starts)


def _minimum_rotated_axis_mm(polygon, scale_mm_per_m: float) -> float:
    rectangle = polygon.minimum_rotated_rectangle
    coords = list(rectangle.exterior.coords)
    if len(coords) < 5:
        return 0.0
    return min(
        math.hypot(
            coords[index + 1][0] - coords[index][0],
            coords[index + 1][1] - coords[index][1],
        )
        for index in range(4)
    ) * float(scale_mm_per_m)


def route_sub_nozzle_heroes(
    layers,
    *,
    scale_mm_per_m: float,
    scene_policy: Mapping,
    printer_profile,
) -> dict:
    """Route unprintable independent heroes into anonymous city mass.

    This is the pre-BuildingMass half of the height hierarchy contract.  It
    only changes semantic ownership (``BL`` -> ``BO``); it never reads terrain
    and never changes Z.  Keeping it separate prevents the old circular order
    where terrain-owned height capping ran before anonymous mass aggregation.
    """

    evidence = {
        "policy_version": POLICY_VERSION,
        "stage_role": "pre_building_mass_routing",
        "geometry_changed": False,
        "global_z_changed": False,
        "only_sub_nozzle_heroes_may_be_demoted": True,
    }
    if scene_policy.get("activation") != "active":
        return {
            **evidence,
            "status": "inactive",
            "reason": "scene policy is audit-only",
            "sub_nozzle_heroes_demoted_to_mass": 0,
        }

    extrusion_width_mm = float(printer_profile.extrusion_width_mm)
    roles = list(getattr(layers, "BL_height_roles", ()) or ())
    categories = list(getattr(layers, "BL_categories", ()) or ())
    if len(roles) != len(layers.BL):
        roles = ["legacy_unspecified"] * len(layers.BL)
    if len(categories) != len(layers.BL):
        categories = [None] * len(layers.BL)

    retained_bl = []
    retained_roles = []
    retained_categories = []
    demoted = []
    for item, role, category in zip(layers.BL, roles, categories):
        polygon, _height = item
        if (_minimum_rotated_axis_mm(polygon, scale_mm_per_m)
                + 1e-9 < extrusion_width_mm):
            demoted.append(polygon)
            continue
        retained_bl.append(item)
        retained_roles.append(role)
        retained_categories.append(category)

    if demoted:
        prior_bo_count = len(layers.BO)
        layers.BO.extend(demoted)
        if len(getattr(layers, "BO_heights", ())) == prior_bo_count:
            layers.BO_heights.extend(
                [float(printer_profile.min_surface_height_mm)] * len(demoted))
        layers.BL = retained_bl
        layers.BL_height_roles = retained_roles
        layers.BL_categories = retained_categories

    return {
        **evidence,
        "status": "active",
        "geometry_changed": bool(demoted),
        "sub_nozzle_heroes_demoted_to_mass": len(demoted),
        "minimum_independent_width_mm": extrusion_width_mm,
    }


def cap_building_heights_to_terrain(
    layers,
    terrain_mesh,
    *,
    scale_mm_per_m: float,
    scene_policy: Mapping,
    printer_profile,
) -> dict:
    """Apply the post-BuildingMass terrain-ownership Z ceiling.

    The input layers must already contain their final anonymous mass and any
    optional region-first height emphasis.  This function can only reduce
    existing relative BL heights; it never edits XY geometry.
    """

    archetype = str(scene_policy.get("archetype") or "")
    terrain_owned = archetype in _TERRAIN_OWNED_ARCHETYPES
    evidence = {
        "policy_version": POLICY_VERSION,
        "stage_role": "post_building_mass_terrain_cap",
        "archetype": archetype,
        "terrain_owned": terrain_owned,
        "geometry_changed": False,
        "global_z_changed": False,
        "only_relative_building_heights_may_decrease": True,
    }
    if scene_policy.get("activation") != "active":
        return {
            **evidence,
            "status": "inactive",
            "reason": "scene policy is audit-only",
        }
    if not terrain_owned:
        return {**evidence, "status": "not_applicable"}
    is_surface_plan = hasattr(terrain_mesh, "surface_z_grid_mm")
    if terrain_mesh is None or (
            not is_surface_plan
            and not len(getattr(terrain_mesh, "vertices", ()))):
        return {
            **evidence,
            "status": "unavailable",
            "reason": "terrain surface plan or mesh missing",
        }
    if not getattr(layers, "BL", None):
        return {**evidence, "status": "ready", "hero_count": 0}

    layer_mm = float(printer_profile.layer_height_mm)
    minimum_height_mm = float(printer_profile.min_surface_height_mm)
    if is_surface_plan:
        top_z = np.asarray(
            terrain_mesh.surface_z_grid_mm, dtype=float).ravel()
        evidence["terrain_source"] = "TerrainSurfacePlan"
        evidence["terrain_surface_fingerprint"] = getattr(
            terrain_mesh, "fingerprint", None)
    else:
        vertices = np.asarray(terrain_mesh.vertices, dtype=float)
        top_z = _top_surface_z(vertices)
        evidence["terrain_source"] = "terrain_mesh_compatibility"
    terrain_peak_mm = float(top_z.max())
    terrain_low_mm = float(np.percentile(top_z, 10))
    terrain_relief_mm = max(0.0, terrain_peak_mm - terrain_low_mm)

    minimum_valid_relief_mm = max(4.0 * layer_mm, minimum_height_mm)
    if terrain_relief_mm < minimum_valid_relief_mm:
        return {
            **evidence,
            "status": "invalid_terrain_evidence",
            "reason": "terrain-owned scene has flat or missing DEM relief",
            "terrain_peak_mm": round(terrain_peak_mm, 5),
            "terrain_low_p10_mm": round(terrain_low_mm, 5),
            "terrain_relief_mm": round(terrain_relief_mm, 5),
            "minimum_valid_relief_mm": round(minimum_valid_relief_mm, 5),
        }

    relative_cap_mm = _floor_to_layer(terrain_relief_mm * 0.65, layer_mm)
    relative_cap_mm = max(minimum_height_mm, relative_cap_mm)
    peak_margin_mm = max(minimum_height_mm, 2.0 * layer_mm)

    # ``sample_terrain_z`` consumes model-mm XY.  BL polygons remain in local
    # metres at this stage, so the conversion is an explicit Stage input.
    scale_mm_per_m = float(scale_mm_per_m)
    if scale_mm_per_m <= 0:
        raise ValueError("scale_mm_per_m must be positive")
    centroids = [polygon.centroid for polygon, _height in layers.BL]
    sample_x = np.asarray([
        point.x * scale_mm_per_m for point in centroids])
    sample_y = np.asarray([
        point.y * scale_mm_per_m for point in centroids])
    if is_surface_plan:
        from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import (
            sample_terrain_surface_plan_z,
        )
        ground = sample_terrain_surface_plan_z(
            terrain_mesh, sample_x, sample_y)
    else:
        ground = sample_terrain_z(terrain_mesh, sample_x, sample_y)

    roles = list(getattr(layers, "BL_height_roles", ()) or ())
    if len(roles) != len(layers.BL):
        roles = ["legacy_unspecified"] * len(layers.BL)

    adjusted = []
    before = []
    after = []
    capped_relative = 0
    capped_peak = 0
    unavoidable_peak_exceptions = 0
    for ((polygon, raw_height), local_ground, _role) in zip(
            layers.BL, ground, roles):
        raw_height = float(raw_height)
        before.append(raw_height)
        maximum_from_peak = _floor_to_layer(
            terrain_peak_mm - peak_margin_mm - float(local_ground), layer_mm)
        allowed = min(raw_height, relative_cap_mm)
        capped_relative += int(allowed + 1e-9 < raw_height)
        if maximum_from_peak >= minimum_height_mm:
            if maximum_from_peak + 1e-9 < allowed:
                capped_peak += 1
            allowed = min(allowed, maximum_from_peak)
        else:
            allowed = min(allowed, minimum_height_mm)
            unavoidable_peak_exceptions += 1
        allowed = max(minimum_height_mm, _floor_to_layer(allowed, layer_mm))
        adjusted.append((polygon, allowed))
        after.append(allowed)

    layers.BL = adjusted
    mapping = dict(getattr(layers, "building_height_evidence", {}) or {})
    mapping.update({
        "height_hierarchy_policy_version": POLICY_VERSION,
        "printable_landmark_count": len(adjusted),
        "printable_height_role_counts": dict(sorted(Counter(roles).items())),
        "model_height_min_mm": round(float(min(after)), 3) if after else None,
        "model_height_p50_mm": (
            round(float(np.percentile(after, 50)), 3) if after else None),
        "model_height_max_mm": round(float(max(after)), 3) if after else None,
    })
    layers.building_height_evidence = mapping

    return {
        **evidence,
        "status": "active",
        "height_changed": any(
            abs(float(left) - float(right)) > 1e-9
            for left, right in zip(before, after)
        ),
        "hero_count": len(adjusted),
        "terrain_peak_mm": round(terrain_peak_mm, 5),
        "terrain_low_p10_mm": round(terrain_low_mm, 5),
        "terrain_relief_mm": round(terrain_relief_mm, 5),
        "relative_height_cap_mm": round(relative_cap_mm, 5),
        "peak_margin_mm": round(peak_margin_mm, 5),
        "before_height_mm": {
            "min": round(float(min(before)), 5),
            "p50": round(float(np.percentile(before, 50)), 5),
            "max": round(float(max(before)), 5),
        },
        "after_height_mm": {
            "min": round(float(min(after)), 5),
            "p50": round(float(np.percentile(after, 50)), 5),
            "max": round(float(max(after)), 5),
        },
        "capped_by_relative_relief": capped_relative,
        "capped_by_peak_ownership": capped_peak,
        "unavoidable_peak_exceptions": unavoidable_peak_exceptions,
    }


def apply_building_height_hierarchy(
    layers,
    terrain_mesh,
    *,
    scale_mm_per_m: float,
    scene_policy: Mapping,
    printer_profile,
) -> dict:
    """Keep terrain-owned scenes vertically legible.

    The policy is deliberately one-way: it may compress an existing building
    height but never raises it.  Anonymous buildings should already have been
    routed to quiet/urban mass before this function runs.
    """

    # Backward-compatible composite entry point.  New orchestration calls the
    # two halves around BuildingMass explicitly.
    routing = route_sub_nozzle_heroes(
        layers,
        scale_mm_per_m=scale_mm_per_m,
        scene_policy=scene_policy,
        printer_profile=printer_profile,
    )
    if scene_policy.get("activation") != "active":
        return routing

    capping = cap_building_heights_to_terrain(
        layers,
        terrain_mesh,
        scale_mm_per_m=scale_mm_per_m,
        scene_policy=scene_policy,
        printer_profile=printer_profile,
    )
    return {
        **capping,
        "geometry_changed": bool(routing.get("geometry_changed")),
        "sub_nozzle_heroes_demoted_to_mass": routing.get(
            "sub_nozzle_heroes_demoted_to_mass", 0),
        "minimum_independent_width_mm": routing.get(
            "minimum_independent_width_mm"),
        "routing": routing,
        "terrain_capping": capping,
    }
