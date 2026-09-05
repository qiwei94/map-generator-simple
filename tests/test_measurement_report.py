import json
from pathlib import Path

import pytest

from aesthetic.measurement_report import (
    FAMILY_STATUSES,
    REQUIRED_FAMILIES,
    SCHEMA_VERSION,
    build_measurement_report,
    write_measurement_report,
)


def _scene_character():
    return {
        "version": "scene-character-v6",
        "summary": {
            "traits": ["grid_structure", "coastal_city"],
            "relative_thresholds": {"dense_cell_p75": 0.42},
        },
        "feature_counts": {
            "buildings": 701_972,
            "roads": 247_385,
            "water": 1_655,
        },
        "metrics": {
            "buildings": {
                "coverage_ratio": 0.1786,
                "sub_nozzle_short_axis_fraction": 0.99566,
                "block_grammar": {
                    "status": "ready",
                    "printable_comparable_sample": {
                        "metrics": {"short_axis_mm": {"p50": 0.31}},
                    },
                },
            },
            "building_data_quality": {
                "status": "ready",
                "completeness_score": 0.8851,
            },
            "road_structure": {
                "grid_score": 0.7034,
                "junction_count": 12_345,
            },
            "water_topology": {
                "coast_score": 0.9908,
                "river_axis_score": 0.6454,
            },
            "terrain": {
                "relief_m": 25.79,
                "slope_p90_deg": 0.572,
                "landform": {"open_plain_score": 0.8358},
            },
            "data_confidence": {"status": "ready"},
        },
        "cells": [
            {"row": 0, "column": 0, "role": "coast", "urban_signal": 0.7},
            {"row": 0, "column": 1, "role": "dense_core", "urban_signal": 0.9},
        ],
        "future_measurement": {
            "sentinel": "must-survive",
            "nested": [17, {"leaf": "also-survives"}],
            "empty_mapping": {},
            "empty_sequence": [],
        },
    }


def _scene_policy():
    return {
        "policy_version": "scene-policy-v6",
        "activation": "active",
        "scene_class": "urban",
        "archetype": "coast_grid_compact_core",
        "scores": {"coast": 0.9908, "grid": 0.7034},
        "roles": {
            "roads": {"visible_budget": 3_089},
            "buildings": {"simplification_mode": "select_and_aggregate"},
        },
    }


def _build_report(**overrides):
    inputs = {
        "run": {
            "city": "Chicago",
            "bbox": [41.7650535, -87.8587977, 41.9911465, -87.5571755],
            "model_span_mm": 196.0,
            "nested": {"list": [1, {"deep": True}]},
        },
        "source_features": {
            "buildings": 701_972,
            "roads": 247_385,
            "nested": {"verified_nonzero": True},
        },
        "scene_character": _scene_character(),
        "scene_policy": _scene_policy(),
        "auto_parameter_evidence": {
            "profile": "scene-adaptive",
            "resolved": {"road_budget": 3_089},
        },
        "preprocess_evidence": {
            "road_roles": {"primary": 31, "secondary": 317},
            "water_roles": {"visible_polygon_count": 10},
        },
        "generation_outcomes": {
            "building_mass": {"status": "active", "output_components": 2_609},
            "height_hierarchy": {"status": "not_applicable"},
            "height_emphasis": {"status": "inactive"},
            "validation": {"status": "pending"},
        },
        "generated_at": "2026-08-30T00:00:00+00:00",
    }
    inputs.update(overrides)
    return build_measurement_report(**inputs)


def _entry(report, tree, path):
    return next(
        item for item in report["measurement_index"]
        if item.get("tree") == tree and item["path"] == path
    )


def _impact(report, impact_id):
    return next(
        item for item in report["impact_chains"]
        if item["id"] == impact_id
    )


