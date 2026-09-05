import geopandas as gpd
from shapely.geometry import LineString

import _TEXTURE_STYLE_OF_DEEPSEEK.buildings as buildings_module


def _roads(lines):
    return gpd.GeoDataFrame(
        {
            "highway": ["primary"] * len(lines),
            "geometry": lines,
        },
        geometry="geometry",
    )


def test_pre_noded_city_blocks_do_not_require_global_union(monkeypatch):
    roads = _roads([
        LineString([(5, 0), (5, 5)]),
        LineString([(5, 5), (5, 10)]),
        LineString([(0, 5), (5, 5)]),
        LineString([(5, 5), (10, 5)]),
    ])

    def fail_global_union(_lines):
        raise AssertionError("pre-noded graph should avoid global union")

    monkeypatch.setattr(buildings_module, "unary_union", fail_global_union)
    blocks = buildings_module._build_city_blocks(
        roads, None, road_tier=4, bbox_local=(0, 0, 10, 10))

    assert len(blocks) == 4
    assert sum(block.area for block in blocks) == 100


def test_incomplete_pre_noding_falls_back_to_global_union(monkeypatch):
    roads = _roads([
        LineString([(5, 0), (5, 10)]),
        LineString([(0, 5), (10, 5)]),
    ])
    real_union = buildings_module.unary_union
    calls = []

    def recording_union(lines):
        calls.append(len(lines))
        return real_union(lines)

    monkeypatch.setattr(buildings_module, "unary_union", recording_union)
    blocks = buildings_module._build_city_blocks(
        roads, None, road_tier=4, bbox_local=(0, 0, 10, 10))

    assert calls
    assert len(blocks) == 4
    assert sum(block.area for block in blocks) == 100


def test_nested_faces_are_partitioned_without_global_line_noding(monkeypatch):
    roads = _roads([
        LineString([(1, 1), (3, 1), (3, 3), (1, 3), (1, 1)]),
        LineString([(7, 7), (9, 7), (9, 9), (7, 9), (7, 7)]),
    ])
    real_union = buildings_module.unary_union
    union_inputs = []

    def recording_union(geometries):
        values = list(geometries)
        union_inputs.append(values)
        return real_union(values)

    monkeypatch.setattr(buildings_module, "unary_union", recording_union)
    blocks = buildings_module._build_city_blocks(
        roads, None, road_tier=4, bbox_local=(0, 0, 10, 10))

    # GEOS can directly return the bbox remainder with holes plus the two
    # inner faces here, so neither nested-face repair nor global line noding
    # should be necessary.
    assert union_inputs == []
    assert len(blocks) == 3
    assert sum(block.area for block in blocks) == 100
