import math
from types import SimpleNamespace

import geopandas as gpd
import pytest
from shapely.geometry import LineString, box
from shapely.ops import unary_union

from _TEXTURE_STYLE_OF_DEEPSEEK.print_profile import PrinterProfile
from aesthetic.building_mass_strategy import (
    BuildingMassPolicy,
    _assign_to_blocks,
    _candidate_shape,
    _regularize_silhouette,
    _silhouette_metrics,
    _source_seed_cluster_infill,
    apply_building_mass_to_layers,
    build_building_mass_candidate,
    compose_building_mass_with_baseline,
    resolve_building_representation_mode,
    resolve_component_width_target,
    resolve_building_mass_policy,
)


def test_block_assignment_batches_preserve_global_source_indexes():
    blocks = [box(0, 0, 10, 10), box(20, 0, 30, 10)]
    buildings = [
        box(1, 1, 2, 2),
        box(21, 1, 22, 2),
        box(4, 4, 5, 5),
        box(24, 4, 25, 5),
        box(40, 40, 41, 41),
    ]

    assert _assign_to_blocks(buildings, blocks, batch_size=2) == {
        0: [0, 2],
        1: [1, 3],
    }


@pytest.mark.parametrize("batch_size", [0, -1, 1.5, True])
def test_block_assignment_rejects_invalid_batch_size(batch_size):
    with pytest.raises(ValueError, match="positive integer"):
        _assign_to_blocks([box(1, 1, 2, 2)], [box(0, 0, 10, 10)],
                          batch_size=batch_size)


def test_cluster_infill_hard_splits_before_large_union():
    buildings = [
        box(column * 3, row * 3, column * 3 + 2, row * 3 + 2)
        for row in range(4)
        for column in range(10)
    ]
    policy = BuildingMassPolicy(
        urban_cluster_max_source_members=4,
        urban_cluster_max_split_depth=1,
    )

    result, evidence = _source_seed_cluster_infill(
        buildings,
        clip=box(-1, -1, 31, 13),
        nozzle_real_m=1.0,
        target_min_real_m=2.0,
        target_max_real_m=8.0,
        policy=policy,
    )

    assert result
    assert evidence["hard_source_splits"] > 0
    assert evidence["maximum_source_members_per_leaf"] <= 4
    assert evidence["maximum_source_members_policy"] == 4


def test_source_clearance_audit_separates_loss_from_synthesized_area():
    buildings = [box(0, 0, 5, 20), box(30, 30, 70, 70)]
    _, evidence = _candidate_shape(
        buildings, box(0, 0, 100, 100), role="sparse_printable",
        nozzle_real_m=10, policy=BuildingMassPolicy(),
        exclusion=box(200, 200, 210, 210),
        target_min_real_m=32.5, target_max_real_m=57.5)
    audit = evidence["source_clearance_audit"]
    assert audit["input_footprints"] == 2
    assert audit["footprints_surviving_clearance"] == 1
    assert audit["source_inside_block_area_m2"] == 1700
    assert audit["source_after_clearance_area_m2"] == 1600
    assert audit["output_supported_source_area_m2"] == 1600


def test_empty_clearance_still_records_input_loss():
    _, evidence = _candidate_shape(
        [box(0, 0, 5, 5)], box(0, 0, 10, 10), role="sparse_printable",
        nozzle_real_m=10, policy=BuildingMassPolicy(),
        exclusion=box(200, 200, 210, 210),
        target_min_real_m=32.5, target_max_real_m=57.5)
    audit = evidence["source_clearance_audit"]
    assert audit["source_inside_block_area_m2"] == 25
    assert audit["source_after_clearance_area_m2"] == 0
    assert audit["footprints_surviving_clearance"] == 0


def test_pipeline_candidate_carries_clearance_audit_and_unassigned_count():
    candidate = build_building_mass_candidate(
        gpd.GeoDataFrame(geometry=[box(30, 30, 70, 70), box(200, 200, 210, 210)]),
        [box(0, 0, 100, 100)], nozzle_real_m=10,
        printer_profile=PrinterProfile())
    audit = candidate.evidence["source_clearance_audit"]
    assert audit["unassigned_source_footprints"] == 1
    assert audit["clearance_source_area_retention"] == 1
    assert 0 <= audit["shaping_source_area_retention"] <= 1


def test_complete_city_merges_sub_nozzle_leaves_before_physical_filter():
    # Four source-supported 8 m footprints cannot survive a 10 m nozzle on
    # their own.  Their two recursively split rows can, however, be merged
    # into one bounded 28 m neighbourhood mass.  Filtering before that merge
    # was the production regression that emptied Chicago's dense grid.
    footprints = [
        box(0, 0, 8, 8), box(20, 0, 28, 8),
        box(0, 20, 8, 28), box(20, 20, 28, 28),
    ]
    clusters, evidence = _source_seed_cluster_infill(
        footprints,
        clip=box(-2, -2, 30, 30),
        nozzle_real_m=10.0,
        target_min_real_m=32.5,
        target_max_real_m=57.5,
        policy=BuildingMassPolicy(
            complete_cluster_merge_max_area_growth_fraction=0.90,
        ),
        representation_mode="complete_source_union",
    )

    assert clusters
    assert all(not item.buffer(-5.0).is_empty for item in clusters)
    assert evidence["sub_printable_leaves_preserved_for_agglomeration"] is True
    assert evidence["agglomerative_merges"] > 0
    assert evidence["post_agglomeration_physical_core_rejections"] == 0


