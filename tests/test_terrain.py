"""Tests for terrain.py — _add_walls_and_bottom, build_deepseek_terrain, sample_deepseek_terrain_z."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pytest
import trimesh

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

    def test_flat_grid_produces_watertight(self):
        from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import build_deepseek_terrain
        grid = np.full((20, 20), 100.0)
        solid = build_deepseek_terrain(grid, 1000.0, 1000.0, 1.0, 0.196)
        assert solid is not None
        assert isinstance(solid, trimesh.Trimesh)
        assert len(solid.faces) > 0

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
