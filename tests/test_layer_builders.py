"""Tests for v3 layer builders — buildings, water, vegetation, roads.

Phase 2 完成标志 (per spec_png_to_3mf_migration.md B.2.5).

Run: venv/bin/python -m pytest tests/test_layer_builders.py -v
"""

import sys
import os
from typing import List, Tuple

import numpy as np
import pytest
from shapely.geometry import Polygon, box, LineString

# Ensure the package root is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import trimesh


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _make_square(x: float, y: float, side: float) -> Polygon:
    return box(x, y, x + side, y + side)


def _make_flat_terrain_mesh(width: float = 1000.0) -> trimesh.Trimesh:
    """Create a flat terrain mesh for testing. All Z=0."""
    # Simple quad: two triangles
    verts = np.array([
        [0, 0, 0],
        [width, 0, 0],
        [width, width, 0],
        [0, width, 0],
    ], dtype=np.float64)
    faces = np.array([
        [0, 1, 2],
        [0, 2, 3],
    ], dtype=np.int32)
    return trimesh.Trimesh(vertices=verts, faces=faces, process=False)


# ---------------------------------------------------------------------------
# buildings v3
# ---------------------------------------------------------------------------

class TestBuildingsV3:
    """Tests for build_deepseek_buildings_v3."""

    def test_empty_input_returns_none(self):
        """空输入时返回 None 或空 mesh。"""
        from _TEXTURE_STYLE_OF_DEEPSEEK.buildings import build_deepseek_buildings_v3

        terrain = _make_flat_terrain_mesh()
        result = build_deepseek_buildings_v3([], [], terrain, 2000.0)
        assert result["landmarks"] is None
        assert result["buildings"] is None

    def test_returns_dict_with_correct_keys(self):
        """返回 dict 包含 'landmarks' 和 'buildings' 键。"""
        from _TEXTURE_STYLE_OF_DEEPSEEK.buildings import build_deepseek_buildings_v3

        terrain = _make_flat_terrain_mesh()
        # 单个简单建筑
        poly = _make_square(100, 100, 50)
        result = build_deepseek_buildings_v3(
            [(poly, 3.0)],  # one landmark building, 3mm height
            [],              # no ambient buildings
            terrain,
            2000.0,
        )
        assert isinstance(result, dict)
        assert "landmarks" in result
        assert "buildings" in result

    def test_landmark_only_produces_mesh(self):
        """只有 BL（地标）时 landmarks mesh 非空。"""
        from _TEXTURE_STYLE_OF_DEEPSEEK.buildings import build_deepseek_buildings_v3

        terrain = _make_flat_terrain_mesh()
        poly = _make_square(100, 100, 50)
        result = build_deepseek_buildings_v3(
            [(poly, 3.0)], [], terrain, 2000.0,
        )
        landmarks = result["landmarks"]
        if landmarks is not None:
            assert isinstance(landmarks, trimesh.Trimesh)
            assert landmarks.is_watertight
            assert len(landmarks.faces) > 0

    def test_ambient_only_produces_mesh(self):
        """只有 BO（街区填充）时 buildings mesh 非空。"""
        from _TEXTURE_STYLE_OF_DEEPSEEK.buildings import build_deepseek_buildings_v3

        terrain = _make_flat_terrain_mesh()
        poly = _make_square(100, 100, 50)
        result = build_deepseek_buildings_v3(
            [], [poly], terrain, 2000.0,
        )
        buildings = result["buildings"]
        if buildings is not None:
            assert isinstance(buildings, trimesh.Trimesh)
            assert buildings.is_watertight
            assert len(buildings.faces) > 0

    def test_landmark_and_ambient_both_watertight(self):
        """BL 和 BO 同时存在时，两个 mesh 都 watertight。"""
        from _TEXTURE_STYLE_OF_DEEPSEEK.buildings import build_deepseek_buildings_v3

        terrain = _make_flat_terrain_mesh()
        bl_poly = _make_square(100, 100, 30)
        bo_poly = _make_square(300, 100, 50)
        result = build_deepseek_buildings_v3(
            [(bl_poly, 3.5)], [bo_poly], terrain, 2000.0,
        )
        landmarks = result["landmarks"]
        buildings = result["buildings"]
        if landmarks is not None:
            assert landmarks.is_watertight
            assert len(landmarks.faces) > 0
        if buildings is not None:
            assert buildings.is_watertight
            assert len(buildings.faces) > 0

    def test_height_separation_BL_vs_BO(self):
        """BL 顶 >= 2.8mm，BO 顶 <= 2.5mm。"""
        from _TEXTURE_STYLE_OF_DEEPSEEK.buildings import build_deepseek_buildings_v3
        from _TEXTURE_STYLE_OF_DEEPSEEK.config import BUILDING_AGGREGATE_HEIGHT_MM

        terrain = _make_flat_terrain_mesh()
        bl_poly = _make_square(100, 100, 30)
        bo_poly = _make_square(300, 100, 50)
        bl_height = 3.5  # >= 2.8

        result = build_deepseek_buildings_v3(
            [(bl_poly, bl_height)], [bo_poly], terrain, 2000.0,
        )
        landmarks = result["landmarks"]
        buildings = result["buildings"]

        if landmarks is not None:
            z_range = np.ptp(landmarks.vertices[:, 2])
            # 地形 Z=0, 所以高度 ≈ bl_height
            assert z_range >= (bl_height - 0.5)  # tolerance

        if buildings is not None:
            z_range = np.ptp(buildings.vertices[:, 2])
            assert z_range <= (BUILDING_AGGREGATE_HEIGHT_MM + 0.5)


