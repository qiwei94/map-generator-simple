import pandas as pd
import pytest
from shapely.geometry import box

from _TEXTURE_STYLE_OF_DEEPSEEK._landmark import LandmarkCategory
from _TEXTURE_STYLE_OF_DEEPSEEK._layer_preprocess import (
    BUILDING_HEIGHT_ROLE_POLICY_VERSION,
    _apply_subtraction_and_filter,
    _resolve_building_height_role,
)


def _height(row, *, height, threshold=50.0,
            category=LandmarkCategory.GEOMETRIC):
    return _resolve_building_height_role(
        pd.Series(row),
        box(0, 0, 40, 40),
        area_m2=1600.0,
        est_height_m=height,
        height_top_thr=threshold,
        category=category,
        hotspot=False,
        height_ceiling_m=300.0,
        layer_height_mm=0.12,
        narrow_threshold=6.0,
        narrow_penalty_factor=0.5,
    )


def test_policy_version_is_explicit():
    assert BUILDING_HEIGHT_ROLE_POLICY_VERSION == "identity-anchor-background-v2"


def test_identity_with_verified_height_uses_exact_z():
    height_mm, role = _height(
        {
            "building": "stadium",
            "height_source": "wikidata",
            "wikidata": "Q123",
        },
        height=100.0,
        category=LandmarkCategory.URBAN_HUB,
    )

    assert role == "identity_exact"
    assert height_mm > 3.5
    assert height_mm / 0.12 == pytest.approx(round(height_mm / 0.12))


def test_reliable_anonymous_vertical_outlier_is_visual_anchor():
    height_mm, role = _height(
        {"building": "yes", "height_source": "osm_height"},
        height=120.0,
        threshold=80.0,
    )

    assert role == "visual_anchor_exact"
    assert height_mm > 3.5


def test_anonymous_level_estimates_share_quiet_background_band():
    low_mm, low_role = _height(
        {"building": "yes", "height_source": "osm_levels"},
        height=24.0,
        threshold=80.0,
    )
    high_mm, high_role = _height(
        {"building": "yes", "height_source": "osm_levels"},
        height=120.0,
        threshold=80.0,
    )

    assert low_role == high_role == "background_stylized"
    assert low_mm == high_mm == pytest.approx(3.12)


def test_ordinary_named_office_does_not_receive_exact_z():
    height_mm, role = _height(
        {
            "building": "office",
            "name": "Example Office",
            "height_source": "osm_levels",
        },
        height=90.0,
        threshold=80.0,
    )

    assert role == "background_stylized"
    assert height_mm == pytest.approx(3.12)


def test_identity_without_verified_height_is_stylized_not_fake_exact():
    height_mm, role = _height(
        {
            "building": "church",
            "height_source": "default",
            "historic": "yes",
        },
        height=10.0,
        category=LandmarkCategory.SPIRITUAL,
    )

    assert role == "identity_stylized"
    assert height_mm > 3.12


def test_precision_filter_keeps_categories_and_height_roles_aligned():
    small = box(0, 0, 5, 5)
    large = box(20, 20, 40, 40)
    result = _apply_subtraction_and_filter(
        [(small, 4.0), (large, 3.12)],
        [LandmarkCategory.SPIRITUAL, LandmarkCategory.GEOMETRIC],
        [], [], [], [], [], set(), 100.0,
        BL_height_roles=["identity_exact", "background_stylized"],
    )

    assert result["BL"] == [(large, 3.12)]
    assert result["BL_categories"] == [LandmarkCategory.GEOMETRIC]
    assert result["BL_height_roles"] == ["background_stylized"]
