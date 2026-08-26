import pytest
import json

from _TEXTURE_STYLE_OF_DEEPSEEK.print_profile import (
    DEFAULT_PRINTER_PROFILE,
    PrinterProfile,
    PrintScale,
    build_printability_report,
    quantize_thickness_mm,
)
from generate_city_legacy import _load_printer_profile


@pytest.mark.parametrize(
    ("extent_m", "expected_nozzle_m"),
    [(5000.0, 5000.0 / 196.0 * 0.4),
     (15000.0, 15000.0 / 196.0 * 0.4),
     (25000.0, 25000.0 / 196.0 * 0.4)],
)
def test_real_nozzle_footprint_tracks_requested_extent(
    extent_m, expected_nozzle_m,
):
    scale = PrintScale(extent_m, extent_m)
    assert scale.model_mm_to_real_m(0.4) == pytest.approx(expected_nozzle_m)
    assert scale.real_m_to_model_mm(expected_nozzle_m) == pytest.approx(0.4)


def test_rectangular_extent_preserves_aspect_ratio():
    scale = PrintScale(25000.0, 15000.0)
    assert scale.model_width_mm == pytest.approx(196.0)
    assert scale.model_height_mm == pytest.approx(117.6)


def test_default_profile_matches_existing_nozzle_assumption():
    scale = PrintScale(25000.0, 25000.0)
    old_formula = 0.4 / scale.scale_mm_per_m
    report = build_printability_report(DEFAULT_PRINTER_PROFILE, scale)
    assert report["derived_xy_real_m"]["nozzle_diameter"] == pytest.approx(
        old_formula)


def test_final_block_base_gap_reserves_two_extrusion_lines():
    assert DEFAULT_PRINTER_PROFILE.final_block_base_gap_mm == pytest.approx(0.84)
    custom = PrinterProfile(
        profile_id="wide-gap",
        min_gap_mm=1.1,
    )
    assert custom.final_block_base_gap_mm == pytest.approx(1.1)


@pytest.mark.parametrize(
    ("value", "mode", "expected"),
    [(0.4, "ceil", 0.48),
     (0.5, "nearest", 0.48),
     (0.5, "ceil", 0.60),
     (0.01, "floor", 0.24)],
)
def test_thickness_quantization(value, mode, expected):
    assert quantize_thickness_mm(
        value, 0.12, mode=mode, min_layers=2) == pytest.approx(expected)


def test_zero_thickness_can_remain_disabled():
    assert quantize_thickness_mm(0, 0.12, min_layers=0) == 0


@pytest.mark.parametrize("kwargs", [
    {"nozzle_diameter_mm": 0},
    {"layer_height_mm": -0.1},
    {"extrusion_width_mm": 0.2},
    {"min_colored_strip_mm": 0.2},
    {"min_surface_layers": 0},
])
def test_profile_rejects_invalid_physical_constraints(kwargs):
    with pytest.raises(ValueError):
        PrinterProfile(**kwargs)


def test_report_separates_xy_scale_from_semantic_z():
    report = build_printability_report(
        DEFAULT_PRINTER_PROFILE,
        PrintScale(15000, 15000),
        current_thresholds={"min_printable_area_m2": 4000},
        z_thicknesses_mm={"road_thickness_mm": 0.4},
    )
    assert report["printer_profile"]["profile_id"] == "fdm-0.4-balanced-v1"
    assert report["derived_z_model_mm"]["min_surface_height"] == pytest.approx(0.24)
    assert report["current_pipeline_thresholds"]["min_printable_area_m2"] == 4000
    road = report["z_layer_audit"]["road_thickness_mm"]
    assert road["requested_mm"] == 0.4
    assert road["nearest_layer_grid_mm"] == pytest.approx(0.36)
    assert road["safe_quantized_mm"] == pytest.approx(0.48)
    assert road["on_layer_grid"] is False
    assert any("not treated as real-world" in note for note in report["notes"])


def test_cli_profile_loader_accepts_calibrated_json(tmp_path):
    path = tmp_path / "printer.json"
    path.write_text(json.dumps({
        "profile_id": "test-0.6",
        "nozzle_diameter_mm": 0.6,
        "extrusion_width_mm": 0.65,
        "layer_height_mm": 0.2,
        "min_colored_strip_mm": 0.9,
        "min_gap_mm": 0.75,
        "min_surface_layers": 2,
    }), encoding="utf-8")

    profile = _load_printer_profile(path)

    assert profile.profile_id == "test-0.6"
    assert profile.nozzle_diameter_mm == pytest.approx(0.6)


def test_cli_profile_loader_rejects_unknown_fields(tmp_path):
    path = tmp_path / "bad-printer.json"
    path.write_text('{"unknown": 1}', encoding="utf-8")
    with pytest.raises(SystemExit, match="invalid --printer-profile-json"):
        _load_printer_profile(path)
