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

    assert heights is None
    assert names is None
