"""Versioned, auditable city-expression policy resolution.

The resolver consumes :mod:`aesthetic.scene_character` evidence and assigns
bounded perceptual roles.  It never emits mesh vertices, global Z values,
boolean instructions, or replacement geometry.  Printer constraints remain
the hard authority; scene policy decides priority only within those bounds.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

from aesthetic.block_grammar import resolve_block_grammar_strategy


SCHEMA_VERSION = "1.0"
POLICY_VERSION = "scene-policy-v6"
VISUAL_GRAMMAR_VERSION = "restrained-three-value-v1"
BUILDING_GRAMMAR_VERSION = "adaptive-local-printable-city-mass-v4"

_ACTIVATION_MODES = {"audit_only", "active"}


def _clamp01(value: Any) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(max(0.0, min(1.0, numeric)), 4)


def _mapping(value: Any) -> dict:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "to_dict"):
        result = value.to_dict()
        return dict(result) if isinstance(result, Mapping) else {}
    return {}


def _score_scene(report: Mapping) -> dict[str, float]:
    summary = report.get("summary", {}) or {}
    metrics = report.get("metrics", {}) or {}
    water = metrics.get("water_topology", {}) or {}
    roads = metrics.get("road_structure", {}) or {}
    buildings = metrics.get("buildings", {}) or {}
    terrain = metrics.get("terrain", {}) or {}
    landform = (terrain.get("landform", {}) or {}).get("scores", {}) or {}
    external_urban = metrics.get("external_urban", {}) or {}

    terrain_score = (
        0.45 * _clamp01(float(terrain.get("relief_to_span") or 0.0) / 0.02)
        + 0.35 * _clamp01(float(terrain.get("rugged_fraction") or 0.0) / 0.25)
        + 0.20 * _clamp01(
            (1.0 - float(terrain.get("buildable_low_slope_fraction") or 1.0))
            / 0.65)
    ) if terrain.get("status") == "ready" else 0.0
    compact_core = (
        0.65 * _clamp01(
            float(buildings.get("height_mass_concentration") or 0.0) / 0.40)
        + 0.25 * _clamp01(
            float(summary.get("dense_core_cell_fraction") or 0.0) / 0.20)
        + 0.10 * _clamp01(
            float(summary.get("landmark_focus_cells") or 0.0) / 3.0)
    )
    significant_height_cells = int(
        buildings.get("significant_height_cells") or 0)
    polycentric = _clamp01((significant_height_cells - 1) / 4.0)
    scores = {
        "coast": _clamp01(water.get("coast_score")),
        "river_axis": _clamp01(water.get("river_axis_score")),
        "water_network": _clamp01(water.get("water_network_score")),
        "island": _clamp01(water.get("island_field_score")),
        "confluence": _clamp01(water.get("confluence_score")),
        "grid": _clamp01(roads.get("orthogonal_grid_score")),
        "ring": _clamp01(roads.get("ring_score")),
        "radial": _clamp01(roads.get("radial_score")),
        "terrain": _clamp01(terrain_score),
        "compact_core": _clamp01(compact_core),
        "polycentric": polycentric,
        "cross_source_urban": (
            _clamp01(external_urban.get("urban_network_support"))
            if external_urban.get("status") == "evidence_only" else 0.0),
        "isolated_prominence": _clamp01(
            landform.get("isolated_prominence")),
        "iconic_peak": _clamp01(landform.get("iconic_peak")),
        "ridge_network": _clamp01(landform.get("ridge_network")),
        "crater_rim": _clamp01(landform.get("crater_rim")),
        "basin": _clamp01(landform.get("basin")),
        "canyon_valley": _clamp01(landform.get("canyon_valley")),
        "repeated_cones": _clamp01(landform.get("repeated_cones")),
        "open_plain": _clamp01(landform.get("open_plain")),
    }
    return {key: round(value, 4) for key, value in scores.items()}


def _resolve_scene_class(scores: Mapping[str, float], report: Mapping) -> str:
    summary = report.get("summary", {}) or {}
    metrics = report.get("metrics", {}) or {}
    buildings = metrics.get("buildings", {}) or {}
    external_urban = metrics.get("external_urban", {}) or {}
    building_coverage = float(
        buildings.get("footprint_frame_coverage") or 0.0)
    water_score = max(
        scores["coast"], scores["river_axis"], scores["water_network"],
        scores["island"], scores["confluence"])
    landform_score = max(
        scores["isolated_prominence"], scores["iconic_peak"],
        scores["ridge_network"], scores["crater_rim"],
        scores["canyon_valley"], scores["repeated_cones"],
        scores["open_plain"],
    )
    cells = report.get("cells", []) or []
    land_cells = [
        cell for cell in cells
        if float(cell.get("water_fraction") or 0.0) < 0.80
    ]
    building_presence_fraction = (
        sum(int(cell.get("building_count") or 0) > 0 for cell in land_cells)
        / len(land_cells)
        if land_cells else 0.0
    )
    feature_counts = report.get("feature_counts", {}) or {}
    building_count = int(feature_counts.get("buildings") or 0)
    vector_urban_support = (
        building_coverage >= 0.0025
        or building_presence_fraction >= 0.15
        or building_count >= 200
    )
    cross_source_urban = (
        external_urban.get("status") == "evidence_only"
        and scores["cross_source_urban"] >= 0.62
        and float(external_urban.get("road_presence_cell_fraction") or 0.0)
        >= 0.55
        and vector_urban_support
    )
    # Cross-source urban spread is resolved before terrain sparsity. A lake,
    # hill or broad river inside a city remains mixed urban geography rather
    # than becoming wilderness merely because OSM footprints are incomplete.
    if cross_source_urban:
        if (water_score >= 0.35 or scores["terrain"] >= 0.35
                or float(summary.get("water_fraction") or 0.0) >= 0.08):
            return "mixed"
        return "urban"
    if ((scores["terrain"] >= 0.62 or landform_score >= 0.55)
            and building_coverage < 0.025):
        return "landscape"
    if water_score >= 0.68 and building_coverage < 0.015:
        return "water_landscape"
    if (water_score >= 0.45 or scores["terrain"] >= 0.45
            or float(summary.get("water_fraction") or 0.0) >= 0.12):
        return "mixed"
    return "urban"


def _resolve_archetype(scores: Mapping[str, float], scene_class: str,
                       report: Mapping) -> str:
    if scene_class in {"landscape", "water_landscape"}:
        water_fraction = float(
            (report.get("summary", {}) or {}).get("water_fraction") or 0.0)
        if scores["crater_rim"] >= 0.52 and water_fraction >= 0.005:
            return "crater_lake_rim"
        if scores["repeated_cones"] >= 0.50:
            return "volcanic_field"
        if (scores["ridge_network"] >= 0.62
                and scores["ridge_network"] > scores["iconic_peak"]):
            return "peak_valley_network"
        if (scores["isolated_prominence"] >= 0.58
                and scores["repeated_cones"] < 0.48):
            return "isolated_monolith"
        if scores["iconic_peak"] >= 0.55:
            return "iconic_peak_ridge"
        if scores["canyon_valley"] >= 0.52:
            return "canyon_valley"
        if ((scores["coast"] >= 0.50 or scores["island"] >= 0.50)
                and scores["terrain"] >= 0.38):
            return "characteristic_shore_relief"
        if scores["ridge_network"] >= 0.48:
            return "peak_valley_network"
        if scene_class == "water_landscape":
            if scores["water_network"] >= 0.55:
                return "wetland_water_network"
            if scores["island"] >= 0.50:
                return "island_water_landscape"
            return "great_lake_shore"
        if scores["open_plain"] >= 0.65:
            return "open_steppe"
        return "terrain_first_sparse_settlement"
    water_score = max(
        scores["coast"], scores["river_axis"], scores["water_network"],
        scores["island"], scores["confluence"])
    external_urban = ((report.get("metrics", {}) or {}).get(
        "external_urban", {}) or {})
    green_land_fraction = float(
        external_urban.get("green_land_fraction") or 0.0)
    if (scene_class == "mixed"
            and scores["cross_source_urban"] >= 0.62
            and water_score >= 0.45
            and (scores["terrain"] >= 0.28
                 or green_land_fraction >= 0.12)):
        return "water_terrain_garden_city"
    if scores["confluence"] >= 0.62 and scores["terrain"] >= 0.42:
        return "terrain_confluence"
    if scores["water_network"] >= 0.58:
        return "water_network_lowrise"
    if scores["island"] >= 0.62 and scores["compact_core"] >= 0.40:
        return "island_metropolis_clustered_core"
    if scores["coast"] >= 0.60 and scores["grid"] >= 0.38:
        return "coast_grid_compact_core"
    if scores["coast"] >= 0.60 and scores["polycentric"] >= 0.35:
        return "coastal_polycentric"
    if scores["river_axis"] >= 0.55 and scores["compact_core"] >= 0.42:
        return "broad_river_compact_core"
    if scores["river_axis"] >= 0.50 and scores["radial"] >= 0.45:
        return "river_radial_lowrise"
    if scores["ring"] >= 0.45:
        return "ring_axis_lowrise"
    if scores["river_axis"] >= 0.48:
        return "meandering_river_lowrise"
    if scores["grid"] >= 0.40:
        return "grid_lowrise"
    if scores["coast"] >= 0.48:
        return "coastal_lowrise"
    return "urban_lowrise"


def _dominant_structure(scores: Mapping[str, float], scene_class: str) -> dict:
    if scene_class in {"landscape", "water_landscape"}:
        priority = (
            "crater_rim", "isolated_prominence", "iconic_peak",
            "canyon_valley", "ridge_network", "repeated_cones", "confluence",
            "coast", "river_axis", "water_network", "island", "terrain",
            "open_plain",
        )
    else:
        # Flatness or a minor landform is context in an urban/mixed crop.  It
        # must never outrank the city-defining coast, river or street system.
        priority = (
            "confluence", "coast", "river_axis", "water_network", "island",
            "terrain", "ring", "radial", "grid", "compact_core",
        )
    ranked = sorted(
        priority, key=lambda key: (-scores.get(key, 0.0), priority.index(key)))
    primary = ranked[0]
    secondary = [key for key in ranked[1:] if scores.get(key, 0.0) >= 0.42][:2]
    return {
        "primary": primary if scores.get(primary, 0.0) >= 0.32 else "none",
        "primary_score": scores.get(primary, 0.0),
        "secondary": secondary,
    }


def resolve_scene_policy(
    scene_character: Mapping,
    *,
    printer_profile: Mapping | Any | None = None,
    activation: str = "audit_only",
) -> dict:
    """Resolve a bounded visual policy from measured scene evidence."""

    if activation not in _ACTIVATION_MODES:
        raise ValueError(f"activation must be one of {sorted(_ACTIVATION_MODES)}")
    if not isinstance(scene_character, Mapping):
        raise TypeError("scene_character must be a mapping")
    version = str(scene_character.get("version") or "")
    if not version.startswith("scene-character-v"):
        raise ValueError("scene_character has no supported version")

    summary = scene_character.get("summary", {}) or {}
    metrics = scene_character.get("metrics", {}) or {}
    confidence = (metrics.get("data_confidence", {}) or {}).get(
        "osm_internal_consistency", summary.get("osm_internal_consistency", "low"))
    scores = _score_scene(scene_character)
    scene_class = _resolve_scene_class(scores, scene_character)
    landscape_mode = scene_class in {"landscape", "water_landscape"}
    archetype = _resolve_archetype(scores, scene_class, scene_character)
    dominant = _dominant_structure(scores, scene_class)
    physical = _mapping(printer_profile)

    water_first = max(
        scores["coast"], scores["river_axis"], scores["water_network"],
        scores["island"], scores["confluence"])
    structural_cut_scale = _clamp01(
        0.50 + 0.24 * scores["grid"] + 0.22 * scores["ring"]
        + 0.12 * scores["water_network"])
    visible_road_scale = round(max(0.78, min(
        1.12,
        0.96 + 0.10 * scores["grid"] + 0.08 * scores["ring"]
        - 0.14 * water_first - 0.08 * scores["terrain"],
    )), 4)
    quiet_texture_scale = round(max(0.65, min(
        1.20,
        0.80 + 0.22 * scores["grid"] + 0.16 * scores["ring"]
        + 0.12 * scores["water_network"],
    )), 4)

    warnings = []
    if confidence == "low":
        warnings.append(
            "low source confidence: policy must remain advisory until data gaps are reviewed")
    elif confidence == "medium":
        warnings.append(
            "medium source confidence: do not interpret local holes as intentional negative space")
    terrain = metrics.get("terrain", {}) or {}
    if terrain.get("status") != "ready":
        warnings.append(
            "terrain evidence unavailable: terrain ownership and landscape classification are provisional")
    buildings = metrics.get("buildings", {}) or {}
    building_quality = metrics.get("building_data_quality", {}) or {}
    quality_summary = building_quality.get("summary", {}) or {}
    strategy_fractions = (
        quality_summary.get("strategy_fractions_of_land", {}) or {})
    local_quality_ready = building_quality.get("status") == "ready"
    local_completeness = _clamp01(
        quality_summary.get("local_data_completeness_score"))
    local_continuity = _clamp01(
        quality_summary.get("building_distribution_continuity_score"))
    block_base_support_fraction = _clamp01(
        strategy_fractions.get("block_base_support"))
    building_distribution_profile = quality_summary.get(
        "distribution_profile", "unknown")
    suspected_building_gap_fraction = _clamp01(
        quality_summary.get("suspected_building_gap_fraction_of_urban"))
    positive_building_coverage_cv = quality_summary.get(
        "positive_building_coverage_cv")
    try:
        positive_building_coverage_cv = float(positive_building_coverage_cv)
    except (TypeError, ValueError):
        positive_building_coverage_cv = None
    if (positive_building_coverage_cv is not None
            and not math.isfinite(positive_building_coverage_cv)):
        positive_building_coverage_cv = None
    building_coverage = float(
        buildings.get("footprint_frame_coverage") or 0.0)
    pressure_available = buildings.get("regularization_pressure") is not None
    regularization_pressure = _clamp01(
        buildings.get("regularization_pressure"))
    quiet_texture_need = _clamp01(
        0.55 * _clamp01(building_coverage / 0.12)
        + 0.45 * regularization_pressure)
    block_grammar_strategy = resolve_block_grammar_strategy(
        buildings.get("block_grammar"),
        local_completeness=local_completeness,
        local_continuity=local_continuity,
        block_base_support_fraction=block_base_support_fraction,
        external_urban_support=scores["cross_source_urban"],
        grid_score=scores["grid"],
        water_network_score=max(
            scores["water_network"], scores["river_axis"], scores["coast"]),
    )
    if block_grammar_strategy.get("status") == "ready":
        grammar_aggregation = _clamp01(
            block_grammar_strategy.get("aggregation_pressure"))
        grammar_texture = _clamp01(
            block_grammar_strategy.get("texture_promotion_pressure"))
        quiet_texture_need = max(
            quiet_texture_need,
            _clamp01(0.55 * grammar_texture + 0.35 * grammar_aggregation),
        )
    else:
        grammar_aggregation = 0.0
        grammar_texture = 0.0
    if landscape_mode:
        building_simplification = "suppress_urban_mass"
    elif local_quality_ready and block_base_support_fraction >= 0.08:
        building_simplification = "adaptive_local_mass_and_block_support"
    elif not pressure_available:
        building_simplification = "measure_before_activation"
    elif regularization_pressure >= 0.65:
        building_simplification = "merge_and_regularize"
    elif regularization_pressure >= 0.30:
        building_simplification = "selective_merge_and_regularize"
    else:
        building_simplification = "preserve_printable_compact_bodies"
    if not landscape_mode and block_grammar_strategy.get("status") == "ready":
        grammar_mode = block_grammar_strategy.get("mode")
        if grammar_mode == "promote_sub_nozzle_texture_into_bounded_blocks":
            building_simplification = "bounded_texture_promotion_and_regularization"
        elif grammar_mode == "select_and_aggregate_complete_dense_source":
            building_simplification = "select_aggregate_and_regularize"
        elif grammar_mode == "aggregate_and_regularize":
            building_simplification = "scale_aware_aggregate_and_regularize"
    independent_body_budget_scale = round(max(
        0.15,
        1.0 - 0.72 * regularization_pressure
        - 0.25 * grammar_aggregation
        - 0.15 * _clamp01(
            block_grammar_strategy.get("selection_pressure")),
    ), 4)
    if buildings.get("sub_nozzle_fraction") is None:
        warnings.append(
            "printer-scaled building width evidence unavailable")
    if not local_quality_ready:
        warnings.append(
            "local building distribution evidence unavailable: use frame-wide building policy only")
    landform = terrain.get("landform", {}) or {}
    if scene_class == "landscape" and landform.get("status") != "ready":
        warnings.append(
            "landscape classification lacks ready landform evidence")

    landscape_strategy = {
        "enabled": landscape_mode,
        "archetype": archetype if landscape_mode else None,
        "geometry_owner": "DEM" if landscape_mode else None,
        "urban_block_base": "disable" if landscape_mode else "urban_policy",
        "structural_bottom": "closed_terrain_mating_solid",
        "water": (
            "independent_planar_surface_with_shared_shoreline"
            if landscape_mode else "urban_water_policy"),
        "protected_evidence": (
            [
                key for key in (
                    "isolated_prominence", "iconic_peak", "ridge_network",
                    "crater_rim", "canyon_valley", "repeated_cones")
                if scores[key] >= 0.42
            ] if landscape_mode else []),
        "human_features": (
            "sparse_verified_context_only" if landscape_mode
            else "urban_policy"),
        "z_rule": (
            "bounded_deterministic_relief_mapping_from_DEM_and_print_profile"
            if landscape_mode else "urban_policy"),
    }
    garden_city_mode = archetype == "water_terrain_garden_city"
    external_urban = metrics.get("external_urban", {}) or {}
    garden_city_strategy = {
        "enabled": garden_city_mode,
        "urban_block_base": "retain" if garden_city_mode else None,
        "urban_mass_topology_tier": 2 if garden_city_mode else None,
        "major_water": (
            "dominant_continuous_composition" if garden_city_mode else None),
        "green_and_terrain": (
            "preserve_as_broad_quiet_relief_and_negative_space"
            if garden_city_mode else None),
        "road_network": (
            "continuous_city_evidence_with_restrained_visible_hierarchy"
            if garden_city_mode else None),
        "urban_mass": (
            "quiet_mid_frequency_density_carrier" if garden_city_mode
            else None),
        "cross_source_urban_support": (
            scores["cross_source_urban"] if garden_city_mode else None),
        "cross_source_green_fraction": (
            float(external_urban.get("green_land_fraction") or 0.0)
            if garden_city_mode else None),
        "constraint": (
            "policy roles only; no external raster geometry enters mesh, Z "
            "or boolean operations"
        ),
    }

    decisions = [
        {
            "key": "scene_class",
            "value": scene_class,
            "reason": "resolved from water, terrain and urban coverage evidence",
        },
        {
            "key": "archetype",
            "value": archetype,
            "reason": "strongest compatible scene-character scores",
        },
        {
            "key": "dominant_structure",
            "value": dominant["primary"],
            "reason": f"highest score={dominant['primary_score']:.4f}",
        },
        {
            "key": "visible_road_budget_scale",
            "value": visible_road_scale,
            "reason": "identity structure first; water/terrain scenes reserve more negative space",
        },
        {
            "key": "quiet_texture_budget_scale",
            "value": quiet_texture_scale,
            "reason": "restore density through low-contrast texture instead of uniformly loud roads",
        },
        {
            "key": "building_simplification",
            "value": building_simplification,
            "reason": (
                "printer-scaled independent-body survival and footprint "
                f"regularization pressure={regularization_pressure:.4f}"
            ),
        },
        {
            "key": "building_quiet_texture_need",
            "value": quiet_texture_need,
            "reason": "preserve urban density as low relief when literal footprints cannot survive",
        },
        {
            "key": "building_local_data_strategy",
            "value": (
                "adaptive" if local_quality_ready else "frame_wide_fallback"),
            "reason": (
                "local completeness="
                f"{local_completeness:.4f}, continuity={local_continuity:.4f}, "
                f"block-base-support={block_base_support_fraction:.4f}"
            ),
        },
        {
            "key": "building_block_grammar_strategy",
            "value": block_grammar_strategy.get("mode", "unavailable"),
            "reason": (
                "printer-scaled source granularity, silhouette regularity, "
                "orientation and spatial fullness"
            ),
        },
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "source_scene_version": version,
        "activation": activation,
        "decision_contract": {
            "measurement_authority": "scene_character evidence",
            "geometry_authority": "project source vectors and DEM",
            "print_authority": "printer profile hard constraints",
            "aesthetic_authority": "versioned deterministic visual grammar",
            "forbidden_controls": [
                "mesh vertices", "global Z values", "boolean operations",
                "invented replacement geometry",
            ],
        },
        "scene_class": scene_class,
        "archetype": archetype,
        "dominant_structure": dominant,
        "scores": scores,
        "visual_grammar": {
            "profile": VISUAL_GRAMMAR_VERSION,
            "building_profile": BUILDING_GRAMMAR_VERSION,
            "roles": {
                "black_structural_negative": [
                    "major_water", "identity_defining_incisions"],
                "neutral_substrate": [
                    "terrain", "block_field", "quiet_urban_mass"],
                "white_relief_texture": [
                    "hero_buildings", "urban_mass", "quiet_density_texture"],
            },
            "contrast_rule": (
                "reserve high contrast for complete identity structures; "
                "encode supporting density with relief and shadow"),
        },
        "roles": {
            "water": {
                "primary": "continuous_negative_space",
                "secondary": "identity_support",
                "minor": "retain_only_coherent_printable_network",
                "priority_scale": round(0.85 + 0.15 * water_first, 4),
            },
            "roads": {
                "identity": (
                    "verified_scenic_route_only" if landscape_mode
                    else "complete_structural_cut"),
                "context": (
                    "sparse_scale_reference" if landscape_mode
                    else "quiet_low_contrast_texture"),
                "topology": "may_cut_block_base_without_visible_ink",
                "structural_cut_density_scale": structural_cut_scale,
                "visible_road_budget_scale": visible_road_scale,
            },
            "buildings": {
                "grammar": BUILDING_GRAMMAR_VERSION,
                "hero_identity": (
                    "verified_visitor_landmark_only" if landscape_mode
                    else "trusted_named_or_vertical_anchor"),
                "urban_mass": (
                    "suppress" if landscape_mode
                    else "merge_anonymous_footprints_by_neighbourhood"),
                "quiet_texture": (
                    "none" if landscape_mode
                    else "low_relief_density_carrier"),
                "relief_tiers": {
                    "quiet_texture": "layer_quantized_low_relief",
                    "urban_mass": "layer_quantized_standard_relief",
                    "hero_identity": "trusted_height_bounded_emphasis",
                },
                "simplification_mode": building_simplification,
                "regularization_pressure": regularization_pressure,
                "independent_body_budget_scale": (
                    0.0 if landscape_mode else independent_body_budget_scale),
                "quiet_texture_need": (
                    0.0 if landscape_mode else quiet_texture_need),
                "width_gate": "selected_printer_profile_hard_floor",
                "shape_goal": (
                    "compact_softened_neighbourhood_masses_not_raw_slivers"),
                "height_owner": (
                    "terrain" if landscape_mode or scores["terrain"] >= 0.62
                    else "compact_core" if scores["compact_core"] >= 0.45
                    else "lowrise_field"),
                "post_subtraction_sliver_guard": True,
                "local_strategy": (
                    building_quality if local_quality_ready else {
                        "status": "unavailable",
                        "reason": "source scene report predates local building quality",
                    }),
                "block_base_policy": (
                    "disable_for_landscape" if landscape_mode
                    else "adaptive_local_support" if local_quality_ready
                    else "legacy_frame_wide"),
                "local_data_completeness_score": local_completeness,
                "building_distribution_continuity_score": local_continuity,
                "distribution_profile": building_distribution_profile,
                "suspected_building_gap_fraction_of_urban": (
                    suspected_building_gap_fraction),
                "positive_building_coverage_cv": (
                    positive_building_coverage_cv),
                "footprint_frame_coverage": building_coverage,
                "recommended_representation": quality_summary.get(
                    "recommended_representation"),
                "block_base_support_fraction": block_base_support_fraction,
                "block_grammar_measurement": buildings.get("block_grammar"),
                "block_grammar_strategy": block_grammar_strategy,
            },
            "terrain": {
                "role": (
                    "primary_identity_relief" if landscape_mode
                    else "primary_relief" if scores["terrain"] >= 0.62
                    else "neutral_substrate"),
                "preserve_water_planarity": True,
            },
            "density": {
                "quiet_texture_budget_scale": quiet_texture_scale,
                "rule": "density is carried by low relief before extra dark ink",
            },
        },
        "landscape_strategy": landscape_strategy,
        "garden_city_strategy": garden_city_strategy,
        "tradeoff_order": {
            "hard_constraints": [
                "source_truth", "print_survival", "valid_closed_geometry"],
            "optimization_priority": (
                [
                    "landform_identity", "silhouette_and_rim_continuity",
                    "water_terrain_relationship", "relief_hierarchy",
                    "sparse_human_context",
                ] if landscape_mode else [
                    "lake_river_terrain_relationship",
                    "continuous_but_quiet_city_network",
                    "green_and_water_negative_space",
                    "mid_frequency_urban_mass",
                    "ordinary_feature_fidelity",
                ] if garden_city_mode else [
                    "city_identity", "dominant_structure_continuity",
                    "visual_hierarchy", "mid_frequency_density",
                    "ordinary_feature_fidelity"]),
        },
        "printer_profile": physical,
        "data_confidence": confidence,
        "decisions": decisions,
        "warnings": warnings,
    }


def write_scene_policy(output_dir: os.PathLike | str,
                       policy: Mapping, *,
                       filename: str = "scene_policy.json") -> str:
    """Atomically persist an optionally attempt-scoped scene policy."""

    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    if Path(filename).name != filename or not filename.endswith(".json"):
        raise ValueError("scene policy filename must be a plain JSON name")
    destination = directory / filename
    payload = json.dumps(policy, ensure_ascii=False,
                         indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=directory,
        prefix=".scene_policy.", suffix=".tmp", delete=False,
    ) as stream:
        temporary = Path(stream.name)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, destination)
    return str(destination)
