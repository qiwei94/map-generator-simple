import json

import numpy as np

from aesthetic.pipeline_contract import (
    CONTRACT_VERSION,
    PIPELINE_STAGES,
    S10_REQUIRED_ARTIFACT_BUNDLE,
)
from aesthetic.pipeline_observation import (
    build_pipeline_observation_report,
    write_pipeline_observation,
)


class _Mesh:
    vertices = np.zeros((8, 3), dtype=float)
    faces = np.zeros((12, 3), dtype=int)
    bounds = np.asarray([[0, 0, 0], [196, 196, 4]], dtype=float)
    is_watertight = True


def _scene_character():
    return {
        "version": "scene-character-v6",
        "summary": {"traits": ["grid_structure"]},
        "feature_counts": {"buildings": 50_000},
        "metrics": {
            "buildings": {
                "block_grammar": {
                    "version": "source-block-grammar-v1",
                    "status": "ready",
                    "source_footprint_count": 50_000,
                    "spatial_fullness": {
                        "footprint_frame_coverage": 0.08,
                    },
                    "printable_comparable_sample": {
                        "sample_count": 400,
                        "estimated_population_count": 8_000,
                        "metrics": {
                            "short_axis_mm": {"p50": 0.31},
                            "solidity": {"p50": 0.91},
                            "rectangularity": {"p50": 0.78},
                            "perimeter_excess": {"p50": 1.08},
                        },
                        "orientation": {"orthogonal_coherence": 0.73},
                    },
                    "reference_envelope": {"authority": "soft only"},
                },
            },
            "building_data_quality": {"status": "ready"},
        },
    }


def _scene_policy():
    return {
        "policy_version": "scene-policy-v6",
        "activation": "active",
        "scene_class": "urban",
        "archetype": "coast_grid_compact_core",
        "dominant_structure": {"primary": "coast"},
        "scores": {"grid": 0.73, "coast": 0.88},
        "roles": {"buildings": {
            "simplification_mode": "select_aggregate_and_regularize",
            "block_grammar_strategy": {
                "status": "ready",
                "mode": "select_and_aggregate_complete_dense_source",
                "aggregation_pressure": 0.82,
                "outline_regularization_pressure": 0.31,
                "orientation_preservation_pressure": 0.73,
                "texture_promotion_pressure": 0.0,
            },
        }},
        "tradeoff_order": {"hard_constraints": ["source_truth"]},
        "decision_contract": {
            "forbidden_controls": ["mesh vertices", "global Z values"],
        },
    }


def test_pipeline_observation_contains_measurement_strategy_and_all_stages(
        tmp_path):
    artifact = tmp_path / "city.3mf"
    artifact.write_bytes(b"3mf-evidence")
    design_spec = tmp_path / "design_spec.json"
    design_spec.write_text("{}")
    artifacts = {"3mf": artifact, "design_spec": design_spec}
    for name in S10_REQUIRED_ARTIFACT_BUNDLE:
        if name in artifacts:
            continue
        suffix = ".html" if name.endswith("_html") else ".json"
        path = tmp_path / f"{name}{suffix}"
        path.write_text(f"artifact:{name}")
        artifacts[name] = path
    report = build_pipeline_observation_report(
        run={
            "city": "Chicago",
            "model_span_mm": 196.0,
            "source_path": tmp_path / "private-cache" / "chicago.osm.pbf",
            "windows_height_store": r"C:\private\height\chicago.sqlite",
            "source_error": f"cannot open {tmp_path}/secret/source.osm.pbf",
        },
        source_features={"buildings": 50_000, "roads": 1_000},
        printable_features={"buildings": 900},
        composition_spec={"schema_version": "composition-spec-v1"},
        scene_character=_scene_character(),
        scene_policy=_scene_policy(),
        building_mass_evidence={"status": "active", "output_components": 900},
        height_hierarchy_evidence={"status": "active"},
        height_emphasis_evidence={"status": "inactive"},
        meshes={"terrain": _Mesh(), "water": None},
        block_base_clearance={"status": "pass", "minimum_gap_mm": 0.84},
        artifacts=artifacts,
        validation={"status": "pending"},
        generated_at="2026-08-30T00:00:00+00:00",
        run_id="run-chicago-25km",
        attempt_id="attempt-03",
        revision=7,
        derived_from_revision="abc1234",
    )

    assert report["schema_version"] == "pipeline-observation-v1"
    assert report["language"] == "zh-CN"
    assert report["purpose"] == "生成后只读观测；不会成为几何控制面"
    assert report["contract_version"] == CONTRACT_VERSION
    assert report["run_id"] == "run-chicago-25km"
    assert report["attempt_id"] == "attempt-03"
    assert report["revision"] == 7
    assert report["derived_from_revision"] == "abc1234"
    assert report["run_identity"] == {
        "run_id": "run-chicago-25km",
        "attempt_id": "attempt-03",
        "revision": 7,
        "derived_from_revision": "abc1234",
    }
    assert [stage["id"] for stage in report["stages"]] == [
        f"S{index}" for index in range(12)]
    assert [stage["name"] for stage in report["stages"]] == [
        stage.name for stage in PIPELINE_STAGES]
    assert [stage["input"] for stage in report["stages"]] == [
        list(stage.inputs) for stage in PIPELINE_STAGES]
    assert [stage["output"] for stage in report["stages"]] == [
        list(stage.outputs) for stage in PIPELINE_STAGES]
    assert [stage["context_in"] for stage in report["stages"]] == [
        stage.context_in for stage in PIPELINE_STAGES]
    assert [stage["context_out"] for stage in report["stages"]] == [
        stage.context_out for stage in PIPELINE_STAGES]
    assert all(stage["status_source"]["reference"]
               for stage in report["stages"])
    assert report["measurement_summary"]["short_axis_p50_mm"] == 0.31
    assert report["generation_strategy"]["block_grammar_strategy"][
        "mode"] == "select_and_aggregate_complete_dense_source"
    assert report["stages"][8]["evidence"]["meshes"]["terrain"][
        "watertight"] is True
    assert report["stages"][10]["status"] == "completed"
    assert report["stages"][11]["status"] == "pending_validation"
    assert report["delivery_gates"]["generation"] == {
        "stage_id": "S10",
        "status": "generated",
        "generated": True,
        "bundle_complete": True,
        "required_artifacts": list(S10_REQUIRED_ARTIFACT_BUNDLE),
        "present_artifacts": sorted(S10_REQUIRED_ARTIFACT_BUNDLE),
        "status_source": {
            "kind": "artifact_evidence",
            "reference": "artifacts.3mf",
            "rule": "存在 3MF 只能证明导出完成，不能证明正式验收通过",
        },
    }
    assert report["delivery_gates"]["acceptance"]["status"] == (
        "pending_validation")
    assert report["delivery_gates"]["acceptance"]["accepted"] is None
    assert report["artifacts"]["3mf"] == {
        "filename": "city.3mf",
        "size_bytes": len(b"3mf-evidence"),
    }
    assert report["run"]["source_path"] == "chicago.osm.pbf"
    assert report["run"]["windows_height_store"] == "chicago.sqlite"
    assert report["run"]["source_error"] == "cannot open source.osm.pbf"
    assert str(tmp_path) not in json.dumps(report)
    assert r"C:\private\height" not in json.dumps(report)


