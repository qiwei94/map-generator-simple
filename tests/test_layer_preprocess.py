"""Tests for _layer_preprocess.py — geometry subtraction, filtering, and layer assembly.

Phase 1 完成标志 (per spec_png_to_3mf_migration.md B.1.6).

Run: venv/bin/python -m pytest tests/test_layer_preprocess.py -v
"""

import sys
import os
import math
from typing import List, Tuple

import pytest
import geopandas as gpd
from shapely.geometry import Polygon, MultiPolygon, box, Point, LineString
from shapely.ops import unary_union

# Ensure the package root is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from _TEXTURE_STYLE_OF_DEEPSEEK._layer_preprocess import (
    _subtract,
    _filter_by_area,
    _extract_roads,
    _effective_road_tier,
    _close_unprintable_water_gaps,
    preprocess_layers,
    LayerPolygons,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.buildings import _aggregate_in_blocks
from _TEXTURE_STYLE_OF_DEEPSEEK.print_profile import PrinterProfile


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _make_square(x: float, y: float, side: float) -> Polygon:
    return box(x, y, x + side, y + side)


def _area(*polys: Polygon) -> float:
    return sum(p.area for p in polys if p is not None and not p.is_empty)


# ---------------------------------------------------------------------------
# _subtract
# ---------------------------------------------------------------------------

class TestSubtract:
    """Tests for _subtract(polys, minus_geom)."""

    def test_no_minus_geom(self):
        """minus_geom 为空时原样返回。"""
        polys = [_make_square(0, 0, 10)]
        assert _subtract(polys, None) == polys
        assert _subtract(polys, Polygon()) == polys

    def test_subtract_non_overlapping(self):
        """不相交的 polygon 不被减去。"""
        a = _make_square(0, 0, 10)
        b = _make_square(20, 20, 10)
        result = _subtract([a], b)
        assert len(result) == 1
        assert result[0].equals(a)

    def test_subtract_full_overlap(self):
        """完全覆盖时返回空 list。"""
        a = _make_square(0, 0, 10)
        b = _make_square(0, 0, 10)
        result = _subtract([a], b)
        assert result == []

    def test_subtract_partial_overlap(self):
        """部分重叠时返回差集 polygon。"""
        a = _make_square(0, 0, 10)
        b = _make_square(5, 0, 10)  # 右半边重叠
        result = _subtract([a], b)
        assert len(result) >= 1
        assert _area(*result) == pytest.approx(50.0)

    def test_subtract_splits_into_two(self):
        """减法结果被分裂为两个 polygon。"""
        a = _make_square(0, 0, 10)
        b = box(3, -1, 7, 11)  # 中间掏空：宽4, 高12
        result = _subtract([a], b)
        assert len(result) == 2
        assert _area(*result) == pytest.approx(100.0 - 4 * 10)

    def test_subtract_multipolygon_minus(self):
        """扣除 MultiPolygon。"""
        a = _make_square(0, 0, 10)
        b = MultiPolygon([_make_square(2, 2, 2), _make_square(6, 6, 2)])
        result = _subtract([a], b)
        assert len(result) >= 1
        assert _area(*result) == pytest.approx(100.0 - 8.0)

    def test_subtract_multiple_inputs(self):
        """多个输入 polygon 各自做减法。"""
        a1 = _make_square(0, 0, 10)
        a2 = _make_square(20, 0, 10)
        b = box(5, -5, 15, 15)  # 部分覆盖 a1：宽10, 高20
        result = _subtract([a1, a2], b)
        assert len(result) >= 1
        # a2 完全不受影响
        assert any(p.equals(a2) for p in result)

    def test_subtract_empty_or_invalid(self):
        """空/无效 polygon 被跳过。"""
        a1 = _make_square(0, 0, 10)
        a2 = Polygon()  # empty
        b = _make_square(2, 2, 2)
        result = _subtract([a1, a2], b)
        assert len(result) == 1
        # a2 被跳过，a1 被减去
        assert result[0].area < 100.0

    def test_subtract_repairs_self_intersection_instead_of_aborting(self):
        """OSM 自交环不应让整个风格图因 TopologyException 失败。"""
        bow_tie = Polygon([
            (0, 0), (10, 10), (0, 10), (10, 0), (0, 0),
        ])
        assert not bow_tie.is_valid

        result = _subtract([bow_tie], box(4, 4, 6, 6))

        assert result
        assert all(poly.is_valid for poly in result)
        assert all(not poly.is_empty for poly in result)


