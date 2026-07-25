"""Tests for decision_report.py."""

import json
import os
import tempfile

import pytest

from _TEXTURE_STYLE_OF_DEEPSEEK.auto_params.city_profile import CityProfile
from _TEXTURE_STYLE_OF_DEEPSEEK.auto_params.param_resolver import resolve_params
from _TEXTURE_STYLE_OF_DEEPSEEK.auto_params.decision_report import save_decision_report


class TestSaveDecisionReport:
    def test_writes_valid_json(self):
        profile = CityProfile(
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
        params = resolve_params(profile)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = save_decision_report(profile, params, tmpdir, "test_city")

            assert os.path.exists(path)
            with open(path, encoding="utf-8") as f:
                data = json.load(f)

            assert data["city"] == "test_city"
            assert "timestamp" in data
            assert "detected_features" in data
            assert "style_selected" in data
            assert "params_applied" in data
            assert data["detected_features"]["area_km2"] == 150
            assert data["style_selected"] == "classic"
