import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from aesthetic.pipeline_contract import (
    S10_REQUIRED_ARTIFACT_BUNDLE,
    feature_source_counts_fingerprint,
    stage_by_id,
    stages_for_mode,
)
from aesthetic.pipeline_ledger import (
    ArtifactBoundaryError,
    ConcurrentLedgerUpdateError,
    ContextChainError,
    PipelineLedger,
    PipelineLedgerError,
    StageOrderError,
    validate_pipeline_ledger_state,
)
from aesthetic.pipeline_gates import evaluate_feature_survival


_TEST_3MF_SHA256 = hashlib.sha256(b"artifact:3mf").hexdigest()


def _create(tmp_path, *, mode="full", run_id="run-1"):
    output = tmp_path / "output"
    output.mkdir()
    return PipelineLedger.create(
        output / "pipeline_state.json",
        output_dir=output,
        run_id=run_id,
        attempt_id="attempt-1",
        mode=mode,
        metadata={"city": "Chicago"},
    )


def _complete_through(ledger, terminal_id):
    for stage in stages_for_mode(ledger.mode):
        ledger.start_stage(stage.id, context_in={"request": 1}
                           if stage.id == "S0" else None)
        artifacts = None
        if stage.id == "S10":
            artifacts = {}
            for name in S10_REQUIRED_ARTIFACT_BUNDLE:
                suffix = ".3mf" if name == "3mf" else ".json"
                path = ledger.output_dir / f"{name}{suffix}"
                path.write_bytes(f"artifact:{name}".encode("utf-8"))
                artifacts[name] = path
        ledger.complete_stage(
            stage.id,
            context_out=_context(
                stage.id, **({"mode": ledger.mode} if stage.id == "S0" else {})),
            artifacts=artifacts,
        )
        if stage.id == terminal_id:
            break