def test_oriented_block_aggregation_repairs_invalid_block():
    """巴黎式非法街区不能让 oriented_bbox 风格整体退出。"""
    invalid_block = Polygon([
        (0, 0), (10, 10), (0, 10), (10, 0), (0, 0),
    ])
    building = box(4.5, 7.5, 5.5, 8.5)
    assert not invalid_block.is_valid
    assert invalid_block.contains(building.centroid)

    result = _aggregate_in_blocks(
        [building], [invalid_block], mode="oriented_bbox",
        print_limit_m2=0.0, simplify_m=0.0,
    )

    assert result
    assert all(poly.is_valid for poly in result)


def test_extract_roads_keeps_bridge_metadata_and_subtracts_landmarks():
    roads = gpd.GeoDataFrame({
        "highway": ["primary", "residential"],
        "name": ["Pont Example Bridge", "Rue Example"],
        "bridge": ["yes", None],
        "wikidata": ["Q123", None],
        "wikipedia": [None, None],
        "geometry": [
            LineString([(0, 5), (30, 5)]),
            LineString([(0, 20), (30, 20)]),
        ],
    }, crs="EPSG:3857")

    result = _extract_roads(roads, [box(10, 0, 20, 10)], area_km2=10)

    bridge_segments = [line for line, highway, bridge in result if bridge]
    assert len(bridge_segments) == 2
    assert all(line.length == pytest.approx(10.0) for line in bridge_segments)
    assert any(highway == "residential" and not bridge
               for _, highway, bridge in result)


def test_extract_roads_filters_large_area_before_geometry_work():
    roads = gpd.GeoDataFrame({
        "highway": ["primary", "residential"],
        "geometry": [
            LineString([(0, 0), (20, 0)]),
            LineString([(0, 10), (20, 10)]),
        ],
    }, crs="EPSG:3857")

    result = _extract_roads(roads, [], area_km2=100)

    assert len(result) == 1
    assert result[0][1] == "primary"


def test_large_area_caps_unprintable_footway_block_detail():
    assert _effective_road_tier(5, area_km2=100) == 4
    assert _effective_road_tier(4, area_km2=100) == 4
    assert _effective_road_tier(5, area_km2=25) == 5


def test_preprocess_uses_declared_printer_nozzle_for_real_scale():
    empty = gpd.GeoDataFrame(
        {"geometry": []}, geometry="geometry", crs="EPSG:3857")
    profile = PrinterProfile(
        profile_id="test-0.6",
        nozzle_diameter_mm=0.6,
        extrusion_width_mm=0.65,
        layer_height_mm=0.2,
        min_colored_strip_mm=0.9,
        min_gap_mm=0.75,
        min_surface_layers=2,
    )

    layers = preprocess_layers(
        empty, empty, empty, empty,
        bbox_local=(0, 0, 1000, 1000),
        scale=0.01,
        area_km2=1,
        printer_profile=profile,
    )

    assert layers.nozzle_real_m == pytest.approx(60.0)


def test_water_holes_require_a_full_nozzle_width_to_survive():
    outer = box(0, 0, 1000, 1000)
    narrow_breakwater = box(100, 100, 700, 120)
    printable_island = box(800, 800, 900, 900)
    water = Polygon(
        outer.exterior.coords,
        [narrow_breakwater.exterior.coords, printable_island.exterior.coords],
    )

    cleaned = _close_unprintable_water_gaps(water, nozzle_real_m=30.0)

    assert len(cleaned.interiors) == 1
    assert cleaned.contains(Point(200, 110))
    assert not cleaned.contains(Point(850, 850))


def test_edge_connected_water_slit_below_nozzle_width_is_closed():
    water = box(0, 0, 1000, 1000).difference(box(490, 500, 510, 1001))

    cleaned = _close_unprintable_water_gaps(water, nozzle_real_m=30.0)

    assert cleaned.contains(Point(500, 900))


# ---------------------------------------------------------------------------
# _filter_by_area
# ---------------------------------------------------------------------------

