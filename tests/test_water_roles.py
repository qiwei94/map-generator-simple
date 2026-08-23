import geopandas as gpd
from shapely.geometry import LineString, box

from _TEXTURE_STYLE_OF_DEEPSEEK.water_roles import (
    WaterLineCandidate,
    retain_continuous_water_source,
    select_visible_water_lines,
)


def test_source_limit_never_drops_linear_water_connectors():
    lines = [LineString([(index, 0), (index + 1, 0)]) for index in range(8)]
    polygons = [box(index, 10, index + 1, 11) for index in range(6)]
    water = gpd.GeoDataFrame({
        "est_area": [1.0] * len(lines) + list(range(1, 7)),
        "geometry": lines + polygons,
    }, geometry="geometry", crs="EPSG:3857")

    retained, evidence = retain_continuous_water_source(
        water, max_polygon_features=2)

    assert sum(retained.geometry.geom_type == "LineString") == len(lines)
    assert sum(retained.geometry.geom_type == "Polygon") == 2
    assert evidence["dropped_polygon_features"] == 4


def test_named_water_corridor_is_selected_as_a_whole_and_small_gap_is_closed():
    candidates = [
        WaterLineCandidate(
            LineString([(0, 500), (450, 500)]), "river", "name:identity river", 30),
        WaterLineCandidate(
            LineString([(500, 500), (1000, 500)]), "river", "name:identity river", 30),
    ]

    selected = select_visible_water_lines(
        candidates,
        bbox_local=(0, 0, 1000, 1000),
        nozzle_real_m=30.0,
    )

    assert selected.evidence["selected_groups"] == 1
    assert selected.evidence["gap_bridges"] == 1
    assert sum(line.length for line, _, _ in selected.lines) == 1000


def test_large_area_water_budget_prefers_frame_spanning_named_corridor():
    candidates = [
        WaterLineCandidate(
            LineString([(0, 5000), (10000, 5000)]),
            "canal", "name:grand canal", 20),
    ]
    candidates.extend(
        WaterLineCandidate(
            LineString([(100 + index * 100, 100),
                        (150 + index * 100, 150)]),
            "canal", f"name:pond drain {index}", 20)
        for index in range(30)
    )

    selected = select_visible_water_lines(
        candidates,
        bbox_local=(0, 0, 10000, 10000),
        nozzle_real_m=50.0,
    )

    identities = selected.evidence["selected_groups"]
    assert identities < selected.evidence["candidate_groups"]
    assert any(line.length >= 9999 for line, _, _ in selected.lines)


def test_large_area_uses_one_global_budget_instead_of_promoting_each_class():
    candidates = [
        WaterLineCandidate(
            LineString([(0, 5000), (10000, 5000)]),
            "river", "name:identity river", 30),
        WaterLineCandidate(
            LineString([(0, 2000), (10000, 2000)]),
            "canal", "name:identity canal", 20),
        WaterLineCandidate(
            LineString([(0, 1000), (10000, 1000)]),
            "stream", "name:minor stream", 6),
        WaterLineCandidate(
            LineString([(0, 700), (10000, 700)]),
            "drain", "name:storm drain", 3),
    ]

    selected = select_visible_water_lines(
        candidates,
        bbox_local=(0, 0, 10000, 10000),
        nozzle_real_m=50.0,
    )

    visible_types = {waterway for _, waterway, _ in selected.lines}
    assert visible_types == {"river", "canal"}
    assert selected.evidence["class_budgets"]["stream"]["selected_groups"] == 0
    assert selected.evidence["class_budgets"]["drain"]["selected_groups"] == 0


def test_large_area_caps_visible_water_corridors_for_visual_hierarchy():
    candidates = [
        WaterLineCandidate(
            LineString([(1000, 1000 + index * 1500),
                        (8500, 1000 + index * 1500)]),
            "river", f"name:river {index}", 25)
        for index in range(5)
    ]

    selected = select_visible_water_lines(
        candidates,
        bbox_local=(0, 0, 10000, 10000),
        nozzle_real_m=50.0,
    )

    assert selected.evidence["selected_groups"] == 3
    assert selected.evidence["max_visible_corridors"] == 3
