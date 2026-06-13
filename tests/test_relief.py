"""Tests for terrain3d/processors/relief.py — Z mapping and building height compression."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pytest

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.relief import (
    compute_terrain_z_mapping,
    map_terrain_z,
    compress_building_height,
)


class TestComputeTerrainZMapping:

    def test_flat_terrain(self):
        mapping = compute_terrain_z_mapping(100.0, 100.0)
        assert mapping["z_range_m"] == pytest.approx(0.0)
        assert mapping["thickness_mm"] > 0
        assert mapping["scale"] == pytest.approx(0.0)

    def test_hilly_terrain(self):
        mapping = compute_terrain_z_mapping(0.0, 500.0)
        assert mapping["z_range_m"] == pytest.approx(500.0)
        assert mapping["thickness_mm"] > 0
        assert mapping["scale"] > 0

    def test_thickness_within_bounds(self):
        from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.config import get_relief_config
        cfg = get_relief_config()
        z_min_mm = cfg["terrain_z_min_mm"]
        z_max_mm = cfg["terrain_z_max_mm"]

        mapping = compute_terrain_z_mapping(0.0, 1000.0)
        assert mapping["thickness_mm"] >= z_min_mm
        assert mapping["thickness_mm"] <= z_max_mm

    def test_result_keys(self):
        mapping = compute_terrain_z_mapping(0.0, 100.0)
        assert "z_min_m" in mapping
        assert "z_max_m" in mapping
        assert "z_range_m" in mapping
        assert "thickness_mm" in mapping
        assert "scale" in mapping


class TestMapTerrainZ:

    def test_linear_mapping(self):
        mapping = {
            "z_min_m": 0.0,
            "z_max_m": 100.0,
            "z_range_m": 100.0,
            "thickness_mm": 4.0,
            "scale": 0.04,
        }
        verts = np.array([
            [0, 0, 0],
            [0, 0, 50],
            [0, 0, 100],
        ], dtype=np.float64)
        result = map_terrain_z(verts, mapping)
        assert result[0, 2] == pytest.approx(0.0)
        assert result[1, 2] == pytest.approx(2.0)
        assert result[2, 2] == pytest.approx(4.0)

    def test_flat_terrain_midpoint(self):
        mapping = {
            "z_min_m": 50.0,
            "z_max_m": 50.0,
            "z_range_m": 0.0,
            "thickness_mm": 4.0,
            "scale": 0.0,
        }
        verts = np.array([[0, 0, 50]], dtype=np.float64)
        result = map_terrain_z(verts, mapping)
        assert result[0, 2] == pytest.approx(2.0)

    def test_xy_preserved(self):
        mapping = {
            "z_min_m": 0.0,
            "z_max_m": 100.0,
            "z_range_m": 100.0,
            "thickness_mm": 4.0,
            "scale": 0.04,
        }
        verts = np.array([[5.5, 7.3, 50]], dtype=np.float64)
        result = map_terrain_z(verts, mapping)
        assert result[0, 0] == pytest.approx(5.5)
        assert result[0, 1] == pytest.approx(7.3)


class TestCompressBuildingHeight:

    def test_low_building(self):
        h = compress_building_height(10.0)
        assert h >= 2.5
        assert h <= 6.0

    def test_tall_building(self):
        h = compress_building_height(100.0)
        assert h > compress_building_height(10.0)

    def test_area_fallback(self):
        h = compress_building_height(0, area_m2=500.0)
        assert h > 0

    def test_clamped_range(self):
        from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.config import get_relief_config
        cfg = get_relief_config()
        h_min = cfg["building_height_min_mm"]
        h_max = cfg["building_height_max_mm"]

        h_low = compress_building_height(1.0)
        h_high = compress_building_height(500.0)
        assert h_low >= h_min
        assert h_high <= h_max

    def test_monotonic(self):
        heights = [compress_building_height(h) for h in [10, 30, 60, 100, 150]]
        for i in range(len(heights) - 1):
            assert heights[i] <= heights[i + 1]
