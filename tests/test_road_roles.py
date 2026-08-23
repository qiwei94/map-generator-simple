import geopandas as gpd
import pytest
from shapely.geometry import LineString, Point

from _TEXTURE_STYLE_OF_DEEPSEEK.road_roles import (
    resolve_printable_road_width_m,
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
        bbox_local=(0, 0, 1000, 1000),
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