def test_complete_city_stops_merging_at_reliable_print_strip_floor():
    # These two compact source buildings already survive the selected
    # material strip floor.  They are below the softer 10 m visual target,
    # but a complete high-confidence city should retain that useful grain
    # instead of joining them merely to make the component coarser.
    footprints = [box(0, 0, 7, 7), box(12, 0, 19, 7)]
    clusters, evidence = _source_seed_cluster_infill(
        footprints,
        clip=box(-2, -2, 21, 9),
        nozzle_real_m=4.0,
        target_min_real_m=10.0,
        target_max_real_m=20.0,
        complete_preserve_min_real_m=7.0,
        policy=BuildingMassPolicy(
            experimental_complete_source_filter=True,
            complete_cluster_merge_max_area_growth_fraction=0.90,
        ),
        representation_mode="complete_source_union",
    )

    assert len(clusters) == 2
    assert evidence["agglomerative_merges"] == 0
    assert evidence["minimum_cluster_axis_stop_real_m"] == 7.0
    assert evidence["minimum_cluster_axis_stop_origin"] == (
        "printer_reliable_strip_floor")
    assert evidence["soft_component_target_min_real_m"] == 10.0
    assert evidence["agglomeration"]["neighbor_search"] == (
        "per_block_strtree_bounded_passes_v1")
    assert evidence["agglomeration"]["maximum_passes"] == 3
    assert evidence["agglomeration"][
        "settled_components_excluded_from_neighbor_search"] == 2


def test_rejected_complete_filter_is_off_by_default():
    assert BuildingMassPolicy().experimental_complete_source_filter is False
    _, evidence = _source_seed_cluster_infill(
        [box(0, 0, 7, 7), box(12, 0, 19, 7)],
        clip=box(-2, -2, 21, 9), nozzle_real_m=4.0,
        target_min_real_m=10.0, target_max_real_m=20.0,
        complete_preserve_min_real_m=7.0, policy=BuildingMassPolicy(),
        representation_mode="complete_source_union")
    assert evidence["minimum_cluster_axis_stop_origin"] == "soft_component_target"
    assert evidence["final_core_floor_real_m"] == 4.0


def test_incomplete_city_still_merges_toward_soft_component_target():
    footprints = [box(0, 0, 7, 7), box(12, 0, 19, 7)]
    _clusters, evidence = _source_seed_cluster_infill(
        footprints,
        clip=box(-2, -2, 21, 9),
        nozzle_real_m=4.0,
        target_min_real_m=10.0,
        target_max_real_m=20.0,
        complete_preserve_min_real_m=7.0,
        policy=BuildingMassPolicy(),
        representation_mode="supported_cluster_infill",
    )

    assert evidence["minimum_cluster_axis_stop_real_m"] == 10.0
    assert evidence["minimum_cluster_axis_stop_origin"] == (
        "soft_component_target")


def _representation_scene_policy(
    *, profile, completeness, continuity, gap_fraction, coverage,
    cells=(), urban_mass="merge_anonymous_footprints_by_neighbourhood",
):
    return {
        "policy_version": "scene-policy-v4",
        "roles": {"buildings": {
            "urban_mass": urban_mass,
            "distribution_profile": profile,
            "local_data_completeness_score": completeness,
            "building_distribution_continuity_score": continuity,
            "suspected_building_gap_fraction_of_urban": gap_fraction,
            "footprint_frame_coverage": coverage,
            "positive_building_coverage_cv": 0.5,
            "local_strategy": {
                "status": "ready",
                "cells": list(cells),
            },
        }},
    }


def _grid_buildings(origin_x, origin_y, columns, rows, *, size=24, gap=12):
    result = []
    for row in range(rows):
        for column in range(columns):
            x = origin_x + column * (size + gap)
            y = origin_y + row * (size + gap)
            result.append(box(x, y, x + size, y + size))
    return result


def _fixture():
    blocks = [
        box(0, 0, 400, 400),
        box(430, 0, 830, 400),
        box(860, 0, 1260, 400),
        box(1290, 0, 1690, 400),  # deliberately empty
    ]
    buildings = (
        _grid_buildings(30, 30, 9, 9)
        + _grid_buildings(460, 40, 4, 3, size=30, gap=45)
        + [box(930, 80, 970, 120)]
    )
    return blocks, buildings


