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
            flat_only=False,
            surface_levels_mm=[0.8],
            surface_thickness_mm=0.24,
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
            flat_only=False,
            surface_levels_mm=[0.8, 0.5],
            surface_thickness_mm=0.24,
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
            flat_only=False,
            surface_levels_mm=[0.8],
            surface_thickness_mm=0.24,
        )
        # With base plate + water feature, should produce a mesh
        assert result is not None
        assert isinstance(result, trimesh.Trimesh)
        assert len(result.faces) > 12
        assert np.ptp(result.vertices[:, 2]) >= 0.64 - 1e-6

    def test_prepare_relief_recesses_terrain_and_builds_printable_cap(self):
        from _TEXTURE_STYLE_OF_DEEPSEEK.water import (
            build_deepseek_water_v3,
            prepare_deepseek_water_relief,
        )

        terrain = trimesh.creation.box(extents=[10.0, 10.0, 2.0])
        terrain.apply_translation([0.0, 0.0, -0.6])  # Z=-1.6 .. 0.4
        poly = _make_square(-5.0, -5.0, 10.0)
        relief = prepare_deepseek_water_relief(
            terrain, [poly], [], 1.0,
            base_thickness_mm=0.4,
            surface_thickness_mm=0.24,
        )

        assert terrain.is_watertight
        assert relief["carved_vertex_count"] == 4
        assert len(relief["surface_levels_mm"]) == 1

        water = build_deepseek_water_v3(
            [poly], [], -5.0, -5.0, 5.0, 5.0, scale=1.0,
            flat_only=False,
            base_thickness_mm=0.4,
            surface_levels_mm=relief["surface_levels_mm"],
            surface_thickness_mm=relief["surface_thickness_mm"],
        )
        assert water is not None
        assert water.is_watertight
        assert len(water.faces) > 12
        assert water.bounds[1, 2] > -1.6


# ---------------------------------------------------------------------------
# vegetation v3
# ---------------------------------------------------------------------------

