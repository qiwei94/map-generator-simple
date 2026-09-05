import geopandas as gpd
from shapely.geometry import box

from aesthetic.block_grammar import (
    measure_source_block_grammar,
    resolve_block_grammar_strategy,
)
from aesthetic.building_mass_strategy import (
    BuildingMassPolicy,
    resolve_building_mass_policy,
)


FRAME = (0.0, 0.0, 4000.0, 4000.0)


def _grid_buildings(width=40.0, height=100.0):
    geometries = []
    for row in range(4):
        for column in range(4):
            x = 200 + column * 900
            y = 200 + row * 900
            geometries.append(box(x, y, x + width, y + height))
    return gpd.GeoDataFrame(
        {"building": ["yes"] * len(geometries)}, geometry=geometries,
        crs="EPSG:3857")


def test_source_block_grammar_is_printer_scaled_and_rotation_invariant():
    report = measure_source_block_grammar(
        _grid_buildings(), FRAME,
        model_span_mm=196.0,
        nozzle_real_m=4000.0 / 196.0 * 0.4,
        footprint_frame_coverage=0.01,
        occupied_cell_fraction=1.0,
    )

    comparable = report["printable_comparable_sample"]
    assert report["status"] == "ready"
    assert comparable["metrics"]["short_axis_mm"]["p50"] == 1.96
    assert comparable["metrics"]["rectangularity"]["p50"] == 1.0
    assert comparable["orientation"]["orthogonal_coherence"] == 1.0
    assert report["constraints"][-1] == (
        "printer profile remains hard authority")


def test_low_coverage_continuous_city_promotes_bounded_texture():
    measurement = measure_source_block_grammar(
        _grid_buildings(), FRAME,
        model_span_mm=196.0,
        nozzle_real_m=4000.0 / 196.0 * 0.4,
        footprint_frame_coverage=0.02,
        occupied_cell_fraction=1.0,
    )
    strategy = resolve_block_grammar_strategy(
        measurement,
        local_completeness=0.75,
        local_continuity=0.80,
        block_base_support_fraction=0.20,
        external_urban_support=0.90,
        grid_score=0.20,
        water_network_score=0.70,
    )

    assert strategy["status"] == "ready"
    assert strategy["mode"] == (
        "promote_sub_nozzle_texture_into_bounded_blocks")
    assert strategy["texture_promotion_pressure"] > 0.6
    assert "road/water topology blocks" in strategy["hard_boundaries"]


def test_block_grammar_strategy_only_changes_bounded_mass_policy_fields():
    base = BuildingMassPolicy()
    policy, evidence = resolve_building_mass_policy(
        [box(0, 0, 20, 20)],
        nozzle_real_m=10.0,
        base_policy=base,
        source_scene_policy={
            "roles": {"buildings": {
                "regularization_pressure": 0.5,
                "block_grammar_strategy": {
                    "status": "ready",
                    "soft_target_short_axis_band_mm": [0.9, 1.7],
                    "aggregation_pressure": 0.8,
                    "outline_regularization_pressure": 0.7,
                    "orientation_preservation_pressure": 0.9,
                    "texture_promotion_pressure": 0.6,
                    "selection_pressure": 0.2,
                },
            }},
        },
    )

    assert evidence["resolver_version"] == (
        "building-mass-pressure-resolver-v2")
    assert policy.component_short_axis_p50_target_min_mm == 0.9
    assert policy.component_short_axis_p50_target_max_mm == 1.7
    assert policy.urban_merge_radius_nozzles > base.urban_merge_radius_nozzles
    assert policy.silhouette_rounding_nozzles < base.silhouette_rounding_nozzles
    assert policy.block_inset_nozzles == base.block_inset_nozzles
    assert policy.boundary_clearance_safety_nozzles == (
        base.boundary_clearance_safety_nozzles)