def test_candidate_is_bounded_by_existing_blocks_and_preserves_empty_blocks():
    blocks, buildings = _fixture()
    exclusion = box(135, 135, 265, 265)

    candidate = build_building_mass_candidate(
        buildings,
        blocks,
        nozzle_real_m=10.0,
        printer_profile=PrinterProfile(),
        exclusion_polys=[exclusion],
    )

    allowed = unary_union(blocks[:3])
    empty_block = blocks[3]
    assert candidate.all_polygons
    for polygon in candidate.all_polygons:
        assert polygon.difference(allowed).area < 1e-6
        assert polygon.intersection(exclusion).area < 1e-6
        assert polygon.intersection(empty_block).area < 1e-6
    assert candidate.evidence["occupied_blocks"] == 3
    assert candidate.evidence["input_blocks"] == 4


def test_candidate_exposes_dense_quiet_and_sparse_roles():
    blocks, buildings = _fixture()

    candidate = build_building_mass_candidate(
        buildings,
        blocks,
        nozzle_real_m=10.0,
        printer_profile=PrinterProfile(),
        policy=BuildingMassPolicy(
            quiet_score_quantile=0.20,
            urban_mass_score_quantile=0.65,
        ),
    )

    assert candidate.urban_mass
    assert candidate.quiet_texture
    assert candidate.sparse_printable
    assert candidate.evidence["role_block_counts"] == {
        "quiet_texture": 1,
        "urban_mass": 1,
        "sparse_printable": 1,
    }
    assert candidate.evidence["density_guarded_urban_block_fills"] == 1
    assert candidate.evidence["density_guarded_urban_cluster_infills"] == 1
    carrier = candidate.evidence["urban_density_carrier"]
    assert carrier["method"] == "source_seed_cluster_infill"
    assert carrier["complete_block_replacement"] is False
    assert carrier["closing_multiplier"] == 1.35
    assert carrier["growth_slack_nozzles"] == 0.3
    assert carrier["guarded_blocks"] == 1
    assert carrier["source_footprints_in_guarded_blocks"] == 81
    assert carrier["output_clusters"] > 1
    assert carrier["recursive_splits"] > 0
    # The dense block becomes a coherent mid-frequency mass instead of a
    # collection of individually printable but visually noisy footprints.
    assert unary_union(candidate.urban_mass).area > sum(
        polygon.area for polygon in buildings[:81])
    assert unary_union(candidate.urban_mass).area < blocks[0].area * 0.95
    assert candidate.evidence["block_abstraction"][
        "inferred_fill_output_fraction"] > 0


def test_candidate_components_retain_a_printable_core_after_shaping():
    blocks, buildings = _fixture()
    nozzle_real_m = 10.0

    candidate = build_building_mass_candidate(
        buildings,
        blocks,
        nozzle_real_m=nozzle_real_m,
        printer_profile=PrinterProfile(),
    )

    assert all(
        not polygon.buffer(-nozzle_real_m * 0.5).is_empty
        for polygon in candidate.all_polygons
    )
    assert candidate.evidence["hard_boundaries"][
        "post_shape_printable_core_required"] is True
    assert candidate.evidence["hard_boundaries"]["formal_mesh_affected"] is False
    seam = candidate.evidence["required_road_seam"]
    assert seam["final_gap_model_mm"] == 0.84
    assert seam["physical_half_seam_nozzles"] == 1.05
    assert seam["pre_shape_block_inset_each_side_nozzles"] == 1.15
    assert seam["safety_reserve_nozzles"] == 0.02
    boundaries = candidate.evidence["hard_boundaries"]
    assert boundaries["boundary_clearance_passed"] is True
    assert boundaries["observed_minimum_two_sided_seam_nozzles"] >= 2.1
    closing = candidate.evidence["scale_aware_closing"]
    assert closing["quiet_radius_model_mm"] == pytest.approx(1.3 * 0.40)
    assert closing["urban_radius_model_mm"] == pytest.approx(1.3 * 0.55)
    silhouette = candidate.evidence["silhouette_regularization"]
    assert silhouette["target_solidity"] == 0.97
    assert silhouette["maximum_perimeter_excess"] == 1.05
    assert silhouette["anonymous_components_measured"] > 0
    assert silhouette["target_met_after_fraction"] is not None


def test_merged_block_base_delegates_physical_road_clearance_once():
    blocks, buildings = _fixture()
    nozzle_real_m = 10.0

    candidate = build_building_mass_candidate(
        buildings,
        blocks,
        nozzle_real_m=nozzle_real_m,
        printer_profile=PrinterProfile(),
        downstream_final_clearance=True,
    )

    seam = candidate.evidence["required_road_seam"]
    assert seam["final_gap_model_mm"] == 0.84
    assert seam["clearance_owner"] == "post_aggregation_surface_plan"
    assert seam["downstream_final_clearance"] is True
    # S6 reserves only simplification movement and safety. The physical road
    # gap is cut after BO is merged into block_base, so it is not charged twice.
    assert seam["pre_shape_block_inset_each_side_nozzles"] == 0.1
    boundaries = candidate.evidence["hard_boundaries"]
    assert boundaries["boundary_clearance_passed"] is None
    assert boundaries["boundary_clearance_status"] == (
        "delegated_to_final_surface_plan")