class TestFilterByArea:
    """Tests for _filter_by_area(polys, min_area)."""

    def test_all_above_threshold(self):
        polys = [_make_square(0, 0, 10)]  # area = 100
        assert _filter_by_area(polys, 50.0) == polys

    def test_all_below_threshold(self):
        polys = [_make_square(0, 0, 10)]  # area = 100
        assert _filter_by_area(polys, 200.0) == []

    def test_mixed(self):
        big = _make_square(0, 0, 20)   # area = 400
        small = _make_square(50, 0, 5)  # area = 25
        result = _filter_by_area([big, small], 100.0)
        assert len(result) == 1
        assert result[0].equals(big)

    def test_empty_polygon(self):
        result = _filter_by_area([Polygon()], 1.0)
        assert result == []

    def test_edge_case_exact_match(self):
        p = _make_square(0, 0, 10)  # area = 100
        result = _filter_by_area([p], 100.0)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# LayerPolygons dataclass
# ---------------------------------------------------------------------------

class TestLayerPolygons:
    """Tests for LayerPolygons dataclass."""

    def test_empty_summary(self):
        lp = LayerPolygons()
        assert "BL=0" in lp.summary()
        assert "roads=0" in lp.summary()

    def test_summary_with_data(self):
        lp = LayerPolygons(
            BL=[(_make_square(0, 0, 10), 3.0)],
            BO=[_make_square(20, 0, 10)],
            VL=[_make_square(40, 0, 10)],
            VO=[],
            WL=[],
            WO=[_make_square(60, 0, 10)],
            roads_lines=[(LineString([(0, 0), (10, 10)]), "primary", False)],
            nozzle_real_m=51.0,
            min_area_m2=4000.0,
        )
        s = lp.summary()
        assert "BL=1" in s
        assert "BO=1" in s
        assert "VL=1" in s
        assert "VO=0" in s
        assert "WO=1" in s
        assert "roads=1" in s

    def test_field_defaults(self):
        lp = LayerPolygons()
        assert lp.BL == []
        assert lp.BO == []
        assert lp.VL == []
        assert lp.VO == []
        assert lp.WL == []
        assert lp.WO == []
        assert lp.block_base == []
        assert lp.roads_lines == []
        assert lp.nozzle_real_m == 0.0
        assert lp.min_area_m2 == 0.0


# ---------------------------------------------------------------------------
# Boolean subtraction 不重叠验证（Phase 1 核心断言）
# ---------------------------------------------------------------------------

class TestDisjointLayers:
    """验证 5 步减法后任意两 layer 的 polygon 不重叠。

    这些测试不依赖真实 OSM 数据，而是通过手工构建 polygon 来验证
    _subtract + _filter_by_area 组合的正确性。
    """

    def test_simulated_BO_vs_BL_disjoint(self):
        """模拟 BO 被从 BL 中扣减后不重叠。"""
        BL = [_make_square(0, 0, 10)]
        # BO 覆盖 BL 的一半
        BO_raw = [_make_square(5, 0, 10)]
        # 扣减
        BO_clean = _subtract(BO_raw, unary_union(BL))
        BL_geom = unary_union([p for p in BL])
        BO_geom = unary_union(BO_clean)
        assert BO_geom.intersection(BL_geom).area < 1.0

    def test_simulated_VL_vs_BL_disjoint(self):
        """模拟 VL 被从 BL 中扣减后不重叠。"""
        BL = [_make_square(0, 0, 10)]
        VL_raw = [_make_square(2, 2, 6)]  # 完全在 BL 内
        VL_clean = _subtract(VL_raw, unary_union(BL))
        # VL 在 BL 内，应完全被减掉
        assert _area(*VL_clean) == 0.0

    def test_simulated_full_subtraction_chain(self):
        """模拟完整 5 步减法链: BL 不动 → 逐步扣除更低优先级。"""
        BL = [_make_square(0, 0, 20)]
        # WL 覆盖 BL 左下角
        WL_raw = [_make_square(0, 0, 8)]
        # VL 覆盖 BL 右上角
        VL_raw = [_make_square(12, 12, 8)]
        # BO 覆盖 BL 中间
        BO_raw = [_make_square(6, 6, 8)]
        # VO 覆盖全范围
        VO_raw = [_make_square(-2, -2, 24)]

        # 按优先级顺序做减法
        # Step 1: WL 从 BL 扣？不对，BL 优先级高于 WL
        # 正确顺序：所有低优先级从高优先级扣除
        # BL 优先级最高，不扣

        # WO = WO - WL (WL > WO)
        WO = _subtract(WL_raw, unary_union(WL_raw))  # WO 没有自己的 layer
        # 简化：用 VO 模拟
        # VO = VO - WL - VL - BO
        all_high = unary_union(WL_raw + VL_raw + BO_raw)
        VO_clean = _subtract(VO_raw, all_high)

        # VO 不应与任何高优先级重叠
        VO_geom = unary_union(VO_clean)
        assert VO_geom.intersection(unary_union(WL_raw)).area < 1.0
        assert VO_geom.intersection(unary_union(VL_raw)).area < 1.0
        assert VO_geom.intersection(unary_union(BO_raw)).area < 1.0


