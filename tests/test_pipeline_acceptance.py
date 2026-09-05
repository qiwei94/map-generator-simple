import hashlib
import json
from pathlib import Path

import pytest

from aesthetic.pipeline_acceptance import (
    SLICER_SCHEMA_VERSION,
    accept_pipeline_artifact,
    validate_slicer_evidence,
)
from aesthetic.pipeline_contract import (
    S10_REQUIRED_ARTIFACT_BUNDLE,
    feature_source_counts_fingerprint,
    stage_by_id,
    stages_for_mode,
)
from aesthetic.pipeline_ledger import PipelineLedger, PipelineLedgerError
from aesthetic.pipeline_gates import evaluate_feature_survival


def _pending_run(tmp_path):
    output = tmp_path / "output" / "chicago"
    state_dir = output / ".pipeline_runs"
    state_dir.mkdir(parents=True)
    artifact = output / "chicago.3mf"
    artifact.write_bytes(b"synthetic-3mf")
    artifact_bundle = {"3mf": artifact}
    for name in S10_REQUIRED_ARTIFACT_BUNDLE:
        if name == "3mf":
            continue
        suffix = ".html" if name.endswith("_html") else ".json"
        filename = (
            "design_spec.run-1.attempt-1.json"
            if name == "design_spec" else f"{name}{suffix}"
        )
        path = output / filename
        path.write_text(f"artifact:{name}", encoding="utf-8")
        artifact_bundle[name] = path
    ledger = PipelineLedger.create(
        state_dir / "pipeline_state.run-1.attempt-1.json",
        output_dir=output,
        run_id="run-1",
        attempt_id="attempt-1",
        mode="full",
    )
    for stage in stages_for_mode("full"):
        if stage.id == "S11":
            break
        ledger.start_stage(
            stage.id,
            context_in={"request": 1} if stage.id == "S0" else None,
        )
        context = {
            key: f"{stage.id}:{key}"
            for key in stage_by_id(stage.id).required_context_keys
        }
        preprocess_parameters = {
            "schema_version": "preprocess-parameters-v1",
            "effective_overrides": {},
        }
        preprocess_fingerprint = hashlib.sha256(json.dumps(
            preprocess_parameters, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        source_counts = {name: (1 if name == "roads" else 0) for name in (
            "roads", "water", "buildings", "vegetation")}
        source_fingerprint = feature_source_counts_fingerprint(source_counts)
        terrain_fingerprint = "a" * 64
        if 2 <= stage.order <= 10:
            context.update(
                preprocess_parameters_fingerprint=preprocess_fingerprint,
                source_feature_counts_fingerprint=source_fingerprint,
                terrain_surface_fingerprint=terrain_fingerprint,
            )
        if 4 <= stage.order <= 10:
            context["scene_character_fingerprint"] = "b" * 64
        if 5 <= stage.order <= 10:
            context["scene_policy_fingerprint"] = "c" * 64
        if stage.id == "S0":
            context.update(
                city="Chicago", mode="full",
                bbox_wgs84=[41.8, -87.7, 41.9, -87.6],
                scale_mm_per_m=0.04, printer_profile_id="test-printer")
        elif stage.id == "S1":
            context.update(
                raw_feature_counts={**source_counts, "landuse": 0},
                dem_evidence={
                    "status": "ready", "source": "test",
                    "flat_fallback": False},
                osmium_backend="native_osmium")
        elif stage.id == "S2":
            context.update(
                projected_feature_counts=source_counts,
                bbox_local_m=[0.0, 0.0, 1000.0, 1000.0],
                terrain_surface_plan={"fingerprint": terrain_fingerprint},
                preprocess_parameters=preprocess_parameters,
                preprocess_parameters_fingerprint=preprocess_fingerprint,
            )
        elif stage.id == "S3":
            context.update(
                layer_counts={
                    "BL": 0, "BO": 0, "WL": 0, "WO": 0, "VL": 0,
                    "VO": 0, "roads": 1, "block_base": 0},
                preprocess_policy_version="test-policy-v1")
        elif stage.id == "S4":
            context.update(
                scene_character_version="scene-character-v1",
                scene_status="ready", source_quality={})
        elif stage.id == "S5":
            context.update(
                scene_policy_version="scene-policy-v1",
                activation="audit_only", scene_class="urban",
                archetype="grid")
        elif stage.id == "S6":
            context.update(
                final_layer_counts={
                    "BL": 0, "BO": 0, "WL": 0, "WO": 0, "VL": 0,
                    "VO": 0, "roads": 1, "block_base": 0},
                building_mass_status="inactive",
                height_hierarchy_status="inactive")
        elif stage.id == "S7":
            context["review_artifacts"] = []
        elif stage.id == "S8":
            context.update(
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
        elif stage.id == "S9":
            context.update(
                passed=True, errors=[], warnings=[],
                gate_version="semantic-mesh-gate-v1",
                required_roles=["terrain", "roads"],
                mesh_metrics={
                    "terrain": {
                        "present": True, "vertices": 8, "faces": 12,
                        "watertight": True, "winding_consistent": True,
                        "bounds_mm": [
                            [0.0, 0.0, 0.0], [1.0, 1.0, 1.0]],
                    },
                    "roads": {
                        "present": True, "vertices": 6, "faces": 8,
                        "watertight": True, "winding_consistent": True,
                        "bounds_mm": [
                            [0.0, 0.0, 0.0], [1.0, 1.0, 0.2]],
                    },
                },
                feature_survival=evaluate_feature_survival(
                    source_counts, {"roads": 1}),
            )
        elif stage.id == "S10":
            context.update(
                status="generated_pending_validation",
                artifact_bundle=list(S10_REQUIRED_ARTIFACT_BUNDLE),
                s9_errors=0,
                s9_warnings=0,
            )
        ledger.complete_stage(
            stage.id,
            context_out=context,
            artifacts=artifact_bundle if stage.id == "S10" else None,
        )
    return ledger, artifact, output


def _slicer_evidence(artifact, **overrides):
    payload = {
        "schema_version": SLICER_SCHEMA_VERSION,
        "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "status": "passed",
        "errors": [],
        "warnings": [],
        "tool": {"name": "Bambu Studio", "version": "test-version"},
        "checks": {"loaded": True, "sliced": True},
    }
    payload.update(overrides)
    return payload


def _validator_pass(_path, **_kwargs):
    return {"passed": True, "errors": [], "warnings": [], "rules": []}


def test_s11_accepts_only_hash_bound_validator_and_slicer_evidence(
        tmp_path, monkeypatch):
    ledger, artifact, output = _pending_run(tmp_path)
    slicer_path = output / "slicer.json"
    slicer_path.write_text(
        json.dumps(_slicer_evidence(artifact)), encoding="utf-8")
    monkeypatch.setattr(
        "aesthetic.pipeline_acceptance.validate_3mf", _validator_pass)

    report = accept_pipeline_artifact(
        ledger_path=ledger.path,
        artifact_path=artifact,
        slicer_report_path=slicer_path,
    )

    final = PipelineLedger.load(ledger.path, output_dir=output).snapshot()
    assert report["accepted"] is True
    assert report["ledger_status"] == "validated"
    assert final["status"] == "validated"
    assert final["stages"][11]["status"] == "completed"
    final_hash = hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert report["artifact"]["sha256"] == final_hash
    assert report["artifact"]["size_bytes"] == artifact.stat().st_size
    assert set(report["artifact_bundle"]) == set(
        S10_REQUIRED_ARTIFACT_BUNDLE)
    s10_claim = next(
        item for item in final["stages"][10]["artifacts"]
        if item["name"] == "3mf")
    assert s10_claim["sha256"] == final_hash
    assert s10_claim["size_bytes"] == artifact.stat().st_size
    recorded = {
        item["name"]: item for item in final["stages"][11]["artifacts"]
    }
    assert set(recorded) == {
        "validator_report", "acceptance_report"
    }
    for claim in recorded.values():
        persisted = output / claim["path"]
        assert persisted.stat().st_size == claim["size_bytes"]
        assert hashlib.sha256(persisted.read_bytes()).hexdigest() == claim["sha256"]


def test_s11_validator_uses_attempt_bound_design_spec_not_latest_alias(
        tmp_path, monkeypatch):
    ledger, artifact, output = _pending_run(tmp_path)
    claimed_design_spec = output / next(
        item["path"] for item in ledger.snapshot()["stages"][10]["artifacts"]
        if item["name"] == "design_spec"
    )
    # A later same-city attempt may overwrite this compatibility alias.  S11
    # must never hand it to the validator for the older attempt.
    (output / "design_spec.json").write_text(
        "later-attempt-alias", encoding="utf-8")
    slicer_path = output / "slicer.json"
    slicer_path.write_text(
        json.dumps(_slicer_evidence(artifact)), encoding="utf-8")
    seen = {}

    def capture_validator(path, **kwargs):
        seen["artifact"] = Path(path).resolve()
        seen["design_spec"] = Path(kwargs["design_spec_path"]).resolve()
        return _validator_pass(path, **kwargs)

    monkeypatch.setattr(
        "aesthetic.pipeline_acceptance.validate_3mf", capture_validator)
    report = accept_pipeline_artifact(
        ledger_path=ledger.path,
        artifact_path=artifact,
        slicer_report_path=slicer_path,
    )

    assert report["accepted"] is True
    assert seen["artifact"] == artifact.resolve()
    assert seen["design_spec"] == claimed_design_spec.resolve()
    assert seen["design_spec"].name != "design_spec.json"


def test_slicer_hash_mismatch_is_a_validation_rejection_not_generation_failure(
        tmp_path, monkeypatch):
    ledger, artifact, output = _pending_run(tmp_path)
    slicer = _slicer_evidence(artifact, artifact_sha256="0" * 64)
    slicer_path = output / "slicer.json"
    slicer_path.write_text(json.dumps(slicer), encoding="utf-8")
    monkeypatch.setattr(
        "aesthetic.pipeline_acceptance.validate_3mf", _validator_pass)

    report = accept_pipeline_artifact(
        ledger_path=ledger.path,
        artifact_path=artifact,
        slicer_report_path=slicer_path,
    )

    final = PipelineLedger.load(ledger.path, output_dir=output).snapshot()
    assert report["accepted"] is False
    assert final["status"] == "validation_rejected"
    assert final["stages"][10]["status"] == "completed"
    assert final["stages"][11]["status"] == "rejected"


def test_validator_warning_is_rejection_evidence_not_stage_crash(
        tmp_path, monkeypatch):
    ledger, artifact, output = _pending_run(tmp_path)
    slicer_path = output / "slicer.json"
    slicer_path.write_text(
        json.dumps(_slicer_evidence(artifact)), encoding="utf-8")
    monkeypatch.setattr(
        "aesthetic.pipeline_acceptance.validate_3mf",
        lambda _path, **_kwargs: {
            "passed": True,
            "errors": [],
            "warnings": ["thin feature"],
            "rules": [],
        },
    )

    report = accept_pipeline_artifact(
        ledger_path=ledger.path,
        artifact_path=artifact,
        slicer_report_path=slicer_path,
    )

    final = PipelineLedger.load(ledger.path, output_dir=output).snapshot()
    assert report["accepted"] is False
    assert report["validator"]["strict_passed"] is False
    assert final["status"] == "validation_rejected"
    assert final["stages"][11]["status"] == "rejected"


def test_tampered_s10_artifact_is_rejected_before_s11_starts(tmp_path):
    ledger, artifact, output = _pending_run(tmp_path)
    artifact.write_bytes(b"tampered")
    slicer_path = output / "slicer.json"
    slicer_path.write_text(
        json.dumps(_slicer_evidence(artifact)), encoding="utf-8")

    with pytest.raises(PipelineLedgerError, match="hash"):
        accept_pipeline_artifact(
            ledger_path=ledger.path,
            artifact_path=artifact,
            slicer_report_path=slicer_path,
        )
    state = PipelineLedger.load(ledger.path, output_dir=output).snapshot()
    assert state["status"] == "generated_pending_validation"
    assert state["stages"][11]["status"] == "pending_validation"


def test_s11_is_running_while_expensive_validator_executes(
        tmp_path, monkeypatch):
    ledger, artifact, output = _pending_run(tmp_path)
    slicer_path = output / "slicer.json"
    slicer_path.write_text(
        json.dumps(_slicer_evidence(artifact)), encoding="utf-8")

    def assert_running(_path, **kwargs):
        live = PipelineLedger.load(
            ledger.path, output_dir=output).snapshot()
        assert live["status"] == "running"
        assert live["current_stage_id"] == "S11"
        assert live["stages"][11]["status"] == "running"
        return _validator_pass(_path, **kwargs)

    monkeypatch.setattr(
        "aesthetic.pipeline_acceptance.validate_3mf", assert_running)
    report = accept_pipeline_artifact(
        ledger_path=ledger.path,
        artifact_path=artifact,
        slicer_report_path=slicer_path,
    )
    assert report["accepted"] is True


def test_3mf_changed_during_validation_fails_s11_before_commit(
        tmp_path, monkeypatch):
    ledger, artifact, output = _pending_run(tmp_path)
    slicer_path = output / "slicer.json"
    slicer_path.write_text(
        json.dumps(_slicer_evidence(artifact)), encoding="utf-8")

    def mutate_after_validation(path, **kwargs):
        result = _validator_pass(path, **kwargs)
        artifact.write_bytes(b"changed-during-validator")
        return result

    monkeypatch.setattr(
        "aesthetic.pipeline_acceptance.validate_3mf",
        mutate_after_validation,
    )
    with pytest.raises(PipelineLedgerError, match="identity changed"):
        accept_pipeline_artifact(
            ledger_path=ledger.path,
            artifact_path=artifact,
            slicer_report_path=slicer_path,
        )

    final = PipelineLedger.load(ledger.path, output_dir=output).snapshot()
    assert final["status"] == "failed"
    assert final["stages"][10]["status"] == "completed"
    assert final["stages"][11]["status"] == "failed"


def test_tampered_s10_sidecar_is_rejected_before_s11_starts(tmp_path):
    ledger, artifact, output = _pending_run(tmp_path)
    design_spec = output / next(
        item["path"] for item in ledger.snapshot()["stages"][10]["artifacts"]
        if item["name"] == "design_spec"
    )
    design_spec.write_text(
        "tampered-design-spec", encoding="utf-8")
    slicer_path = output / "slicer.json"
    slicer_path.write_text(
        json.dumps(_slicer_evidence(artifact)), encoding="utf-8")

    with pytest.raises(PipelineLedgerError, match="design_spec artifact hash"):
        accept_pipeline_artifact(
            ledger_path=ledger.path,
            artifact_path=artifact,
            slicer_report_path=slicer_path,
        )
    state = PipelineLedger.load(ledger.path, output_dir=output).snapshot()
    assert state["status"] == "generated_pending_validation"
    assert state["stages"][11]["status"] == "pending_validation"


def test_s10_sidecar_changed_during_validation_fails_s11_before_commit(
        tmp_path, monkeypatch):
    ledger, artifact, output = _pending_run(tmp_path)
    slicer_path = output / "slicer.json"
    slicer_path.write_text(
        json.dumps(_slicer_evidence(artifact)), encoding="utf-8")

    design_spec = output / next(
        item["path"] for item in ledger.snapshot()["stages"][10]["artifacts"]
        if item["name"] == "design_spec"
    )

    def mutate_sidecar_after_validation(path, **kwargs):
        result = _validator_pass(path, **kwargs)
        design_spec.write_text(
            "changed-during-validator", encoding="utf-8")
        return result

    monkeypatch.setattr(
        "aesthetic.pipeline_acceptance.validate_3mf",
        mutate_sidecar_after_validation,
    )
    with pytest.raises(PipelineLedgerError, match="design_spec artifact hash"):
        accept_pipeline_artifact(
            ledger_path=ledger.path,
            artifact_path=artifact,
            slicer_report_path=slicer_path,
        )

    final = PipelineLedger.load(ledger.path, output_dir=output).snapshot()
    assert final["status"] == "failed"
    assert final["stages"][10]["status"] == "completed"
    assert final["stages"][11]["status"] == "failed"


def test_output_dir_cannot_rebind_ledger_artifact_paths(tmp_path):
    ledger, artifact, output = _pending_run(tmp_path)
    slicer_path = output / "slicer.json"
    slicer_path.write_text(
        json.dumps(_slicer_evidence(artifact)), encoding="utf-8")
    alternate = tmp_path / "alternate"
    alternate.mkdir()

    with pytest.raises(PipelineLedgerError, match="cannot re-bind"):
        accept_pipeline_artifact(
            ledger_path=ledger.path,
            artifact_path=artifact,
            slicer_report_path=slicer_path,
            output_dir=alternate,
        )


def test_slicer_evidence_requires_tool_version_and_completed_slice():
    errors = validate_slicer_evidence(
        {
            "schema_version": SLICER_SCHEMA_VERSION,
            "artifact_sha256": "a" * 64,
            "status": "passed",
            "tool": {"name": "Bambu Studio"},
            "checks": {"loaded": True, "sliced": False},
        },
        artifact_sha256="a" * 64,
    )
    assert "slicer tool version is missing" in errors
    assert "slicer did not prove that slicing completed" in errors
    assert "slicer errors must be a list" in errors
    assert "slicer warnings must be a list" in errors
