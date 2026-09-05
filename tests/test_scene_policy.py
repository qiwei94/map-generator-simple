import json

import pytest

from aesthetic.scene_policy import resolve_scene_policy, write_scene_policy


def _report(*, water=None, roads=None, buildings=None, terrain=None,
            landform=None, confidence="high"):
    return {
        "version": "scene-character-v2",
        "summary": {
            "water_fraction": 0.0,
            "dense_core_cell_fraction": 0.0,
            "landmark_focus_cells": 0,
            "osm_internal_consistency": confidence,
        },
        "metrics": {
            "water_topology": {
                "coast_score": 0.0,
                "river_axis_score": 0.0,
                "water_network_score": 0.0,
                "island_field_score": 0.0,
                "confluence_score": 0.0,
                **(water or {}),
            },
            "road_structure": {
                "orthogonal_grid_score": 0.0,
                "ring_score": 0.0,
                "radial_score": 0.0,
                **(roads or {}),
            },
            "buildings": {
                "footprint_frame_coverage": 0.08,
                "height_mass_concentration": 0.0,
                "significant_height_cells": 0,
                "sub_nozzle_fraction": 0.5,
                "sub_nozzle_area_fraction": 0.4,
                "independent_survival_fraction": 0.35,
                "regularization_pressure": 0.48,
                **(buildings or {}),
            },
            "terrain": {
                "status": "ready",
                "relief_to_span": 0.0,
                "rugged_fraction": 0.0,
                "buildable_low_slope_fraction": 1.0,
                "landform": {
                    "status": "ready",
                    "scores": {
                        "isolated_prominence": 0.0,
                        "iconic_peak": 0.0,
                        "ridge_network": 0.0,
                        "crater_rim": 0.0,
                        "basin": 0.0,
                        "canyon_valley": 0.0,
                        "repeated_cones": 0.0,
                        "open_plain": 0.0,
                        **(landform or {}),
                    },
                },
                **(terrain or {}),
            },
            "data_confidence": {
                "osm_internal_consistency": confidence,
                "reason": "fixture",
            },
        },
    }


def test_coast_grid_scene_resolves_compact_core_archetype():
    report = _report(
        water={"coast_score": 0.9},
        roads={"orthogonal_grid_score": 0.75},
        buildings={"height_mass_concentration": 0.42},
    )
    report["summary"]["dense_core_cell_fraction"] = 0.22

    policy = resolve_scene_policy(report)

    assert policy["scene_class"] == "mixed"
    assert policy["archetype"] == "coast_grid_compact_core"
    assert policy["dominant_structure"]["primary"] == "coast"
    assert policy["visual_grammar"]["profile"] == (
        "restrained-three-value-v1")
    assert policy["visual_grammar"]["building_profile"] == (
        "adaptive-local-printable-city-mass-v4")
    assert policy["activation"] == "audit_only"


def test_flat_terrain_does_not_override_an_urban_coast_identity():
    policy = resolve_scene_policy(_report(
        water={"coast_score": 0.78},
        roads={"orthogonal_grid_score": 0.70},
        buildings={"footprint_frame_coverage": 0.20},
        landform={"open_plain": 0.91},
    ))

    assert policy["scene_class"] == "mixed"
    assert policy["dominant_structure"]["primary"] == "coast"
    assert policy["landscape_strategy"]["enabled"] is False


def test_cross_source_city_network_prevents_false_landscape_classification():
    report = _report(
        water={"river_axis_score": 0.62},
        buildings={"footprint_frame_coverage": 0.001},
        terrain={"status": "unavailable"},
    )
    report["feature_counts"] = {"buildings": 800}
    report["metrics"]["external_urban"] = {
        "status": "evidence_only",
        "urban_network_support": 0.91,
        "road_presence_cell_fraction": 0.88,
        "green_land_fraction": 0.16,
    }

    policy = resolve_scene_policy(report)

    assert policy["scene_class"] == "mixed"
    assert policy["archetype"] == "water_terrain_garden_city"
    assert policy["landscape_strategy"]["enabled"] is False
    assert policy["garden_city_strategy"]["enabled"] is True
    assert policy["garden_city_strategy"]["urban_block_base"] == "retain"
    assert policy["garden_city_strategy"]["urban_mass_topology_tier"] == 2
    assert policy["roles"]["buildings"]["urban_mass"] != "suppress"


