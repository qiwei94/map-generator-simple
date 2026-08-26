import pickle

import geopandas as gpd
from shapely.geometry import box

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.building_height_store import (
    BuildingHeightStore,
)
from tools.audit_city_height_usage import audit_height_usage


def test_audit_reports_cached_wikidata_adoption_and_model_z(tmp_path):
    buildings = gpd.GeoDataFrame({
        "height": [None, "30 m", None],
        "building:levels": [None, None, "5"],
        "wikidata": ["Q1", "Q2", None],
        "name": [None, "Tagged Tower", "Levels House"],
        "geometry": [box(i, 0, i + 0.5, 0.5) for i in range(3)],
    }, geometry="geometry", crs="EPSG:4326")
    cache = tmp_path / "gdfs.pkl"
    with cache.open("wb") as stream:
        pickle.dump({"buildings": buildings}, stream)

    store_path = tmp_path / "height-cache" / "building_heights.sqlite3"
    store = BuildingHeightStore(str(store_path))
    store.put_landmark(
        "Q1", status="ok", height_m=120, label="Cached Landmark")
    store.put_landmark("Q2", status="missing")

    report = audit_height_usage(cache, store_path, city="fixture")
    assert report["building_count"] == 3
    assert report["height_source_counts"] == {
        "osm_height": 1, "osm_levels": 1, "wikidata": 1}
    assert report["wikidata"]["cached_height_rows"] == 1
    assert report["matched_landmarks"][0]["qid"] == "Q1"
    assert report["matched_landmarks"][0]["source_height_m"] == 120
    assert report["matched_landmarks"][0]["compressed_base_height_mm"] > 0
