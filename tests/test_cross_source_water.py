import geopandas as gpd
from shapely.geometry import LineString, box

from aesthetic.cross_source_water import compare_water_sources


FRAME = (0.0, 0.0, 10_000.0, 10_000.0)


def _osm(geometries):
    return gpd.GeoDataFrame(
        {"geometry": geometries}, geometry="geometry", crs="EPSG:3857")


def test_reference_only_compact_water_is_reported_by_cell():
    osm = _osm([box(1000, 1000, 3000, 3000)])
    reference = [
        box(1000, 1000, 3000, 3000),
        box(6500, 6500, 7200, 7200),
    ]

    result = compare_water_sources(
        osm, reference, FRAME, grid_size=5, inset_ratio=0.0)

    assert result["status"] == "evidence_only"
    assert result["compact_gap_count"] == 1
    assert result["compact_gap_area_m2"] > 400_000
    assert result["reference_covered_by_osm_ratio"] > 0.85
    assert any(cell["row"] == 3 and cell["column"] == 3
               for cell in result["candidate_cells"])


def test_osm_line_buffer_matches_reference_river_strip():
    osm = _osm([LineString([(1000, 5000), (9000, 5000)])])
    reference = [box(1000, 4975, 9000, 5025)]

    result = compare_water_sources(
        osm, reference, FRAME, inset_ratio=0.0,
        line_buffer_m=30.0, alignment_buffer_m=5.0)

    assert result["reference_covered_by_osm_ratio"] > 0.98
    assert result["compact_gap_count"] == 0
    assert result["linear_or_noise_gap_count"] == 0


def test_missing_secondary_source_is_explicitly_unavailable():
    result = compare_water_sources(_osm([]), [], FRAME)

    assert result["status"] == "unavailable"
    assert result["candidate_cells"] == []


def test_reference_water_with_empty_osm_remains_review_evidence():
    result = compare_water_sources(
        _osm([]), [box(3000, 3000, 5000, 5000)], FRAME,
        inset_ratio=0.0)

    assert result["status"] == "evidence_only"
    assert result["reference_covered_by_osm_ratio"] == 0.0
    assert result["compact_gap_count"] == 1
