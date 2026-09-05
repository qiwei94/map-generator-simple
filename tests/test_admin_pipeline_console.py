import json
import hashlib
from pathlib import Path
import sys
import time
import uuid

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
WEBAPP = ROOT / "webapp"
if str(WEBAPP) not in sys.path:
    sys.path.insert(0, str(WEBAPP))

from pipeline_console import (  # noqa: E402
    build_pipeline_console,
    write_pipeline_snapshot,
)
from aesthetic.pipeline_contract import (  # noqa: E402
    CONTRACT_VERSION,
    PIPELINE_STAGES,
    feature_source_counts_fingerprint,
)
from auth_store import AuthStore  # noqa: E402
from aesthetic.pipeline_gates import evaluate_feature_survival  # noqa: E402
import server  # noqa: E402


def _context_record(type_name, value):
    digest = hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    return {"type": type_name, "sha256": digest, "value": value}


def _required_value(spec):
    value = {key: f"test-{key}" for key in spec.required_context_keys}
    preprocess_parameters = {
        "schema_version": "preprocess-parameters-v1", "effective_overrides": {}}
    preprocess_fingerprint = hashlib.sha256(json.dumps(
        preprocess_parameters, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    source_counts = {name: (1 if name == "roads" else 0) for name in (
        "roads", "water", "buildings", "vegetation")}
    source_fingerprint = feature_source_counts_fingerprint(source_counts)
    terrain_fingerprint = "a" * 64
    if 2 <= spec.order <= 10:
        value.update(
            preprocess_parameters_fingerprint=preprocess_fingerprint,
            source_feature_counts_fingerprint=source_fingerprint,
            terrain_surface_fingerprint=terrain_fingerprint,
        )
    if 4 <= spec.order <= 10:
        value["scene_character_fingerprint"] = "b" * 64
    if 5 <= spec.order <= 10:
        value["scene_policy_fingerprint"] = "c" * 64
    if spec.id == "S0":
        value.update(
            city="console_city", mode="full",
            bbox_wgs84=[30.1, 120.0, 30.3, 120.3],
            scale_mm_per_m=0.04, printer_profile_id="test-printer")
    elif spec.id == "S1":
        value.update(
            raw_feature_counts={**source_counts, "landuse": 0},
            dem_evidence={
                "status": "ready", "source": "test", "flat_fallback": False},
            osmium_backend="native_osmium")
    elif spec.id == "S2":
        value.update(
            projected_feature_counts=source_counts,
            bbox_local_m=[0.0, 0.0, 1000.0, 1000.0],
            terrain_surface_plan={"fingerprint": terrain_fingerprint},
            preprocess_parameters=preprocess_parameters,
            preprocess_parameters_fingerprint=preprocess_fingerprint,
        )
    elif spec.id == "S3":
        value.update(
            layer_counts={
                "BL": 0, "BO": 0, "WL": 0, "WO": 0, "VL": 0,
                "VO": 0, "roads": 1, "block_base": 0},
            preprocess_policy_version="test-policy-v1")
    elif spec.id == "S4":
        value.update(
            scene_character_version="scene-character-v1",
            scene_status="ready", source_quality={})
    elif spec.id == "S5":
        value.update(
            scene_policy_version="scene-policy-v1", activation="audit_only",
            scene_class="urban", archetype="grid")
    elif spec.id == "S6":
        value.update(
            final_layer_counts={
                "BL": 0, "BO": 0, "WL": 0, "WO": 0, "VL": 0,
                "VO": 0, "roads": 1, "block_base": 0},
            building_mass_status="inactive",
            height_hierarchy_status="inactive")
    elif spec.id == "S7":
        value["review_artifacts"] = []
    elif spec.id == "S8":
        value.update(
            mesh_summary_version="semantic-mesh-summary-v2",
            mesh_summary={
                "terrain": {
                    "status": "ready", "vertices": 8, "faces": 12,
                    "watertight": True, "winding_consistent": True,
                    "bounds_mm": [
                        [0.0, 0.0, 0.0], [1.0, 1.0, 1.0]],
                },
                "roads": {
                    "status": "ready", "vertices": 6, "faces": 8,
                    "watertight": True, "winding_consistent": True,
                    "bounds_mm": [
                        [0.0, 0.0, 0.0], [1.0, 1.0, 0.2]],
                },
            },
            water_relief_status="not_applicable")
    elif spec.id == "S11":
        value["artifact_sha256"] = "d" * 64
    return value


def _advance_ledger_through_s10(ledger, *, artifact_sha256):
    previous = ledger["stages"][4]["context_out"]
    for spec in PIPELINE_STAGES[5:11]:
        value = _required_value(spec)
        if spec.id == "S9":
            value.update({"passed": True, "errors": [], "warnings": [],
                          "gate_version": "semantic-mesh-gate-v1",
                          "required_roles": ["terrain", "roads"],
                          "mesh_metrics": {
                              "terrain": {
                                  "present": True,
                                  "vertices": 8,
                                  "faces": 12,
                                  "watertight": True,
                                  "winding_consistent": True,
                                  "bounds_mm": [
                                      [0.0, 0.0, 0.0],
                                      [1.0, 1.0, 1.0]],
                              },
                              "roads": {
                                  "present": True,
                                  "vertices": 6,
                                  "faces": 8,
                                  "watertight": True,
                                  "winding_consistent": True,
                                  "bounds_mm": [
                                      [0.0, 0.0, 0.0],
                                      [1.0, 1.0, 0.2]],
                              },
                          },
                          "feature_survival": evaluate_feature_survival(
                              {"roads": 1}, {"roads": 1})})
        elif spec.id == "S10":
            value.update({
                "status": "generated_pending_validation",
                "artifact_bundle": [
                    "3mf", "design_spec", "measurement_report_json",
                    "measurement_report_html", "pipeline_observation_json",
                    "pipeline_observation_html",
                ],
                "s9_errors": 0, "s9_warnings": 0,
            })
        stage = ledger["stages"][spec.order]
        stage["status"] = "completed"
        stage["context_in"] = previous
        stage["context_out"] = _context_record(spec.context_out, value)
        previous = stage["context_out"]
    ledger["stages"][11]["status"] = "pending_validation"
    ledger["status"] = "generated_pending_validation"
    ledger["current_stage_id"] = "S11"
    ledger["artifacts"] = []
    for name in (
            "3mf", "design_spec", "measurement_report_json",
            "measurement_report_html", "pipeline_observation_json",
            "pipeline_observation_html"):
        filename = (
            "model.3mf" if name == "3mf" else
            "design_spec.json" if name == "design_spec" else
            f"{name}.html" if name.endswith("_html") else
            f"{name}.json"
        )
        ledger["artifacts"].append({
            "name": name, "stage_id": "S10", "path": filename,
            "size_bytes": 3,
            "sha256": artifact_sha256 if name == "3mf" else "d" * 64,
        })
    ledger["stages"][10]["artifacts"] = list(ledger["artifacts"])


def _job(tmp_path, *, status="running"):
    log = tmp_path / "job.log"
    log.write_text(
        "[Stage 4.5] Preprocessing layers\n"
        "BL=3 BO=280 WL=4 WO=8 block_base=92 roads=316\n",
        encoding="utf-8",
    )
    return {
        "id": "pipe1234", "city": "console_city",
        "city_title": "观测城市", "mode": "full", "status": status,
        "started": time.time() - 90, "ended": None,
        "log_path": str(log), "bbox": [30.1, 120.0, 30.3, 120.3],
        "worker_id": "windows-primary",
        "requirements": {
            "pbf_file": "/private/cache/zhejiang-latest.osm.pbf",
            "minimum_memory_mb": 12000,
            "worker_token": "must-not-leak",
        },
    }


def _ledger_payload(*, run_id, attempt_id, revision, marker,
                    updated_at="2026-08-30T01:00:00+00:00"):
    def context_value(spec):
        values = _required_value(spec)
        if spec.id == "S0":
            values.update({"city": "console_city", "mode": "full",
                           "bbox_wgs84": [0, 0, 1, 1],
                           "scale_mm_per_m": 0.01,
                           "printer_profile_id": "test"})
        return values

    stages = []
    previous = None
    for spec in PIPELINE_STAGES:
        status = (
            "completed" if spec.order < 5 else
            "running" if spec.id == "S5" else
            "pending_validation" if spec.id == "S11" else "pending"
        )
        context_in = previous if status in {"completed", "running"} else None
        if spec.id == "S0" and status in {"completed", "running"}:
            context_in = _context_record(spec.context_in, {"request": marker})
        context_out = None
        if status == "completed":
            context_out = _context_record(spec.context_out, context_value(spec))
            previous = context_out
        elif spec.id == "S5":
            context_in = dict(context_in)
            context_in["value"] = dict(context_in["value"])
            context_in["value"]["selected_ledger"] = marker
            context_in = _context_record(spec.context_in, context_in["value"])
            # Keep the preceding handoff identical, as required by the
            # canonical Context chain.
            stages[-1]["context_out"] = context_in
            previous = context_in
        stages.append({
            "id": spec.id, "name": spec.name, "order": spec.order,
            "context_in_type": spec.context_in,
            "context_out_type": spec.context_out,
            "applicable": True, "status": status,
            "started_at": updated_at if spec.id == "S5" else None,
            "completed_at": None,
            "context_in": context_in, "context_out": context_out,
            "artifacts": [], "error": None,
        })
    return {
        "schema_version": "pipeline-ledger-v1",
        "contract_version": CONTRACT_VERSION,
        "run_id": run_id, "attempt_id": attempt_id,
        "revision": revision, "updated_at": updated_at,
        "mode": "full", "status": "running",
        "current_stage_id": "S5", "artifacts": [], "stages": stages,
    }


def test_pipeline_console_combines_live_context_artifacts_and_events(tmp_path):
    output = tmp_path / "output"
    city_dir = output / "console_city"
    city_dir.mkdir(parents=True)
    (city_dir / "scene_character.json").write_text(json.dumps({
        "status": "ready", "summary": {"scene": "garden_city"},
    }), encoding="utf-8")
    (city_dir / "composition_spec.json").write_text(json.dumps({
        "scene": {"primary": "water"},
    }), encoding="utf-8")
    (city_dir / "console_city_topdown.png").write_bytes(b"png")
    job = _job(tmp_path)
    public = {
        "status": "running", "progress_pct": 64, "elapsed_s": 90,
        "stage_label": "正在恢复道路骨架连续性",
        "quality_checks": [{"id": "source_features", "status": "pass"}],
    }
    events = [
        {"id": 1, "type": "progress", "created_at": time.time() - 80,
         "data": {"progress_pct": 4, "stage_code": "stage_0"}},
        {"id": 2, "type": "progress", "created_at": time.time() - 30,
         "data": {"progress_pct": 63, "stage_code": "preprocess"}},
    ]

    report = build_pipeline_console(
        job, public, events=events, output_root=output,
        log_tail=Path(job["log_path"]).read_text(encoding="utf-8"),
    )

    assert report["schema_version"] == "admin-pipeline-console-v1"
    assert report["read_only"] is True
    assert report["current_stage_id"] == "S6"
    stages = {stage["id"]: stage for stage in report["stages"]}
    assert stages["S6"]["status"] == "running"
    assert stages["S4"]["context"]["summary"]["scene"] == "garden_city"
    assert any(item["kind"] == "image" for item in stages["S7"]["artifacts"])
    assert stages["S1"]["context"]["requirements"]["pbf_file"] == (
        "zhejiang-latest.osm.pbf")
    assert "worker_token" not in stages["S1"]["context"]["requirements"]
    assert stages["S1"]["context"]["source_feature_counts"]["roads"] == 316
    assert stages["S6"]["context_in"] == "PipelineContextV5"
    assert stages["S6"]["context_out"] == "PipelineContextV6"
    assert "final_layer_counts" in stages["S6"]["required_context_keys"]
    assert stages["S6"]["context_handoff"]["status"] == "unavailable"


def test_measurement_report_is_the_compact_authoritative_s4_context(tmp_path):
    output = tmp_path / "output"
    city_dir = output / "console_city"
    city_dir.mkdir(parents=True)
    (city_dir / "scene_character.json").write_text(json.dumps({
        "summary": {"scene": "coast_grid_compact_core"},
    }), encoding="utf-8")
    report = {
        "schema_version": "pipeline-measurement-report-v1",
        "status": "generated_pending_validation",
        "generated_at": "2026-08-30T00:00:00+00:00",
        "city": "console_city",
        "coverage": {
            "measurement_leaf_count": 412,
            "classified_leaf_count": 410,
            "unclassified_leaf_count": 2,
            "leaf_counts_by_impact": {"road_structure": 37},
        },
        "impact_chains": [{
            "id": "road_structure",
            "title": "道路结构",
            "measurement_leaf_count": 37,
            "consumers": ["ScenePolicy"],
            "decision_outputs": ["road hierarchy"],
            "realized_status": "applied_indirect_policy",
            "effect": "影响构图策略，不直接改写道路几何。",
            "examples": [{"path": "must.not.enter.console", "value": 9}],
        }],
        "realization_matrix": [{
            "effect_id": "scene.roads_declared",
            "title": "道路预算",
            "consumer_stage": "after S3",
            "consumer_symbol": "no current formal geometry consumer",
            "effect_kind": "declared_only",
            "realization_status": "declared_unconsumed",
            "consumer_called": False,
            "applied_to_current_run": False,
            "actual_outcome_paths": [],
            "mismatch_reason": "不会回改本次道路",
            "resolved_value": {"must_not_enter_compact_context": 7},
        }],
        "raw_measurements": {"secretly_huge": {"value": 123}},
        "measurement_index": [{"path": "all.412.items"}],
    }
    (city_dir / "pipeline_measurement_report.json").write_text(
        json.dumps(report), encoding="utf-8")
    (city_dir / "pipeline_measurement_report.html").write_text(
        "<!doctype html><title>report</title>", encoding="utf-8")
    # A completed observation must not override the more complete S4 report.
    (city_dir / "pipeline_observation.json").write_text(json.dumps({
        "stages": [{"id": "S4", "evidence": {"legacy": True}}],
    }), encoding="utf-8")

    console = build_pipeline_console(
        _job(tmp_path, status="done"),
        {"status": "done", "progress_pct": 100},
        events=[], output_root=output,
    )
    stage = {item["id"]: item for item in console["stages"]}["S4"]
    measurement = stage["context"]["measurement_report"]

    assert measurement["coverage"]["measurement_leaf_count"] == 412
    assert measurement["impact_chains"][0]["id"] == "road_structure"
    assert "examples" not in measurement["impact_chains"][0]
    assert measurement["realization_matrix"][0]["effect_id"] == (
        "scene.roads_declared")
    assert measurement["realization_matrix"][0][
        "applied_to_current_run"] is False
    assert "resolved_value" not in measurement["realization_matrix"][0]
    assert "raw_measurements" not in measurement
    assert "measurement_index" not in measurement
    assert "legacy" not in stage["context"]
    assert stage["context"]["scene_character_summary"]["scene"] == (
        "coast_grid_compact_core")
    report_stage = {
        item["id"]: item for item in console["stages"]
    }["S7"]
    report_artifacts = {
        item["name"]: item for item in report_stage["artifacts"]
    }
    assert report_artifacts["pipeline_measurement_report.json"]["kind"] == (
        "context")
    assert report_artifacts["pipeline_measurement_report.html"]["kind"] == (
        "report")
    stages = {item["id"]: item for item in console["stages"]}
    assert stages["S11"]["status"] == "pending_validation"
    assert console["state_source"] == "legacy_inferred"


def test_design_spec_is_shown_at_s10_without_polluting_s2_inputs(tmp_path):
    output = tmp_path / "output"
    city_dir = output / "console_city"
    city_dir.mkdir(parents=True)
    design_spec = {
        "schema_version": "design-spec-v1",
        "decisions": {
            "scene_policy": {"archetype": "garden_city"},
            "building_mass_strategy": {"mode": "adaptive"},
            "block_base_clearance": {"status": "checked"},
        },
    }
    (city_dir / "design_spec.json").write_text(
        json.dumps(design_spec), encoding="utf-8")

    console = build_pipeline_console(
        _job(tmp_path, status="done"),
        {"status": "done", "progress_pct": 100},
        events=[], output_root=output,
    )
    stages = {item["id"]: item for item in console["stages"]}

    assert "design_spec" not in stages["S2"]["context"]
    assert stages["S10"]["context"]["design_spec"] == design_spec
    assert stages["S5"]["context"]["design_spec_scene_policy"][
        "archetype"] == "garden_city"
    assert stages["S6"]["context"]["building_mass_strategy"][
        "mode"] == "adaptive"
    assert stages["S9"]["context"]["block_base_clearance"][
        "status"] == "checked"


def test_pipeline_modes_stop_at_the_contract_terminal_stage(tmp_path):
    output = tmp_path / "output"
    output.mkdir()

    for mode, terminal, not_applicable in (
        ("fetch", "S1", "S2"),
        ("styles", "S7", "S8"),
        ("draft", "S7", "S8"),
    ):
        job = _job(tmp_path, status="done")
        job["mode"] = mode
        console = build_pipeline_console(
            job, {"status": "done", "progress_pct": 100}, events=[],
            output_root=output,
        )
        stages = {item["id"]: item for item in console["stages"]}

        assert console["current_stage_id"] == terminal
        assert stages[terminal]["status"] == "completed"
        assert stages[not_applicable]["status"] == "not_applicable"


def test_validator_only_artifact_cannot_complete_s11_without_slicer(tmp_path):
    output = tmp_path / "output"
    city_dir = output / "console_city"
    city_dir.mkdir(parents=True)
    (city_dir / "project_validator_report.json").write_text(json.dumps({
        "errors": 0, "warnings": 0,
    }), encoding="utf-8")

    console = build_pipeline_console(
        _job(tmp_path, status="done"),
        {"status": "done", "progress_pct": 100}, events=[],
        output_root=output,
    )
    stages = {item["id"]: item for item in console["stages"]}

    assert stages["S11"]["status"] == "pending_validation"
    assert [item["name"] for item in stages["S11"]["artifacts"]] == [
        "project_validator_report.json",
    ]


def test_unclaimed_hash_bound_acceptance_report_cannot_complete_s11(tmp_path):
    output = tmp_path / "output"
    city_dir = output / "console_city"
    city_dir.mkdir(parents=True)
    digest = "a" * 64
    ledger = _ledger_payload(
        run_id="pipe1234", attempt_id="acceptance-1", revision=30,
        marker="accepted",
    )
    _advance_ledger_through_s10(ledger, artifact_sha256=digest)
    (city_dir / "pipeline_state.pipe1234.acceptance-1.json").write_text(
        json.dumps(ledger), encoding="utf-8")
    (city_dir / "acceptance_report.pipe1234.acceptance-1.json").write_text(json.dumps({
        "schema_version": "pipeline-acceptance-v1",
        "run_id": "pipe1234", "attempt_id": "acceptance-1",
        "artifact": {"filename": "model.3mf", "sha256": digest},
        "accepted": True,
        "errors": [],
        "warnings": [],
        "validator": {"strict_passed": True, "errors": [], "warnings": []},
        "slicer": {
            "schema_version": "slicer-acceptance-v1",
            "artifact_sha256": digest, "status": "passed",
            "errors": [], "warnings": [],
            "tool": {"name": "PrusaSlicer", "version": "2.8.0"},
            "checks": {"loaded": True, "sliced": True},
        },
    }), encoding="utf-8")

    job = _job(tmp_path, status="done")
    job["pipeline_attempt_id"] = "acceptance-1"
    console = build_pipeline_console(
        job,
        {"status": "done", "progress_pct": 100}, events=[],
        output_root=output,
    )
    stages = {item["id"]: item for item in console["stages"]}
    assert stages["S11"]["status"] == "pending_validation"
    assert not any(
        item["name"].startswith("acceptance_report")
        for item in console["artifacts"]
    )


def test_validated_ledger_is_the_authority_that_completes_s11(tmp_path):
    output = tmp_path / "output"
    city_dir = output / "console_city"
    city_dir.mkdir(parents=True)
    digest = "b" * 64
    ledger = _ledger_payload(
        run_id="pipe1234", attempt_id="validated-1", revision=31,
        marker="validated",
    )
    _advance_ledger_through_s10(ledger, artifact_sha256=digest)
    spec = PIPELINE_STAGES[11]
    acceptance = _required_value(spec)
    acceptance.update({
        "schema_version": "pipeline-acceptance-v1",
        "accepted": True,
        "errors": [],
        "warnings": [],
        "artifact_sha256": digest,
        "validator_strict_passed": True,
        "slicer_status": "passed",
    })
    ledger["stages"][11].update({
        "status": "completed",
        "context_in": ledger["stages"][10]["context_out"],
        "context_out": _context_record(spec.context_out, acceptance),
    })
    s11_artifacts = [{
        "name": name,
        "stage_id": "S11",
        "path": f"{name}.pipe1234.validated-1.json",
        "size_bytes": 3,
        "sha256": "e" * 64,
    } for name in ("validator_report", "acceptance_report")]
    ledger["stages"][11]["artifacts"] = s11_artifacts
    ledger["artifacts"].extend(s11_artifacts)
    ledger["status"] = "validated"
    ledger["current_stage_id"] = None
    (city_dir / "pipeline_state.pipe1234.validated-1.json").write_text(
        json.dumps(ledger), encoding="utf-8")

    job = _job(tmp_path, status="done")
    job["pipeline_attempt_id"] = "validated-1"
    console = build_pipeline_console(
        job, {"status": "done", "progress_pct": 100}, events=[],
        output_root=output,
    )

    assert {s["id"]: s for s in console["stages"]}["S11"]["status"] == (
        "completed")


def test_acceptance_evidence_is_fail_closed_and_attempt_isolated(tmp_path):
    output = tmp_path / "output"
    city_dir = output / "console_city"
    city_dir.mkdir(parents=True)
    digest = "c" * 64
    ledger = _ledger_payload(
        run_id="pipe1234", attempt_id="current-attempt", revision=30,
        marker="current",
    )
    _advance_ledger_through_s10(ledger, artifact_sha256=digest)
    (city_dir / "pipeline_state.pipe1234.current-attempt.json").write_text(
        json.dumps(ledger), encoding="utf-8")
    # A complete-looking observation and another attempt's formal report are
    # not authority for this attempt.
    (city_dir / "pipeline_observation.json").write_text(json.dumps({
        "stages": [{"id": "S11", "status": "completed"}],
    }), encoding="utf-8")
    foreign = {
        "schema_version": "pipeline-acceptance-v1",
        "run_id": "pipe1234", "attempt_id": "foreign-attempt",
        "artifact": {"sha256": digest}, "accepted": True,
        "errors": [], "warnings": [],
        "validator": {"strict_passed": True, "errors": [], "warnings": []},
        "slicer": {
            "schema_version": "slicer-acceptance-v1",
            "artifact_sha256": digest, "status": "passed",
            "errors": [], "warnings": [],
            "tool": {"name": "PrusaSlicer", "version": "2.8"},
            "checks": {"loaded": True, "sliced": True},
        },
    }
    (city_dir / "acceptance_report.pipe1234.foreign-attempt.json").write_text(
        json.dumps(foreign), encoding="utf-8")
    job = _job(tmp_path, status="done")
    job["pipeline_attempt_id"] = "current-attempt"
    console = build_pipeline_console(
        job, {"status": "done", "progress_pct": 100}, events=[],
        output_root=output,
    )
    assert {s["id"]: s for s in console["stages"]}["S11"]["status"] == (
        "pending_validation")

    # Same identity but unclaimed evidence is still not state authority,
    # whether it looks successful or failed.
    foreign["attempt_id"] = "current-attempt"
    foreign["slicer"]["checks"]["sliced"] = False
    (city_dir / "acceptance_report.pipe1234.current-attempt.json").write_text(
        json.dumps(foreign), encoding="utf-8")
    console = build_pipeline_console(
        job, {"status": "done", "progress_pct": 100}, events=[],
        output_root=output,
    )
    assert {s["id"]: s for s in console["stages"]}["S11"]["status"] == (
        "pending_validation")


def test_semantically_invalid_inline_ledger_is_not_authoritative(tmp_path):
    output = tmp_path / "output"
    job = _job(tmp_path)
    job["pipeline_attempt_id"] = "bad-heartbeat"
    bad = _ledger_payload(
        run_id=job["id"], attempt_id="bad-heartbeat", revision=50,
        marker="bad",
    )
    bad["stages"][5]["order"] = 99
    bad["stages"][5]["context_in"]["value"].pop("scene_status")
    job["pipeline_ledger"] = bad

    console = build_pipeline_console(
        job, {"status": "running", "progress_pct": 40}, events=[],
        output_root=output,
    )
    assert console["state_source"] == "exact_ledger_missing"
    assert console["integrity"]["status"] == "missing_exact_ledger"
    assert console["ledger_identity"] is None


def test_ledger_context_preserves_empty_zero_issue_lists(tmp_path):
    output = tmp_path / "output"
    job = _job(tmp_path, status="running")
    job["pipeline_attempt_id"] = "attempt-clean"
    ledger = _ledger_payload(
        run_id=job["id"], attempt_id="attempt-clean", revision=20,
        marker="clean",
    )
    _advance_ledger_through_s10(ledger, artifact_sha256="b" * 64)
    ledger["stages"][10]["status"] = "pending"
    ledger["stages"][10]["context_in"] = None
    ledger["stages"][10]["context_out"] = None
    ledger["stages"][10]["artifacts"] = []
    ledger["stages"][11]["status"] = "pending"
    ledger["status"] = "running"
    ledger["current_stage_id"] = None
    ledger["artifacts"] = []
    job["pipeline_ledger"] = ledger

    console = build_pipeline_console(
        job, {"status": "running", "progress_pct": 94}, events=[],
        output_root=output,
    )
    stage = {item["id"]: item for item in console["stages"]}["S9"]
    assert stage["context"]["errors"] == []
    assert stage["context"]["warnings"] == []


def test_explicit_sidecar_context_wins_over_legacy_final_evidence(tmp_path):
    output = tmp_path / "output"
    city_dir = output / "console_city"
    city_dir.mkdir(parents=True)
    (city_dir / "scene_policy.json").write_text(json.dumps({
        "source": "explicit_stage_sidecar", "policy": "current",
    }), encoding="utf-8")
    (city_dir / "pipeline_observation.json").write_text(json.dumps({
        "stages": [{
            "id": "S5", "evidence": {
                "source": "legacy_final_observation", "policy": "stale",
            },
        }],
    }), encoding="utf-8")

    console = build_pipeline_console(
        _job(tmp_path, status="done"),
        {"status": "done", "progress_pct": 100}, events=[],
        output_root=output,
    )
    stage = {item["id"]: item for item in console["stages"]}["S5"]

    assert stage["context"]["source"] == "explicit_stage_sidecar"
    assert stage["context"]["policy"] == "current"


def test_pipeline_ledger_is_authoritative_over_percentage_and_observation(
        tmp_path):
    output = tmp_path / "output"
    city_dir = output / "console_city"
    city_dir.mkdir(parents=True)
    ledger = _ledger_payload(
        run_id="pipe1234", attempt_id="attempt-1", revision=7,
        marker="pipeline_ledger",
    )
    ledger["stages"][5]["context_in"]["value"].update({
        "source": "pipeline_ledger", "policy": "current",
    })
    encoded = json.dumps(
        ledger["stages"][5]["context_in"]["value"], sort_keys=True,
        separators=(",", ":"),
    ).encode()
    ledger["stages"][5]["context_in"]["sha256"] = hashlib.sha256(
        encoded).hexdigest()
    ledger["stages"][4]["context_out"] = dict(
        ledger["stages"][5]["context_in"])
    (city_dir / "pipeline_state.json").write_text(
        json.dumps(ledger), encoding="utf-8")
    (city_dir / "pipeline_observation.json").write_text(json.dumps({
        "stages": [{
            "id": "S5", "evidence": {
                "source": "legacy_final_observation", "policy": "stale",
            },
        }],
    }), encoding="utf-8")

    console = build_pipeline_console(
        _job(tmp_path, status="running"),
        {"status": "running", "progress_pct": 99}, events=[],
        output_root=output,
    )
    stages = {item["id"]: item for item in console["stages"]}

    assert console["state_source"] == "ledger"
    assert console["ledger_identity"] == {
        "run_id": "pipe1234", "attempt_id": "attempt-1",
        "revision": 7, "updated_at": "2026-08-30T01:00:00+00:00",
    }
    assert console["current_stage_id"] == "S5"
    assert stages["S5"]["status"] == "running"
    assert stages["S5"]["state_source"] == "ledger"
    assert stages["S5"]["context"]["source"] == "pipeline_ledger"
    assert stages["S0"]["context_handoff"]["status"] == "root"
    assert stages["S5"]["context_handoff"]["status"] == "verified"
    assert (stages["S5"]["context_handoff"]["input_sha256"] ==
            stages["S5"]["context_handoff"][
                "predecessor_output_sha256"])
    assert stages["S6"]["status"] == "pending"
    assert stages["S11"]["status"] == "pending_validation"


def test_ledger_artifact_provenance_overrides_filename_inference(tmp_path):
    output = tmp_path / "output"
    city_dir = output / "console_city"
    run_dir = city_dir / ".pipeline_runs"
    run_dir.mkdir(parents=True)
    draft_glb = city_dir / "console_city_draft.glb"
    final_report = city_dir / "pipeline_measurement_report.json"
    draft_glb.write_bytes(b"glb")
    final_report.write_text("{}", encoding="utf-8")

    ledger = _ledger_payload(
        run_id="pipe1234", attempt_id="artifact-stage", revision=9,
        marker="artifact-stage",
    )
    _advance_ledger_through_s10(ledger, artifact_sha256="a" * 64)
    draft_claim = {
        "name": "draft_glb", "stage_id": "S7", "path": draft_glb.name,
        "size_bytes": draft_glb.stat().st_size,
        "sha256": hashlib.sha256(draft_glb.read_bytes()).hexdigest(),
    }
    ledger["stages"][7]["artifacts"] = [draft_claim]
    ledger["artifacts"].append(draft_claim)
    report_claim = next(
        item for item in ledger["artifacts"]
        if item["name"] == "measurement_report_json")
    report_claim.update({
        "path": final_report.name,
        "size_bytes": final_report.stat().st_size,
        "sha256": hashlib.sha256(final_report.read_bytes()).hexdigest(),
    })
    ledger["stages"][10]["artifacts"] = [
        dict(item) for item in ledger["artifacts"]
        if item["stage_id"] == "S10"
    ]
    (run_dir / "pipeline_state.pipe1234.artifact-stage.json").write_text(
        json.dumps(ledger), encoding="utf-8")
    job = _job(tmp_path)
    job["pipeline_attempt_id"] = "artifact-stage"

    console = build_pipeline_console(
        job, {"status": "running", "progress_pct": 99}, events=[],
        output_root=output,
    )
    by_name = {item["name"]: item for item in console["artifacts"]}

    assert by_name[draft_glb.name]["stage_id"] == "S7"
    assert by_name[final_report.name]["stage_id"] == "S10"
    assert by_name[draft_glb.name]["stage_source"] == "pipeline_ledger"
    assert by_name[final_report.name]["stage_source"] == "pipeline_ledger"


def test_attempt_ledgers_do_not_cross_concurrent_city_jobs(tmp_path):
    output = tmp_path / "output"
    city_dir = output / "console_city"
    run_dir = city_dir / ".pipeline_runs"
    run_dir.mkdir(parents=True)
    wanted = _ledger_payload(
        run_id="pipe1234", attempt_id="attempt-a", revision=4,
        marker="wanted-run", updated_at="2026-08-30T01:00:00+00:00",
    )
    other = _ledger_payload(
        run_id="other-job", attempt_id="attempt-z", revision=999,
        marker="other-user", updated_at="2026-08-30T09:00:00+00:00",
    )
    # Cover both supported placements: a direct output child and the default
    # hidden attempt directory used by the generator.
    (city_dir / "pipeline_state.pipe1234.attempt-a.json").write_text(
        json.dumps(wanted), encoding="utf-8")
    (run_dir / "pipeline_state.other-job.attempt-z.json").write_text(
        json.dumps(other), encoding="utf-8")

    console = build_pipeline_console(
        _job(tmp_path), {"status": "running", "progress_pct": 99},
        events=[], output_root=output,
    )
    stages = {item["id"]: item for item in console["stages"]}

    assert console["ledger_identity"]["run_id"] == "pipe1234"
    assert console["ledger_identity"]["attempt_id"] == "attempt-a"
    assert stages["S5"]["context"]["selected_ledger"] == "wanted-run"
    assert {
        item["name"] for item in console["artifacts"]
        if item["name"].startswith("pipeline_state.")
    } == {
        "pipeline_state.pipe1234.attempt-a.json",
    }
    selected_ledger = next(
        item for item in console["artifacts"]
        if item["name"] == "pipeline_state.pipe1234.attempt-a.json"
    )
    assert selected_ledger["stage_id"] == "RUN"
    assert all(
        item["name"] != selected_ledger["name"]
        for item in stages["S10"]["artifacts"]
    )


def test_valid_ledger_hides_other_attempt_sidecars_in_same_city(tmp_path):
    output = tmp_path / "output"
    city_dir = output / "console_city"
    run_dir = city_dir / ".pipeline_runs"
    run_dir.mkdir(parents=True)
    ledger = _ledger_payload(
        run_id="pipe1234", attempt_id="isolated", revision=9,
        marker="isolated",
    )
    own = city_dir / "scene_character.pipe1234.isolated.json"
    foreign = city_dir / "scene_character.other.foreign.json"
    own_payload = json.dumps({
        "summary": {"scene": "isolated-attempt"}}).encode()
    own.write_bytes(own_payload)
    foreign.write_bytes(b"foreign")
    (city_dir / "scene_character.json").write_text(json.dumps({
        "summary": {"scene": "latest-alias-from-other-attempt"}}),
        encoding="utf-8")
    claim = {
        "name": "scene_character", "stage_id": "S4", "path": own.name,
        "size_bytes": len(own_payload),
        "sha256": hashlib.sha256(own_payload).hexdigest(),
    }
    ledger["stages"][4]["artifacts"] = [claim]
    ledger["artifacts"] = [claim]
    (run_dir / "pipeline_state.pipe1234.isolated.json").write_text(
        json.dumps(ledger), encoding="utf-8")
    job = _job(tmp_path)
    job["pipeline_attempt_id"] = "isolated"

    console = build_pipeline_console(
        job, {"status": "running", "progress_pct": 40}, events=[],
        output_root=output,
    )
    names = {item["name"] for item in console["artifacts"]}
    stages = {item["id"]: item for item in console["stages"]}

    assert own.name in names
    assert foreign.name not in names
    assert "scene_character.json" not in names
    assert stages["S4"]["context"]["summary"]["scene"] == (
        "isolated-attempt")


def test_retry_selection_honors_attempt_then_highest_revision(tmp_path):
    output = tmp_path / "output"
    city_dir = output / "console_city"
    run_dir = city_dir / ".pipeline_runs"
    run_dir.mkdir(parents=True)
    stale_copy = _ledger_payload(
        run_id="pipe1234", attempt_id="retry-2", revision=2,
        marker="stale-copy", updated_at="2026-08-30T03:00:00+00:00",
    )
    current_copy = _ledger_payload(
        run_id="pipe1234", attempt_id="retry-2", revision=8,
        marker="current-copy", updated_at="2026-08-30T03:00:00+00:00",
    )
    different_attempt = _ledger_payload(
        run_id="pipe1234", attempt_id="retry-3", revision=20,
        marker="different-attempt", updated_at="2026-08-30T04:00:00+00:00",
    )
    (city_dir / "pipeline_state.pipe1234.retry-2.json").write_text(
        json.dumps(stale_copy), encoding="utf-8")
    (run_dir / "pipeline_state.pipe1234.retry-2.json").write_text(
        json.dumps(current_copy), encoding="utf-8")
    (run_dir / "pipeline_state.pipe1234.retry-3.json").write_text(
        json.dumps(different_attempt), encoding="utf-8")

    pinned_job = _job(tmp_path)
    pinned_job["pipeline_attempt_id"] = "retry-2"
    pinned = build_pipeline_console(
        pinned_job, {"status": "running", "progress_pct": 99}, events=[],
        output_root=output,
    )
    pinned_stage = {item["id"]: item for item in pinned["stages"]}["S5"]

    assert pinned["ledger_identity"]["attempt_id"] == "retry-2"
    assert pinned["ledger_identity"]["revision"] == 8
    assert pinned_stage["context"]["selected_ledger"] == "current-copy"
    selected_artifact = next(
        item for item in pinned["artifacts"]
        if item["name"] == "pipeline_state.pipe1234.retry-2.json"
    )
    assert "/.pipeline_runs/" in selected_artifact["url"]

    latest = build_pipeline_console(
        _job(tmp_path), {"status": "running", "progress_pct": 99},
        events=[], output_root=output,
    )
    latest_stage = {item["id"]: item for item in latest["stages"]}["S5"]

    assert latest["ledger_identity"]["attempt_id"] == "retry-3"
    assert latest_stage["context"]["selected_ledger"] == (
        "different-attempt")


def test_pinned_attempt_never_falls_back_to_legacy_wrong_attempt(tmp_path):
    output = tmp_path / "output"
    city_dir = output / "console_city"
    city_dir.mkdir(parents=True)
    legacy = _ledger_payload(
        run_id="pipe1234", attempt_id="old-attempt", revision=99,
        marker="must-not-cross-attempt",
    )
    (city_dir / "pipeline_state.json").write_text(
        json.dumps(legacy), encoding="utf-8")
    (city_dir / "scene_policy.json").write_text(json.dumps({
        "selected_ledger": "mutable-latest-must-not-cross-attempt",
    }), encoding="utf-8")
    (city_dir / "design_spec.json").write_text(json.dumps({
        "decisions": {"scene_policy": {
            "selected_ledger": "mutable-latest-must-not-cross-attempt",
        }},
    }), encoding="utf-8")
    job = _job(tmp_path)
    job["pipeline_attempt_id"] = "current-attempt"

    console = build_pipeline_console(
        job, {"status": "running", "progress_pct": 50}, events=[],
        output_root=output,
    )
    stages = {item["id"]: item for item in console["stages"]}

    assert console["state_source"] == "exact_ledger_missing"
    assert console["integrity"]["status"] == "missing_exact_ledger"
    assert console["ledger_identity"] is None
    assert console["artifacts"] == []
    assert stages["S5"]["context"].get("selected_ledger") is None
    assert "mutable-latest" not in json.dumps(console)
    assert not any(item["status"] == "completed" for item in stages.values())


def test_corrupt_exact_ledger_does_not_fall_back_to_legacy_wrong_attempt(
        tmp_path):
    output = tmp_path / "output"
    city_dir = output / "console_city"
    run_dir = city_dir / ".pipeline_runs"
    run_dir.mkdir(parents=True)
    legacy = _ledger_payload(
        run_id="pipe1234", attempt_id="old-attempt", revision=99,
        marker="must-not-cross-after-corruption",
    )
    (city_dir / "pipeline_state.json").write_text(
        json.dumps(legacy), encoding="utf-8")
    # Exact filename exists but its payload is not a valid canonical Ledger.
    (run_dir / "pipeline_state.pipe1234.current-attempt.json").write_text(
        "{not-json", encoding="utf-8")
    job = _job(tmp_path)
    job["pipeline_attempt_id"] = "current-attempt"

    console = build_pipeline_console(
        job, {"status": "running", "progress_pct": 50}, events=[],
        output_root=output,
    )
    stages = {item["id"]: item for item in console["stages"]}

    assert console["state_source"] == "exact_ledger_missing"
    assert console["integrity"]["status"] == "missing_exact_ledger"
    assert console["ledger_identity"] is None
    assert stages["S5"]["context"].get("selected_ledger") is None
    assert not any(item["status"] == "completed" for item in stages.values())


def test_newest_revision_wins_across_heartbeat_and_filesystem(tmp_path):
    output = tmp_path / "output"
    city_dir = output / "console_city"
    run_dir = city_dir / ".pipeline_runs"
    run_dir.mkdir(parents=True)
    disk = _ledger_payload(
        run_id="pipe1234", attempt_id="live-attempt", revision=99,
        marker="filesystem-copy", updated_at="2026-08-30T09:00:00+00:00",
    )
    disk_path = run_dir / "pipeline_state.pipe1234.live-attempt.json"
    disk_path.write_text(json.dumps(disk), encoding="utf-8")
    job = _job(tmp_path)
    job["pipeline_attempt_id"] = "live-attempt"
    job["pipeline_ledger"] = _ledger_payload(
        run_id="pipe1234", attempt_id="live-attempt", revision=7,
        marker="inline-heartbeat",
        updated_at="2026-08-30T02:00:00+00:00",
    )

    console = build_pipeline_console(
        job, {"status": "running", "progress_pct": 99}, events=[],
        output_root=output,
    )
    stage = {item["id"]: item for item in console["stages"]}["S5"]

    assert console["state_source"] == "ledger"
    assert console["ledger_identity"]["revision"] == 99
    assert stage["context"]["selected_ledger"] == "filesystem-copy"
    assert any(
        item["name"].startswith("pipeline_state")
        for item in console["artifacts"]
    )

    # An inline payload from another attempt is not authoritative; the same
    # job must fall back to its own attempt-isolated filesystem Ledger.
    job["pipeline_ledger"] = _ledger_payload(
        run_id="pipe1234", attempt_id="foreign-attempt", revision=100,
        marker="foreign-inline",
    )
    fallback = build_pipeline_console(
        job, {"status": "running", "progress_pct": 99}, events=[],
        output_root=output,
    )
    fallback_stage = {
        item["id"]: item for item in fallback["stages"]
    }["S5"]

    assert fallback["ledger_identity"]["revision"] == 99
    assert fallback_stage["context"]["selected_ledger"] == "filesystem-copy"


def test_pipeline_snapshot_is_persisted_atomically(tmp_path):
    report = {
        "job": {"id": "pipe1234"}, "stages": [], "read_only": True,
    }
    path = write_pipeline_snapshot(tmp_path, report)

    assert path.name == "pipe1234_pipeline_state.json"
    assert json.loads(path.read_text(encoding="utf-8"))["read_only"] is True
    assert not path.with_suffix(".json.tmp").exists()


def test_admin_pipeline_endpoint_is_not_public():
    response = TestClient(server.app).get(
        "/api/admin/jobs/doesnotexist/pipeline")

    assert response.status_code in {401, 403}


def test_admin_keeps_full_context_while_customer_gets_safe_stage_timeline():
    admin = (WEBAPP / "static" / "admin.html").read_text(encoding="utf-8")
    product = (WEBAPP / "static" / "index.html").read_text(encoding="utf-8")

    assert 'id="pipelinePanel"' in admin
    assert "/api/admin/jobs/${encodeURIComponent(jobId)}/pipeline" in admin
    assert "Context / 观测证据（只读）" in admin
    assert "Context 交接契约" in admin
    assert "源要素非零存活门禁" in admin
    assert "不代表保留比例或视觉质量达标" in admin
    assert "stage.required_context_keys" in admin
    assert "实际交接" in admin
    assert "handoff.predecessor_output_sha256" in admin
    assert "领域内容指纹" in admin
    assert "final_layers_fingerprint" in admin
    assert "s7_review_fingerprint" in admin
    assert 'id="pipelinePanel"' not in product
    assert 'id="jobPipeline"' in product
    assert "详细 Context、策略和中间产物仅管理员可见" in product


def test_public_pipeline_summary_never_exposes_internal_context(tmp_path):
    job = _job(tmp_path)
    summary = server._public_pipeline_summary(job, 41)

    assert summary["state_source"] == "estimated_progress"
    assert summary["current_stage_id"] == "S5"
    assert [stage["id"] for stage in summary["stages"]] == [
        stage.id for stage in PIPELINE_STAGES
    ]
    serialized = json.dumps(summary, ensure_ascii=False).lower()
    for private_key in ("context", "fingerprint", "token", "path", "artifact"):
        assert private_key not in serialized


def _http_login(client, email):
    started = client.post("/api/auth/email/start", json={"email": email})
    verified = client.post("/api/auth/email/verify", json={
        "email": email, "code": started.json()["dev_code"],
    })
    assert verified.status_code == 200


def test_internal_pipeline_static_files_require_admin(monkeypatch, tmp_path):
    store = AuthStore(
        tmp_path / "reports.db", "report-secret",
        admin_emails={"admin@example.com"},
    )
    monkeypatch.setattr(server, "_AUTH_STORE", store)
    monkeypatch.setattr(server, "AUTH_DEV_ECHO_CODE", True)

    slug = f"report_access_{uuid.uuid4().hex}"
    report_dir = server.OUTPUT_DIR / slug
    report_dir.mkdir(parents=True)
    private_names = (
        "pipeline_state.pipe1234.retry-2.json",
        "pipeline_observation.json",
        "pipeline_observation.pipe1234.retry-2.json",
        "pipeline_observation.html",
        "pipeline_measurement_report.json",
        "pipeline_measurement_report.pipe1234.retry-2.html",
        "pipeline_measurement_report.html",
        "scene_character.json",
        "scene_character.v2.json",
        "scene_policy.json",
        "scene_policy.v2.json",
        "composition_spec.json",
        "composition_spec.v2.json",
        "param_decision.json",
        "param_decision.v2.json",
        "block_grammar.pipe1234.retry-2.json",
        "building_mass.pipe1234.retry-2.json",
        "building_data_quality.pipe1234.retry-2.json",
        "height_hierarchy.pipe1234.retry-2.json",
        "height_emphasis.pipe1234.retry-2.json",
        "landform_character.pipe1234.retry-2.json",
        "validator_report.pipe1234.retry-2.json",
        "slicer_report.pipe1234.retry-2.json",
        "acceptance_report.pipe1234.retry-2.json",
    )
    private_paths = [report_dir / name for name in private_names]
    public_path = report_dir / "customer_preview.png"
    for private_path in private_paths:
        private_path.write_bytes(b"private-pipeline-artifact")
    public_path.write_bytes(b"public-preview")
    try:
        anonymous = TestClient(server.app)
        user = TestClient(server.app)
        admin = TestClient(server.app)
        _http_login(user, "user@example.com")
        _http_login(admin, "admin@example.com")
        for private_path in private_paths:
            report_url = f"/files/{slug}/{private_path.name}"
            assert anonymous.get(report_url).status_code == 404
            assert user.get(report_url).status_code == 404
            allowed = admin.get(report_url)
            assert allowed.status_code == 200
            assert allowed.content == b"private-pipeline-artifact"
        assert anonymous.get(
            f"/files/{slug}/{public_path.name}").status_code == 200
    finally:
        for private_path in private_paths:
            private_path.unlink(missing_ok=True)
        public_path.unlink(missing_ok=True)
        report_dir.rmdir()


def test_public_artifacts_api_does_not_return_internal_param_decision(
        monkeypatch, tmp_path):
    output = tmp_path / "output"
    city_dir = output / "private_strategy_city"
    city_dir.mkdir(parents=True)
    (city_dir / "param_decision.json").write_text(json.dumps({
        "internal_threshold": 0.314159,
        "strategy": "must-not-be-public",
    }), encoding="utf-8")
    (city_dir / "design_spec.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(server, "OUTPUT_DIR", output)

    response = TestClient(server.app).get(
        "/api/artifacts/private_strategy_city")

    assert response.status_code == 200
    payload = response.json()
    assert "param_decision" not in payload["artifacts"]
    assert "must-not-be-public" not in response.text
