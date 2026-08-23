"""City identity comes from characteristic geography and road structure."""

import geopandas as gpd
from types import SimpleNamespace
from shapely.geometry import LineString

from aesthetic.city_signature import (
    analyze_city_signature,
    analyze_road_topology,
    compare_visible_road_signature,
)


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


def test_visible_network_must_preserve_dominant_source_trait():
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
    unrelated = LineString([(1000, 1000), (24_000, 1000)])

    preserved = compare_visible_road_signature(
        _roads([ring, unrelated]), _roads([ring]), FRAME)
    lost = compare_visible_road_signature(
        _roads([ring, unrelated]), _roads([unrelated]), FRAME)

    assert preserved["dominant_trait"] == "ring"
    assert preserved["passed"] is True
    assert lost["passed"] is False
    assert "lost" in lost["reason"]


def test_ambiguous_source_signature_is_non_blocking():
    road = LineString([(1000, 1000), (24_000, 1000)])

    result = compare_visible_road_signature(_roads([road]), None, FRAME)

    assert result["source_is_distinctive"] is False
    assert result["passed"] is True


def _profile(water_ratio):
    return SimpleNamespace(
        building_density=0.0,
        road_density_km_per_km2=0.0,
        water_ratio=water_ratio,
        elevation_range_m=0.0,
        osm_quality="good",
    )


def test_tiny_water_noise_does_not_become_city_signature():
    result = analyze_city_signature(
        None, None, FRAME, _profile(0.001),
        {"narrative_score": 1.0}, "landscape")

    assert result["geography_score"] == 0.0
    assert "water" not in result["traits"]


def test_frame_scale_water_remains_city_signature():
    result = analyze_city_signature(
        None, None, FRAME, _profile(0.03),
        {"narrative_score": 0.8}, "water_landscape")

    assert result["geography_score"] == 0.8
    assert "water" in result["traits"]