def test_every_input_leaf_is_losslessly_present_in_search_index():
    report = _build_report()

    expected = {
        "run.city": "Chicago",
        "run.nested.list[1].deep": True,
        "source_features.nested.verified_nonzero": True,
        "scene_character.future_measurement.sentinel": "must-survive",
        "scene_character.future_measurement.nested[1].leaf": "also-survives",
        "scene_character.future_measurement.empty_mapping": {},
        "scene_character.future_measurement.empty_sequence": [],
        "preprocess_evidence.road_roles.primary": 31,
    }
    for path, value in expected.items():
        assert _entry(report, "inputs", path)["value"] == value

    input_entries = [
        item for item in report["measurement_index"]
        if item.get("tree") == "inputs"
    ]
    assert report["coverage"]["leaf_counts_by_tree"]["inputs"] == len(
        input_entries)


def test_search_index_covers_inputs_decisions_and_realized_outcomes():
    report = _build_report()

    assert {
        item.get("tree") for item in report["measurement_index"]
    } == {"inputs", "decisions", "outcomes"}
    assert _entry(
        report, "decisions", "scene_policy.activation")["value"] == "active"
    assert _entry(
        report, "decisions", "auto_parameters.resolved.road_budget"
    )["value"] == 3_089
    assert _entry(
        report, "outcomes", "building_mass.output_components"
    )["value"] == 2_609
    assert _entry(
        report, "outcomes", "validation.status")["value"] == "pending"
    assert report["coverage"]["leaf_counts_by_tree"] == {
        tree: sum(
            item.get("tree") == tree
            for item in report["measurement_index"]
        )
        for tree in ("inputs", "decisions", "outcomes")
    }


def test_failed_mass_filter_keeps_source_loss_visible_in_admin_report():
    report = _build_report(generation_outcomes={
        "building_mass": {
            "status": "guarded_fallback",
            "candidate": {
                "source_clearance_audit": {
                    "input_footprints": 100,
                    "footprints_surviving_clearance": 42,
                    "clearance_source_area_retention": 0.36,
                    "shaping_source_area_retention": 0.5,
                },
                "source_fidelity_filter": {
                    "experimental_filter_enabled": False,
                },
            },
        },
    })
    assert _entry(report, "outcomes", "building_mass.status")["value"] == (
        "guarded_fallback")
    for name, value in (
        ("input_footprints", 100), ("footprints_surviving_clearance", 42),
        ("clearance_source_area_retention", 0.36),
        ("shaping_source_area_retention", 0.5),
    ):
        assert _entry(report, "outcomes",
                      "building_mass.candidate.source_clearance_audit." + name)[
                          "value"] == value


def test_secrets_are_redacted_from_all_report_surfaces(tmp_path):
    secret_values = {
        "password": "mail-password-value",
        "worker_token": "worker-token-value",
        "authorization": "Bearer private-value",
        "api_secret": "api-secret-value",
    }
    report = _build_report(
        run={"city": "Chicago", "credentials": secret_values},
        preprocess_evidence={
            "nested": {"worker_token": "nested-worker-token-value"},
        },
    )
    paths = write_measurement_report(tmp_path, report)
    json_text = Path(paths["json"]).read_text(encoding="utf-8")
    html_text = Path(paths["html"]).read_text(encoding="utf-8")

    for secret in (*secret_values.values(), "nested-worker-token-value"):
        assert secret not in json_text
        assert secret not in html_text
    assert json_text.count("[redacted]") >= 5


def test_html_is_chinese_searchable_and_escapes_script_payload(tmp_path):
    injection = "</script><script>window.reportOwned=true</script>"
    report = _build_report(
        run={"city": "芝加哥", "untrusted_label": injection},
    )
    paths = write_measurement_report(tmp_path, report)
    html_text = Path(paths["html"]).read_text(encoding="utf-8")

    assert "地图模型生成完整测量报告" in html_text
    assert "测量如何影响生成" in html_text
    assert "全部测量项" in html_text
    assert 'id="search"' in html_text
    assert 'id="rows"' in html_text
    assert "搜索路径、数值或影响类别" in html_text
    assert injection not in html_text
    assert "\\u003c/script\\u003e\\u003cscript\\u003e" in html_text
    assert "实际执行模块" in html_text
    assert "规范化测量族（技术字段）" in html_text

    loaded = json.loads(Path(paths["json"]).read_text(encoding="utf-8"))
    assert loaded["raw_measurements"]["run"]["untrusted_label"] == injection
    assert loaded["language"] == "zh-CN"
    assert loaded["status_label_zh"] == "已完成测量"
    assert loaded["labels_zh"]["families"]["block_base"] == "街区底座"