def _context(stage_id, **overrides):
    context = {
        key: f"{stage_id}:{key}"
        for key in stage_by_id(stage_id).required_context_keys
    }
    preprocess_parameters = {
        "schema_version": "preprocess-parameters-v1", "effective_overrides": {}}
    preprocess_fingerprint = hashlib.sha256(json.dumps(
        preprocess_parameters, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    source_counts = {name: (1 if name == "roads" else 0) for name in (
        "roads", "water", "buildings", "vegetation")}
    source_fingerprint = feature_source_counts_fingerprint(source_counts)
    terrain_fingerprint = "a" * 64
    order = int(stage_id[1:])
    if 2 <= order <= 10:
        context.update(
            preprocess_parameters_fingerprint=preprocess_fingerprint,
            source_feature_counts_fingerprint=source_fingerprint,
            terrain_surface_fingerprint=terrain_fingerprint,
        )
    if 4 <= order <= 10:
        context["scene_character_fingerprint"] = "b" * 64
    if 5 <= order <= 10:
        context["scene_policy_fingerprint"] = "c" * 64
    if stage_id == "S0":
        context.update(
            city="Chicago", mode="full", bbox_wgs84=[41.8, -87.7, 41.9, -87.6],
            scale_mm_per_m=0.04, printer_profile_id="test-printer")
    elif stage_id == "S1":
        context.update(
            raw_feature_counts={**source_counts, "landuse": 0},
            dem_evidence={
                "status": "ready", "source": "test", "flat_fallback": False},
            osmium_backend="native_osmium")
    elif stage_id == "S2":
        context.update(
            projected_feature_counts=source_counts,
            bbox_local_m=[0.0, 0.0, 1000.0, 1000.0],
            terrain_surface_plan={"fingerprint": terrain_fingerprint},
            preprocess_parameters=preprocess_parameters,
            preprocess_parameters_fingerprint=preprocess_fingerprint)
    elif stage_id == "S3":
        context.update(
            layer_counts={
                "BL": 0, "BO": 0, "WL": 0, "WO": 0, "VL": 0,
                "VO": 0, "roads": 1, "block_base": 0},
            preprocess_policy_version="test-policy-v1")
    elif stage_id == "S4":
        context.update(
            scene_character_version="scene-character-v1",
            scene_status="ready", source_quality={})
    elif stage_id == "S5":
        context.update(
            scene_policy_version="scene-policy-v1", activation="audit_only",
            scene_class="urban", archetype="grid")
    elif stage_id == "S6":
        context.update(
            final_layer_counts={
                "BL": 0, "BO": 0, "WL": 0, "WO": 0, "VL": 0,
                "VO": 0, "roads": 1, "block_base": 0},
            building_mass_status="inactive",
            height_hierarchy_status="inactive")
    elif stage_id == "S7":
        context["review_artifacts"] = []
    elif stage_id == "S8":
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
    elif stage_id == "S9":
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
    elif stage_id == "S10":
        context.update(
            status="generated_pending_validation",
            artifact_bundle=list(S10_REQUIRED_ARTIFACT_BUNDLE),
            s9_errors=0,
            s9_warnings=0,
        )
    elif stage_id == "S11":
        context.update(
            artifact_sha256=_TEST_3MF_SHA256,
            accepted=True,
            errors=[],
            warnings=[],
            validator_strict_passed=True,
            slicer_status="passed",
        )
    context.update(overrides)
    return context


def _record_s11_reports(ledger):
    validator = ledger.output_dir / "validator_report.json"
    acceptance = ledger.output_dir / "acceptance_report.json"
    validator.write_text("{}", encoding="utf-8")
    acceptance.write_text("{}", encoding="utf-8")
    ledger.record_artifact(
        "S11", name="validator_report", path=validator,
        media_type="application/json")
    ledger.record_artifact(
        "S11", name="acceptance_report", path=acceptance,
        media_type="application/json")


def test_ledger_create_has_run_attempt_revision_and_mode_statuses(tmp_path):
    ledger = _create(tmp_path, mode="review")
    state = ledger.snapshot()

    assert state["run_id"] == "run-1"
    assert state["attempt_id"] == "attempt-1"
    assert state["revision"] == 1
    assert state["status"] == "initialized"
    assert [stage["status"] for stage in state["stages"][:8]] == [
        "pending"
    ] * 8
    assert [stage["status"] for stage in state["stages"][8:]] == [
        "not_applicable"
    ] * 4
    assert json.loads(ledger.path.read_text())["revision"] == 1
    assert not list(ledger.path.parent.glob(".*.tmp"))


def test_ledger_enforces_order_and_exact_context_handoff(tmp_path):
    ledger = _create(tmp_path)
    with pytest.raises(StageOrderError):
        ledger.start_stage("S1")

    ledger.start_stage("S0", context_in={"request": "city"})
    s0_context = _context("S0", resolved="run")
    ledger.complete_stage("S0", context_out=s0_context)
    with pytest.raises(ContextChainError):
        ledger.start_stage("S1", context_in={"different": True})

    started = ledger.start_stage("S1", context_in=s0_context)
    assert started["context_in"] == ledger.snapshot()["stages"][0][
        "context_out"
    ]
    with pytest.raises(StageOrderError):
        ledger.start_stage("S2")


def test_s0_context_mode_must_match_ledger_mode(tmp_path):
    ledger = _create(tmp_path, mode="review")
    ledger.start_stage("S0", context_in={"request": "city"})

    with pytest.raises(PipelineLedgerError, match="must match"):
        ledger.complete_stage("S0", context_out=_context("S0", mode="full"))

    ledger.complete_stage(
        "S0", context_out=_context("S0", mode="review"))


def test_fetch_mode_stops_after_raw_data_without_fake_later_success(tmp_path):
    ledger = _create(tmp_path, mode="fetch")
    _complete_through(ledger, "S1")

    state = ledger.snapshot()
    assert state["status"] == "completed"
    assert state["stages"][1]["status"] == "completed"
    assert state["stages"][2]["status"] == "not_applicable"
    with pytest.raises(StageOrderError):
        ledger.start_stage("S2")


def test_artifact_is_relative_hashed_and_must_stay_inside_output(tmp_path):
    ledger = _create(tmp_path, mode="fetch")
    artifact = ledger.output_dir / "raw" / "extract.osm.pbf"
    artifact.parent.mkdir()
    artifact.write_bytes(b"portable-osmium-result")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")

    ledger.start_stage("S0", context_in={"request": 1})
    recorded = ledger.record_artifact(
        "S0", name="source-registry", path=artifact,
        media_type="application/octet-stream",
    )
    assert recorded["path"] == "raw/extract.osm.pbf"
    assert recorded["sha256"] == hashlib.sha256(
        b"portable-osmium-result").hexdigest()
    assert recorded["size_bytes"] == len(b"portable-osmium-result")
    with pytest.raises(ArtifactBoundaryError):
        ledger.record_artifact("S0", name="outside", path=outside)


def test_s10_completion_requires_explicit_s11_validation(tmp_path):
    ledger = _create(tmp_path, mode="full")
    _complete_through(ledger, "S10")

    pending = ledger.snapshot()
    assert pending["status"] == "generated_pending_validation"
    assert pending["current_stage_id"] == "S11"
    assert pending["stages"][10]["status"] == "completed"
    assert pending["stages"][11]["status"] == "pending_validation"

    ledger.start_stage("S11")
    with pytest.raises(PipelineLedgerError, match="requires validator_report"):
        ledger.complete_stage("S11", context_out=_context(
            "S11", accepted=True, errors=[], warnings=[]))
    _record_s11_reports(ledger)
    ledger.complete_stage("S11", context_out=_context(
        "S11", accepted=True, errors=[], warnings=[]))
    validated = ledger.snapshot()
    assert validated["status"] == "validated"
    assert validated["stages"][11]["context_out"]["type"] == (
        "AcceptanceReport")


def test_s11_artifact_hash_must_match_the_s10_3mf_claim(tmp_path):
    ledger = _create(tmp_path, mode="full")
    _complete_through(ledger, "S10")
    ledger.start_stage("S11")
    _record_s11_reports(ledger)

    with pytest.raises(
            PipelineLedgerError, match="S11 artifact_sha256.*S10 3mf"):
        ledger.complete_stage(
            "S11",
            context_out=_context("S11", artifact_sha256="f" * 64),
        )

    assert ledger.snapshot()["stages"][11]["status"] == "running"


def test_s9_required_roles_must_equal_s8_ready_semantic_roles(tmp_path):
    ledger = _create(tmp_path, mode="full")
    _complete_through(ledger, "S7")
    ledger.start_stage("S8")
    ledger.complete_stage(
        "S8",
        context_out=_context("S8", mesh_summary={
            "terrain": {
                "status": "ready", "vertices": 8, "faces": 12,
                "watertight": True, "winding_consistent": True,
                "bounds_mm": [[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]],
            },
            "roads": {
                "status": "ready", "vertices": 6, "faces": 8,
                "watertight": True, "winding_consistent": True,
                "bounds_mm": [[0.0, 0.0, 0.0], [1.0, 1.0, 0.2]],
            },
            "water": {
                "status": "ready", "vertices": 6, "faces": 8,
                "watertight": True, "winding_consistent": True,
                "bounds_mm": [[0.0, 0.0, 0.0], [1.0, 1.0, 0.2]],
            },
            # Explicitly absent/omitted roles are not required at S9.
            "vegetation": {"status": "absent", "vertices": 0, "faces": 0},
        }),
    )
    ledger.start_stage("S9")

    with pytest.raises(PipelineLedgerError, match="ungated ready roles: water"):
        ledger.complete_stage("S9", context_out=_context("S9"))

    assert ledger.snapshot()["stages"][9]["status"] == "running"


def test_s9_mesh_counts_must_match_s8_mesh_summary(tmp_path):
    ledger = _create(tmp_path, mode="full")
    _complete_through(ledger, "S8")
    ledger.start_stage("S9")
    context = _context("S9")
    context["mesh_metrics"]["terrain"]["vertices"] = 9

    with pytest.raises(
            PipelineLedgerError, match="S9 terrain vertices.*S8"):
        ledger.complete_stage("S9", context_out=context)

    assert ledger.snapshot()["stages"][9]["status"] == "running"


def test_pre_v2_s8_summary_without_winding_remains_loadable(tmp_path):
    ledger = _create(tmp_path, mode="full")
    _complete_through(ledger, "S7")
    ledger.start_stage("S8")
    legacy_s8 = _context("S8")
    legacy_s8.pop("mesh_summary_version")
    for evidence in legacy_s8["mesh_summary"].values():
        evidence.pop("winding_consistent", None)
    ledger.complete_stage("S8", context_out=legacy_s8)
    ledger.start_stage("S9")
    ledger.complete_stage("S9", context_out=_context("S9"))

    reloaded = PipelineLedger.load(ledger.path)
    assert reloaded.snapshot()["stages"][9]["status"] == "completed"


@pytest.mark.parametrize(("field", "replacement"), [
    ("watertight", False),
    ("winding_consistent", False),
    ("bounds_mm", [[0.0, 0.0, 0.0], [2.0, 1.0, 1.0]]),
])
def test_s9_full_mesh_metrics_must_match_s8_summary(
        tmp_path, field, replacement):
    ledger = _create(tmp_path, mode="full")
    _complete_through(ledger, "S7")
    ledger.start_stage("S8")
    s8_context = _context("S8")
    s8_context["mesh_summary"]["terrain"][field] = replacement
    ledger.complete_stage("S8", context_out=s8_context)
    ledger.start_stage("S9")

    with pytest.raises(
            PipelineLedgerError,
            match=rf"S9 terrain {field}.*S8 mesh summary"):
        ledger.complete_stage("S9", context_out=_context("S9"))

    assert ledger.snapshot()["stages"][9]["status"] == "running"


def test_nonzero_road_family_requires_a_ready_s8_road_mesh(tmp_path):
    ledger = _create(tmp_path, mode="full")
    _complete_through(ledger, "S7")
    ledger.start_stage("S8")
    s8_context = _context("S8")
    s8_context["mesh_summary"] = {
        "terrain": s8_context["mesh_summary"]["terrain"],
    }
    ledger.complete_stage("S8", context_out=s8_context)
    ledger.start_stage("S9")
    s9_context = _context("S9")
    s9_context["required_roles"] = ["terrain"]
    s9_context["mesh_metrics"] = {
        "terrain": s9_context["mesh_metrics"]["terrain"],
    }

    with pytest.raises(
            PipelineLedgerError,
            match="non-zero roads has no ready S8 roads mesh role"):
        ledger.complete_stage("S9", context_out=s9_context)

    assert ledger.snapshot()["stages"][9]["status"] == "running"


@pytest.mark.parametrize(("final_overrides", "message"), [
    ({"WL": 1}, "non-zero water has no ready S8 water mesh role"),
    ({"VL": 1},
     "non-zero vegetation has no ready S8 vegetation mesh role"),
    ({"BL": 1},
     "non-zero buildings.BL has no ready S8 landmarks mesh role"),
    ({"BO": 1},
     "non-zero buildings.BO has no ready S8 buildings or block_base"),
])
def test_nonzero_feature_families_require_their_semantic_mesh_routes(
        tmp_path, final_overrides, message):
    ledger = _create(tmp_path, mode="full")
    _complete_through(ledger, "S5")
    final_counts = {
        "BL": 0, "BO": 0, "WL": 0, "WO": 0, "VL": 0,
        "VO": 0, "roads": 1, "block_base": 0,
    }
    final_counts.update(final_overrides)
    ledger.start_stage("S6")
    ledger.complete_stage(
        "S6", context_out=_context(
            "S6", final_layer_counts=final_counts))
    for stage_id in ("S7", "S8"):
        ledger.start_stage(stage_id)
        ledger.complete_stage(stage_id, context_out=_context(stage_id))
    ledger.start_stage("S9")
    s9_context = _context("S9")
    s9_context["feature_survival"] = evaluate_feature_survival(
        {"roads": 1}, final_counts)

    with pytest.raises(PipelineLedgerError, match=message):
        ledger.complete_stage("S9", context_out=s9_context)

    assert ledger.snapshot()["stages"][9]["status"] == "running"


def test_s9_survival_final_counts_must_match_s6_final_layers(tmp_path):
    ledger = _create(tmp_path, mode="full")
    _complete_through(ledger, "S8")
    ledger.start_stage("S9")
    context = _context("S9")
    context["feature_survival"] = evaluate_feature_survival(
        {"roads": 1}, {"roads": 2})

    with pytest.raises(
            PipelineLedgerError,
            match="feature_survival roads final_count.*S6"):
        ledger.complete_stage("S9", context_out=context)

    assert ledger.snapshot()["stages"][9]["status"] == "running"


def test_generic_complete_stage_cannot_bypass_s9_semantic_gate(tmp_path):
    ledger = _create(tmp_path, mode="full")
    _complete_through(ledger, "S8")
    ledger.start_stage("S9")

    with pytest.raises(ValueError, match="S9 cannot complete"):
        ledger.complete_stage(
            "S9",
            context_out=_context(
                "S9", passed=False, errors=["non-manifold"], warnings=[]),
        )

    state = ledger.snapshot()
    assert state["stages"][9]["status"] == "running"
    assert state["stages"][10]["status"] == "pending"


def test_s10_context_cannot_claim_an_unrecorded_artifact_bundle(tmp_path):
    ledger = _create(tmp_path, mode="full")
    _complete_through(ledger, "S9")
    ledger.start_stage("S10")
    artifact = ledger.output_dir / "only.3mf"
    artifact.write_bytes(b"incomplete bundle")

    with pytest.raises(PipelineLedgerError, match="not recorded"):
        ledger.complete_stage(
            "S10",
            context_out=_context("S10"),
            artifacts={"3mf": artifact},
        )

    assert ledger.snapshot()["stages"][10]["status"] == "running"


def test_generic_complete_stage_cannot_turn_failed_s11_into_validated(
        tmp_path):
    ledger = _create(tmp_path, mode="full")
    _complete_through(ledger, "S10")
    ledger.start_stage("S11")

    with pytest.raises(ValueError, match="S11 cannot complete"):
        ledger.complete_stage(
            "S11",
            context_out=_context(
                "S11",
                accepted=False,
                errors=["slicer rejected"],
                warnings=[],
                validator_strict_passed=True,
                slicer_status="failed",
            ),
        )

    assert ledger.snapshot()["status"] == "running"


def test_rejected_validation_preserves_s10_without_marking_run_validated(
        tmp_path):
    ledger = _create(tmp_path, mode="full")
    _complete_through(ledger, "S10")
    ledger.start_stage("S11")
    _record_s11_reports(ledger)
    rejected = ledger.reject_validation(
        context_out=_context(
            "S11", accepted=False, errors=["slicer rejected"], warnings=[]),
        reason="slicer rejected",
    )

    state = ledger.snapshot()
    assert rejected["status"] == "rejected"
    assert state["status"] == "validation_rejected"
    assert state["stages"][10]["status"] == "completed"
    assert state["stages"][11]["context_out"]["value"]["accepted"] is False
    with pytest.raises(StageOrderError):
        ledger.start_stage("S11")


def test_failed_stage_is_terminal_for_attempt(tmp_path):
    ledger = _create(tmp_path)
    ledger.start_stage("S0", context_in={"request": 1})
    ledger.fail_stage("S0", error="source unavailable")

    assert ledger.snapshot()["status"] == "failed"
    with pytest.raises(StageOrderError):
        ledger.start_stage("S1")


def test_shared_validator_rejects_root_status_that_hides_failed_stage(
        tmp_path):
    ledger = _create(tmp_path)
    ledger.start_stage("S0", context_in={"request": 1})
    ledger.fail_stage("S0", error="source unavailable")
    state = ledger.snapshot()
    state["status"] = "running"
    state["current_stage_id"] = None

    with pytest.raises(PipelineLedgerError, match="root state"):
        validate_pipeline_ledger_state(state)


@pytest.mark.parametrize("field,value", [
    ("mode", "invented"),
    ("run_id", {"not": "a string"}),
    ("attempt_id", ""),
])
def test_shared_validator_rejects_invalid_identity_and_mode(
        tmp_path, field, value):
    ledger = _create(tmp_path)
    state = ledger.snapshot()
    state[field] = value

    with pytest.raises(PipelineLedgerError):
        validate_pipeline_ledger_state(state)


def test_stage_cannot_claim_a_different_preprocess_parameter_fingerprint(
        tmp_path):
    ledger = _create(tmp_path)
    for stage_id in ("S0", "S1", "S2"):
        ledger.start_stage(
            stage_id, context_in={"request": 1} if stage_id == "S0" else None)
        ledger.complete_stage(stage_id, context_out=_context(stage_id))
    ledger.start_stage("S3")

    with pytest.raises(ContextChainError, match="preprocess_parameters"):
        ledger.complete_stage(
            "S3",
            context_out=_context(
                "S3", preprocess_parameters_fingerprint="f" * 64),
        )


@pytest.mark.parametrize("stage_id,key", [
    ("S4", "source_feature_counts_fingerprint"),
    ("S5", "scene_character_fingerprint"),
    ("S6", "scene_policy_fingerprint"),
    ("S7", "terrain_surface_fingerprint"),
    ("S8", "preprocess_parameters_fingerprint"),
    ("S9", "terrain_surface_fingerprint"),
    ("S10", "scene_policy_fingerprint"),
])
def test_lineage_stages_cannot_replace_predecessor_fingerprint(
        tmp_path, stage_id, key):
    ledger = _create(tmp_path)
    for spec in stages_for_mode(ledger.mode):
        ledger.start_stage(
            spec.id, context_in={"request": 1} if spec.id == "S0" else None)
        if spec.id == stage_id:
            with pytest.raises(ContextChainError, match=key):
                ledger.complete_stage(
                    spec.id,
                    context_out=_context(spec.id, **{key: "f" * 64}),
                )
            break
        ledger.complete_stage(spec.id, context_out=_context(spec.id))


def test_stale_loaded_writer_cannot_overwrite_newer_revision(tmp_path):
    ledger = _create(tmp_path)
    stale = PipelineLedger.load(ledger.path, output_dir=ledger.output_dir)
    ledger.start_stage("S0", context_in={"request": 1})

    with pytest.raises(ConcurrentLedgerUpdateError):
        stale.start_stage("S0", context_in={"request": 1})


def test_revision_compare_and_replace_is_atomic_for_concurrent_writers(
        tmp_path, monkeypatch):
    ledger = _create(tmp_path)
    writer_a = PipelineLedger.load(ledger.path, output_dir=ledger.output_dir)
    writer_b = PipelineLedger.load(ledger.path, output_dir=ledger.output_dir)

    # Keep the first writer between its revision check and atomic replace long
    # enough for the second writer to reach the same race window.  With the
    # sidecar lock, the second writer cannot enter this function until after
    # the first revision is durable, so its CAS must fail as stale.
    import aesthetic.pipeline_ledger as ledger_module

    original_write = ledger_module._atomic_write_json
    first_write_entered = threading.Event()
    release_first_write = threading.Event()
    entry_lock = threading.Lock()
    write_entries = 0

    def controlled_write(path, value):
        nonlocal write_entries
        if value.get("revision") == 2:
            with entry_lock:
                write_entries += 1
                ordinal = write_entries
            if ordinal == 1:
                first_write_entered.set()
                assert release_first_write.wait(timeout=5)
        return original_write(path, value)

    monkeypatch.setattr(ledger_module, "_atomic_write_json", controlled_write)
    start_together = threading.Barrier(3)

    def advance(candidate):
        start_together.wait(timeout=5)
        try:
            candidate.start_stage("S0", context_in={"request": 1})
        except ConcurrentLedgerUpdateError:
            return "stale"
        return "committed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(advance, item) for item in (writer_a, writer_b)]
        start_together.wait(timeout=5)
        assert first_write_entered.wait(timeout=5)
        # Give an unlocked implementation a deterministic opportunity to let
        # both writers pass the revision comparison before either replaces.
        time.sleep(0.1)
        release_first_write.set()
        outcomes = [future.result(timeout=5) for future in futures]

    assert sorted(outcomes) == ["committed", "stale"]
    persisted = PipelineLedger.load(ledger.path, output_dir=ledger.output_dir)
    assert persisted.revision == 2
    assert persisted.snapshot()["stages"][0]["status"] == "running"
    assert write_entries == 1


def test_load_infers_output_root_above_pipeline_runs_directory(tmp_path):
    output = tmp_path / "output"
    ledger_path = output / ".pipeline_runs" / "pipeline_state.run.attempt.json"
    ledger = PipelineLedger.create(
        ledger_path,
        output_dir=output,
        run_id="run-1",
        attempt_id="attempt-1",
        mode="fetch",
    )

    loaded = PipelineLedger.load(ledger.path)

    assert loaded.output_dir == output.resolve()
    artifact = output / "raw" / "extract.osm.pbf"
    artifact.parent.mkdir()
    artifact.write_bytes(b"valid extract")
    loaded.start_stage("S0", context_in={"request": 1})
    recorded = loaded.record_artifact(
        "S0", name="source", path=artifact,
    )
    assert recorded["path"] == "raw/extract.osm.pbf"