def test_shallow_gnaw_is_convexified_inside_area_and_topology_budget():
    gnawed = box(0, 0, 100, 100).difference(box(40, 70, 60, 100))
    policy = BuildingMassPolicy()

    regularized, evidence = _regularize_silhouette(
        gnawed,
        safe_clip=box(-10, -10, 110, 110),
        nozzle_real_m=4.0,
        policy=policy,
    )

    before = _silhouette_metrics(gnawed, simplify=0.4)
    after = _silhouette_metrics(regularized, simplify=0.4)
    assert before["solidity"] < policy.silhouette_target_solidity
    assert after["solidity"] >= policy.silhouette_target_solidity
    assert evidence["method"] == "bounded_convex_hull"
    assert evidence["area_change_fraction"] <= 0.12


def test_convexification_never_crosses_a_road_bounded_safe_clip():
    road_bounded = box(0, 0, 100, 100).difference(box(40, 50, 60, 100))
    policy = BuildingMassPolicy()

    regularized, evidence = _regularize_silhouette(
        road_bounded,
        safe_clip=road_bounded,
        nozzle_real_m=4.0,
        policy=policy,
    )

    assert regularized.difference(road_bounded).area < 1e-6
    assert regularized.intersection(box(40, 50, 60, 100)).area < 1e-6
    assert evidence["boundary_limited"] is True


def test_candidate_records_reference_component_width_distribution_target():
    blocks, buildings = _fixture()
    profile = PrinterProfile(nozzle_diameter_mm=0.4)
    candidate = build_building_mass_candidate(
        buildings,
        blocks,
        nozzle_real_m=10.0,
        printer_profile=profile,
    )

    target = candidate.evidence["component_width_target"]
    observed_m = candidate.evidence["component_min_width_p50_m"]
    observed_mm = candidate.evidence["component_min_width_p50_model_mm"]
    assert target["metric"] == "p50_minimum_rotated_xy_axis"
    assert target["reference_crop_km"] == 25.0
    assert target["reference_model_span_mm"] == 196.0
    assert target["reference_target_min_model_mm"] == 1.3
    assert target["reference_target_max_model_mm"] == 2.3
    assert target["current_crop_km"] == pytest.approx(4.9)
    assert target["target_min_model_mm"] == 1.3
    assert target["target_max_model_mm"] == 2.3
    assert target["print_floor_limited"] is False
    assert target["enforcement"] == "soft_aggregation_prior"
    assert target["hard_acceptance_gate"] is False
    assert observed_mm == pytest.approx(observed_m * 0.4 / 10.0, abs=5e-5)
    assert target["observed_model_mm"] == observed_mm
    assert target["status"] in {
        "below_target", "within_target", "within_target_tolerance",
        "above_target"}
    assert target["measurement_tolerance_model_mm"] == 0.01


@pytest.mark.parametrize(
    "kwargs",
    [
        {"component_short_axis_p50_target_min_mm": 0.0},
        {"component_short_axis_p50_target_max_mm": 0.0},
        {"component_width_reference_crop_km": 0.0},
        {"component_width_reference_model_span_mm": 0.0},
        {"component_width_measurement_tolerance_mm": -0.01},
        {
            "component_short_axis_p50_target_min_mm": 2.3,
            "component_short_axis_p50_target_max_mm": 1.3,
        },
        {
            "urban_block_fill_min_density": 0.08,
            "urban_block_fill_supported_min_density": 0.12,
        },
        {"silhouette_target_solidity": 0.0},
        {"silhouette_target_solidity": 1.01},
        {"silhouette_max_perimeter_excess": 0.99},
        {"silhouette_max_area_growth_fraction": 1.01},
        {"silhouette_max_area_loss_fraction": 1.0},
        {"urban_cluster_merge_max_area_growth_fraction": 1.01},
        {"complete_cluster_merge_max_area_growth_fraction": 1.01},
        {"urban_mass_min_short_axis_target_fraction": 1.01},
        {"urban_cluster_max_source_members": 1},
        {"urban_cluster_max_source_members": True},
    ],
)
def test_component_width_distribution_target_is_validated(kwargs):
    with pytest.raises(ValueError):
        BuildingMassPolicy(**kwargs)


@pytest.mark.parametrize(
    ("crop_km", "model_span_mm", "expected_min", "expected_max"),
    [
        (15.0, 196.0, 1.3, 2.3),
        (25.0, 196.0, 1.3, 2.3),
        (30.0, 196.0, 1.3, 2.3),
        (25.0, 245.0, 1.3 * 245.0 / 196.0, 2.3 * 245.0 / 196.0),
    ],
)
def test_component_width_target_keeps_visual_band_at_fixed_model_size(
        crop_km, model_span_mm, expected_min, expected_max):
    target = resolve_component_width_target(
        BuildingMassPolicy(),
        printer_profile=PrinterProfile(),
        scale_mm_per_m=model_span_mm / (crop_km * 1000.0),
        model_span_mm=model_span_mm,
    )

    assert target["target_min_model_mm"] == pytest.approx(
        expected_min, abs=1e-5)
    assert target["target_max_model_mm"] == pytest.approx(
        expected_max, abs=1e-5)


