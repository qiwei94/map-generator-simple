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


def test_spatial_template_assigns_three_roles_without_copying_geometry():
    class Guide:
        version = "synthetic-salience-v1"
        template_policy_version = "synthetic-template-v1"

        @staticmethod
        def road_support(geometry):
            y = geometry.centroid.y
            if y > 700:
                return {
                    "covered_fraction": 0.95,
                    "any_template_fraction": 0.95,
                    "weighted_salience": 0.92,
                    "major_mask_fraction": 0.85,
                    "arterial_or_major_fraction": 0.90,
                    "context_mask_fraction": 0.0,
                }
            if y > 500:
                return {
                    "covered_fraction": 0.90,
                    "any_template_fraction": 0.90,
                    "weighted_salience": 0.66,
                    "major_mask_fraction": 0.0,
                    "arterial_or_major_fraction": 0.82,
                    "context_mask_fraction": 0.0,
                }
            if y > 300:
                return {
                    "covered_fraction": 0.88,
                    "any_template_fraction": 0.88,
                    "weighted_salience": 0.36,
                    "major_mask_fraction": 0.0,
                    "arterial_or_major_fraction": 0.0,
                    "context_mask_fraction": 0.80,
                }
            return {
                "covered_fraction": 0.0,
                "any_template_fraction": 0.0,
                "weighted_salience": 0.0,
                "major_mask_fraction": 0.0,
                "arterial_or_major_fraction": 0.0,
                "context_mask_fraction": 0.0,
            }

    roads = gpd.GeoDataFrame({
        "highway": ["primary", "secondary", "tertiary", "primary"],
        "name": ["Major", "Arterial", "Context", "Unsupported"],
        "geometry": [
            LineString([(100, 800), (900, 800)]),
            LineString([(100, 600), (900, 600)]),
            LineString([(100, 400), (900, 400)]),
            LineString([(100, 200), (900, 200)]),
        ],
    }, geometry="geometry", crs="EPSG:3857")
    source_wkb = dict(zip(roads["name"], roads.geometry.to_wkb()))

    roles = select_road_roles(
        roads,
        topology_tier=4,
        nozzle_real_m=50.0,
        bbox_local=(0, 0, 1000, 1000),
        scale_mm_per_m=0.2,
        visual_salience_guide=Guide(),
    )

    selected = roles.visible.set_index("name")
    assert set(selected.index) == {"Major", "Arterial", "Context"}
    assert selected.loc["Major", "_composition_role"] == "primary"
    assert selected.loc["Arterial", "_composition_role"] == "secondary"
    assert selected.loc["Context", "_composition_role"] == "context"
    assert "tertiary" in roles.evidence["visible_highways"]
    for name, geometry in selected.geometry.items():
        assert geometry.wkb == source_wkb[name]

    budget = roles.evidence["ink_budget"]
    assert budget["method"] == (
        "amap_backbone_plus_osm_mid_frequency_existing_v7")
    assert "hard_ink_limit_ratio" not in budget
    assert budget["corridor_matching"]["selected_corridors"] == 3
    assert budget["composition_roles"]["context"]["features"] == 1


def test_spatial_template_adds_complete_unmasked_crosslink_as_mid_frequency():
    class Guide:
        version = "synthetic-salience-v1"
        template_policy_version = "synthetic-template-v1"

        @staticmethod
        def road_support(geometry):
            supported = geometry.centroid.x in (2000, 8000)
            value = 0.95 if supported else 0.0
            return {
                "covered_fraction": value,
                "any_template_fraction": value,
                "weighted_salience": 0.90 if supported else 0.0,
                "major_mask_fraction": 0.80 if supported else 0.0,
                "arterial_or_major_fraction": 0.90 if supported else 0.0,
                "context_mask_fraction": 0.0,
            }

    roads = gpd.GeoDataFrame({
        "highway": ["primary", "primary", "secondary"],
        "name": ["West Spine", "East Spine", "Middle Connector"],
        "geometry": [
            LineString([(2000, 1000), (2000, 9000)]),
            LineString([(8000, 1000), (8000, 9000)]),
            LineString([(2000, 5000), (8000, 5000)]),
        ],
    }, geometry="geometry", crs="EPSG:3857")
    source_wkb = roads.set_index("name").geometry.to_wkb().to_dict()

    roles = select_road_roles(
        roads, topology_tier=4, nozzle_real_m=50.0,
        bbox_local=(0, 0, 10000, 10000), scale_mm_per_m=0.02,
        visual_salience_guide=Guide(),
    )

    selected = roles.visible.set_index("name")
    assert set(selected.index) == {
        "West Spine", "East Spine", "Middle Connector"}
    assert selected.loc[
        "Middle Connector", COMPOSITION_ROLE_COLUMN] == "context"
    assert selected.loc["Middle Connector"].geometry.wkb == source_wkb[
        "Middle Connector"]
    supplement = roles.evidence["ink_budget"]["corridor_matching"][
        "mid_frequency_supplement"]
    assert supplement["selected_corridors"] == 1
    assert supplement["crosslinks"] == 1
    assert supplement["selected_features"] == 1


