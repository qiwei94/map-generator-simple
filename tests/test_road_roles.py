import geopandas as gpd
import pytest
from shapely.geometry import LineString, Point

from _TEXTURE_STYLE_OF_DEEPSEEK.road_roles import (
    COMPOSITION_ROLE_COLUMN,
    resolve_composed_road_width_m,
    resolve_printable_road_width_m,
    ring_corridor_identity,
    road_width_multiplier_from_layers,
    resolve_structural_tier,
    resolve_visible_highways,
    select_road_roles,
)


def _roads():
    types = [
        "primary", "secondary", "tertiary", "residential", "service",
        "footway",
    ]
    geometries = [LineString([(0, y), (100, y)])
                  for y in range(len(types))]
    return gpd.GeoDataFrame(
        {"highway": types + ["primary"],
         "geometry": geometries + [Point(0, 0)]},
        geometry="geometry", crs="EPSG:3857")


def test_25km_roles_keep_dense_topology_but_reduce_visible_material():
    roles = select_road_roles(
        _roads(), topology_tier=4, nozzle_real_m=51.0)

    assert list(roles.topology["highway"]) == [
        "primary", "secondary", "tertiary", "residential", "service"]
    assert list(roles.structural["highway"]) == [
        "primary", "secondary", "tertiary", "residential"]
    assert list(roles.visible["highway"]) == ["primary", "secondary"]
    assert roles.evidence["source_features"] == 7
    assert roles.evidence["source_line_features"] == 6
    assert roles.evidence["structural_tier"] == 3


def test_5km_roles_keep_more_structure_without_showing_footways():
    roles = select_road_roles(
        _roads(), topology_tier=5, nozzle_real_m=10.2)

    assert len(roles.topology) == 6
    assert list(roles.structural["highway"])[-1] == "service"
    assert "footway" not in set(roles.structural["highway"])
    assert set(roles.visible["highway"]) == {
        "primary", "secondary", "tertiary", "residential"}


@pytest.mark.parametrize(
    ("nozzle_real_m", "expected"),
    [(51.0, 3), (30.0, 3), (15.0, 4), (5.0, 5)],
)
def test_structural_tier_tracks_printer_footprint(nozzle_real_m, expected):
    assert resolve_structural_tier(5, nozzle_real_m) == expected


def test_visible_policy_never_includes_footway_or_service():
    for footprint in (5.0, 10.0, 30.0, 51.0):
        visible = resolve_visible_highways(footprint)
        assert "footway" not in visible
        assert "service" not in visible


def test_missing_highway_tags_fall_back_to_lines_with_explicit_evidence():
    roads = gpd.GeoDataFrame(
        {"geometry": [LineString([(0, 0), (100, 0)]), Point(1, 1)]},
        geometry="geometry", crs="EPSG:3857")

    roles = select_road_roles(roads, topology_tier=4, nozzle_real_m=30.0)

    assert len(roles.topology) == len(roles.structural) == len(roles.visible) == 1
    assert roles.evidence["fallback"] == "missing_highway_column_keep_all_lines"


def test_printable_width_uses_color_floor_and_preserves_hierarchy():
    secondary = resolve_printable_road_width_m(
        "secondary",
        scale_mm_per_m=0.008,
        road_width_multiplier=2.0,
        min_colored_strip_mm=0.63,
    )
    motorway = resolve_printable_road_width_m(
        "motorway",
        scale_mm_per_m=0.008,
        road_width_multiplier=2.0,
        min_colored_strip_mm=0.63,
    )

    assert secondary * 0.008 == pytest.approx(0.63)
    assert motorway * 0.008 == pytest.approx(0.63 * 1.35)
    assert motorway > secondary


def test_composition_width_quiets_secondary_without_breaking_print_floor():
    primary = resolve_composed_road_width_m(
        "primary", composition_role="primary",
        scale_mm_per_m=0.008, road_width_multiplier=4.0,
        min_colored_strip_mm=0.63)
    secondary = resolve_composed_road_width_m(
        "primary", composition_role="secondary",
        scale_mm_per_m=0.008, road_width_multiplier=4.0,
        min_colored_strip_mm=0.63)
    floor_limited = resolve_composed_road_width_m(
        "secondary", composition_role="connector",
        scale_mm_per_m=0.008, road_width_multiplier=0.5,
        min_colored_strip_mm=0.63)

    assert primary > secondary
    assert floor_limited * 0.008 == pytest.approx(0.63)


def test_layer_width_policy_overrides_render_time_fallback():
    class Layers:
        road_roles = {"width_policy": {"road_width_multiplier": 2.75}}

    assert road_width_multiplier_from_layers(Layers(), 5.0) == 2.75


