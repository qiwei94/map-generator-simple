"""Coastal ocean polygons must share the normal water layer safely."""

from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Polygon

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.coastline import (
    coastline_to_sea_polygon,
    materialize_coastal_water,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osmium_cli_fetcher import (
    OsmiumCLIFetcher,
)


BBOX = (0.0, 0.0, 10.0, 10.0)


def test_coastline_right_side_becomes_sea():
    # Northbound line: land west/left, sea east/right.
    sea = coastline_to_sea_polygon(
        [LineString([(5, 0), (5, 10)])], BBOX)

    assert sea is not None
    assert sea.area == pytest.approx(50.0)
    assert sea.contains(Polygon([(8, 2), (9, 2), (9, 3), (8, 3)]).centroid)
    assert not sea.contains(
        Polygon([(1, 2), (2, 2), (2, 3), (1, 3)]).centroid)


def test_reversed_coastline_reverses_sea_side():
    sea = coastline_to_sea_polygon(
        [LineString([(5, 10), (5, 0)])], BBOX)

    assert sea is not None
    assert sea.area == pytest.approx(50.0)
    assert sea.contains(
        Polygon([(1, 2), (2, 2), (2, 3), (1, 3)]).centroid)


def test_closed_island_keeps_land_hole():
    # Counter-clockwise coastline keeps island land on the left and surrounding
    # sea on the right.
    island = LineString([(3, 3), (7, 3), (7, 7), (3, 7), (3, 3)])
    sea = coastline_to_sea_polygon([island], BBOX)

    assert sea is not None
    assert sea.area == pytest.approx(84.0)
    assert not sea.contains(Polygon([(4, 4), (6, 4), (6, 6), (4, 6)]).centroid)


def test_incomplete_inland_coastline_does_not_flood_frame():
    sea = coastline_to_sea_polygon(
        [LineString([(3, 3), (7, 7)])], BBOX)

    assert sea is None


def test_materialization_preserves_inland_water_and_removes_raw_coastline():
    lake = Polygon([(1, 1), (2, 1), (2, 2), (1, 2)])
    water = gpd.GeoDataFrame(
        {
            "natural": ["water", "coastline"],
            "water": ["lake", None],
            "geometry": [lake, LineString([(5, 0), (5, 10)])],
        },
        geometry="geometry",
        crs="EPSG:4326",
    )

    result = materialize_coastal_water(water, BBOX)

    assert len(result) == 2
    assert set(result["water"]) == {"lake", "sea"}
    assert not (result["natural"] == "coastline").any()
    assert set(result.geometry.geom_type) <= {"Polygon", "MultiPolygon"}


def test_water_filter_and_cache_namespace_include_coastline():
    assert "natural=water,coastline" in OsmiumCLIFetcher.TAG_FILTERS["water"]
    assert OsmiumCLIFetcher._CACHE_NAMESPACES["water"] == "water_coastline_v2"


def test_gallery_combined_cache_is_invalidated_for_coastline_data():
    source = (Path(__file__).resolve().parents[1] / "aesthetic" /
              "rerun_harness.py").read_text(encoding="utf-8")

    assert '"water_schema": "coastline_v2"' in source
