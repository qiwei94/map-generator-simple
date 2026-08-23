from pathlib import Path

import geopandas as gpd
from shapely.geometry import box

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.large_water_relations import (
    CACHE_SCHEMA,
    _cache_paths,
    _select_large_relations,
    merge_large_water_relations,
    native_osmium_binary,
)


def _gdf(rows):
    return gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")


def test_native_osmium_rejects_python_fallback(tmp_path):
    native = tmp_path / "osmium"
    native.write_text("#!/bin/sh\n")
    native.chmod(0o755)
    assert native_osmium_binary([str(native)]) == str(native.resolve())
    assert native_osmium_binary(["python3", "tools/osmium_pyosmium.py"]) is None
    assert native_osmium_binary([str(tmp_path / "missing-osmium")]) is None


def test_large_relation_selection_keeps_only_large_tagged_relations():
    large = box(-88.0, 41.0, -87.8, 41.2)
    small = box(-87.7, 41.0, -87.699, 41.001)
    selected = _select_large_relations(_gdf([
        {"@type": "relation", "@id": 1205149, "natural": "water",
         "water": "lake", "name": "Lake", "geometry": large},
        {"@type": "way", "@id": 1, "natural": "water",
         "water": "lake", "geometry": large},
        {"@type": "relation", "@id": 2, "natural": "water",
         "water": "pond", "geometry": small},
    ]))
    assert selected["osm_id"].tolist() == [1205149]
    assert selected["source"].tolist() == ["osm_relation_full"]


def test_merge_adds_only_relation_area_missing_from_regular_water():
    regular = _gdf([
        {"natural": "water", "geometry": box(0.0, 0.0, 0.05, 0.1)},
    ])
    relation = _gdf([
        {"natural": "water", "water": "lake", "osm_id": 9,
         "geometry": box(0.0, 0.0, 0.1, 0.1)},
    ])
    merged = merge_large_water_relations(regular, relation)
    assert len(merged) == 2
    added = merged.iloc[-1].geometry
    assert added.intersection(regular.iloc[0].geometry).area == 0
    assert round(added.area, 6) == 0.005


def test_cache_key_changes_with_pbf_metadata(tmp_path):
    pbf = tmp_path / "region.osm.pbf"
    pbf.write_bytes(b"first")
    first = _cache_paths(str(pbf), str(tmp_path / "cache"))[0]
    pbf.write_bytes(b"second-version")
    second = _cache_paths(str(pbf), str(tmp_path / "cache"))[0]
    assert first != second
    assert CACHE_SCHEMA in first.name