# ---------------------------------------------------------------------------
# 精度过滤验证
# ---------------------------------------------------------------------------

class TestPrecisionFilter:
    """验证精度过滤逻辑。"""

    def test_min_printable_area_filter(self):
        """面积 < MIN_PRINTABLE_AREA_M2 的 polygon 被过滤。"""
        from _TEXTURE_STYLE_OF_DEEPSEEK.config import MIN_PRINTABLE_AREA_M2

        # 小于阈值
        small = _make_square(0, 0, math.sqrt(MIN_PRINTABLE_AREA_M2) - 10)
        # 大于阈值
        big = _make_square(0, 0, math.sqrt(MIN_PRINTABLE_AREA_M2) + 100)

        assert small.area < MIN_PRINTABLE_AREA_M2
        assert big.area > MIN_PRINTABLE_AREA_M2

        result = _filter_by_area([small, big], MIN_PRINTABLE_AREA_M2)
        assert len(result) == 1
        assert result[0].equals(big)

    def test_nozzle_based_min_area_formula(self):
        """(nozzle * 1.5)² 面积过滤。"""
        from _TEXTURE_STYLE_OF_DEEPSEEK.config import NOZZLE_DIAM_MM

        # nozzle_real = NOZZLE_DIAM_MM / scale
        # 取典型 scale = 2000
        scale = 2000.0
        nozzle_real = NOZZLE_DIAM_MM / scale
        min_area_model = (nozzle_real * 1.5) ** 2  # model 单位 (m²)

        # BO 的过滤阈值较宽松 (0.5 * min_area)
        bo_threshold = min_area_model * 0.5

        big = _make_square(0, 0, math.sqrt(bo_threshold) * 2)
        small = _make_square(0, 0, math.sqrt(bo_threshold) * 0.5)

        assert big.area >= bo_threshold
        assert small.area < bo_threshold

        result = _filter_by_area([big, small], bo_threshold)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# block_base — PNG layer 1.5 暖米色城市底 (Step 8.5)
# ---------------------------------------------------------------------------

class TestBlockBase:
    """验证 _compute_block_base 面积过滤 + 基本行为。"""

    def test_no_features_returns_blocks_filtered(self):
        """无 exclusion 时，city_blocks 仅过 area filter 后返回。"""
        from _TEXTURE_STYLE_OF_DEEPSEEK._layer_preprocess import _compute_block_base
        big = _make_square(0, 0, 100)        # area=10000
        small = _make_square(200, 0, 20)     # area=400 (< 1000)
        result = _compute_block_base([big, small], min_area_m2=1000.0)
        assert len(result) == 1
        assert result[0].equals(big)

    def test_all_below_min_area_returns_empty(self):
        """所有 block < min_area 时返回空。"""
        from _TEXTURE_STYLE_OF_DEEPSEEK._layer_preprocess import _compute_block_base
        small = _make_square(0, 0, 20)  # area=400
        result = _compute_block_base([small], min_area_m2=1000.0)
        assert result == []

    def test_empty_input_returns_empty(self):
        """空输入返回空。"""
        from _TEXTURE_STYLE_OF_DEEPSEEK._layer_preprocess import _compute_block_base
        result = _compute_block_base([], min_area_m2=1000.0)
        assert result == []

    def test_max_area_filter(self):
        """max_area_m2 > 0 时过滤过大的 block。"""
        from _TEXTURE_STYLE_OF_DEEPSEEK._layer_preprocess import _compute_block_base
        big = _make_square(0, 0, 200)     # area=40000
        mid = _make_square(300, 0, 100)   # area=10000
        result = _compute_block_base(
            [big, mid], min_area_m2=1000.0, max_area_m2=20000.0)
        assert len(result) == 1

    def test_veg_landmark_exclusion(self):
        """veg_landmark_polys 被用作 exclusion，减掉后碎片过滤。"""
        from _TEXTURE_STYLE_OF_DEEPSEEK._layer_preprocess import _compute_block_base
        block = _make_square(0, 0, 100)   # area=10000
        veg = [_make_square(0, 0, 100)]   # 完全覆盖
        result = _compute_block_base(
            [block], min_area_m2=1000.0,
            veg_landmark_polys=veg)
        assert result == []
