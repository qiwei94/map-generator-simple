"""Tests for param_resolver.py."""

import pytest

from _TEXTURE_STYLE_OF_DEEPSEEK.auto_params.city_profile import CityProfile
from _TEXTURE_STYLE_OF_DEEPSEEK.auto_params.param_resolver import (
    ResolvedParams,
    resolve_params,
    explain_decisions,
)


def _make_profile(**kwargs) -> CityProfile:
    """Helper to create a CityProfile with defaults."""
    defaults = dict(
        area_km2=150,
        elevation_range_m=200,
        relief_ratio="moderate",
        water_ratio=0.08,
        building_density=500,
        avg_building_area_m2=200,
        height_tag_coverage=0.12,
        road_density_km_per_km2=9.0,
        vegetation_ratio=0.15,
        is_coastal=False,
        osm_quality="fair",
    )
    defaults.update(kwargs)
    return CityProfile(**defaults)


class TestStyleSelection:
    def test_mountainous_with_water(self):
        p = _make_profile(relief_ratio="mountainous", water_ratio=0.10)
        params = resolve_params(p)
        assert params.style == "terrain-first"

    def test_water_dominant(self):
        p = _make_profile(water_ratio=0.20)
        params = resolve_params(p)
        assert params.style == "water-first"

    def test_dense_urban(self):
        p = _make_profile(building_density=3000, road_density_km_per_km2=15)
        params = resolve_params(p)
        assert params.style == "classic"

    def test_default_classic(self):
        p = _make_profile()
        params = resolve_params(p)
        assert params.style == "classic"


class TestTerrainParams:
    def test_flat_amplifies_gamma(self):
        p = _make_profile(elevation_range_m=30)
        params = resolve_params(p)
        assert params.z_gamma == 0.60

    def test_normal_gamma(self):
        p = _make_profile(elevation_range_m=200)
        params = resolve_params(p)
        assert params.z_gamma == 0.45

    def test_mountainous_compresses_gamma(self):
        p = _make_profile(elevation_range_m=600)
        params = resolve_params(p)
        assert params.z_gamma == 0.35
        assert params.terrain_thickness_mm == 5.0


class TestBuildingParams:
    def test_flat_mode_low_coverage(self):
        p = _make_profile(height_tag_coverage=0.10)
        params = resolve_params(p)
        assert params.flat_mode is True

    def test_height_mode_high_coverage(self):
        p = _make_profile(height_tag_coverage=0.50)
        params = resolve_params(p)
        assert params.flat_mode is False

    def test_hyper_dense_raises_threshold(self):
        p = _make_profile(building_density=3000)
        params = resolve_params(p)
        assert params.building_density_threshold == 0.01

    def test_sparse_lowers_threshold(self):
        p = _make_profile(building_density=100)
        params = resolve_params(p)
        assert params.building_density_threshold == 0.001


class TestUserOverrides:
    def test_override_takes_priority(self):
        p = _make_profile(elevation_range_m=30)  # would give gamma=0.60
        params = resolve_params(p, user_overrides={"z_gamma": 0.50})
        assert params.z_gamma == 0.50
        assert "user override" in params.reasons["z_gamma"]


class TestExplainDecisions:
    def test_output_structure(self):
        p = _make_profile()
        params = resolve_params(p)
        report = explain_decisions(p, params)

        assert "detected_features" in report
        assert "style_selected" in report
        assert "params_applied" in report
        assert report["style_selected"] == "classic"
        assert "z_gamma" in report["params_applied"]
        assert "value" in report["params_applied"]["z_gamma"]
        assert "reason" in report["params_applied"]["z_gamma"]