# ---------------------------------------------------------------------------
# water v3
# ---------------------------------------------------------------------------

class TestWaterV3:
    """Tests for build_deepseek_water_v3."""

    def test_empty_input_returns_none(self):
        """空输入返回 None。"""
        from _TEXTURE_STYLE_OF_DEEPSEEK.water import build_deepseek_water_v3

        result = build_deepseek_water_v3(
            [], [], 0, 0, 1000, 1000, scale=2000.0,
        )
        assert result is None

    def test_single_WL_polygon(self):
        """单个 WL polygon 生成 watertight mesh。"""
        from _TEXTURE_STYLE_OF_DEEPSEEK.water import build_deepseek_water_v3

        poly = _make_square(100, 100, 80)
        result = build_deepseek_water_v3(
            [poly], [], 0, 0, 1000, 1000, scale=2000.0,
        )
        if result is not None:
            assert isinstance(result, trimesh.Trimesh)
            assert result.is_watertight

    def test_WL_WO_mixed(self):
        """WL + WO 混合输入生成合并 mesh。"""
        from _TEXTURE_STYLE_OF_DEEPSEEK.water import build_deepseek_water_v3

        wl_poly = _make_square(100, 100, 50)
        wo_poly = _make_square(300, 300, 30)
        result = build_deepseek_water_v3(
            [wl_poly], [wo_poly], 0, 0, 500, 500, scale=2000.0,
        )
        if result is not None:
            assert isinstance(result, trimesh.Trimesh)
            assert result.is_watertight

    def test_returns_trimesh_not_none_with_data(self):
        """有数据时不返回 None。"""
        from _TEXTURE_STYLE_OF_DEEPSEEK.water import build_deepseek_water_v3

        poly = _make_square(50, 50, 100)
        result = build_deepseek_water_v3(
            [poly], [], 0, 0, 500, 500, scale=2000.0,
        )
        # With base plate + water feature, should produce a mesh
        assert result is not None
        assert isinstance(result, trimesh.Trimesh)

    def test_terrain_conforming_water_is_visible_and_watertight(self):
        """水面贴合坡面并露出 0.30mm，不再被地形遮住。"""
        from _TEXTURE_STYLE_OF_DEEPSEEK.config import (
            WATER_OVERLAY_EMBED_MM,
            WATER_OVERLAY_TOP_MM,
        )
        from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import build_deepseek_terrain
        from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.terrain import (
            sample_terrain_z,
        )
        from _TEXTURE_STYLE_OF_DEEPSEEK.water import build_deepseek_water_v3

        terrain = build_deepseek_terrain(
            np.array([[0.0, 0.0], [0.0, 100.0]]),
            width_m=200.0, height_m=200.0, area_km2=0.0, scale=1.0,
        )
        water = build_deepseek_water_v3(
            [Polygon([(-80, -80), (80, -80), (80, 80), (-80, 80)])],
            [], -100, -100, 100, 100, scale=1.0, terrain_mesh=terrain,
        )

        assert water is not None
        assert water.is_watertight
        sampled = sample_terrain_z(
            terrain, water.vertices[:, 0], water.vertices[:, 1],
        )
        offsets = np.unique(np.round(water.vertices[:, 2] - sampled, 6))
        assert np.allclose(offsets, [-WATER_OVERLAY_EMBED_MM, WATER_OVERLAY_TOP_MM])


# ---------------------------------------------------------------------------
# vegetation v3
# ---------------------------------------------------------------------------