def test_larger_crop_increases_real_aggregation_and_road_seam_distances():
    policy = BuildingMassPolicy()
    profile = PrinterProfile()
    small = resolve_component_width_target(
        policy,
        printer_profile=profile,
        scale_mm_per_m=196.0 / 15_000.0,
        model_span_mm=196.0,
    )
    large = resolve_component_width_target(
        policy,
        printer_profile=profile,
        scale_mm_per_m=196.0 / 50_000.0,
        model_span_mm=196.0,
    )

    assert small["target_min_model_mm"] == large["target_min_model_mm"]
    assert small["target_max_model_mm"] == large["target_max_model_mm"]
    assert large["target_min_real_m"] > small["target_min_real_m"]
    assert large["aggregation_real_scale_factor"] > 1.0
    assert (large["scale_aware_aggregation"]["urban_merge_radius_real_m"]
            > small["scale_aware_aggregation"]["urban_merge_radius_real_m"])
    assert (large["scale_aware_aggregation"]["minimum_road_seam_real_m"]
            > small["scale_aware_aggregation"]["minimum_road_seam_real_m"])


def test_component_width_target_never_drops_below_print_floor():
    profile = PrinterProfile(min_colored_strip_mm=0.63)
    target = resolve_component_width_target(
        BuildingMassPolicy(),
        printer_profile=profile,
        scale_mm_per_m=49.0 / 5_000.0,
        model_span_mm=49.0,
    )

    assert target["print_floor_limited"] is True
    assert target["target_min_model_mm"] >= 0.63
    assert target["target_max_model_mm"] >= target["target_min_model_mm"]


def test_semantic_relief_uses_printer_layers_and_not_absolute_z():
    blocks, buildings = _fixture()
    profile = PrinterProfile(layer_height_mm=0.16)
    policy = BuildingMassPolicy(
        quiet_relief_layers=3,
        urban_mass_relief_layers=7,
    )

    candidate = build_building_mass_candidate(
        buildings,
        blocks,
        nozzle_real_m=10.0,
        printer_profile=profile,
        policy=policy,
    )

    heights = candidate.evidence["semantic_heights"]
    assert math.isclose(heights["quiet_texture_mm"], 0.48)
    assert math.isclose(heights["urban_mass_mm"], 1.12)
    assert "absolute_z" not in heights


def test_candidate_accepts_geodataframe_and_is_deterministic():
    blocks, buildings = _fixture()
    gdf = gpd.GeoDataFrame({"kind": ["building"] * len(buildings)},
                           geometry=buildings, crs="EPSG:3857")
    kwargs = dict(
        blocks=blocks,
        nozzle_real_m=10.0,
        printer_profile=PrinterProfile(),
        source_scene_policy={"policy_version": "scene-policy-v3"},
    )

    first = build_building_mass_candidate(gdf, **kwargs)
    second = build_building_mass_candidate(gdf, **kwargs)

    assert [polygon.wkb for polygon in first.all_polygons] == [
        polygon.wkb for polygon in second.all_polygons]
    assert first.evidence == second.evidence
    assert first.evidence["source_scene_policy_version"] == "scene-policy-v3"


def test_pressure_resolver_broadens_mass_only_for_sub_nozzle_footprints():
    tiny = [box(index * 12, 0, index * 12 + 4, 4) for index in range(50)]
    large = [box(index * 30, 0, index * 30 + 20, 20) for index in range(50)]

    tiny_policy, tiny_evidence = resolve_building_mass_policy(
        tiny, nozzle_real_m=10.0)
    large_policy, large_evidence = resolve_building_mass_policy(
        large, nozzle_real_m=10.0)

    assert tiny_evidence["regularization_pressure"] == 1.0
    assert tiny_policy.urban_mass_score_quantile < 0.4
    assert tiny_policy.quiet_merge_radius_nozzles > 0.5
    assert tiny_policy.urban_block_fill_min_density == 0.12
    assert tiny_policy.urban_block_fill_min_count == 6
    assert large_evidence["regularization_pressure"] == 0.0
    assert large_policy == BuildingMassPolicy()


def test_pressure_resolver_prefers_versioned_scene_measurement():
    policy, evidence = resolve_building_mass_policy(
        [box(0, 0, 20, 20)],
        nozzle_real_m=10.0,
        source_scene_policy={
            "roles": {"buildings": {"regularization_pressure": 0.7}},
        },
    )

    assert evidence["source"] == "scene_policy"
    assert evidence["measurements"] is None
    assert policy.urban_mass_score_quantile < BuildingMassPolicy(
    ).urban_mass_score_quantile


