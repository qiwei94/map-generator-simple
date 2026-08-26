from shapely.geometry import box

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.building_height_store import (
    BuildingHeightStore,
)


def test_observations_survive_reopen_and_are_spatially_queryable(tmp_path):
    path = tmp_path / "heights.sqlite3"
    store = BuildingHeightStore(str(path))
    accepted = store.put_observations(
        [{
            "source_feature_id": "building-1",
            "height_m": 42.0,
            "num_floors": 12,
            "name": "Test Tower",
            "geometry": box(120.0, 30.0, 120.001, 30.001),
        }],
        source="overture",
        source_release="2026-08-19.0",
    )
    assert accepted == 1

    reopened = BuildingHeightStore(str(path))
    found = reopened.query_bbox(
        "overture", (29.99, 119.99, 30.01, 120.01))
    assert len(found) == 1
    assert found.iloc[0]["source_feature_id"] == "building-1"
    assert found.iloc[0]["height"] == 42.0


def test_observation_upsert_does_not_duplicate_same_release(tmp_path):
    store = BuildingHeightStore(str(tmp_path / "heights.sqlite3"))
    common = {
        "source_feature_id": "same-id",
        "geometry": box(0, 0, 0.001, 0.001),
    }
    store.put_observations(
        [{**common, "height_m": 20}], source="overture", source_release="r1")
    store.put_observations(
        [{**common, "height_m": 25}], source="overture", source_release="r1")

    found = store.query_bbox("overture", (-1, -1, 1, 1))
    assert len(found) == 1
    assert found.iloc[0]["height"] == 25


def test_coverage_requires_containment_not_merely_intersection(tmp_path):
    store = BuildingHeightStore(str(tmp_path / "heights.sqlite3"))
    store.register_coverage(
        "overture", (30.0, 120.0, 30.2, 120.2), observation_count=10)

    assert store.covers_bbox("overture", (30.05, 120.05, 30.1, 120.1))
    assert not store.covers_bbox("overture", (30.1, 120.1, 30.3, 120.3))


def test_missing_landmark_is_persisted_as_negative_cache(tmp_path):
    path = tmp_path / "heights.sqlite3"
    BuildingHeightStore(str(path)).put_landmark(
        "Q123", status="missing", raw={"claims": {}})

    rows = BuildingHeightStore(str(path)).get_landmarks(["q123", "Q999"])
    assert rows["Q123"]["status"] == "missing"
    assert "Q999" not in rows