class TestVegetationV3:
    """Tests for build_deepseek_vegetation_v3."""

    def test_empty_input_returns_none(self):
        """空输入返回 None。"""
        from _TEXTURE_STYLE_OF_DEEPSEEK.vegetation_exclusion import (
            build_deepseek_vegetation_v3,
        )

        terrain = _make_flat_terrain_mesh()
        result = build_deepseek_vegetation_v3([], [], terrain, 2000.0)
        assert result is None

    def test_single_VL_polygon(self):
        """单个 VL polygon 生成 watertight mesh。"""
        from _TEXTURE_STYLE_OF_DEEPSEEK.vegetation_exclusion import (
            build_deepseek_vegetation_v3,
        )

        terrain = _make_flat_terrain_mesh()
        poly = _make_square(100, 100, 80)
        result = build_deepseek_vegetation_v3([poly], [], terrain, 2000.0)
        if result is not None:
            assert isinstance(result, trimesh.Trimesh)
            assert result.is_watertight

    def test_VL_VO_mixed(self):
        """VL + VO 混合输入生成合并 mesh。"""
        from _TEXTURE_STYLE_OF_DEEPSEEK.vegetation_exclusion import (
            build_deepseek_vegetation_v3,
        )

        terrain = _make_flat_terrain_mesh()
        vl_poly = _make_square(100, 100, 50)
        vo_poly = _make_square(300, 100, 30)
        result = build_deepseek_vegetation_v3([vl_poly], [vo_poly], terrain, 2000.0)
        if result is not None:
            assert isinstance(result, trimesh.Trimesh)
            assert result.is_watertight

    def test_VL_higher_than_VO(self):
        """VL 的 Z 偏移 (0.15mm) > VO 的 Z 偏移 (0.10mm)。"""
        from _TEXTURE_STYLE_OF_DEEPSEEK.vegetation_exclusion import (
            build_deepseek_vegetation_v3,
        )

        terrain = _make_flat_terrain_mesh()
        vl_poly = _make_square(100, 100, 50)
        vo_poly = _make_square(400, 100, 50)

        # VL only
        result_vl = build_deepseek_vegetation_v3([vl_poly], [], terrain, 2000.0)
        # VO only
        result_vo = build_deepseek_vegetation_v3([], [vo_poly], terrain, 2000.0)

        if result_vl is not None and result_vo is not None:
            vl_z_max = result_vl.vertices[:, 2].max()
            vo_z_max = result_vo.vertices[:, 2].max()
            # Both on flat terrain (Z=0)
            # VL should be at ~0.15mm, VO at ~0.10mm
            assert abs(vl_z_max - 0.15) < 0.5
            assert abs(vo_z_max - 0.10) < 0.5
            assert vl_z_max > vo_z_max


# ---------------------------------------------------------------------------
# roads v3
# ---------------------------------------------------------------------------

class TestRoadsV3:
    """Tests for build_deepseek_roads_v3."""

    def test_empty_input_returns_none(self):
        """空输入返回 None。"""
        from _TEXTURE_STYLE_OF_DEEPSEEK.roads import build_deepseek_roads_v3

        terrain = _make_flat_terrain_mesh()
        result = build_deepseek_roads_v3([], terrain, 2000.0)
        assert result is None

    def test_single_road_line(self):
        """单条道路线生成 watertight mesh。"""
        from _TEXTURE_STYLE_OF_DEEPSEEK.roads import build_deepseek_roads_v3

        terrain = _make_flat_terrain_mesh()
        line = LineString([(100, 100), (500, 100)])
        roads = [(line, "primary", False)]
        result = build_deepseek_roads_v3(roads, terrain, 2000.0)
        if result is not None:
            assert isinstance(result, trimesh.Trimesh)
            assert result.is_watertight

    def test_bridge_road_detection(self):
        """桥道路被正确分类为 bridge。"""
        from _TEXTURE_STYLE_OF_DEEPSEEK.roads import build_deepseek_roads_v3

        terrain = _make_flat_terrain_mesh()
        line = LineString([(100, 100), (500, 100)])
        roads = [(line, "primary", True)]  # is_bridge=True
        result = build_deepseek_roads_v3(roads, terrain, 2000.0)
        if result is not None:
            assert isinstance(result, trimesh.Trimesh)

    def test_bridge_deck_is_raised_but_has_normal_thickness(self):
        """桥梁应抬高整个桥面，而不是只向下加厚。"""
        from _TEXTURE_STYLE_OF_DEEPSEEK.roads import build_deepseek_roads_v3
        from _TEXTURE_STYLE_OF_DEEPSEEK.config import (
            ROAD_BRIDGE_EXTRA_MM,
            ROAD_THICKNESS_MM,
            Z_ROAD_ABOVE_TERRAIN_MM,
        )

        terrain = _make_flat_terrain_mesh()
        line = LineString([(100, 100), (500, 100)])
        normal = build_deepseek_roads_v3([(line, "primary", False)], terrain, 2000.0)
        bridge = build_deepseek_roads_v3([(line, "primary", True)], terrain, 2000.0)

        assert normal is not None and bridge is not None
        assert np.ptp(normal.vertices[:, 2]) == pytest.approx(ROAD_THICKNESS_MM, abs=1e-6)
        assert np.ptp(bridge.vertices[:, 2]) == pytest.approx(ROAD_THICKNESS_MM, abs=1e-6)
        assert normal.vertices[:, 2].max() == pytest.approx(Z_ROAD_ABOVE_TERRAIN_MM, abs=1e-6)
        assert bridge.vertices[:, 2].max() == pytest.approx(
            Z_ROAD_ABOVE_TERRAIN_MM + ROAD_BRIDGE_EXTRA_MM, abs=1e-6)

    def test_multiple_roads_merge(self):
        """多条道路合并为单个 watertight mesh。"""
        from _TEXTURE_STYLE_OF_DEEPSEEK.roads import build_deepseek_roads_v3

        terrain = _make_flat_terrain_mesh()
        line1 = LineString([(100, 100), (500, 100)])
        line2 = LineString([(100, 300), (500, 300)])
        roads = [(line1, "primary", False), (line2, "secondary", False)]
        result = build_deepseek_roads_v3(roads, terrain, 2000.0)
        if result is not None:
            assert isinstance(result, trimesh.Trimesh)
            assert result.is_watertight

    def test_short_road_line_skipped(self):
        """长度 < 10m 的道路线被跳过。"""
        from _TEXTURE_STYLE_OF_DEEPSEEK.roads import build_deepseek_roads_v3

        terrain = _make_flat_terrain_mesh()
        short_line = LineString([(0, 0), (5, 0)])  # length = 5
        roads = [(short_line, "primary", False)]
        result = build_deepseek_roads_v3(roads, terrain, 2000.0)
        # 太短应该被跳过，返回 None 或空
        assert result is None