def test_spatial_template_rejects_unmasked_one_ended_mid_frequency_branch():
    class Guide:
        version = "synthetic-salience-v1"
        template_policy_version = "synthetic-template-v1"

        @staticmethod
        def road_support(geometry):
            supported = geometry.centroid.y == 5000
            value = 0.95 if supported else 0.0
            return {
                "covered_fraction": value,
                "any_template_fraction": value,
                "weighted_salience": 0.90 if supported else 0.0,
                "major_mask_fraction": 0.80 if supported else 0.0,
                "arterial_or_major_fraction": 0.90 if supported else 0.0,
                "context_mask_fraction": 0.0,
            }

    roads = gpd.GeoDataFrame({
        "highway": ["primary", "secondary"],
        "name": ["Main Axis", "One End Branch"],
        "geometry": [
            LineString([(1000, 5000), (9000, 5000)]),
            LineString([(5000, 5000), (5000, 7000)]),
        ],
    }, geometry="geometry", crs="EPSG:3857")

    roles = select_road_roles(
        roads, topology_tier=4, nozzle_real_m=50.0,
        bbox_local=(0, 0, 10000, 10000), scale_mm_per_m=0.02,
        visual_salience_guide=Guide(),
    )

    assert list(roles.visible["name"]) == ["Main Axis"]
    supplement = roles.evidence["ink_budget"]["corridor_matching"][
        "mid_frequency_supplement"]
    assert supplement["selected_corridors"] == 0
    assert supplement["rejected_one_ended"] == 1


def test_spatial_template_rejects_unmasked_parallel_mid_frequency_axis():
    class Guide:
        version = "synthetic-salience-v1"
        template_policy_version = "synthetic-template-v1"

        @staticmethod
        def road_support(geometry):
            supported = geometry.centroid.y == 5000
            value = 0.95 if supported else 0.0
            return {
                "covered_fraction": value,
                "any_template_fraction": value,
                "weighted_salience": 0.90 if supported else 0.0,
                "major_mask_fraction": 0.80 if supported else 0.0,
                "arterial_or_major_fraction": 0.90 if supported else 0.0,
                "context_mask_fraction": 0.0,
            }

    roads = gpd.GeoDataFrame({
        "highway": ["primary", "secondary"],
        "name": ["Main Axis", "Parallel Axis"],
        "geometry": [
            LineString([(1000, 5000), (9000, 5000)]),
            LineString([(1000, 5040), (9000, 5040)]),
        ],
    }, geometry="geometry", crs="EPSG:3857")

    roles = select_road_roles(
        roads, topology_tier=4, nozzle_real_m=50.0,
        bbox_local=(0, 0, 10000, 10000), scale_mm_per_m=0.02,
        visual_salience_guide=Guide(),
    )

    assert list(roles.visible["name"]) == ["Main Axis"]
    supplement = roles.evidence["ink_budget"]["corridor_matching"][
        "mid_frequency_supplement"]
    assert supplement["selected_corridors"] == 0
    assert supplement["rejected_parallel"] == 1


