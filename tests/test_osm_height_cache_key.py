from _TEXTURE_STYLE_OF_DEEPSEEK import config
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.building_height_store import (
    BuildingHeightStore,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import (
    _building_height_cache_key,
)


def test_building_tile_key_changes_only_with_normalized_height_evidence(
        tmp_path, monkeypatch):
    cache_dir = tmp_path / "height-cache"
    monkeypatch.setattr(config, "OVERTURE_CACHE_DIR", str(cache_dir))

    assert _building_height_cache_key() == "building_height_v3_none"
    store = BuildingHeightStore(str(cache_dir / "building_heights.sqlite3"))
    store.put_landmark("Q1", status="ok", height_m=50, label="Tower")
    first = _building_height_cache_key()

    store.put_landmark(
        "Q1", status="ok", height_m=50, label="Tower", raw={"new": True})
    assert _building_height_cache_key() == first

    store.put_landmark("Q1", status="ok", height_m=70, label="Tower")
    assert _building_height_cache_key() != first