@pytest.mark.parametrize("status", ["done", "complete", "", None, 17])
def test_unknown_status_is_rejected(status):
    with pytest.raises(ValueError, match="unsupported measurement report status"):
        _build_report(status=status)


def test_road_and_water_measurements_do_not_claim_geometry_application():
    report = _build_report(
        generation_outcomes={
            "building_mass": {"status": "inactive"},
            "height_hierarchy": {"status": "not_applicable"},
            "height_emphasis": {"status": "inactive"},
        },
    )

    road = _impact(report, "road_structure")
    water = _impact(report, "water_topology")
    assert road["realized_status"] == "applied_indirect_policy"
    assert water["realized_status"] == "applied_indirect_policy"
    assert road["realized_status"] not in {
        "applied_bounded_geometry", "applied_hard_or_preprocess"
    }
    assert water["realized_status"] not in {
        "applied_bounded_geometry", "applied_hard_or_preprocess"
    }
    assert "不会回改本次道路" in road["effect"]
    assert "水体几何仍由" in water["effect"]


def test_chicago_sub_nozzle_height_demotion_is_reported_as_applied_geometry():
    """Chicago demotes real BL geometry even though height capping is N/A."""

    report = _build_report(
        generation_outcomes={
            "building_mass": {"status": "inactive"},
            "height_hierarchy": {
                "status": "not_applicable",
                "geometry_changed": True,
                "sub_nozzle_heroes_demoted_to_mass": 391,
                "minimum_independent_width_mm": 0.42,
            },
            "height_emphasis": {"status": "inactive"},
        },
    )

    building = _impact(report, "building_morphology")
    assert building["realized_status"] == "applied_bounded_geometry"
    assert _entry(
        report,
        "outcomes",
        "height_hierarchy.sub_nozzle_heroes_demoted_to_mass",
    )["value"] == 391


def test_required_measurement_families_never_silently_disappear():
    report = _build_report()

    assert report["schema_version"] == SCHEMA_VERSION
    assert report["schema_version"] == "pipeline-measurement-report-v2"
    for phase, names in REQUIRED_FAMILIES.items():
        assert set(report["measurements"][phase]) == set(names)
        for name in names:
            family = report["measurements"][phase][name]
            assert family["status"] in FAMILY_STATUSES
            assert "data" in family
    assert report["measurements"]["acceptance"]["validator"]["status"] == (
        "pending")
    assert report["measurements"]["acceptance"]["slicer"]["status"] == (
        "pending")


def test_canonical_indexes_cover_unknown_measurements_and_every_phase():
    report = _build_report()
    indexes = report["indexes"]

    assert set(indexes) == set(REQUIRED_FAMILIES)
    assert any(
        item["path"].endswith(
            "scene_character.data.future_measurement.sentinel")
        and item["value"] == "must-survive"
        for item in indexes["inputs"]
    )
    assert report["coverage"]["canonical_leaf_counts_by_phase"] == {
        phase: len(items) for phase, items in indexes.items()
    }


def test_realization_matrix_names_unconsumed_and_actual_consumers():
    report = _build_report()
    matrix = {
        item["effect_id"]: item for item in report["realization_matrix"]
    }

    assert matrix["auto.elevation_smoothing"]["realization_status"] == (
        "declared_unconsumed")
    assert matrix["scene.roads_declared"]["applied_to_current_run"] is False
    assert matrix["scene.water_declared"]["applied_to_current_run"] is False
    assert matrix["preprocess.road_roles"]["applied_to_current_run"] is True
    assert matrix["preprocess.water_roles"]["applied_to_current_run"] is True
    assert matrix["acceptance.validator"]["realization_status"] == "pending"