def test_spatial_template_rejects_short_intersection_spur_but_keeps_corridor():
    class Guide:
        version = "synthetic-salience-v1"
        template_policy_version = "synthetic-template-v1"

        @staticmethod
        def road_support(geometry):
            return {
                "covered_fraction": 0.95,
                "any_template_fraction": 0.95,
                "weighted_salience": 0.90,
                "major_mask_fraction": 0.80,
                "arterial_or_major_fraction": 0.90,
                "context_mask_fraction": 0.0,
            }

    corridor = [
        LineString([(100 + index * 100, 500),
                    (200 + index * 100, 500)])
        for index in range(5)
    ]
    roads = gpd.GeoDataFrame({
        "highway": ["primary"] * 6,
        "name": ["Main Axis"] * 5 + ["Cross Spur"],
        "geometry": corridor + [LineString([(350, 475), (350, 525)])],
    }, geometry="geometry", crs="EPSG:3857")

    roles = select_road_roles(
        roads,
        topology_tier=4,
        nozzle_real_m=100.0,
        bbox_local=(0, 0, 1000, 1000),
        scale_mm_per_m=0.2,
        visual_salience_guide=Guide(),
    )

    assert set(roles.visible["name"]) == {"Main Axis"}
    assert len(roles.visible) == 5


def test_spatial_template_selects_complete_corridor_across_mask_gap():
    class Guide:
        version = "synthetic-salience-v1"
        template_policy_version = "synthetic-template-v1"

        @staticmethod
        def road_support(geometry):
            supported = not 4000 < geometry.centroid.x < 4200
            value = 0.95 if supported else 0.0
            return {
                "covered_fraction": value,
                "any_template_fraction": value,
                "weighted_salience": 0.90 if supported else 0.0,
                "major_mask_fraction": 0.80 if supported else 0.0,
                "arterial_or_major_fraction": 0.90 if supported else 0.0,
                "context_mask_fraction": 0.0,
            }

    roads = gpd.GeoDataFrame({
        "highway": ["primary", "primary", "primary"],
        "name": ["Main Axis"] * 3,
        "geometry": [
            LineString([(1000, 5000), (4000, 5000)]),
            LineString([(4000, 5000), (4200, 5000)]),
            LineString([(4200, 5000), (9000, 5000)]),
        ],
    }, geometry="geometry", crs="EPSG:3857")
    source_wkb = roads.geometry.iloc[1].wkb

    roles = select_road_roles(
        roads, topology_tier=4, nozzle_real_m=50.0,
        bbox_local=(0, 0, 10000, 10000), scale_mm_per_m=0.02,
        visual_salience_guide=Guide(),
    )

    assert len(roles.visible) == 3
    assert roles.visible.geometry.iloc[1].wkb == source_wkb
    budget = roles.evidence["ink_budget"]
    matching = budget["corridor_matching"]
    assert matching["selected_corridors"] == 1
    assert matching["promoted_complete_path_features"] == 1
    assert budget["continuity_restoration"]["restored_paths"] == 0


def test_spatial_template_keeps_required_bounded_link_in_complete_corridor():
    class Guide:
        version = "synthetic-salience-v1"
        template_policy_version = "synthetic-template-v1"

        @staticmethod
        def road_support(geometry):
            supported = geometry.length > 1000
            value = 0.95 if supported else 0.0
            return {
                "covered_fraction": value,
                "any_template_fraction": value,
                "weighted_salience": 0.90 if supported else 0.0,
                "major_mask_fraction": 0.80 if supported else 0.0,
                "arterial_or_major_fraction": 0.90 if supported else 0.0,
                "context_mask_fraction": 0.0,
            }

    roads = gpd.GeoDataFrame({
        "highway": ["primary", "primary_link", "primary"],
        "name": ["Through Axis"] * 3,
        "geometry": [
            LineString([(1000, 5000), (4000, 5000)]),
            LineString([(4000, 5000), (4200, 5000)]),
            LineString([(4200, 5000), (9000, 5000)]),
        ],
    }, geometry="geometry", crs="EPSG:3857")

    roles = select_road_roles(
        roads, topology_tier=4, nozzle_real_m=50.0,
        bbox_local=(0, 0, 10000, 10000), scale_mm_per_m=0.02,
        visual_salience_guide=Guide(),
    )

    assert set(roles.visible["highway"]) == {"primary", "primary_link"}
    matching = roles.evidence["ink_budget"]["corridor_matching"]
    assert matching["internal_link_features"] == 1


