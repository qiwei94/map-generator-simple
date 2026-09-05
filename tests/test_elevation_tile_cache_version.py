import numpy as np

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers import elevation
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d import config as terrain_config
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.elevation import (
    ELEV_TILE_CACHE_VERSION,
    ELEV_TILE_RES,
    _elev_tile_path,
)


def test_tile_cache_key_records_source_policy_version():
    path = _elev_tile_path(2400, 603)
    assert ELEV_TILE_CACHE_VERSION in path
    assert "elevtile_2400_603_61.npy" not in path


def test_all_zero_tile_cache_is_bypassed_and_replaced(tmp_path, monkeypatch):
    cache_path = tmp_path / "poison_tile.npy"
    np.save(cache_path, np.zeros((ELEV_TILE_RES, ELEV_TILE_RES)))
    replacement = np.add.outer(
        np.arange(ELEV_TILE_RES, dtype=np.float64),
        np.arange(ELEV_TILE_RES, dtype=np.float64),
    )
    calls = []
    monkeypatch.setattr(
        elevation, "_elev_tile_path", lambda *_args: str(cache_path))

    def compute(*_args):
        calls.append(True)
        return replacement.copy()

    monkeypatch.setattr(elevation, "_compute_tile_elevation", compute)

    result = elevation.fetch_elevation_grid_tiled(
        0.0, 0.0, 0.05, 0.05, resolution=ELEV_TILE_RES,
        step=0.05, use_cache=True)

    assert calls == [True]
    assert np.ptp(result) > 0
    assert np.ptp(np.load(cache_path)) > 0


def test_tiled_fetch_honors_requested_resolution_at_source(
        tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(terrain_config, "ELEVATION_SMOOTHING_SIGMA", 0.0)
    monkeypatch.setattr(
        elevation, "_preferred_tile_source_identity",
        lambda *_args: "fixture-source",
    )
    monkeypatch.setattr(
        elevation, "_elev_tile_path",
        lambda _ix, _iy, resolution, *_args: str(
            tmp_path / f"tile-r{resolution}.npy"),
    )

    def cop30(_s, _w, _n, _e, rows, cols):
        calls.append((rows, cols))
        return np.add.outer(
            np.arange(rows, dtype=np.float64),
            np.arange(cols, dtype=np.float64),
        ) + 1.0

    monkeypatch.setattr(elevation, "_fetch_elevation_grid_from_cop30", cop30)
    monkeypatch.setattr(
        elevation, "_fetch_elevation_grid_from_srtm",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("valid Copernicus data must avoid SRTM")),
    )

    low = elevation.fetch_elevation_grid_tiled(
        0.0, 0.0, 0.05, 0.05,
        resolution=31, step=0.05, use_cache=False)
    high = elevation.fetch_elevation_grid_tiled(
        0.0, 0.0, 0.05, 0.05,
        resolution=121, step=0.05, use_cache=False)

    assert calls == [(31, 31), (121, 121)]
    assert low.shape == (31, 31)
    assert high.shape == (121, 121)


def test_raw_tile_cache_identity_tracks_resolution_source_and_sampling(
        tmp_path, monkeypatch):
    monkeypatch.setattr(
        elevation, "select_cache_path", lambda *_args: str(tmp_path))

    base = elevation._elev_tile_path(
        2400, 603, 181, "srtm1-aaa", 0.05)
    assert base == elevation._elev_tile_path(
        2400, 603, 181, "srtm1-aaa", 0.05)
    assert base != elevation._elev_tile_path(
        2400, 603, 121, "srtm1-aaa", 0.05)
    assert base != elevation._elev_tile_path(
        2400, 603, 181, "cop30-bbb", 0.05)
    assert base != elevation._elev_tile_path(
        2400, 603, 181, "srtm1-aaa", 0.025)


def test_tiled_smoothing_runs_once_after_stitch(tmp_path, monkeypatch):
    source_calls = []
    smoothing_calls = []
    monkeypatch.setattr(terrain_config, "ELEVATION_SMOOTHING_SIGMA", 1.0)
    monkeypatch.setattr(
        elevation, "_preferred_tile_source_identity",
        lambda *_args: "fixture-source",
    )
    monkeypatch.setattr(
        elevation, "_elev_tile_path",
        lambda ix, iy, resolution, *_args: str(
            tmp_path / f"tile-{ix}-{iy}-r{resolution}.npy"),
    )

    def cop30(south, west, _north, _east, rows, cols):
        source_calls.append((rows, cols))
        return (
            np.add.outer(
                np.arange(rows, dtype=np.float64),
                np.arange(cols, dtype=np.float64),
            ) + 1.0 + south * 100.0 + west * 100.0
        )

    def smoothing_spy(grid, *, sigma, mode):
        smoothing_calls.append((grid.shape, float(sigma), mode))
        return grid

    monkeypatch.setattr(elevation, "_fetch_elevation_grid_from_cop30", cop30)
    monkeypatch.setattr(elevation, "gaussian_filter", smoothing_spy)

    result = elevation.fetch_elevation_grid_tiled(
        0.0, 0.0, 0.1, 0.1,
        resolution=9, step=0.05, use_cache=False)

    assert source_calls == [(5, 5)] * 4
    assert result.shape == (9, 9)
    assert len(smoothing_calls) == 1
    assert smoothing_calls[0][0] == (9, 9)
    assert smoothing_calls[0][2] == "nearest"


def test_physical_smoothing_cap_prevents_coarse_grid_overblur():
    sigma_cells, cell_m, sigma_m = elevation._resolved_smoothing_sigma(
        2.5,
        south=30.0, west=120.0, north=30.05, east=120.05,
        shape=(61, 61),
    )

    assert cell_m > 80.0
    assert sigma_cells < 1.0
    assert sigma_m <= elevation.ELEVATION_SMOOTHING_MAX_METERS + 1e-9
