from pathlib import Path

import pytest

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers import height_enrichment


def _make_executable(path: Path) -> None:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)


def test_overture_cli_resolves_next_to_active_python(monkeypatch, tmp_path):
    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    cli = python.with_name("overturemaps")
    _make_executable(cli)

    monkeypatch.delenv("OVERTUREMAPS_BIN", raising=False)
    monkeypatch.setattr(height_enrichment.sys, "executable", str(python))
    monkeypatch.setattr(height_enrichment.shutil, "which", lambda _: None)

    assert height_enrichment._resolve_overture_cli() == str(cli.resolve())


def test_overture_cli_override_takes_priority(monkeypatch, tmp_path):
    cli = tmp_path / "custom-overturemaps"
    _make_executable(cli)
    monkeypatch.setenv("OVERTUREMAPS_BIN", str(cli))

    assert height_enrichment._resolve_overture_cli() == str(cli.resolve())


def test_download_skips_cleanly_when_cli_is_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(height_enrichment, "_resolve_overture_cli", lambda: None)

    result = height_enrichment._download_overture(
        (41.88, -87.63, 41.89, -87.62),
        cache_dir=str(tmp_path),
    )

    assert result is None
    assert list(tmp_path.iterdir()) == []


def test_cache_never_reuses_a_different_city(monkeypatch, tmp_path):
    (tmp_path / "overture_41.88_-87.63.parquet").touch()
    (tmp_path / "legacy_buildings.parquet").touch()

    result = height_enrichment._find_overture_cache(
        (39.85, 116.30, 39.95, 116.45),
        cache_dir=str(tmp_path),
    )

    assert result is None


def test_truncated_parquet_is_not_accepted_as_cache(tmp_path):
    broken = tmp_path / "overture_31.23_121.47.parquet"
    broken.write_bytes(b"PAR1")

    result = height_enrichment._find_overture_cache(
        (31.1, 121.3, 31.4, 121.7),
        cache_dir=str(tmp_path),
    )

    assert result is None


def test_offline_preview_does_not_attempt_download(monkeypatch, tmp_path):
    import geopandas as gpd
    from shapely.geometry import Point

    osm = gpd.GeoDataFrame(
        {"geometry": [Point(121.47, 31.23)]},
        geometry="geometry", crs="EPSG:4326",
    )
    monkeypatch.setenv("OVERTURE_AUTO_DOWNLOAD", "0")
    monkeypatch.setattr(
        height_enrichment,
        "_download_overture",
        lambda *_args, **_kwargs: pytest.fail("download must be skipped"),
    )

    heights, names = height_enrichment.load_overture_heights(
        osm,
        bbox_wgs84=(31.1, 121.3, 31.4, 121.7),
        cache_dir=str(tmp_path),
    )

    assert heights is None
    assert names is None


def test_overture_download_is_opt_in_by_default(monkeypatch, tmp_path):
    import geopandas as gpd
    from shapely.geometry import Point

    osm = gpd.GeoDataFrame(
        {"geometry": [Point(120.15, 30.25)]},
        geometry="geometry", crs="EPSG:4326",
    )
    monkeypatch.delenv("OVERTURE_AUTO_DOWNLOAD", raising=False)
    monkeypatch.setattr(
        height_enrichment,
        "_download_overture",
        lambda *_args, **_kwargs: pytest.fail(
            "default generation must not consume overseas bandwidth"),
    )

    heights, names = height_enrichment.load_overture_heights(
        osm,
        bbox_wgs84=(30.1, 120.0, 30.4, 120.3),
        cache_dir=str(tmp_path),
    )

    assert heights is None
    assert names is None


def test_overture_download_can_be_explicitly_enabled(monkeypatch, tmp_path):
    import geopandas as gpd
    from shapely.geometry import Point

    osm = gpd.GeoDataFrame(
        {"geometry": [Point(120.15, 30.25)]},
        geometry="geometry", crs="EPSG:4326",
    )
    sentinel = tmp_path / "downloaded.parquet"
    monkeypatch.setenv("OVERTURE_AUTO_DOWNLOAD", "1")
    monkeypatch.setattr(
        height_enrichment,
        "_download_overture",
        lambda *_args, **_kwargs: sentinel,
    )
    monkeypatch.setattr(
        gpd,
        "read_parquet",
        lambda *_args, **_kwargs: gpd.GeoDataFrame(
            geometry=[], crs="EPSG:4326"),
    )

    heights, names = height_enrichment.load_overture_heights(
        osm,
        bbox_wgs84=(30.1, 120.0, 30.4, 120.3),
        cache_dir=str(tmp_path),
    )

    # A successful empty provider response is represented as aligned NaNs and
    # persisted as negative coverage, so it is not downloaded again.
    assert heights.isna().all()
    assert names.isna().all()


