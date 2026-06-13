"""Tests for config.py — compute_scale, get_area_class, estimate_building_height_from_area."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from _TEXTURE_STYLE_OF_DEEPSEEK.config import (
    INTERNAL_SPAN_MM,
    AREA_SMALL_THRESHOLD,
    AREA_LARGE_THRESHOLD,
    BUILDING_AREA_HEIGHTS,
    compute_scale,
    get_area_class,
    estimate_building_height_from_area,
)


class TestComputeScale:

    def test_square_area(self):
        scale = compute_scale(1000.0, 1000.0)
        assert scale == pytest.approx(INTERNAL_SPAN_MM / 1000.0)

    def test_rectangular_area_width_dominant(self):
        scale = compute_scale(2000.0, 500.0)
        assert scale == pytest.approx(INTERNAL_SPAN_MM / 2000.0)

    def test_rectangular_area_height_dominant(self):
        scale = compute_scale(500.0, 2000.0)
        assert scale == pytest.approx(INTERNAL_SPAN_MM / 2000.0)

    def test_equal_dimensions(self):
        s1 = compute_scale(500.0, 500.0)
        s2 = compute_scale(500.0, 500.0)
        assert s1 == s2


class TestGetAreaClass:

    def test_small(self):
        assert get_area_class(1.0) == "small"
        assert get_area_class(AREA_SMALL_THRESHOLD - 0.1) == "small"

    def test_medium(self):
        assert get_area_class(AREA_SMALL_THRESHOLD) == "medium"
        assert get_area_class(AREA_LARGE_THRESHOLD - 0.1) == "medium"

    def test_large(self):
        assert get_area_class(AREA_LARGE_THRESHOLD) == "large"
        assert get_area_class(500.0) == "large"


class TestEstimateBuildingHeight:

    def test_smallest_tier(self):
        h = estimate_building_height_from_area(50.0)
        assert h == 8.0

    def test_mid_tiers(self):
        assert estimate_building_height_from_area(150.0) == 10.0
        assert estimate_building_height_from_area(300.0) == 15.0
        assert estimate_building_height_from_area(700.0) == 25.0
        assert estimate_building_height_from_area(1500.0) == 40.0

    def test_skyscraper_fallback(self):
        assert estimate_building_height_from_area(5000.0) == 60.0

    def test_boundary_values(self):
        thresholds = sorted(BUILDING_AREA_HEIGHTS.keys())
        for t in thresholds:
            h_below = estimate_building_height_from_area(t - 1)
            assert h_below == BUILDING_AREA_HEIGHTS[t]
