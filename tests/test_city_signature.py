"""City identity comes from characteristic geography and road structure."""

import geopandas as gpd
from shapely.geometry import LineString

from aesthetic.city_signature import analyze_road_topology


FRAME = (0.0, 0.0, 25_000.0, 25_000.0)


def _roads(lines):
    return gpd.GeoDataFrame(
        {"highway": ["primary"] * len(lines), "geometry": lines},
        geometry="geometry", crs="EPSG:32650")


def test_ring_road_is_detected_as_city_signature():
    center = 12_500.0
    ring = LineString([
        (center + 6000, center),
        (center + 4243, center + 4243),
        (center, center + 6000),
        (center - 4243, center + 4243),
        (center - 6000, center),
        (center - 4243, center - 4243),
        (center, center - 6000),
        (center + 4243, center - 4243),
        (center + 6000, center),
    ])

    result = analyze_road_topology(_roads([ring]), FRAME)

    assert result["ring_score"] >= 0.55
    assert "ring" in result["traits"]


def test_radial_avenues_are_detected_as_city_signature():
    center = 12_500.0
    lines = []
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1),
                   (0.707, 0.707), (-0.707, 0.707),
                   (0.707, -0.707), (-0.707, -0.707)):
        lines.append(LineString([
            (center + dx * 800, center + dy * 800),
            (center + dx * 9000, center + dy * 9000),
        ]))

    result = analyze_road_topology(_roads(lines), FRAME)

    assert result["radial_score"] >= 0.55
    assert "radial" in result["traits"]


def test_empty_roads_have_no_topology_signature():
    result = analyze_road_topology(None, FRAME)

    assert result["road_signature_score"] == 0
    assert result["traits"] == []
