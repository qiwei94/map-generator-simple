"""Tests for terrain.py — _add_walls_and_bottom, build_deepseek_terrain, sample_deepseek_terrain_z."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pytest
import trimesh
from scipy.ndimage import gaussian_filter

from _TEXTURE_STYLE_OF_DEEPSEEK.config import (
    TERRAIN_THICKNESS_MM,
    Z_TERRAIN_BASE,
)


def _make_open_surface(width=100.0, height=100.0, z=0.0):
    """Create an open quad surface (2 triangles) at given Z."""
    verts = np.array([
        [0, 0, z],
        [width, 0, z],
        [width, height, z],
        [0, height, z],
    ], dtype=np.float64)
    faces = np.array([[0, 1, 2], [0, 2, 3]], dtype=np.int32)
    return trimesh.Trimesh(vertices=verts, faces=faces, process=False)


def _make_hill_surface(width=100.0, height=100.0, peak_z=10.0, n=5):
    """Create a grid surface with a central peak."""
    xs = np.linspace(0, width, n)
    ys = np.linspace(0, height, n)
    verts = []
    for y in ys:
        for x in xs:
            cx, cy = width / 2, height / 2
            dist = np.sqrt((x - cx) ** 2 + (y - cy) ** 2)
            max_dist = np.sqrt(cx ** 2 + cy ** 2)
            z = peak_z * max(0, 1 - dist / max_dist)
            verts.append([x, y, z])
    verts = np.array(verts, dtype=np.float64)

    faces = []
    for j in range(n - 1):
        for i in range(n - 1):
            idx = j * n + i
            faces.append([idx, idx + 1, idx + n + 1])
            faces.append([idx, idx + n + 1, idx + n])
    faces = np.array(faces, dtype=np.int32)
    return trimesh.Trimesh(vertices=verts, faces=faces, process=False)


class TestAddWallsAndBottom:

    def test_flat_surface_becomes_watertight(self):
        from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import _add_walls_and_bottom
        surface = _make_open_surface(z=5.0)
        solid = _add_walls_and_bottom(surface, bottom_z=0.0)
        assert solid.is_watertight

    def test_bottom_z_correct(self):
        from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import _add_walls_and_bottom
        surface = _make_open_surface(z=10.0)
        solid = _add_walls_and_bottom(surface, bottom_z=3.0)
        assert solid.vertices[:, 2].min() == pytest.approx(3.0, abs=0.01)

    def test_vertex_count_increases(self):
        from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import _add_walls_and_bottom
        surface = _make_open_surface()
        n_before = len(surface.vertices)
        solid = _add_walls_and_bottom(surface, bottom_z=-1.0)
        assert len(solid.vertices) > n_before

    def test_hill_surface_watertight(self):
        from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import _add_walls_and_bottom
        surface = _make_hill_surface(peak_z=5.0, n=10)
        solid = _add_walls_and_bottom(surface, bottom_z=-2.0)
        assert solid.is_watertight

    def test_top_z_preserved(self):
        from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import _add_walls_and_bottom
        surface = _make_open_surface(z=7.5)
        solid = _add_walls_and_bottom(surface, bottom_z=0.0)
        assert solid.vertices[:, 2].max() == pytest.approx(7.5, abs=0.01)


class TestBuildDeepseekTerrain:

    def test_source_conditioning_preserves_hills_and_quiets_flat_dem_noise(self):
        from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import (
            condition_terrain_source,
        )

        rng = np.random.default_rng(42)
        flat_city = (
            np.linspace(4.0, 11.0, 120)[:, None]
            + rng.normal(0.0, 2.5, size=(120, 120))
        )
        quiet, quiet_evidence = condition_terrain_source(
            flat_city, cell_size_mm=(0.475, 0.475))
        original_high_band = flat_city - gaussian_filter(
            flat_city, sigma=4.0, mode="reflect")
        quiet_high_band = quiet - gaussian_filter(
            quiet, sigma=4.0, mode="reflect")

        assert quiet_evidence["denoise_strength"] > 0.99
        assert np.std(quiet_high_band) < np.std(original_high_band) * 0.45
        assert quiet_evidence["random_texture"] is False

        yy, xx = np.mgrid[-1:1:120j, -1:1:120j]
        hills = 20.0 + 420.0 * np.exp(-2.0 * (xx * xx + yy * yy))
        preserved, hill_evidence = condition_terrain_source(
            hills, cell_size_mm=(0.475, 0.475))

        assert hill_evidence["landform_strength"] == pytest.approx(1.0)
        assert np.array_equal(preserved, hills)

    def test_surface_plan_is_immutable_and_materializes_same_terrain(self):
        from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import (
            build_deepseek_terrain,
            build_terrain_evidence,
            materialize_terrain_surface_plan,
            resolve_terrain_surface_plan,
        )

        grid = np.linspace(15.0, 240.0, 48 * 52).reshape(48, 52)
        plan = resolve_terrain_surface_plan(
            grid,
            1000.0,
            800.0,
            0.1,
            max_surface_edge_mm=0.84,
        )
        assert plan.regular_grid_m.flags.writeable is False
        assert plan.surface_z_grid_mm.flags.writeable is False
        with pytest.raises(ValueError):
            plan.surface_z_grid_mm[0, 0] = 999.0

        planned = materialize_terrain_surface_plan(plan, area_km2=0.8)
        direct = build_deepseek_terrain(
            grid,
            1000.0,
            800.0,
            0.8,
            0.1,
            max_surface_edge_mm=0.84,
        )
        assert np.allclose(planned.bounds, direct.bounds)
        assert np.allclose(
            np.sort(planned.vertices[:, 2]),
            np.sort(direct.vertices[:, 2]),
        )
        evidence = build_terrain_evidence(planned)
        assert evidence["fingerprint"] == plan.fingerprint
        assert evidence["policy_version"] == "terrain-surface-plan-v2"

    def test_surface_plan_sampling_matches_materialized_mesh(self):
        from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import (
            materialize_terrain_surface_plan,
            resolve_terrain_surface_plan,
            sample_deepseek_terrain_z,
            sample_terrain_surface_plan_z,
        )

        yy, xx = np.mgrid[-1:1:45j, -1:1:45j]
        grid = 20.0 + 180.0 * np.exp(-2.0 * (xx * xx + yy * yy))
        plan = resolve_terrain_surface_plan(
            grid, 1000.0, 1000.0, 0.1, max_surface_edge_mm=0.84)
        mesh = materialize_terrain_surface_plan(plan, area_km2=1.0)
        xs = np.array([-30.0, 0.0, 24.0])
        ys = np.array([18.0, 0.0, -22.0])
        assert np.allclose(
            sample_terrain_surface_plan_z(plan, xs, ys),
            sample_deepseek_terrain_z(mesh, xs, ys),
            atol=0.03,
        )

    def test_flat_grid_produces_watertight(self):
        from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import build_deepseek_terrain
        grid = np.full((20, 20), 100.0)
        solid = build_deepseek_terrain(grid, 1000.0, 1000.0, 1.0, 0.196)
        assert solid is not None
        assert isinstance(solid, trimesh.Trimesh)
        assert len(solid.faces) > 0
        assert np.ptp(solid.vertices[:, 2]) == pytest.approx(
            TERRAIN_THICKNESS_MM, abs=0.02)

    def test_formal_grid_has_printer_bounded_triangles_and_no_qem(self):
        from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import (
            build_deepseek_terrain,
            build_terrain_evidence,
        )

        yy, xx = np.mgrid[-1:1:80j, -1:1:100j]
        grid = 20.0 + 160.0 * np.exp(-3.0 * (xx * xx + yy * yy))
        solid = build_deepseek_terrain(
            grid, 1000.0, 800.0, 0.8, 0.196,
            max_surface_edge_mm=0.84)
        evidence = build_terrain_evidence(solid)

        assert evidence["formal_qem_decimation"] is False
        assert evidence["grid"]["qem_decimation"] is False
        assert evidence["surface_mesh"]["max_xy_edge_mm"] <= 0.840001
        assert evidence["surface_mesh"]["faces_over_2mm"] == 0
        assert evidence["surface_mesh"]["faces_over_5mm"] == 0

    def test_height_mapping_clips_outliers_and_protects_lowlands(self):
        from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import (
            resolve_terrain_height_mapping,
        )

        lowlands = np.linspace(3.0, 25.0, 9_900)
        hills = np.linspace(80.0, 380.0, 99)
        grid = np.concatenate([lowlands, hills, [9000.0]]).reshape(100, 100)
        mapping = resolve_terrain_height_mapping(
            grid,
            scale_mm_per_m=0.0071,
            requested_relief_mm=3.76,
            requested_gamma=0.35,
        )

        assert mapping["robust_high_m"] < 9000.0
        assert mapping["lowland_dominated"] is True
        assert mapping["resolved_gamma"] >= 0.85
        assert mapping["output_relief_mm"] <= 3.76

    def test_low_relief_city_is_not_forced_to_one_point_two_mm(self):
        from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import (
            resolve_terrain_height_mapping,
        )

        grid = np.linspace(2.0, 27.0, 10_000).reshape(100, 100)
        mapping = resolve_terrain_height_mapping(
            grid,
            scale_mm_per_m=0.0078,
            requested_relief_mm=3.76,
            requested_gamma=0.45,
        )

        assert mapping["robust_range_m"] < 35.0
        assert mapping["relief_floor_mm"] == pytest.approx(0.36)
        assert mapping["output_relief_mm"] == pytest.approx(0.36)

    def test_multiscale_detail_strengthens_hills_but_not_flat_city_noise(self):
        from scipy.ndimage import gaussian_filter
        from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import enhance_terrain_detail

        yy, xx = np.mgrid[-1:1:181j, -1:1:181j]
        macro = 0.72 * np.exp(-2.3 * (xx * xx + yy * yy))
        ridges = 0.035 * np.sin(31.0 * xx + 4.0 * yy) * macro
        surface = np.clip(macro + ridges, 0.0, 1.0)

        enhanced, evidence = enhance_terrain_detail(
            surface,
            robust_range_m=360.0,
            output_relief_mm=3.0,
            cell_size_mm=(0.59, 0.59),
        )
        original_band = surface - gaussian_filter(surface, sigma=2.0)
        enhanced_band = enhanced - gaussian_filter(enhanced, sigma=2.0)

        assert enhanced.shape == surface.shape
        assert np.isfinite(enhanced).all()
        assert enhanced.min() >= 0.0 and enhanced.max() <= 1.0
        assert np.sqrt(np.mean(enhanced_band ** 2)) > np.sqrt(
            np.mean(original_band ** 2))
        assert evidence["scene_strength"] == pytest.approx(1.0)
        assert 0.0 < evidence["delta_max_abs_mm"] <= 0.090001
        assert evidence["random_texture"] is False

        quiet, quiet_evidence = enhance_terrain_detail(
            surface,
            robust_range_m=20.0,
            output_relief_mm=1.2,
            cell_size_mm=(0.59, 0.59),
        )
        assert np.array_equal(quiet, surface)
        assert quiet_evidence["scene_strength"] == 0.0

    def test_relief_grid_z_range(self):
        from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import build_deepseek_terrain
        grid = np.linspace(0, 200, 400).reshape(20, 20)
        solid = build_deepseek_terrain(grid, 1000.0, 1000.0, 1.0, 0.196)
        z_min = solid.vertices[:, 2].min()
        z_max = solid.vertices[:, 2].max()
        z_range = z_max - z_min
        assert z_range > 0
        assert z_range <= TERRAIN_THICKNESS_MM + 1.0

    def test_z_base_position(self):
        from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import build_deepseek_terrain
        grid = np.full((10, 10), 50.0)
        solid = build_deepseek_terrain(grid, 500.0, 500.0, 0.25, 0.392)
        z_min = solid.vertices[:, 2].min()
        assert z_min == pytest.approx(Z_TERRAIN_BASE, abs=0.5)

    def test_requested_print_base_moves_formal_terrain_base(self):
        from _TEXTURE_STYLE_OF_DEEPSEEK.config import Z_WATER_BASE_MM
        from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import build_deepseek_terrain

        grid = np.linspace(0, 100, 400).reshape(20, 20)
        solid = build_deepseek_terrain(
            grid, 1000.0, 1000.0, 1.0, 0.196,
            base_thickness_mm=0.8)
        assert solid.vertices[:, 2].min() == pytest.approx(
            Z_WATER_BASE_MM + 0.8, abs=0.02)


class TestSampleDeepseekTerrainZ:

    def test_known_flat_points(self):
        from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import (
            build_deepseek_terrain,
            sample_deepseek_terrain_z,
        )
        grid = np.full((10, 10), 100.0)
        solid = build_deepseek_terrain(grid, 500.0, 500.0, 0.25, 0.392)

        xs = np.array([50.0, 100.0])
        ys = np.array([50.0, 100.0])
        zs = sample_deepseek_terrain_z(solid, xs, ys)
        assert len(zs) == 2
        assert all(np.isfinite(zs))