def test_spatial_template_infers_aligned_unnamed_link_inside_same_ring():
    class Guide:
        version = "synthetic-salience-v1"
        template_policy_version = "synthetic-template-v1"

        @staticmethod
        def road_support(geometry):
            supported = geometry.length > 1000
            return {
                "covered_fraction": 0.95 if supported else 0.35,
                "any_template_fraction": 0.95 if supported else 0.35,
                "weighted_salience": 0.90 if supported else 0.14,
                "major_mask_fraction": 0.80 if supported else 0.0,
                "arterial_or_major_fraction": 0.90 if supported else 0.0,
                "context_mask_fraction": 0.0,
            }

    roads = gpd.GeoDataFrame({
        "highway": ["trunk", "trunk_link", "trunk"],
        "name": ["East 3 Ring", None, "South 3 Ring"],
        "geometry": [
            LineString([(1000, 5000), (4000, 5000)]),
            LineString([(4000, 5000), (4200, 5000)]),
            LineString([(4200, 5000), (9000, 5000)]),
        ],
    }, geometry="geometry", crs="EPSG:3857")

    roles = select_road_roles(
        roads, topology_tier=4, nozzle_real_m=50.0,
        bbox_local=(0, 0, 10000, 10000), scale_mm_per_m=0.02,
        visual_salience_guide=Guide(),
    )

    assert set(roles.visible["highway"]) == {"trunk", "trunk_link"}
    matching = roles.evidence["ink_budget"]["corridor_matching"]
    assert matching["physical_grouping"][
        "cross_identity_continuation_pairs"] >= 1


def test_spatial_template_closes_ring_even_when_other_side_is_connected():
    class Guide:
        version = "synthetic-salience-v1"
        template_policy_version = "synthetic-template-v1"

        @staticmethod
        def road_support(geometry):
            supported = geometry.length > 1000
            return {
                "covered_fraction": 0.95 if supported else 0.35,
                "any_template_fraction": 0.95 if supported else 0.35,
                "weighted_salience": 0.90 if supported else 0.14,
                "major_mask_fraction": 0.80 if supported else 0.0,
                "arterial_or_major_fraction": 0.90 if supported else 0.0,
                "context_mask_fraction": 0.0,
            }

    roads = gpd.GeoDataFrame({
        "highway": ["trunk"] * 5 + ["trunk_link"],
        "name": [
            "East 3 Ring", "East 3 Ring", "South 3 Ring",
            "West 3 Ring", "North 3 Ring", None,
        ],
        "geometry": [
            LineString([(4000, 1000), (4000, 4900)]),
            LineString([(4000, 5100), (4000, 9000)]),
            LineString([(4000, 1000), (2000, 1000)]),
            LineString([(2000, 1000), (2000, 9000)]),
            LineString([(2000, 9000), (4000, 9000)]),
            LineString([(4000, 4900), (4000, 5100)]),
        ],
    }, geometry="geometry", crs="EPSG:3857")

    roles = select_road_roles(
        roads, topology_tier=4, nozzle_real_m=50.0,
        bbox_local=(0, 0, 10000, 10000), scale_mm_per_m=0.02,
        visual_salience_guide=Guide(),
    )

    assert set(roles.visible["highway"]) == {"trunk", "trunk_link"}
    matching = roles.evidence["ink_budget"]["corridor_matching"]
    assert matching["physical_grouping"][
        "cross_identity_continuation_pairs"] >= 1