def test_old_layer_width_policy_uses_compatible_fallback():
    class Layers:
        pass

    assert road_width_multiplier_from_layers(Layers(), 5.0) == 5.0


def test_large_area_ink_budget_keeps_each_arterial_class_but_drops_links():
    roads = gpd.GeoDataFrame({
        "highway": ["primary"] * 4 + ["secondary"] * 4 + ["primary_link"],
        "name": [f"P{i}" for i in range(4)] + [f"S{i}" for i in range(4)]
                + ["link"],
        "geometry": [
            LineString([(50, 100 + i * 150), (950, 100 + i * 150)])
            for i in range(4)
        ] + [
            LineString([(100 + i * 150, 50), (100 + i * 150, 950)])
            for i in range(4)
        ] + [LineString([(0, 0), (1000, 1000)])],
    }, geometry="geometry", crs="EPSG:3857")

    roles = select_road_roles(
        roads,
        topology_tier=4,
        nozzle_real_m=30.0,
        bbox_local=(0, 0, 3000, 3000),
        scale_mm_per_m=0.02,
        road_width_multiplier=2.0,
        min_colored_strip_mm=0.63,
    )

    assert set(roles.visible["highway"]) == {"primary", "secondary"}
    assert 0 < len(roles.visible) < 8
    assert roles.evidence["visible_candidates"] == 9
    assert roles.evidence["visible_selected"] == len(roles.visible)
    assert roles.evidence["ink_budget"]["applied"] is True


def test_large_area_identity_budget_restores_short_connector_between_selected_corridors():
    roads = gpd.GeoDataFrame({
        "highway": ["primary", "primary", "primary_link"],
        "name": ["East Spine", "West Spine", None],
        "geometry": [
            LineString([(0, 4000), (4900, 4000)]),
            LineString([(5100, 4000), (10000, 4000)]),
            LineString([(4900, 4000), (5100, 4000)]),
        ],
    }, geometry="geometry", crs="EPSG:3857")

    roles = select_road_roles(
        roads,
        topology_tier=4,
        nozzle_real_m=50.0,
        bbox_local=(0, 0, 10000, 10000),
        scale_mm_per_m=0.01,
        road_width_multiplier=2.0,
        min_colored_strip_mm=0.63,
    )

    assert set(roles.visible["highway"]) == {"primary", "primary_link"}
    assert roles.evidence["ink_budget"]["connector_features"] == 1


def test_identity_score_prefers_two_axis_ring_over_equal_length_edge_road():
    ring = LineString([
        (2500, 2500), (7500, 2500), (7500, 7500),
        (2500, 7500), (2500, 2500),
    ])
    edge = LineString([(0, 100), (10000, 100)])
    roads = gpd.GeoDataFrame({
        "highway": ["primary", "primary"],
        "name": ["Central Ring", "Edge Road"],
        "geometry": [ring, edge],
    }, geometry="geometry", crs="EPSG:3857")

    roles = select_road_roles(
        roads,
        topology_tier=4,
        nozzle_real_m=50.0,
        bbox_local=(0, 0, 10000, 10000),
        scale_mm_per_m=0.01,
        road_width_multiplier=2.0,
        min_colored_strip_mm=0.63,
    )

    assert "Central Ring" in set(roles.visible["name"])


def test_city_scale_budget_adds_distributed_supporting_secondary_axes():
    roads = gpd.GeoDataFrame({
        "highway": ["secondary"] * 8,
        "name": [f"Grid {index}" for index in range(8)],
        "geometry": [
            LineString([(1000, 1000 + index * 900),
                        (6000, 1000 + index * 900)])
            for index in range(8)
        ],
    }, geometry="geometry", crs="EPSG:3857")

    roles = select_road_roles(
        roads,
        topology_tier=4,
        nozzle_real_m=50.0,
        bbox_local=(0, 0, 10000, 10000),
        scale_mm_per_m=0.01,
        road_width_multiplier=2.0,
        min_colored_strip_mm=0.63,
    )

    # The class pass keeps two 5 km corridors.  The context pass uses its
    # separate safety allowance for two distributed rows, not all six
    # remaining roads.
    assert len(roles.visible) == 4
    budget = roles.evidence["ink_budget"]
    assert budget["class_budgets"]["secondary"]["selected_groups"] == 2
    assert budget["context_selected_groups"] == 2
    assert budget["context_satisfied_after"] > budget["context_satisfied_before"]
    assert budget["selected_estimated_ink_ratio"] <= 0.0558
    assert set(roles.visible[COMPOSITION_ROLE_COLUMN]) == {
        "primary", "secondary"}
    composition = budget["composition_roles"]
    assert composition["primary"]["features"] == 3
    assert composition["secondary"]["features"] == 1
    assert composition["primary_identity_target"] == 3
    assert composition["background"]["role"] == "block_base_only"


