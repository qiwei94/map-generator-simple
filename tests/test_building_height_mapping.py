import geopandas as gpd
import pytest
from shapely.geometry import box

from _TEXTURE_STYLE_OF_DEEPSEEK.buildings import (
    _compress_height,
    _quantize_height_mm,
    building_height_mapping_context,
)


def _height_gdf(heights, sources):
    return gpd.GeoDataFrame(
        {
            "est_height": heights,
            "height_source": sources,
            "geometry": [box(i, 0, i + 0.5, 0.5)
                         for i in range(len(heights))],
        },
        geometry="geometry",
        crs="EPSG:3857",
    )


def test_city_relative_mapping_preserves_tall_landmark_order():
    buildings = _height_gdf(
        [20, 100, 269, 468, 999],
        ["osm_levels", "osm_height", "wikidata", "wikidata", "ndsm"],
    )
    context = building_height_mapping_context(buildings)

    assert context["verified_height_count"] == 4
    assert context["height_ceiling_m"] == pytest.approx(468)
    assert context["source_counts"]["wikidata"] == 2
    assert _compress_height(269, 5000, height_ceiling_m=468) < _compress_height(
        468, 5000, height_ceiling_m=468)


def test_unverified_remote_estimate_does_not_control_city_ceiling():
    buildings = _height_gdf(
        [40, 900], ["osm_height", "ndsm"])
    context = building_height_mapping_context(buildings)
    assert context["verified_height_count"] == 1
    assert context["height_ceiling_m"] == pytest.approx(150)


def test_model_z_rounds_up_to_complete_printer_layer():
    assert _quantize_height_mm(3.01, 0.12) == pytest.approx(3.12)
    assert _quantize_height_mm(3.00, 0.12) == pytest.approx(3.0)