def test_overlap_match_chooses_best_candidate_not_first_intersection():
    import geopandas as gpd
    from shapely.geometry import box

    osm = gpd.GeoDataFrame(
        {"geometry": [box(0, 0, 1, 1)]},
        index=["osm-a"], geometry="geometry", crs="EPSG:4326",
    )
    candidates = gpd.GeoDataFrame(
        {
            "source_feature_id": ["small-first", "best-second"],
            "height": [120.0, 35.0],
            "name": ["Wrong", "Correct"],
            "geometry": [box(0, 0, 0.4, 1), box(0.02, 0.02, 0.98, 0.98)],
        },
        geometry="geometry", crs="EPSG:4326",
    )

    heights, names = height_enrichment._match_overture_candidates(
        osm, candidates)

    assert heights.loc["osm-a"] == 35.0
    assert names.loc["osm-a"] == "Correct"


def test_persistent_store_reuses_contained_bbox_without_raw_or_network(
        monkeypatch, tmp_path):
    import geopandas as gpd
    from shapely.geometry import box
    from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.building_height_store import (
        BuildingHeightStore,
    )

    store = BuildingHeightStore(str(tmp_path / "building_heights.sqlite3"))
    store.put_observations(
        [{
            "source_feature_id": "stored",
            "height_m": 48,
            "geometry": box(120.0, 30.0, 120.001, 30.001),
        }],
        source="overture",
    )
    store.register_coverage("overture", (29.9, 119.9, 30.1, 120.1))
    osm = gpd.GeoDataFrame(
        {"geometry": [box(120.0, 30.0, 120.001, 30.001)]},
        geometry="geometry", crs="EPSG:4326",
    )
    monkeypatch.setattr(
        height_enrichment, "_find_overture_cache",
        lambda *_args, **_kwargs: pytest.fail("raw cache lookup must be skipped"),
    )
    monkeypatch.setattr(
        height_enrichment, "_download_overture",
        lambda *_args, **_kwargs: pytest.fail("network must be skipped"),
    )

    heights, _ = height_enrichment.load_overture_heights(
        osm, bbox_wgs84=(29.99, 119.99, 30.01, 120.01),
        cache_dir=str(tmp_path), auto_download=True,
    )

    assert heights.iloc[0] == 48


def test_height_priority_demotes_ndsm_below_cached_vector_sources():
    import geopandas as gpd
    import pandas as pd
    from shapely.geometry import box
    from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import (
        _estimate_building_heights,
    )

    buildings = gpd.GeoDataFrame(
        {
            "height": ["30", None, None, None, None, None],
            "building:levels": [None, "5", None, None, None, None],
            "geometry": [box(i, 0, i + 0.5, 0.5) for i in range(6)],
        },
        geometry="geometry", crs="EPSG:4326",
    )
    ndsm = pd.Series([99, 99, 99, 99, 25, float("nan")])
    overture = pd.Series([88, 88, 88, 45, float("nan"), float("nan")])
    wikidata = pd.Series([77, 77, 324, float("nan"), float("nan"), float("nan")])

    heights = _estimate_building_heights(
        buildings, ndsm, overture, wikidata_heights=wikidata)

    assert list(heights) == [30, 17.5, 324, 45, 25, 10]
    assert list(buildings["height_source"]) == [
        "osm_height", "osm_levels", "wikidata", "overture", "ndsm", "default"
    ]


def test_explicit_osm_heights_are_retained_in_store(tmp_path):
    import geopandas as gpd
    from shapely.geometry import box
    from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.building_height_store import (
        BuildingHeightStore,
    )

    buildings = gpd.GeoDataFrame(
        {
            "osm_type": ["way", "way", "way"],
            "osm_id": [1, 2, 3],
            "height": ["42 m", None, None],
            "building:levels": [None, 6, None],
            "geometry": [box(i, 0, i + 0.5, 0.5) for i in range(3)],
        },
        geometry="geometry", crs="EPSG:4326",
    )

    count = height_enrichment.persist_osm_height_tags(
        buildings, (-1, -1, 1, 4), cache_dir=str(tmp_path))

    assert count == 2
    stored = BuildingHeightStore(
        str(tmp_path / "building_heights.sqlite3")
    ).query_bbox("osm", (-1, -1, 1, 4))
    assert sorted(stored["height"].tolist()) == [21.0, 42.0]