def test_single_external_corridor_does_not_override_real_landscape():
    report = _report(
        buildings={"footprint_frame_coverage": 0.001},
        terrain={
            "relief_to_span": 0.05,
            "rugged_fraction": 0.45,
            "buildable_low_slope_fraction": 0.20,
        },
    )
    report["feature_counts"] = {"buildings": 20}
    report["metrics"]["external_urban"] = {
        "status": "evidence_only",
        "urban_network_support": 0.25,
        "road_presence_cell_fraction": 0.18,
    }

    policy = resolve_scene_policy(report)

    assert policy["scene_class"] == "landscape"
    assert policy["landscape_strategy"]["enabled"] is True


def test_ring_city_gets_ring_axis_policy_without_city_name_lookup():
    policy = resolve_scene_policy(_report(
        roads={"ring_score": 0.82, "orthogonal_grid_score": 0.35}))

    assert policy["archetype"] == "ring_axis_lowrise"
    assert policy["roles"]["roads"]["identity"] == (
        "complete_structural_cut")
    assert policy["roles"]["roads"]["visible_road_budget_scale"] <= 1.12


def test_building_pressure_selects_two_tier_merge_strategy_without_mesh_control():
    policy = resolve_scene_policy(_report(buildings={
        "footprint_frame_coverage": 0.14,
        "sub_nozzle_fraction": 0.91,
        "sub_nozzle_area_fraction": 0.74,
        "independent_survival_fraction": 0.06,
        "regularization_pressure": 0.86,
    }))

    buildings = policy["roles"]["buildings"]
    assert policy["policy_version"] == "scene-policy-v6"
    assert buildings["grammar"] == "adaptive-local-printable-city-mass-v4"
    assert buildings["simplification_mode"] == "merge_and_regularize"
    assert buildings["independent_body_budget_scale"] < 0.4
    assert buildings["quiet_texture_need"] > 0.8
    assert buildings["relief_tiers"]["hero_identity"] == (
        "trusted_height_bounded_emphasis")
    assert "mesh vertices" in policy["decision_contract"]["forbidden_controls"]


def test_block_grammar_measurement_becomes_bounded_building_policy():
    report = _report(buildings={
        "footprint_frame_coverage": 0.03,
        "regularization_pressure": 0.55,
        "block_grammar": {
            "status": "ready",
            "source_footprint_count": 50_000,
            "spatial_fullness": {"footprint_frame_coverage": 0.03},
            "printable_comparable_sample": {
                "metrics": {
                    "short_axis_mm": {"p50": 0.32},
                    "solidity": {"p50": 0.75},
                    "rectangularity": {"p50": 0.62},
                    "perimeter_excess": {"p50": 1.22},
                },
                "orientation": {"orthogonal_coherence": 0.82},
            },
        },
    })
    report["metrics"]["external_urban"] = {
        "status": "evidence_only",
        "urban_network_support": 0.88,
    }

    policy = resolve_scene_policy(report)
    buildings = policy["roles"]["buildings"]
    strategy = buildings["block_grammar_strategy"]

    assert strategy["status"] == "ready"
    assert strategy["aggregation_pressure"] > 0.5
    assert strategy["outline_regularization_pressure"] > 0.5
    assert strategy["orientation_preservation_pressure"] == 0.82
    assert buildings["simplification_mode"] == (
        "bounded_texture_promotion_and_regularization")
    forbidden = policy["decision_contract"]["forbidden_controls"]
    assert "mesh vertices" in forbidden
    assert "global Z values" in forbidden
    assert "boolean operations" in forbidden


def test_policy_consumes_local_distribution_strategy_without_city_lookup():
    report = _report(buildings={
        "footprint_frame_coverage": 0.08,
        "regularization_pressure": 0.70,
    })
    report["metrics"]["building_data_quality"] = {
        "version": "local-building-quality-v1",
        "status": "ready",
        "city_name_lookup": False,
        "summary": {
            "local_data_completeness_score": 0.61,
            "building_distribution_continuity_score": 0.58,
            "suspected_building_gap_fraction_of_urban": 0.12,
            "positive_building_coverage_cv": 0.91,
            "distribution_profile": "heterogeneous_urban_mosaic",
            "strategy_fractions_of_land": {
                "block_base_support": 0.14,
                "hybrid_mass": 0.22,
                "neighborhood_mass": 0.64,
            },
        },
        "cells": [],
    }

    policy = resolve_scene_policy(report)
    buildings = policy["roles"]["buildings"]

    assert buildings["simplification_mode"] == (
        "adaptive_local_mass_and_block_support")
    assert buildings["block_base_policy"] == "adaptive_local_support"
    assert buildings["block_base_support_fraction"] == 0.14
    assert buildings["distribution_profile"] == "heterogeneous_urban_mosaic"
    assert buildings["suspected_building_gap_fraction_of_urban"] == 0.12
    assert buildings["positive_building_coverage_cv"] == 0.91
    assert buildings["footprint_frame_coverage"] == 0.08
    assert buildings["local_strategy"]["city_name_lookup"] is False