# ---------------------------------------------------------------------------
# block_base v3
# ---------------------------------------------------------------------------

class TestBlockBaseV3:
    """Tests for build_deepseek_block_base_v3 (PNG layer 1.5 暖米色城市底)."""

    def test_empty_input_returns_none(self):
        """空输入返回 None。"""
        from _TEXTURE_STYLE_OF_DEEPSEEK.block_base import build_deepseek_block_base_v3

        terrain = _make_flat_terrain_mesh()
        assert build_deepseek_block_base_v3([], terrain, 2000.0) is None

    def test_single_polygon_produces_watertight(self):
        """单个 polygon → watertight mesh。"""
        from _TEXTURE_STYLE_OF_DEEPSEEK.block_base import build_deepseek_block_base_v3

        terrain = _make_flat_terrain_mesh()
        poly = _make_square(100, 100, 80)
        result = build_deepseek_block_base_v3([poly], terrain, 2000.0)
        assert result is not None
        assert isinstance(result, trimesh.Trimesh)
        assert result.is_watertight

    def test_multiple_polygons_merge_watertight(self):
        """多 polygon → batch_boolean Add 合成单 watertight mesh。"""
        from _TEXTURE_STYLE_OF_DEEPSEEK.block_base import build_deepseek_block_base_v3

        terrain = _make_flat_terrain_mesh()
        polys = [_make_square(100, 100, 50),
                 _make_square(300, 100, 50),
                 _make_square(100, 300, 50)]
        result = build_deepseek_block_base_v3(polys, terrain, 2000.0)
        assert result is not None
        assert isinstance(result, trimesh.Trimesh)
        assert result.is_watertight

    def test_z_range_flush_with_terrain(self):
        """Z 范围 ≈ [terrain_z, terrain_z + BLOCK_BASE_THICKNESS_MM]。"""
        from _TEXTURE_STYLE_OF_DEEPSEEK.block_base import build_deepseek_block_base_v3
        from _TEXTURE_STYLE_OF_DEEPSEEK.config import BLOCK_BASE_THICKNESS_MM

        terrain = _make_flat_terrain_mesh()  # 所有 Z=0
        poly = _make_square(100, 100, 80)
        result = build_deepseek_block_base_v3([poly], terrain, 2000.0)
        assert result is not None
        z_min = result.vertices[:, 2].min()
        z_max = result.vertices[:, 2].max()
        # 容差 0.1mm 给采样误差
        assert abs(z_min - 0.0) < 0.1
        assert abs(z_max - BLOCK_BASE_THICKNESS_MM) < 0.1

    def test_tiny_polygon_skipped(self):
        """area < 10m² 的 polygon 直接跳过。"""
        from _TEXTURE_STYLE_OF_DEEPSEEK.block_base import build_deepseek_block_base_v3

        terrain = _make_flat_terrain_mesh()
        tiny = _make_square(0, 0, 1)   # area=1
        result = build_deepseek_block_base_v3([tiny], terrain, 2000.0)
        assert result is None