def test_spatial_template_matches_physical_route_across_name_and_class_change():
    class Guide:
        version = "synthetic-salience-v1"
        template_policy_version = "synthetic-template-v1"

        @staticmethod
        def road_support(geometry):
            supported = geometry.length > 1500
            return {
                "covered_fraction": 0.95 if supported else 0.0,
                "any_template_fraction": 0.95 if supported else 0.0,
                "weighted_salience": 0.90 if supported else 0.0,
                "major_mask_fraction": 0.80 if supported else 0.0,
                "arterial_or_major_fraction": 0.90 if supported else 0.0,
                "context_mask_fraction": 0.0,
            }

    roads = gpd.GeoDataFrame({
        "highway": ["primary", "secondary", "primary", "secondary"],
        "name": ["Alpha Road", "Interchange", "Gamma Avenue", "Short Spur"],
        "geometry": [
            LineString([(1000, 5000), (4000, 5000)]),
            LineString([(4000, 5000), (4200, 5000)]),
            LineString([(4200, 5000), (9000, 5000)]),
            LineString([(4000, 5000), (4000, 6000)]),
        ],
    }, geometry="geometry", crs="EPSG:3857")

    roles = select_road_roles(
        roads, topology_tier=4, nozzle_real_m=50.0,
        bbox_local=(0, 0, 10000, 10000), scale_mm_per_m=0.02,
        visual_salience_guide=Guide(),
    )

    assert set(roles.visible["name"]) == {
        "Alpha Road", "Interchange", "Gamma Avenue"}
    matching = roles.evidence["ink_budget"]["corridor_matching"]
    assert matching["route_matching_method"] == (
        "global_existing_osm_physical_corridors_v1")
    assert matching["physical_grouping"][
        "cross_identity_continuation_pairs"] >= 2


def test_spatial_template_selects_complete_long_semantic_corridor():
    class Guide:
        version = "synthetic-salience-v1"
        template_policy_version = "synthetic-template-v1"

        @staticmethod
        def road_support(geometry):
            supported = geometry.length > 1000
            value = 0.95 if supported else 0.0
            return {
                "covered_fraction": value,
                "any_template_fraction": value,
                "weighted_salience": 0.90 if supported else 0.0,
                "major_mask_fraction": 0.80 if supported else 0.0,
                "arterial_or_major_fraction": 0.90 if supported else 0.0,
                "context_mask_fraction": 0.0,
            }

    roads = gpd.GeoDataFrame({
        "highway": ["primary", "primary", "primary"],
        "name": ["Long Axis"] * 3,
        "geometry": [
            LineString([(1000, 5000), (4000, 5000)]),
            LineString([(4000, 5000), (4600, 5000)]),
            LineString([(4600, 5000), (9000, 5000)]),
        ],
    }, geometry="geometry", crs="EPSG:3857")

    roles = select_road_roles(
        roads, topology_tier=4, nozzle_real_m=50.0,
        bbox_local=(0, 0, 10000, 10000), scale_mm_per_m=0.02,
        visual_salience_guide=Guide(),
    )

    assert len(roles.visible) == 3
    matching = roles.evidence["ink_budget"]["corridor_matching"]
    assert matching["selected_corridors"] == 1
    assert matching["promoted_complete_path_features"] == 1


def test_spatial_template_joins_aligned_offset_osm_corridor_segments():
    class Guide:
        version = "synthetic-salience-v1"
        template_policy_version = "synthetic-template-v1"

        @staticmethod
        def road_support(geometry):
            supported = geometry.length > 1000
            value = 0.95 if supported else 0.0
            return {
                "covered_fraction": value,
                "any_template_fraction": value,
                "weighted_salience": 0.90 if supported else 0.0,
                "major_mask_fraction": 0.80 if supported else 0.0,
                "arterial_or_major_fraction": 0.90 if supported else 0.0,
                "context_mask_fraction": 0.0,
            }

    roads = gpd.GeoDataFrame({
        "highway": ["primary", "primary", "primary"],
        "name": ["Offset Axis"] * 3,
        "geometry": [
            LineString([(1000, 5000), (4000, 5000)]),
            LineString([(4008, 5006), (4592, 5006)]),
            LineString([(4600, 5000), (9000, 5000)]),
        ],
    }, geometry="geometry", crs="EPSG:3857")

    roles = select_road_roles(
        roads, topology_tier=4, nozzle_real_m=50.0,
        bbox_local=(0, 0, 10000, 10000), scale_mm_per_m=0.02,
        visual_salience_guide=Guide(),
    )

    assert len(roles.visible) == 3
    matching = roles.evidence["ink_budget"]["corridor_matching"]
    assert matching["selected_corridors"] == 1
    assert matching["joined_endpoint_pairs"] == 2
    assert matching["promoted_complete_path_features"] == 1


