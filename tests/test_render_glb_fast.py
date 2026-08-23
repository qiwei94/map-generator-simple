"""Fast browser-preview geometry deliberately trades print topology for speed."""
from __future__ import annotations

import numpy as np
import pytest
import geopandas as gpd
from shapely.geometry import LineString, box
from types import SimpleNamespace

import _TEXTURE_STYLE_OF_DEEPSEEK.render_glb as render_glb_module
from _TEXTURE_STYLE_OF_DEEPSEEK.render_glb import (
    _TerrainSampler,
    _drape_lines,
    render_glb_preview,
)


def test_fast_road_ribbon_follows_terrain_and_is_open_shell():
    grid = np.tile(np.linspace(0, 100, 32), (32, 1))
    sampler = _TerrainSampler(
        grid, (0, 0, 1000, 1000), scale=0.1,
        z_gamma=1.0, relief_mm_max=10.0,
    )

    mesh = _drape_lines(
        [(LineString([(0, 500), (1000, 500)]), 20.0)],
        sampler, scale=0.1, color=(74, 74, 74, 255),
        offset_mm=0.6, cell_m=100.0,
    )

    assert mesh is not None
    expected = sampler.z_mm_vec(
        mesh.vertices[:, 0] / 0.1, mesh.vertices[:, 1] / 0.1) + 0.6
    assert np.max(np.abs(mesh.vertices[:, 2] - expected)) < 1e-6
    assert not mesh.is_watertight
    assert len(mesh.faces) <= 24


def test_preview_quality_rejects_unknown_mode(tmp_path):
    with pytest.raises(ValueError, match="preview_quality"):
        render_glb_preview(
            object(), {"bbox_local": (0, 0, 10, 10), "scale": 1},
            str(tmp_path / "invalid.glb"), preview_quality="print",
        )


def test_preview_does_not_reintroduce_raw_water_after_preprocess(
        tmp_path, monkeypatch):
    layers = SimpleNamespace(
        block_base=[], VL=[], VO=[], WL=[box(100, 100, 900, 900)], WO=[],
        roads_lines=[], BO=[], BL=[], road_roles={},
    )
    raw_water = gpd.GeoDataFrame({
        "waterway": ["river"],
        "geometry": [LineString([(0, 500), (1000, 500)])],
    }, geometry="geometry", crs="EPSG:3857")

    def fail_raw_water(*_args, **_kwargs):
        raise AssertionError("raw water fallback must not run")

    monkeypatch.setattr(render_glb_module, "_river_polys_from_gdf",
                        fail_raw_water)
    monkeypatch.setattr(render_glb_module, "_amap_water_polys",
                        fail_raw_water)

    output = tmp_path / "selected-water.glb"
    render_glb_preview(
        layers, {"bbox_local": (0, 0, 1000, 1000), "scale": 0.1},
        str(output), water_gdf=raw_water, preview_quality="fast",
    )

    assert output.exists()
