"""Composition recommendations use measured river geometry."""
import geopandas as gpd
from shapely.geometry import LineString

from aesthetic.framing import analyze_water_framing


FRAME = (0.0, 0.0, 25_000.0, 25_000.0)


def _water(line):
    return gpd.GeoDataFrame({"waterway": ["river"], "geometry": [line]},
                            geometry="geometry", crs="EPSG:32650")


def test_long_bending_river_recommends_25_km():
    river = LineString([
        (1000, 5000), (6000, 9000), (12_000, 17_000),
        (18_000, 18_000), (24_000, 12_000),
    ])
    advice = analyze_water_framing(_water(river), FRAME, water_ratio=0.08)

    assert advice["recommended_size_km"] == 25
    assert advice["river_bend_score"] >= 0.45
    assert "河流" in advice["reason"]


def test_small_straight_waterway_keeps_15_km_detail():
    stream = LineString([(11_000, 11_000), (13_000, 11_000)])
    advice = analyze_water_framing(_water(stream), FRAME, water_ratio=0.01)

    assert advice["recommended_size_km"] == 15
    assert advice["river_bend_score"] < 0.45


def test_water_area_can_recommend_25_without_centerlines():
    advice = analyze_water_framing(None, FRAME, water_ratio=0.12)

    assert advice["recommended_size_km"] == 25
    assert advice["water_line_count"] == 0