def test_spatial_template_rejects_nearby_offset_segment_with_other_identity():
    class Guide:
        version = "synthetic-salience-v1"
        template_policy_version = "synthetic-template-v1"

        @staticmethod
        def road_support(geometry):
            supported = geometry.length > 1000
            value = 0.95 if supported else 0.0
            return {
                "covered_fraction": value,
                "any_template_fraction": value,
                "weighted_salience": 0.90 if supported else 0.0,
                "major_mask_fraction": 0.80 if supported else 0.0,
                "arterial_or_major_fraction": 0.90 if supported else 0.0,
                "context_mask_fraction": 0.0,
            }

    roads = gpd.GeoDataFrame({
        "highway": ["primary", "primary", "primary"],
        "name": ["Main Axis", "Side Axis", "Main Axis"],
        "geometry": [
            LineString([(1000, 5000), (4000, 5000)]),
            LineString([(4008, 5006), (4592, 5006)]),
            LineString([(4600, 5000), (9000, 5000)]),
        ],
    }, geometry="geometry", crs="EPSG:3857")

    roles = select_road_roles(
        roads, topology_tier=4, nozzle_real_m=50.0,
        bbox_local=(0, 0, 10000, 10000), scale_mm_per_m=0.02,
        visual_salience_guide=Guide(),
    )

    assert set(roles.visible["name"]) == {"Main Axis"}
    restoration = roles.evidence["ink_budget"]["continuity_restoration"]
    assert restoration["proximity_corridor_paths"] == 0


def test_spatial_template_does_not_add_link_between_connected_anchors():
    class Guide:
        version = "synthetic-salience-v1"
        template_policy_version = "synthetic-template-v1"

        @staticmethod
        def road_support(geometry):
            supported = geometry.length > 250
            value = 0.95 if supported else 0.0
            return {
                "covered_fraction": value,
                "any_template_fraction": value,
                "weighted_salience": 0.90 if supported else 0.0,
                "major_mask_fraction": 0.80 if supported else 0.0,
                "arterial_or_major_fraction": 0.90 if supported else 0.0,
                "context_mask_fraction": 0.0,
            }

    roads = gpd.GeoDataFrame({
        "highway": ["primary", "primary", "primary", "primary_link"],
        "name": ["Through Axis"] * 4,
        "geometry": [
            LineString([(1000, 5000), (4000, 5000)]),
            LineString([(4000, 5000), (4100, 5250), (4200, 5000)]),
            LineString([(4200, 5000), (9000, 5000)]),
            LineString([(4000, 5000), (4200, 5000)]),
        ],
    }, geometry="geometry", crs="EPSG:3857")

    roles = select_road_roles(
        roads, topology_tier=4, nozzle_real_m=50.0,
        bbox_local=(0, 0, 10000, 10000), scale_mm_per_m=0.02,
        visual_salience_guide=Guide(),
    )

    assert set(roles.visible["highway"]) == {"primary"}
    assert roles.evidence["ink_budget"]["continuity_restoration"][
        "restored_link_features"] == 0


def test_spatial_template_does_not_restore_one_ended_osm_branch():
    class Guide:
        version = "synthetic-salience-v1"
        template_policy_version = "synthetic-template-v1"

        @staticmethod
        def road_support(geometry):
            supported = geometry.centroid.y == 5000
            value = 0.95 if supported else 0.0
            return {
                "covered_fraction": value,
                "any_template_fraction": value,
                "weighted_salience": 0.90 if supported else 0.0,
                "major_mask_fraction": 0.80 if supported else 0.0,
                "arterial_or_major_fraction": 0.90 if supported else 0.0,
                "context_mask_fraction": 0.0,
            }

    roads = gpd.GeoDataFrame({
        "highway": ["primary", "primary"],
        "name": ["Main Axis", "Main Axis"],
        "geometry": [
            LineString([(1000, 5000), (9000, 5000)]),
            LineString([(5000, 5000), (5000, 5200)]),
        ],
    }, geometry="geometry", crs="EPSG:3857")

    roles = select_road_roles(
        roads, topology_tier=4, nozzle_real_m=50.0,
        bbox_local=(0, 0, 10000, 10000), scale_mm_per_m=0.02,
        visual_salience_guide=Guide(),
    )

    assert len(roles.visible) == 1
    assert roles.evidence["ink_budget"]["continuity_restoration"][
        "restored_features"] == 0