def test_pipeline_observation_keeps_only_explicit_relative_artifact_path():
    report = build_pipeline_observation_report(
        run={"city": "Chicago"},
        source_features={}, printable_features={}, composition_spec={},
        scene_character=_scene_character(), scene_policy=_scene_policy(),
        building_mass_evidence={}, height_hierarchy_evidence={},
        height_emphasis_evidence={}, meshes={}, block_base_clearance=None,
        artifacts={"review": "review/dense_detail_topdown.png"},
    )

    assert report["artifacts"]["review"] == {
        "filename": "dense_detail_topdown.png",
        "relative_path": "review/dense_detail_topdown.png",
    }


def test_pipeline_observation_writes_machine_and_human_reports(tmp_path):
    report = build_pipeline_observation_report(
        run={"city": "杭州", "model_span_mm": 196.0},
        source_features={}, printable_features={}, composition_spec={},
        scene_character=_scene_character(), scene_policy=_scene_policy(),
        building_mass_evidence={"status": "active"},
        height_hierarchy_evidence={}, height_emphasis_evidence={},
        meshes={"terrain": _Mesh()}, block_base_clearance=None,
        artifacts={}, generated_at="2026-08-30T00:00:00+00:00",
    )
    paths = write_pipeline_observation(tmp_path, report)

    loaded = json.loads((tmp_path / "pipeline_observation.json").read_text())
    page = (tmp_path / "pipeline_observation.html").read_text()
    assert loaded["run"]["city"] == "杭州"
    assert paths["json"].endswith("pipeline_observation.json")
    assert paths["html"].endswith("pipeline_observation.html")
    assert "S11" in page
    assert "S10 · 生成交付物" in page
    assert "S11 · 正式验收" in page
    assert "上下文交接" in page
    assert "状态来源" in page
    assert "生成成功不代表可打印验收通过" in page
    assert "本次生成策略" in page
    assert "生成过程观测报告" in page
    assert "pipeline-observation" in page


def test_pipeline_observation_accepts_only_formal_acceptance_report(
        tmp_path):
    artifact = tmp_path / "city.3mf"
    artifact.write_bytes(b"3mf")
    report = build_pipeline_observation_report(
        run={"city": "Chicago"},
        source_features={}, printable_features={}, composition_spec={},
        scene_character=_scene_character(), scene_policy=_scene_policy(),
        building_mass_evidence={}, height_hierarchy_evidence={},
        height_emphasis_evidence={}, meshes={"terrain": _Mesh()},
        block_base_clearance=None, artifacts={"3mf": artifact},
        validation={
            "schema_version": "pipeline-acceptance-v1",
            "accepted": True,
            "errors": [],
            "warnings": [],
            "validator": {
                "strict_passed": True, "errors": [], "warnings": []},
            "slicer": {
                "status": "passed", "errors": [], "warnings": []},
        },
        stage_statuses={
            "S4": {
                "status": "completed",
                "status_source": {
                    "kind": "pipeline_ledger",
                    "reference": "pipeline_state.json#S4",
                    "rule": "durable stage lifecycle",
                },
            },
        },
    )

    assert report["delivery_gates"]["generation"]["generated"] is True
    assert report["delivery_gates"]["generation"]["bundle_complete"] is False
    assert report["delivery_gates"]["acceptance"]["status"] == "accepted"
    assert report["stages"][11]["status"] == "completed"
    assert report["stages"][4]["status_source"]["kind"] == "pipeline_ledger"


def test_pipeline_observation_does_not_promote_validator_summary_to_s11():
    report = build_pipeline_observation_report(
        run={"city": "Chicago"},
        source_features={}, printable_features={}, composition_spec={},
        scene_character=_scene_character(), scene_policy=_scene_policy(),
        building_mass_evidence={}, height_hierarchy_evidence={},
        height_emphasis_evidence={}, meshes={}, block_base_clearance=None,
        artifacts={},
        validation={
            "strict_passed": True, "errors": [], "warnings": [],
        },
    )

    assert report["delivery_gates"]["acceptance"]["status"] == (
        "pending_validation")
    assert report["stages"][11]["status"] == "pending_validation"