def test_candidate_consumes_local_distribution_strategy_before_global_quantiles():
    blocks, buildings = _fixture()
    cells = [
        {"bounds": [0, 0, 425, 425], "strategy": "neighborhood_mass"},
        {"bounds": [425, 0, 845, 425], "strategy": "hybrid_mass"},
        {"bounds": [845, 0, 1275, 425],
         "strategy": "preserve_printable_footprints"},
        {"bounds": [1275, 0, 1700, 425], "strategy": "open_space_preserve"},
    ]

    candidate = build_building_mass_candidate(
        buildings,
        blocks,
        nozzle_real_m=10.0,
        printer_profile=PrinterProfile(),
        source_scene_policy={
            "policy_version": "scene-policy-v4",
            "roles": {"buildings": {"local_strategy": {
                "status": "ready",
                "cells": cells,
            }}},
        },
    )

    assert candidate.evidence["local_strategy_cells_available"] == 4
    assert candidate.evidence["local_strategy_block_counts"] == {
        "hybrid_mass": 1,
        "neighborhood_mass": 1,
        "preserve_printable_footprints": 1,
    }
    assert candidate.evidence["role_block_counts"] == {
        "quiet_texture": 1,
        "urban_mass": 1,
        "sparse_printable": 1,
    }


def test_cross_source_neighborhood_support_uses_lower_fill_gate():
    block = box(0, 0, 200, 200)
    buildings = [
        box(20 + column * 50, 20 + row * 80,
            40 + column * 50, 50 + row * 80)
        for row in range(2) for column in range(3)
    ]
    common = dict(
        buildings=buildings,
        blocks=[block],
        nozzle_real_m=5.0,
        printer_profile=PrinterProfile(),
    )

    unsupported = build_building_mass_candidate(**common)
    supported = build_building_mass_candidate(
        **common,
        source_scene_policy={
            "policy_version": "scene-policy-v4",
            "roles": {"buildings": {"local_strategy": {
                "status": "ready",
                "cells": [{
                    "bounds": [0, 0, 200, 200],
                    "strategy": "neighborhood_mass",
                }],
            }}},
        },
    )

    assert unsupported.evidence["density_guarded_urban_block_fills"] == 0
    assert supported.evidence["density_guarded_urban_block_fills"] == 1
    assert (supported.evidence["output_area_m2"]
            > unsupported.evidence["output_area_m2"])


@pytest.mark.parametrize(
    ("profile", "completeness", "continuity", "gap", "coverage", "mode"),
    [
        ("continuous_urban_fabric", 0.86, 0.83, 0.0, 0.14,
         "complete_source_union"),
        ("mixed_complete_urban_fabric", 0.78, 0.72, 0.03, 0.09,
         "complete_source_union"),
        ("heterogeneous_urban_mosaic", 0.70, 0.64, 0.08, 0.06,
         "supported_cluster_infill"),
        ("fragmented_or_incomplete_urban_evidence", 0.64, 0.55, 0.16,
         0.029, "sparse_local_preserve"),
    ],
)
def test_global_representation_router_uses_quality_not_city_name(
        profile, completeness, continuity, gap, coverage, mode):
    evidence = resolve_building_representation_mode(
        _representation_scene_policy(
            profile=profile,
            completeness=completeness,
            continuity=continuity,
            gap_fraction=gap,
            coverage=coverage,
        ))

    assert evidence["mode"] == mode
    assert evidence["city_name_lookup"] is False
    assert evidence["geometry_authority"] == (
        "source_vectors_and_existing_topology_only")


def test_legacy_scene_policy_keeps_reviewed_cluster_path():
    evidence = resolve_building_representation_mode({
        "roles": {"buildings": {"regularization_pressure": 0.8}},
    })

    assert evidence["mode"] == "supported_cluster_infill"
    assert evidence["local_quality_ready"] is False


def test_complete_sparse_and_incomplete_modes_bound_inferred_mass():
    block = box(0, 0, 400, 400)
    buildings = _grid_buildings(30, 30, 9, 9)
    cells = [{
        "bounds": [0, 0, 400, 400],
        "strategy": "neighborhood_mass",
    }]
    common = dict(
        buildings=buildings,
        blocks=[block],
        nozzle_real_m=10.0,
        printer_profile=PrinterProfile(),
    )
    complete = build_building_mass_candidate(
        **common,
        source_scene_policy=_representation_scene_policy(
            profile="continuous_urban_fabric",
            completeness=0.86,
            continuity=0.83,
            gap_fraction=0.0,
            coverage=0.14,
            cells=cells,
        ),
    )
    incomplete = build_building_mass_candidate(
        **common,
        source_scene_policy=_representation_scene_policy(
            profile="heterogeneous_urban_mosaic",
            completeness=0.70,
            continuity=0.64,
            gap_fraction=0.08,
            coverage=0.06,
            cells=cells,
        ),
    )
    sparse = build_building_mass_candidate(
        **common,
        source_scene_policy=_representation_scene_policy(
            profile="fragmented_or_incomplete_urban_evidence",
            completeness=0.64,
            continuity=0.55,
            gap_fraction=0.16,
            coverage=0.029,
            cells=cells,
        ),
    )

    assert complete.evidence["global_representation"]["mode"] == (
        "complete_source_union")
    assert incomplete.evidence["global_representation"]["mode"] == (
        "supported_cluster_infill")
    assert sparse.evidence["global_representation"]["mode"] == (
        "sparse_local_preserve")
    assert complete.evidence["density_guarded_urban_cluster_infills"] == 0
    assert incomplete.evidence["density_guarded_urban_cluster_infills"] == 1
    assert sparse.evidence["density_guarded_urban_cluster_infills"] == 0
    assert incomplete.evidence["invented_area_m2"] > complete.evidence[
        "invented_area_m2"]
    assert sparse.evidence["role_block_counts"]["urban_mass"] == 0
    assert sparse.evidence["role_block_counts"]["quiet_texture"] == 1
    assert sparse.evidence["urban_density_carrier"][
        "growth_slack_nozzles"] == 0.0
    assert sparse.evidence["invented_area_m2"] < incomplete.evidence[
        "invented_area_m2"]