def test_spatial_template_keeps_complete_named_corridor_without_gap_limit():
    class Guide:
        version = "synthetic-salience-v1"
        template_policy_version = "synthetic-template-v1"

        @staticmethod
        def road_support(geometry):
            supported = geometry.length > 1000
            value = 0.95 if supported else 0.0
            return {
                "covered_fraction": value,
                "any_template_fraction": value,
                "weighted_salience": 0.90 if supported else 0.0,
                "major_mask_fraction": 0.80 if supported else 0.0,
                "arterial_or_major_fraction": 0.90 if supported else 0.0,
                "context_mask_fraction": 0.0,
            }

    roads = gpd.GeoDataFrame({
        "highway": ["primary"] * 4,
        "name": ["Main Axis"] * 4,
        "geometry": [
            LineString([(1000, 5000), (4000, 5000)]),
            LineString([(4000, 5000), (4000, 5300)]),
            LineString([(4000, 5300), (4200, 5000)]),
            LineString([(4200, 5000), (9000, 5000)]),
        ],
    }, geometry="geometry", crs="EPSG:3857")

    roles = select_road_roles(
        roads, topology_tier=4, nozzle_real_m=50.0,
        bbox_local=(0, 0, 10000, 10000), scale_mm_per_m=0.02,
        visual_salience_guide=Guide(),
    )

    assert len(roles.visible) == 4
    budget = roles.evidence["ink_budget"]
    assert budget["corridor_matching"]["selected_corridors"] == 1
    assert budget["corridor_matching"][
        "promoted_complete_path_features"] == 2
    assert budget["continuity_restoration"]["restored_features"] == 0


def test_spatial_template_collapses_unprintable_parallel_carriageway():
    class Guide:
        version = "synthetic-salience-v1"
        template_policy_version = "synthetic-template-v1"

        @staticmethod
        def road_support(geometry):
            stronger = geometry.centroid.y < 5030
            return {
                "covered_fraction": 0.95 if stronger else 0.72,
                "any_template_fraction": 0.95 if stronger else 0.72,
                "weighted_salience": 0.90 if stronger else 0.62,
                "major_mask_fraction": 0.80 if stronger else 0.55,
                "arterial_or_major_fraction": 0.90,
                "context_mask_fraction": 0.0,
            }

    roads = gpd.GeoDataFrame({
        "highway": ["primary", "primary"],
        "name": ["Main Axis", "Main Axis"],
        "geometry": [
            LineString([(1000, 5000), (9000, 5000)]),
            LineString([(1000, 5040), (9000, 5040)]),
        ],
    }, geometry="geometry", crs="EPSG:3857")

    roles = select_road_roles(
        roads,
        topology_tier=4,
        nozzle_real_m=50.0,
        bbox_local=(0, 0, 10000, 10000),
        scale_mm_per_m=0.02,
        visual_salience_guide=Guide(),
    )

    assert len(roles.visible) == 1
    assert roles.visible.geometry.iloc[0].centroid.y == 5000
    matching = roles.evidence["ink_budget"]["corridor_matching"]
    assert matching["collapsed_parallel_corridors"] == 1
    assert matching["collapsed_parallel_features"] == 1