def test_printable_compact_buildings_are_not_forced_into_aggressive_merge():
    policy = resolve_scene_policy(_report(buildings={
        "footprint_frame_coverage": 0.07,
        "sub_nozzle_fraction": 0.02,
        "sub_nozzle_area_fraction": 0.01,
        "independent_survival_fraction": 0.94,
        "regularization_pressure": 0.03,
    }))

    buildings = policy["roles"]["buildings"]
    assert buildings["simplification_mode"] == (
        "preserve_printable_compact_bodies")
    assert buildings["independent_body_budget_scale"] > 0.9


def test_sparse_rugged_scene_assigns_height_to_terrain():
    policy = resolve_scene_policy(_report(
        buildings={"footprint_frame_coverage": 0.005},
        terrain={
            "relief_to_span": 0.04,
            "rugged_fraction": 0.40,
            "buildable_low_slope_fraction": 0.20,
        },
    ))

    assert policy["scene_class"] == "landscape"
    assert policy["archetype"] == "terrain_first_sparse_settlement"
    assert policy["roles"]["buildings"]["height_owner"] == "terrain"


def test_crater_lake_gets_landscape_specific_policy_without_geometry_control():
    report = _report(
        water={"coast_score": 0.65},
        buildings={"footprint_frame_coverage": 0.002},
        terrain={
            "relief_to_span": 0.06,
            "rugged_fraction": 0.35,
            "buildable_low_slope_fraction": 0.25,
        },
        landform={"crater_rim": 0.88, "basin": 0.91},
    )
    report["summary"]["water_fraction"] = 0.08

    policy = resolve_scene_policy(report)

    assert policy["scene_class"] == "landscape"
    assert policy["archetype"] == "crater_lake_rim"
    assert policy["landscape_strategy"]["urban_block_base"] == "disable"
    assert policy["landscape_strategy"]["geometry_owner"] == "DEM"
    assert policy["activation"] == "audit_only"
    assert "global Z values" in policy["decision_contract"]["forbidden_controls"]


@pytest.mark.parametrize(
    ("landform", "expected"),
    [
        ({"isolated_prominence": 0.82}, "isolated_monolith"),
        ({"ridge_network": 0.84, "iconic_peak": 0.58},
         "peak_valley_network"),
        ({"canyon_valley": 0.80}, "canyon_valley"),
        ({"repeated_cones": 0.72}, "volcanic_field"),
        ({"open_plain": 0.88}, "open_steppe"),
    ],
)
def test_landscape_archetypes_are_measurement_driven(landform, expected):
    policy = resolve_scene_policy(_report(
        buildings={"footprint_frame_coverage": 0.002},
        landform=landform,
    ))

    assert policy["scene_class"] == "landscape"
    assert policy["archetype"] == expected
    assert policy["tradeoff_order"]["optimization_priority"][0] == (
        "landform_identity")


def test_sparse_large_water_scene_uses_landscape_water_policy():
    report = _report(
        water={"coast_score": 0.88},
        buildings={"footprint_frame_coverage": 0.001},
    )
    report["summary"]["water_fraction"] = 0.72

    policy = resolve_scene_policy(report)

    assert policy["scene_class"] == "water_landscape"
    assert policy["archetype"] == "great_lake_shore"
    assert policy["landscape_strategy"]["enabled"] is True


def test_low_confidence_policy_is_explicitly_warning_bounded():
    policy = resolve_scene_policy(_report(confidence="low"))

    assert policy["data_confidence"] == "low"
    assert any("advisory" in warning for warning in policy["warnings"])
    assert policy["tradeoff_order"]["hard_constraints"][0] == "source_truth"


def test_policy_is_deterministic_and_written_atomically(tmp_path):
    report = _report(water={"river_axis_score": 0.72})
    first = resolve_scene_policy(report, printer_profile={
        "nozzle_diameter_mm": 0.4,
        "layer_height_mm": 0.12,
    })
    second = resolve_scene_policy(report, printer_profile={
        "nozzle_diameter_mm": 0.4,
        "layer_height_mm": 0.12,
    })

    assert first == second
    path = write_scene_policy(tmp_path, first)
    assert path.endswith("scene_policy.json")
    assert json.loads((tmp_path / "scene_policy.json").read_text()) == first


def test_policy_rejects_unversioned_scene_report():
    with pytest.raises(ValueError, match="supported version"):
        resolve_scene_policy({"summary": {}, "metrics": {}})
