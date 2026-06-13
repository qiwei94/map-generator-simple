"""Tests for _bridge.py — trimesh <-> Manifold conversion."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pytest
import trimesh

from _TEXTURE_STYLE_OF_DEEPSEEK._bridge import (
    trimesh_to_manifold,
    manifold_to_trimesh,
    is_manifold_available,
)


class TestIsManifoldAvailable:

    def test_returns_true(self):
        assert is_manifold_available() is True


class TestTrimeshToManifold:

    def test_watertight_box_converts(self):
        mesh = trimesh.creation.box(extents=[10, 10, 10])
        m = trimesh_to_manifold(mesh)
        assert not m.is_empty()

    def test_empty_mesh_raises(self):
        mesh = trimesh.Trimesh(vertices=[], faces=[])
        with pytest.raises(ValueError, match="Empty mesh"):
            trimesh_to_manifold(mesh)

    def test_none_raises(self):
        with pytest.raises(ValueError, match="Empty mesh"):
            trimesh_to_manifold(None)

    def test_sphere_converts(self):
        mesh = trimesh.creation.icosphere(subdivisions=2)
        m = trimesh_to_manifold(mesh)
        assert not m.is_empty()
        assert m.num_tri() > 0


class TestManifoldToTrimesh:

    def test_box_round_trip(self):
        original = trimesh.creation.box(extents=[10, 10, 10])
        m = trimesh_to_manifold(original)
        result = manifold_to_trimesh(m)
        assert isinstance(result, trimesh.Trimesh)
        assert len(result.faces) > 0
        assert result.volume == pytest.approx(original.volume, rel=0.01)

    def test_sphere_round_trip(self):
        original = trimesh.creation.icosphere(subdivisions=2)
        m = trimesh_to_manifold(original)
        result = manifold_to_trimesh(m)
        assert isinstance(result, trimesh.Trimesh)
        assert result.volume == pytest.approx(original.volume, rel=0.05)

    def test_empty_manifold(self):
        import manifold3d
        m = manifold3d.Manifold()
        result = manifold_to_trimesh(m)
        assert len(result.faces) == 0


class TestBooleanViaManifold:

    def test_subtract_makes_smaller(self):
        box_a = trimesh.creation.box(extents=[20, 20, 20])
        box_b = trimesh.creation.box(extents=[10, 10, 10])
        ma = trimesh_to_manifold(box_a)
        mb = trimesh_to_manifold(box_b)
        result = manifold_to_trimesh(ma - mb)
        assert result.volume < box_a.volume
        assert result.volume == pytest.approx(
            box_a.volume - box_b.volume, rel=0.05)

    def test_union_makes_larger_or_equal(self):
        box_a = trimesh.creation.box(extents=[10, 10, 10])
        box_b = trimesh.creation.box(extents=[10, 10, 10])
        box_b.vertices[:, 0] += 5  # partial overlap
        ma = trimesh_to_manifold(box_a)
        mb = trimesh_to_manifold(box_b)
        result = manifold_to_trimesh(ma + mb)
        assert result.volume >= box_a.volume