def test_mid_frequency_filter_demotes_narrow_mass_without_deleting_geometry():
    block = box(0, 0, 400, 400)
    buildings = (
        _grid_buildings(25, 25, 6, 6, size=20, gap=24)
        # 17 m clears the 0.63 mm reliable strip floor at this scale but
        # remains below the 0.78 mm standard-relief role threshold.
        + [box(325, 40, 338, 53), box(350, 40, 363, 53)]
    )
    cells = [{
        "bounds": [0, 0, 400, 400],
        "strategy": "neighborhood_mass",
    }]

    candidate = build_building_mass_candidate(
        buildings,
        [block],
        nozzle_real_m=10.0,
        printer_profile=PrinterProfile(),
        source_scene_policy=_representation_scene_policy(
            profile="continuous_urban_fabric",
            completeness=0.86,
            continuity=0.83,
            gap_fraction=0.0,
            coverage=0.14,
            cells=cells,
        ),
    )

    role_filter = candidate.evidence["mid_frequency_role_filter"]
    role_metrics = candidate.evidence["component_metrics_by_role"]
    assert role_filter["method"] == (
        "demote_after_safe_merge_and_silhouette_review_without_geometry_"
        "change")
    assert role_filter["minimum_standard_relief_short_axis_model_mm"] == (
        pytest.approx(0.78))
    assert role_filter["demoted_to_quiet_texture"] > 0
    assert role_filter["geometry_deleted"] is False
    assert role_filter["geometry_widened"] is False
    assert role_filter["road_or_water_topology_changed"] is False
    assert role_metrics["urban_mass"]["short_axis_model_mm"]["p10"] >= 0.78
    assert role_metrics["urban_mass"]["solidity"]["p10"] >= 0.97
    assert (candidate.evidence["urban_density_carrier"]
            ["maximum_merge_area_growth_fraction"] == 0.35)


def test_candidate_composition_retains_the_existing_city_carrier():
    blocks, buildings = _fixture()
    baseline = [box(20, 20, 380, 380)]
    candidate = build_building_mass_candidate(
        buildings,
        blocks,
        nozzle_real_m=10.0,
        printer_profile=PrinterProfile(),
    )

    quiet, mass, evidence = compose_building_mass_with_baseline(
        baseline, candidate, nozzle_real_m=10.0)

    assert evidence["status"] == "composed"
    assert evidence["area_gain_ratio"] >= 1.02
    assert quiet
    assert mass
    assert unary_union(quiet + mass).area >= unary_union(baseline).area * 1.02


def test_sparse_mode_never_replaces_an_accepted_baseline():
    baseline = [box(0, 0, 100, 100)]
    candidate = build_building_mass_candidate(
        [box(10, 10, 40, 40), box(50, 10, 80, 40)],
        [box(0, 0, 100, 100)],
        nozzle_real_m=5.0,
        printer_profile=PrinterProfile(),
        source_scene_policy=_representation_scene_policy(
            profile="fragmented_or_incomplete_urban_evidence",
            completeness=0.64,
            continuity=0.55,
            gap_fraction=0.16,
            coverage=0.029,
            cells=[{
                "bounds": [0, 0, 100, 100],
                "strategy": "neighborhood_mass",
            }],
        ),
    )

    quiet, mass, evidence = compose_building_mass_with_baseline(
        baseline, candidate, nozzle_real_m=5.0)

    assert evidence["status"] == "policy_preserved_baseline"
    assert evidence["representation_mode"] == "sparse_local_preserve"
    assert [item.wkb for item in quiet] == [item.wkb for item in baseline]
    assert mass == []


