"""Tests for _geom_utils.py — winding, densification, CrossSection conversion."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pytest
from shapely.geometry import Polygon, box

from _TEXTURE_STYLE_OF_DEEPSEEK._geom_utils import (
    signed_area_2d,
    ensure_ccw,
    ensure_cw,
    densify_ring,
    densify_polygon,
    shapely_poly_to_crosssection,
)


class TestSignedArea2D:

    def test_ccw_triangle_positive(self):
        contour = np.array([[0, 0], [1, 0], [0, 1], [0, 0]], dtype=np.float64)
        assert signed_area_2d(contour) > 0

    def test_cw_triangle_negative(self):
        contour = np.array([[0, 0], [0, 1], [1, 0], [0, 0]], dtype=np.float64)
        assert signed_area_2d(contour) < 0

    def test_unit_square_area(self):
        contour = np.array([[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]], dtype=np.float64)
        assert signed_area_2d(contour) == pytest.approx(1.0)

    def test_degenerate_line(self):
        contour = np.array([[0, 0], [1, 0], [0, 0]], dtype=np.float64)
        assert signed_area_2d(contour) == pytest.approx(0.0)


class TestEnsureCCW:

    def test_already_ccw_unchanged(self):
        ccw = np.array([[0, 0], [1, 0], [0, 1], [0, 0]], dtype=np.float64)
        result = ensure_ccw(ccw)
        np.testing.assert_array_equal(result, ccw)

    def test_cw_reversed(self):
        cw = np.array([[0, 0], [0, 1], [1, 0], [0, 0]], dtype=np.float64)
        result = ensure_ccw(cw)
        assert signed_area_2d(result) > 0


class TestEnsureCW:

    def test_already_cw_unchanged(self):
        cw = np.array([[0, 0], [0, 1], [1, 0], [0, 0]], dtype=np.float64)
        result = ensure_cw(cw)
        np.testing.assert_array_equal(result, cw)

    def test_ccw_reversed(self):
        ccw = np.array([[0, 0], [1, 0], [0, 1], [0, 0]], dtype=np.float64)
        result = ensure_cw(ccw)
        assert signed_area_2d(result) < 0


class TestDensifyRing:

    def test_short_edges_unchanged(self):
        coords = np.array([[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]], dtype=np.float64)
        result = densify_ring(coords, max_edge_m=10.0)
        assert len(result) == len(coords)

    def test_long_edge_split(self):
        coords = np.array([[0, 0], [100, 0], [0, 0]], dtype=np.float64)
        result = densify_ring(coords, max_edge_m=30.0)
        assert len(result) > len(coords)

    def test_split_count(self):
        coords = np.array([[0, 0], [100, 0], [100, 100], [0, 0]], dtype=np.float64)
        result = densify_ring(coords, max_edge_m=25.0)
        assert len(result) >= 3 + 3 + 3  # each 100m edge split into >=4 segments

    def test_single_point(self):
        coords = np.array([[5, 5]], dtype=np.float64)
        result = densify_ring(coords, max_edge_m=10.0)
        assert len(result) == 1


class TestDensifyPolygon:

    def test_exterior_densified(self):
        poly = box(0, 0, 100, 100)
        dense = densify_polygon(poly, max_edge_m=30.0)
        assert len(dense.exterior.coords) > len(poly.exterior.coords)

    def test_empty_polygon(self):
        poly = Polygon()
        result = densify_polygon(poly, max_edge_m=10.0)
        assert result.is_empty

    def test_holes_preserved(self):
        outer = [(0, 0), (200, 0), (200, 200), (0, 200)]
        hole = [(50, 50), (150, 50), (150, 150), (50, 150)]
        poly = Polygon(outer, [hole])
        dense = densify_polygon(poly, max_edge_m=50.0)
        assert len(dense.interiors) == 1

    def test_zero_max_edge_returns_same(self):
        poly = box(0, 0, 10, 10)
        result = densify_polygon(poly, max_edge_m=0)
        assert result.equals(poly)


class TestShapelyPolyToCrosssection:

    def test_simple_square(self):
        poly = box(0, 0, 10, 10)
        cs = shapely_poly_to_crosssection(poly)
        assert cs.area() > 0

    def test_polygon_with_hole(self):
        outer = [(0, 0), (100, 0), (100, 100), (0, 100)]
        hole = [(30, 30), (70, 30), (70, 70), (30, 70)]
        poly = Polygon(outer, [hole])
        cs = shapely_poly_to_crosssection(poly)
        assert cs.area() == pytest.approx(100 * 100 - 40 * 40, rel=0.05)

    def test_empty_polygon(self):
        poly = Polygon()
        cs = shapely_poly_to_crosssection(poly)
        assert cs.area() == pytest.approx(0.0)

    def test_3d_coords_handled(self):
        poly = Polygon([(0, 0, 5), (10, 0, 5), (10, 10, 5), (0, 10, 5)])
        cs = shapely_poly_to_crosssection(poly)
        assert cs.area() == pytest.approx(100.0, rel=0.05)