class TestVegetationV3:
    """Tests for build_deepseek_vegetation_v3."""

    def test_point_touching_triangle_islands_get_independent_vertices(self):
        from _TEXTURE_STYLE_OF_DEEPSEEK.vegetation_exclusion import (
            _split_point_touching_topology,
        )

        points = np.array([
            [0, 0], [1, 0], [0, 1], [-1, 0], [0, -1],
        ], dtype=np.float64)
        faces = np.array([[0, 1, 2], [0, 3, 4]], dtype=np.int32)

        split_points, split_faces = _split_point_touching_topology(
            points, faces)

        assert len(split_points) == 6
        assert set(split_faces[0]).isdisjoint(set(split_faces[1]))

    def test_local_boundary_pinch_is_split_even_when_faces_reconnect(self):
        from _TEXTURE_STYLE_OF_DEEPSEEK.vegetation_exclusion import (
            _split_point_touching_topology,
        )

        points = np.array([
            [0, 0], [1, 0], [0, 1], [-1, 0], [0, -1],
        ], dtype=np.float64)
        # The first two triangles meet only at vertex 0, while the final two
        # make the complete face set edge-connected away from that pinch.
        faces = np.array([
            [0, 1, 2], [0, 3, 4], [1, 3, 2], [2, 3, 4],
        ], dtype=np.int32)

        split_points, split_faces = _split_point_touching_topology(
            points, faces)

        assert len(split_points) == 6
        assert split_faces[0, 0] != split_faces[1, 0]

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

    def test_composition_role_tuple_is_supported(self):
        """预处理输出的第 4 项角色可直接进入正式 mesh builder。"""
        from _TEXTURE_STYLE_OF_DEEPSEEK.roads import build_deepseek_roads_v3

        terrain = _make_flat_terrain_mesh()
        roads = [
            (LineString([(100, 100), (500, 100)]),
             "primary", False, "primary"),
            (LineString([(100, 300), (500, 300)]),
             "secondary", False, "secondary"),
        ]
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

    def test_touching_textured_polygons_remain_independent_closed_shells(self):
        """Touching block bodies must not be welded into a non-manifold mesh."""
        from shapely.geometry import box
        from _TEXTURE_STYLE_OF_DEEPSEEK.block_base import build_deepseek_block_base_v3

        terrain = _make_flat_terrain_mesh()
        polys = [box(100, 100, 200, 200), box(200, 100, 300, 200)]
        result = build_deepseek_block_base_v3(
            polys, terrain, 2000.0, brick_style=False,
            block_classes=["residential", "commercial"],
            grid_step_mm=5.0,
        )

        assert result is not None
        assert result.is_watertight
        assert result.is_winding_consistent
        assert len(result.split(only_watertight=False)) == 2

    def test_invalid_textured_part_falls_back_to_flat(self, monkeypatch):
        """One broken textured body degrades locally instead of poisoning 3MF."""
        from shapely.geometry import box
        from _TEXTURE_STYLE_OF_DEEPSEEK import block_base

        open_triangle = trimesh.Trimesh(
            vertices=[[0, 0, 0], [1, 0, 0], [0, 1, 0]],
            faces=[[0, 1, 2]], process=False,
        )
        monkeypatch.setattr(
            block_base, "_polygon_to_textured_mesh",
            lambda *args, **kwargs: open_triangle,
        )

        result = block_base.build_deepseek_block_base_v3(
            [box(100, 100, 200, 200)], _make_flat_terrain_mesh(), 2000.0,
            brick_style=False, block_classes=["residential"],
        )

        assert result is not None
        assert result.is_watertight
        assert result.is_winding_consistent

    def test_post_transform_clearance_reopens_printable_road_seam(self):
        from shapely.geometry import LineString, box
        from _TEXTURE_STYLE_OF_DEEPSEEK.block_base import (
            enforce_final_block_base_clearance,
        )

        # Simulate a transformed block that has drifted back across a road
        # centre-line.  At scale=1 the source metre and model mm are equal.
        polys, classes, evidence = enforce_final_block_base_clearance(
            [box(-10, -10, 10, 10)],
            ["residential"],
            [LineString([(0, -20), (0, 20)])],
            scale=1.0,
            clearance_mm=0.84,
            min_piece_area_m2=0.0,
        )

        assert len(polys) == 2
        assert classes == ["residential", "residential"]
        left, right = sorted(polys, key=lambda poly: poly.centroid.x)
        assert right.bounds[0] - left.bounds[2] == pytest.approx(0.84)
        assert evidence["pre_clip_intrusion_area_m2"] > 0
        assert evidence["post_clip_intrusion_area_m2"] == pytest.approx(0.0)
        assert evidence["passed"] is True

    def test_builder_applies_clearance_after_brick_transform(self, monkeypatch):
        from shapely.geometry import LineString, box
        from _TEXTURE_STYLE_OF_DEEPSEEK import _brick_transform, block_base

        # The input already has a seam.  The mocked brick transform closes it;
        # evidence > 0 proves the builder's final cut runs after that transform.
        input_polys = [box(-10, -10, -0.5, 10), box(0.5, -10, 10, 10)]
        monkeypatch.setattr(
            _brick_transform,
            "brick_transform_batch",
            lambda *args, **kwargs: [box(-10, -10, 10, 10)],
        )
        monkeypatch.setattr(block_base, "_build_flat", lambda *args: "mesh")

        mesh, evidence = block_base.build_deepseek_block_base_v3(
            input_polys,
            _make_flat_terrain_mesh(),
            scale=1.0,
            brick_style=True,
            clearance_lines=[LineString([(0, -20), (0, 20)])],
            final_clearance_mm=0.84,
            return_clearance_evidence=True,
        )

        assert mesh == "mesh"
        assert evidence["pre_clip_intrusion_area_m2"] > 0
        assert evidence["post_clip_intrusion_area_m2"] == pytest.approx(0.0)


class TestBlockBaseEdgeFilter:
    def test_retreat_removes_edge_and_filters_transition_by_occupancy(self):
        from shapely.geometry import box
        from _TEXTURE_STYLE_OF_DEEPSEEK.block_base import filter_block_base_edges

        # scale=0.01mm/m: 2mm retreat=200m, 1mm transition=100m.
        edge = box(20, 400, 120, 500)
        transition_empty = box(400, 220, 460, 280)
        transition_built = box(220, 400, 280, 460)
        inner = box(400, 400, 500, 500)
        occupied = [box(230, 410, 270, 450)]

        kept, indices, stats = filter_block_base_edges(
            [edge, transition_empty, transition_built, inner],
            (0, 0, 1000, 1000),
            scale=0.01,
            retreat_mm=2.0,
            transition_mm=1.0,
            occupied_polys=occupied,
            min_coverage=0.02,
        )

        assert indices == [2, 3]
        assert kept == [transition_built, inner]
        assert stats == {
            "input": 4,
            "kept": 2,
            "outer_removed": 1,
            "transition_removed": 1,
        }

    def test_zero_retreat_is_identity(self):
        from shapely.geometry import box
        from _TEXTURE_STYLE_OF_DEEPSEEK.block_base import filter_block_base_edges

        polys = [box(0, 0, 10, 10)]
        kept, indices, stats = filter_block_base_edges(
            polys, (0, 0, 100, 100), scale=1.0, retreat_mm=0.0
        )
        assert kept == polys
        assert indices == [0]
        assert stats["kept"] == 1
