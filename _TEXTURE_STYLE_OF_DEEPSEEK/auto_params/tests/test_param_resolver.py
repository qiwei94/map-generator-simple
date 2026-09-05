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
    def test_vegetation_surface_defaults_off(self):
        assert resolve_params(_make_profile()).vegetation_enabled is False

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

    def test_distributed_external_city_corrects_false_wilderness(self):
        p = _make_profile(
            area_km2=625,
            relief_ratio="mountainous",
            water_ratio=0.12,
            building_density=52,
            road_density_km_per_km2=7.0,
            vegetation_ratio=0.43,
        )
        params = resolve_params(p, external_urban_evidence={
            "status": "evidence_only",
            "urban_network_support": 0.91,
            "road_presence_cell_fraction": 0.88,
        })

        assert params.style == "garden-city"
        assert params.building_v2_road_tier == 2
        assert params.building_density_threshold >= 0.003
        assert params.vegetation_enabled is False

    def test_single_external_corridor_does_not_override_landscape(self):
        p = _make_profile(
            area_km2=625,
            relief_ratio="mountainous",
            water_ratio=0.12,
            building_density=52,
            road_density_km_per_km2=7.0,
            vegetation_ratio=0.43,
        )
        params = resolve_params(p, external_urban_evidence={
            "status": "evidence_only",
            "urban_network_support": 0.38,
            "road_presence_cell_fraction": 0.20,
        })

        assert params.style == "terrain-first"


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

    def test_complete_dense_source_preserves_urban_grid_before_aggregation(self):
        p = _make_profile(
            osm_quality="good",
            building_density=615,
            avg_building_area_m2=168,
            height_tag_coverage=0.49,
            road_density_km_per_km2=21.4,
            water_ratio=0.34,
        )

        params = resolve_params(p)

        assert params.building_print_limit_m2 == 1000.0
        assert params.building_simplify_tol_m == 5.0
        assert params.building_v2_road_tier == 5
        assert params.road_width_multiplier == 2.0
        assert "preserve urban grid" in params.reasons[
            "building_print_limit_m2"]

    def test_dense_but_unreliable_source_keeps_guarded_defaults(self):
        p = _make_profile(
            osm_quality="fair",
            building_density=615,
            avg_building_area_m2=168,
            height_tag_coverage=0.49,
            road_density_km_per_km2=21.4,
        )

        params = resolve_params(p)

        assert params.building_print_limit_m2 == 2500.0
        assert params.building_simplify_tol_m == 25.0
        assert params.building_v2_road_tier == 4
        assert params.road_width_multiplier == 4.0


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
