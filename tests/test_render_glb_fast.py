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
    _TerrainSurfacePlanSampler,
    _drape_lines,
    _surface_plan_preview_grid,
    _terrain_heightfield_from_surface_plan,
    render_glb_preview,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import (
    resolve_terrain_surface_plan,
    sample_terrain_surface_plan_z,
)


def _surface_plan():
    yy, xx = np.mgrid[-1:1:17j, -1:1:19j]
    elevation = 40.0 + 120.0 * np.exp(-2.3 * (xx * xx + yy * yy))
    return resolve_terrain_surface_plan(
        elevation,
        width_m=120.0,
        height_m=80.0,
        scale_mm_per_m=0.2,
        max_surface_edge_mm=4.0,
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


def test_plan_sampler_matches_canonical_height_sampling():
    plan = _surface_plan()
    bbox_local = (-60.0, -40.0, 60.0, 40.0)
    sampler = _TerrainSurfacePlanSampler(
        plan, bbox_local, plan.scale_mm_per_m)
    xs_m = np.array([-58.0, -17.5, 0.0, 23.0, 59.0])
    ys_m = np.array([-39.0, 14.0, 0.0, -8.5, 38.0])

    actual = sampler.z_mm_vec(xs_m, ys_m)
    expected = sample_terrain_surface_plan_z(
        plan,
        xs_m * plan.scale_mm_per_m,
        ys_m * plan.scale_mm_per_m,
    )

    assert sampler.surface_plan_fingerprint == plan.fingerprint
    np.testing.assert_array_equal(actual, expected)


def test_preview_heightfield_is_deterministic_exact_plan_subset():
    plan = _surface_plan()
    first = _surface_plan_preview_grid(plan, grid_n=7)
    second = _surface_plan_preview_grid(plan, grid_n=7)
    xs, ys, preview_z, evidence = first

    np.testing.assert_array_equal(xs, second[0])
    np.testing.assert_array_equal(ys, second[1])
    np.testing.assert_array_equal(preview_z, second[2])
    assert evidence == second[3]
    assert evidence["fingerprint"] == plan.fingerprint
    source_subset = plan.surface_z_grid_mm[np.ix_(
        evidence["row_indices"], evidence["column_indices"])]
    np.testing.assert_array_equal(preview_z, source_subset)

    mesh = _terrain_heightfield_from_surface_plan(
        plan,
        (-60.0, -40.0, 60.0, 40.0),
        plan.scale_mm_per_m,
        grid_n=7,
    )
    top_count = len(xs) * len(ys)
    np.testing.assert_array_equal(
        mesh.vertices[:top_count, 2], preview_z.ravel())
    assert mesh.metadata["terrain_surface_plan"]["fingerprint"] == (
        plan.fingerprint)
    assert mesh.metadata["terrain_surface_plan"]["sampling"] == (
        "deterministic_index_subset")


def test_canonical_preview_never_enters_legacy_dem_z_path(
        tmp_path, monkeypatch):
    plan = _surface_plan()
    layers = SimpleNamespace(
        block_base=[], VL=[], VO=[], WL=[], WO=[], roads_lines=[], BO=[],
        BL=[], road_roles={},
    )

    class ForbiddenLegacySampler:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("canonical preview must not recompute DEM Z")

    monkeypatch.setattr(
        render_glb_module, "_TerrainSampler", ForbiddenLegacySampler)
    output = tmp_path / "surface-plan.glb"
    render_glb_preview(
        layers,
        {"bbox_local": (-60.0, -40.0, 60.0, 40.0), "scale": 0.2},
        str(output),
        terrain_surface_plan=plan,
        preview_quality="fast",
    )

    assert output.exists()


@pytest.mark.parametrize(("enabled", "expected_calls"), [(False, 0), (True, 1)])
def test_preview_vegetation_surface_is_opt_in(
        tmp_path, monkeypatch, enabled, expected_calls):
    plan = _surface_plan()
    layers = SimpleNamespace(
        block_base=[], VL=[box(-20, -20, 20, 20)], VO=[], WL=[], WO=[],
        roads_lines=[], BO=[], BL=[], road_roles={},
    )
    original = render_glb_module._extrude_polys
    calls = []

    def record(polys, *args, **kwargs):
        if polys:
            calls.append(polys)
        return original(polys, *args, **kwargs)

    monkeypatch.setattr(render_glb_module, "_extrude_polys", record)
    render_glb_preview(
        layers,
        {"bbox_local": (-60.0, -40.0, 60.0, 40.0), "scale": 0.2},
        str(tmp_path / f"vegetation-{enabled}.glb"),
        terrain_surface_plan=plan,
        vegetation_enabled=enabled,
        preview_quality="fast",
    )

    assert len(calls) == expected_calls


def test_surface_plan_rejects_ambiguous_legacy_terrain_inputs(tmp_path):
    plan = _surface_plan()
    layers = SimpleNamespace(
        block_base=[], VL=[], VO=[], WL=[], WO=[], roads_lines=[], BO=[],
        BL=[], road_roles={},
    )
    with pytest.raises(ValueError, match="cannot be combined"):
        render_glb_preview(
            layers,
            {"bbox_local": (-60.0, -40.0, 60.0, 40.0), "scale": 0.2},
            str(tmp_path / "ambiguous.glb"),
            elevation_grid=plan.regular_grid_m,
            terrain_surface_plan=plan,
            preview_quality="fast",
        )


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