def test_spatial_template_prunes_multi_segment_dangling_thread():
    class Guide:
        version = "synthetic-salience-v1"
        template_policy_version = "synthetic-template-v1"

        @staticmethod
        def road_support(geometry):
            return {
                "covered_fraction": 0.95,
                "any_template_fraction": 0.95,
                "weighted_salience": 0.90,
                "major_mask_fraction": 0.80,
                "arterial_or_major_fraction": 0.90,
                "context_mask_fraction": 0.0,
            }

    roads = gpd.GeoDataFrame({
        "highway": ["primary", "primary", "primary"],
        "name": ["Main Axis", "Side Thread", "Side Thread"],
        "geometry": [
            LineString([(1000, 5000), (9000, 5000)]),
            LineString([(5000, 5000), (5000, 5100)]),
            LineString([(5000, 5100), (5000, 5220)]),
        ],
    }, geometry="geometry", crs="EPSG:3857")

    roles = select_road_roles(
        roads,
        topology_tier=4,
        nozzle_real_m=50.0,
        bbox_local=(0, 0, 10000, 10000),
        scale_mm_per_m=0.02,
        visual_salience_guide=Guide(),
    )

    assert list(roles.visible["name"]) == ["Main Axis"]
    matching = roles.evidence["ink_budget"]["corridor_matching"]
    assert matching["skeleton_dropped_corridors"] == 1
    assert matching["skeleton_dropped_features"] == 2
    assert matching["skeleton_dropped_length_m"] == 220.0
    pruning = roles.evidence["ink_budget"]["dangling_chain_pruning"]
    assert pruning["method"] == (
        "disabled_by_complete_corridor_leaf_graph_v1")
    assert pruning["removed_features"] == 0
    assert pruning["removed_length_m"] == 0.0


def test_spatial_template_keeps_short_road_connected_at_both_ends():
    class Guide:
        version = "synthetic-salience-v1"
        template_policy_version = "synthetic-template-v1"

        @staticmethod
        def road_support(geometry):
            return {
                "covered_fraction": 0.95,
                "any_template_fraction": 0.95,
                "weighted_salience": 0.90,
                "major_mask_fraction": 0.80,
                "arterial_or_major_fraction": 0.90,
                "context_mask_fraction": 0.0,
            }

    roads = gpd.GeoDataFrame({
        "highway": ["primary", "primary", "primary"],
        "name": ["North Axis", "South Axis", "True Connector"],
        "geometry": [
            LineString([(1000, 5110), (9000, 5110)]),
            LineString([(1000, 4890), (9000, 4890)]),
            LineString([(5000, 4890), (5000, 5110)]),
        ],
    }, geometry="geometry", crs="EPSG:3857")

    roles = select_road_roles(
        roads,
        topology_tier=4,
        nozzle_real_m=50.0,
        bbox_local=(0, 0, 10000, 10000),
        scale_mm_per_m=0.02,
        visual_salience_guide=Guide(),
    )

    assert set(roles.visible["name"]) == {
        "North Axis", "South Axis", "True Connector",
    }
    pruning = roles.evidence["ink_budget"]["dangling_chain_pruning"]
    assert pruning["removed_features"] == 0


def test_spatial_template_protects_bridge_and_frame_edge_leaf():
    class Guide:
        version = "synthetic-salience-v1"
        template_policy_version = "synthetic-template-v1"

        @staticmethod
        def road_support(geometry):
            return {
                "covered_fraction": 0.95,
                "any_template_fraction": 0.95,
                "weighted_salience": 0.90,
                "major_mask_fraction": 0.80,
                "arterial_or_major_fraction": 0.90,
                "context_mask_fraction": 0.0,
            }

    roads = gpd.GeoDataFrame({
        "highway": ["primary", "primary", "primary"],
        "name": ["Main Axis", "Bridge Leaf", "Boundary Leaf"],
        "bridge": [None, "yes", None],
        "geometry": [
            LineString([(1000, 5000), (9000, 5000)]),
            LineString([(5000, 5000), (5000, 5200)]),
            LineString([(0, 2000), (200, 2000)]),
        ],
    }, geometry="geometry", crs="EPSG:3857")

    roles = select_road_roles(
        roads,
        topology_tier=4,
        nozzle_real_m=50.0,
        bbox_local=(0, 0, 10000, 10000),
        scale_mm_per_m=0.02,
        visual_salience_guide=Guide(),
    )

    assert set(roles.visible["name"]) == {
        "Main Axis", "Bridge Leaf", "Boundary Leaf",
    }
