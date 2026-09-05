import numpy as np
import pytest

from generate_city_legacy import _resolve_dem_failure, parse_args
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers import elevation
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.elevation import (
    ElevationDataError,
    _fill_nodata,
    require_usable_elevation_grid,
)


BASE_ARGS = [
    "--bbox", "30.1,120.0,30.2,120.1",
    "--pbf", "fixture.osm.pbf",
    "--city", "fixture-city",
]


def test_dem_failure_is_fail_closed_by_default():
    with pytest.raises(RuntimeError, match="implicit flat terrain"):
        _resolve_dem_failure(
            OSError("offline"), 32, allow_flat_fallback=False)


def test_flat_dem_fallback_requires_explicit_cli_flag_and_is_evidenced():
    assert parse_args(BASE_ARGS).allow_flat_dem_fallback is False
    assert parse_args([
        *BASE_ARGS, "--allow-flat-dem-fallback",
    ]).allow_flat_dem_fallback is True

    grid, evidence = _resolve_dem_failure(
        OSError("offline"), 16, allow_flat_fallback=True)
    assert grid.shape == (16, 16)
    assert grid.dtype == np.float64
    assert not np.any(grid)
    assert evidence == {
        "status": "flat_fallback",
        "source": "explicit_diagnostic_fallback",
        "flat_fallback": True,
        "shape": [16, 16],
        "range_m": [0.0, 0.0],
        "failure_type": "OSError",
    }


def test_all_non_finite_dem_is_never_silently_converted_to_zero():
    with pytest.raises(ElevationDataError, match="all elevation values"):
        _fill_nodata(np.full((4, 4), np.nan))
    with pytest.raises(ElevationDataError, match="all elevation values"):
        _fill_nodata(np.full((4, 4), np.inf))


def test_dem_validation_rejects_poison_flat_grid_but_allows_real_coast_signal():
    with pytest.raises(ElevationDataError, match="all-zero DEM"):
        require_usable_elevation_grid(
            np.zeros((3, 3)), source="poison cache")

    coast = np.array([
        [0.0, 0.0, 1.5],
        [-2.0, 0.0, 3.0],
        [0.0, 4.0, 8.0],
    ])
    assert require_usable_elevation_grid(
        coast, source="valid coast") is coast


def test_poisoned_grid_cache_is_ignored_and_replaced(tmp_path, monkeypatch):
    cache_path = tmp_path / "poison.npy"
    np.save(cache_path, np.zeros((4, 4), dtype=np.float64))
    replacement = np.arange(16, dtype=np.float64).reshape(4, 4)

    monkeypatch.setattr(
        elevation, "_grid_cache_path", lambda *_args: str(cache_path))
    monkeypatch.setattr(
        elevation.cache_mgr, "is_valid", lambda *_args: True)
    monkeypatch.setattr(
        elevation.cache_mgr, "format_age", lambda *_args: "fresh")
    monkeypatch.setattr(
        elevation, "_fetch_elevation_grid_from_cop30",
        lambda *_args: replacement.copy(),
    )
    monkeypatch.setattr(
        elevation, "_fetch_elevation_grid_from_srtm",
        lambda *_args: pytest.fail("valid replacement must avoid SRTM"),
    )

    result = elevation.fetch_elevation_grid(
        30.0, 120.0, 30.1, 120.1, resolution=4)

    assert np.ptp(result) > 0
    assert np.ptp(np.load(cache_path)) > 0
