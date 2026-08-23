import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString, box

from _TEXTURE_STYLE_OF_DEEPSEEK.water_roles import (
    WaterLineCandidate,
    has_printable_water_mass,
    is_exposed_water_line,
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
            "river", "name:identity river", 30, True),
        WaterLineCandidate(
            LineString([(0, 2000), (10000, 2000)]),
            "canal", "name:identity canal", 30, True),
        WaterLineCandidate(
            LineString([(0, 1000), (10000, 1000)]),
            "stream", "name:minor stream", 30, True),
        WaterLineCandidate(
            LineString([(0, 700), (10000, 700)]),
            "drain", "name:storm drain", 30, True),
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
            "river", f"name:river {index}", 25, True)
        for index in range(5)
    ]

    selected = select_visible_water_lines(
        candidates,
        bbox_local=(0, 0, 10000, 10000),
        nozzle_real_m=50.0,
    )

    assert selected.evidence["selected_groups"] == 3
    assert selected.evidence["max_visible_corridors"] == 3


def test_surface_water_suppresses_widthless_centrelines_at_city_scale():
    candidates = [
        WaterLineCandidate(
            LineString([(0, 5000), (10000, 5000)]),
            "river", "name:already expressed river", 30),
    ]

    selected = select_visible_water_lines(
        candidates,
        bbox_local=(0, 0, 10000, 10000),
        nozzle_real_m=50.0,
        visible_surface_ratio=0.01,
    )

    assert selected.lines == []
    assert selected.evidence["selected_groups"] == 0


def test_widthless_centreline_is_a_single_fallback_when_surface_is_missing():
    candidates = [
        WaterLineCandidate(
            LineString([(0, 5000), (10000, 5000)]),
            "river", "name:first", 30),
        WaterLineCandidate(
            LineString([(0, 3000), (10000, 3000)]),
            "river", "name:second", 30),
    ]

    selected = select_visible_water_lines(
        candidates,
        bbox_local=(0, 0, 10000, 10000),
        nozzle_real_m=50.0,
    )

    assert selected.evidence["selected_groups"] == 1
    assert selected.evidence["fallback_without_surface"] is True


def test_covered_or_underground_water_is_not_visible_material():
    assert is_exposed_water_line(pd.Series({"tunnel": "culvert"})) is False
    assert is_exposed_water_line(pd.Series({"covered": "yes"})) is False
    assert is_exposed_water_line(pd.Series({"location": "underground"})) is False
    assert is_exposed_water_line(pd.Series({"tunnel": "no"})) is True


def test_long_sub_nozzle_polygon_is_not_promoted_by_area_alone():
    assert has_printable_water_mass(
        box(0, 0, 10000, 20), nozzle_real_m=50.0) is False
    assert has_printable_water_mass(
        box(0, 0, 10000, 500), nozzle_real_m=50.0) is True