def test_active_garden_city_blends_mid_frequency_mass_into_formal_layers(
        monkeypatch):
    blocks, building_polygons = _fixture()
    buildings = gpd.GeoDataFrame(
        {"building": ["yes"] * len(building_polygons)},
        geometry=building_polygons,
        crs="EPSG:3857",
    )
    roads = gpd.GeoDataFrame(
        {"highway": ["primary"]},
        geometry=[LineString([(0, 0), (1690, 400)])],
        crs="EPSG:3857",
    )
    water = gpd.GeoDataFrame(
        {"natural": []}, geometry=[], crs="EPSG:3857")
    def make_layers():
        return SimpleNamespace(
            BO=[box(40, 40, 100, 100)],
            BO_heights=[],
            BL=[],
            nozzle_real_m=10.0,
        )
    layers = make_layers()
    monkeypatch.setattr(
        "_TEXTURE_STYLE_OF_DEEPSEEK.buildings._build_city_blocks",
        lambda *_args, **_kwargs: blocks,
    )

    preparation_cache = {}
    kwargs = dict(
        printer_profile=PrinterProfile(layer_height_mm=0.12),
        scene_policy={
            "policy_version": "scene-policy-v3",
            "activation": "active",
            "garden_city_strategy": {"enabled": True},
            "roles": {
                "buildings": {"regularization_pressure": 0.75},
            },
        },
        topology_tier=2,
        topology_blocks=blocks,
        topology_evidence={
            "policy_version": "scale-aware-block-topology-v2",
            "status": "met",
        },
        model_span_mm=196.0,
        preparation_cache=preparation_cache,
    )
    evidence = apply_building_mass_to_layers(
        layers,
        buildings,
        roads,
        water,
        (0, 0, 1690, 400),
        **kwargs,
    )

    assert evidence["status"] == "active"
    assert evidence["area_gain_ratio"] > 1.02
    assert len(layers.BO_heights) == len(layers.BO)
    assert set(layers.BO_heights) == {0.24, 0.84}
    assert evidence["candidate"]["hard_boundaries"][
        "formal_mesh_affected"] is True
    assert evidence["preparation_cache_hit"] is False
    assert evidence["topology_source"] == "preprocessed_scale_aware"
    assert evidence["topology_evidence"]["status"] == "met"

    second = apply_building_mass_to_layers(
        make_layers(), buildings, roads, water, (0, 0, 1690, 400),
        **kwargs,
    )
    assert second["preparation_cache_hit"] is True


def test_complete_urban_fabric_activates_formal_mid_frequency_mass(
        monkeypatch):
    blocks, building_polygons = _fixture()
    buildings = gpd.GeoDataFrame(
        {"building": ["yes"] * len(building_polygons)},
        geometry=building_polygons,
        crs="EPSG:3857",
    )
    roads = gpd.GeoDataFrame(
        {"highway": ["primary"]},
        geometry=[LineString([(0, 0), (1690, 400)])],
        crs="EPSG:3857",
    )
    water = gpd.GeoDataFrame(
        {"natural": []}, geometry=[], crs="EPSG:3857")
    layers = SimpleNamespace(
        BO=[box(40, 40, 100, 100)],
        BO_heights=[],
        BL=[],
        nozzle_real_m=10.0,
    )
    monkeypatch.setattr(
        "_TEXTURE_STYLE_OF_DEEPSEEK.buildings._build_city_blocks",
        lambda *_args, **_kwargs: blocks,
    )

    evidence = apply_building_mass_to_layers(
        layers,
        buildings,
        roads,
        water,
        (0, 0, 1690, 400),
        printer_profile=PrinterProfile(layer_height_mm=0.12),
        scene_policy={
            "policy_version": "scene-policy-v5",
            "activation": "active",
            "scene_class": "mixed",
            "garden_city_strategy": {"enabled": False},
            "landscape_strategy": {"enabled": False},
            "roles": {"buildings": {
                "local_strategy": {
                    "status": "ready",
                    "cells": [{
                        "bounds": [0, 0, 1690, 400],
                        "strategy": "neighborhood_mass",
                    }],
                },
            }},
        },
        topology_tier=2,
        topology_blocks=blocks,
        model_span_mm=196.0,
    )

    assert evidence["status"] == "active"
    assert evidence["activation_basis"] == "local_building_quality"
    assert evidence["candidate"]["activation"] == (
        "active_local_building_quality")
    assert evidence["output_components"] > 1
    assert len(layers.BO_heights) == len(layers.BO)


def test_landscape_never_activates_local_urban_mass():
    buildings = gpd.GeoDataFrame(
        {"building": ["yes"]}, geometry=[box(10, 10, 30, 30)],
        crs="EPSG:3857")
    roads = gpd.GeoDataFrame(
        {"highway": ["primary"]},
        geometry=[LineString([(0, 0), (100, 100)])],
        crs="EPSG:3857")
    water = gpd.GeoDataFrame(
        {"natural": []}, geometry=[], crs="EPSG:3857")
    layers = SimpleNamespace(
        BO=[box(0, 0, 100, 100)], BO_heights=[], BL=[],
        nozzle_real_m=10.0)

    evidence = apply_building_mass_to_layers(
        layers, buildings, roads, water, (0, 0, 100, 100),
        printer_profile=PrinterProfile(),
        scene_policy={
            "policy_version": "scene-policy-v5",
            "activation": "active",
            "scene_class": "landscape",
            "garden_city_strategy": {"enabled": False},
            "landscape_strategy": {"enabled": True},
            "roles": {"buildings": {"local_strategy": {
                "status": "ready",
                "cells": [{
                    "bounds": [0, 0, 100, 100],
                    "strategy": "neighborhood_mass",
                }],
            }}},
        },
    )

    assert evidence["status"] == "inactive"
    assert evidence["reason"] == (
        "landscape policy suppresses urban building mass")