def test_directional_ring_names_are_protected_as_one_city_identity():
    ring_parts = [
        LineString([(2500, 2500), (7500, 2500)]),
        LineString([(7500, 2500), (7500, 7500)]),
        LineString([(7500, 7500), (2500, 7500)]),
        LineString([(2500, 7500), (2500, 2500)]),
    ]
    roads = gpd.GeoDataFrame({
        "highway": ["trunk", "trunk", "primary", "primary"],
        "name": ["South 2 Ring", "East 2 Ring",
                 "North 2 Ring", "West 2 Ring"],
        "geometry": ring_parts,
    }, geometry="geometry", crs="EPSG:3857")

    roles = select_road_roles(
        roads,
        topology_tier=4,
        nozzle_real_m=50.0,
        bbox_local=(0, 0, 10000, 10000),
        scale_mm_per_m=0.01,
        road_width_multiplier=2.0,
        min_colored_strip_mm=0.63,
    )

    assert ring_corridor_identity("East 2 Ring") == "numbered-ring:2"
    assert set(roles.visible["name"]) == set(roads["name"])
    protected = roles.evidence["ink_budget"]["protected_ring"]
    assert protected["selected"] is True
    assert protected["identity"] == "numbered-ring:2"
    assert protected["features"] == 4


def test_oversized_generic_ring_does_not_automatically_become_hero():
    oversized = LineString([
        (1000, 1000), (9000, 1000), (9000, 9000),
        (1000, 9000), (1000, 1000),
    ])
    roads = gpd.GeoDataFrame({
        "highway": ["primary", "primary"],
        "name": ["Inner Ring Road", "Central Avenue"],
        "geometry": [oversized, LineString([(5000, 0), (5000, 10000)])],
    }, geometry="geometry", crs="EPSG:3857")

    roles = select_road_roles(
        roads,
        topology_tier=4,
        nozzle_real_m=50.0,
        bbox_local=(0, 0, 10000, 10000),
        scale_mm_per_m=0.01,
        road_width_multiplier=2.0,
        min_colored_strip_mm=0.63,
    )

    assert roles.evidence["ink_budget"]["protected_ring"]["selected"] is False


def test_sparse_frame_keeps_one_complete_corridor_under_global_ink_limit():
    roads = gpd.GeoDataFrame({
        "highway": ["primary"],
        "name": ["Only Avenue"],
        "geometry": [LineString([(0, 500), (1000, 500)])],
    }, geometry="geometry", crs="EPSG:3857")

    roles = select_road_roles(
        roads,
        topology_tier=4,
        nozzle_real_m=30.0,
        bbox_local=(0, 0, 1000, 1000),
        scale_mm_per_m=0.02,
        road_width_multiplier=2.0,
        min_colored_strip_mm=0.63,
    )

    assert list(roles.visible["name"]) == ["Only Avenue"]
    budget = roles.evidence["ink_budget"]
    assert budget["class_budgets"]["primary"]["selected_groups"] == 0
    assert budget["empty_selection_fallback"]["identity"] == "name:only avenue"
    assert budget["selected_estimated_ink_ratio"] <= 0.055


def test_visual_salience_promotes_complete_osm_identity_not_fragments():
    class Guide:
        version = "synthetic-salience-v1"

        @staticmethod
        def road_support(geometry):
            supported = geometry.centroid.y > 1200
            return {
                "covered_fraction": 0.95 if supported else 0.0,
                "weighted_salience": 0.90 if supported else 0.0,
            }

    roads = gpd.GeoDataFrame({
        "highway": ["secondary"] * 8,
        "name": [f"Corridor {letter}" for letter in "ABCDEFGH"],
        "geometry": [
            LineString([(1000, 1300 if index == 3 else 1000),
                        (6000, 1300 if index == 3 else 1000)])
            for index in range(8)
        ],
    }, geometry="geometry", crs="EPSG:3857")

    baseline = select_road_roles(
        roads, topology_tier=4, nozzle_real_m=50.0,
        bbox_local=(0, 0, 10000, 10000), scale_mm_per_m=0.01)
    guided = select_road_roles(
        roads, topology_tier=4, nozzle_real_m=50.0,
        bbox_local=(0, 0, 10000, 10000), scale_mm_per_m=0.01,
        visual_salience_guide=Guide())

    assert "Corridor D" not in set(baseline.visible["name"])
    assert "Corridor D" in set(guided.visible["name"])
    evidence = guided.evidence["ink_budget"]["visual_salience"]
    assert evidence["enabled"] is True
    assert evidence["selected_identities"] == [
        "secondary:name:corridor d"]
