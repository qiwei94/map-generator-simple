"""Tests for city_profile.py."""

import numpy as np
import pytest

from _TEXTURE_STYLE_OF_DEEPSEEK.auto_params.city_profile import (
    CityProfile,
    detect_city_profile,
    _detect_relief,
    _assess_osm_quality,
)


class TestDetectRelief:
    def test_flat_terrain(self):
        grid = np.ones((100, 100)) * 50  # uniform elevation
        elev_range, relief = _detect_relief(grid, 25e6)  # 25 km²
        assert relief == "flat"
        assert elev_range == 0.0

    def test_moderate_terrain(self):
        grid = np.linspace(0, 200, 100 * 100).reshape(100, 100)
        # bbox 25 km² → diagonal ≈ 7071m
        # relief ratio = 200 / 7071 ≈ 0.028 → moderate
        elev_range, relief = _detect_relief(grid, 25e6)
        assert relief == "moderate"
        assert abs(elev_range - 200.0) < 0.1

    def test_mountainous_terrain(self):
        grid = np.linspace(0, 1000, 100 * 100).reshape(100, 100)
        # bbox 4 km² → diagonal ≈ 2828m
        # relief ratio = 1000 / 2828 ≈ 0.354 → mountainous
        elev_range, relief = _detect_relief(grid, 4e6)
        assert relief == "mountainous"


class TestOsmQuality:
    def test_good_quality(self):
        assert _assess_osm_quality(800, 0.4, 12.0) == "good"

    def test_fair_quality(self):
        assert _assess_osm_quality(300, 0.15, 6.0) == "fair"

    def test_poor_quality(self):
        assert _assess_osm_quality(50, 0.05, 2.0) == "poor"


class TestDetectCityProfile:
    def test_basic_profile(self):
        elevation = np.linspace(0, 456, 841 * 1024).reshape(841, 1024)

        profile = detect_city_profile(
            bbox_area_km2=685.0,
            elevation_grid=elevation,
            buildings_gdf=None,
            roads_gdf=None,
            water_gdf=None,
            vegetation_gdf=None,
            bbox_local_area_m2=685e6,
        )

        assert isinstance(profile, CityProfile)
        assert profile.area_km2 == 685.0
        assert profile.elevation_range_m == pytest.approx(456.0, abs=1.0)
        assert profile.building_density == 0.0
        assert profile.is_coastal is False

    def test_to_dict(self):
        profile = CityProfile(
            area_km2=100,
            elevation_range_m=200,
            relief_ratio="moderate",
            water_ratio=0.08,
            building_density=500,
            avg_building_area_m2=150,
            height_tag_coverage=0.12,
            road_density_km_per_km2=9.0,
            vegetation_ratio=0.15,
            is_coastal=False,
            osm_quality="fair",
        )
        d = profile.to_dict()
        assert d["area_km2"] == 100
        assert d["relief_ratio"] == "moderate"
