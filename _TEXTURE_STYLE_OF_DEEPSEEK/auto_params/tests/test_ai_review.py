"""Tests for ai_review.py — parse/clamp logic (no real API calls)."""

import pytest

from _TEXTURE_STYLE_OF_DEEPSEEK.auto_params.ai_review import (
    _parse_review,
    _apply_suggestions,
    ReviewResult,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.auto_params.param_resolver import ResolvedParams


class TestParseReview:
    def test_valid_json(self):
        raw = '''
        {
          "scores": {"balance": 4, "density": 3, "readability": 4, "style": 3, "emphasis": 4, "overall": 4},
          "issues": ["西南角建筑过密"],
          "suggestions": [
            {"param": "building_density_threshold", "direction": "increase", "magnitude": "slight", "reason": "reduce noise"}
          ]
        }
        '''
        result = _parse_review(raw)
        assert result.overall == 4
        assert len(result.suggestions) == 1
        assert result.suggestions[0]["param"] == "building_density_threshold"

    def test_markdown_fenced_json(self):
        raw = '''```json
        {"scores": {"overall": 3}, "issues": [], "suggestions": []}
        ```'''
        result = _parse_review(raw)
        assert result.overall == 3

    def test_invalid_json_returns_empty(self):
        result = _parse_review("not json at all")
        assert result.overall == 0
        assert result.suggestions == []


class TestApplySuggestions:
    def test_increase_param(self):
        params = ResolvedParams(z_gamma=0.45)
        suggestions = [
            {"param": "z_gamma", "direction": "increase", "magnitude": "moderate"}
        ]
        adjusted = _apply_suggestions(params, suggestions)
        # moderate increase: 0.45 + (0.70 - 0.45) * 0.25 = 0.5125
        assert adjusted.z_gamma > 0.45
        assert adjusted.z_gamma <= 0.70

    def test_decrease_param(self):
        params = ResolvedParams(road_width_multiplier=5.0)
        suggestions = [
            {"param": "road_width_multiplier", "direction": "decrease", "magnitude": "strong"}
        ]
        adjusted = _apply_suggestions(params, suggestions)
        assert adjusted.road_width_multiplier < 5.0
        assert adjusted.road_width_multiplier >= 2.0

    def test_clamp_upper_bound(self):
        params = ResolvedParams(z_gamma=0.68)
        suggestions = [
            {"param": "z_gamma", "direction": "increase", "magnitude": "strong"}
        ]
        adjusted = _apply_suggestions(params, suggestions)
        assert adjusted.z_gamma <= 0.70

    def test_max_three_suggestions(self):
        params = ResolvedParams()
        suggestions = [
            {"param": "z_gamma", "direction": "increase", "magnitude": "slight"},
            {"param": "road_width_multiplier", "direction": "decrease", "magnitude": "slight"},
            {"param": "vegetation_min_area_m2", "direction": "increase", "magnitude": "slight"},
            {"param": "building_density_threshold", "direction": "increase", "magnitude": "slight"},
        ]
        adjusted = _apply_suggestions(params, suggestions)
        # Only first 3 should be applied
        assert adjusted.building_density_threshold == params.building_density_threshold

    def test_unknown_param_ignored(self):
        params = ResolvedParams(z_gamma=0.45)
        suggestions = [
            {"param": "nonexistent_param", "direction": "increase", "magnitude": "moderate"}
        ]
        adjusted = _apply_suggestions(params, suggestions)
        assert adjusted.z_gamma == 0.45
