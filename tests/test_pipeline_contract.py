import pytest

from aesthetic.pipeline_contract import (
    CONTRACT_VERSION,
    PIPELINE_STAGES,
    S10_REQUIRED_ARTIFACT_BUNDLE,
    contract_as_dict,
    feature_source_counts_fingerprint,
    is_stage_applicable,
    stage_by_id,
    stages_for_mode,
    terminal_stage,
    validate_stage_context,
)
from aesthetic.pipeline_gates import evaluate_feature_survival


EMPTY_SOURCE_COUNTS = {
    "roads": 0, "water": 0, "buildings": 0, "vegetation": 0}
EMPTY_SOURCE_FINGERPRINT = feature_source_counts_fingerprint(
    EMPTY_SOURCE_COUNTS)
PROJECTED_SOURCE_COUNTS = {
    "roads": 1, "water": 0, "buildings": 0, "vegetation": 0}
PROJECTED_SOURCE_FINGERPRINT = feature_source_counts_fingerprint(
    PROJECTED_SOURCE_COUNTS)
VALID_REQUIRED_ROLES = ["terrain"]
VALID_MESH_METRICS = {
    "terrain": {
        "present": True,
        "vertices": 8,
        "faces": 12,
        "watertight": True,
        "winding_consistent": True,
        "bounds_mm": [[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]],
    },
}
VALID_LINEAGE = {
    "preprocess_parameters_fingerprint": "1" * 64,
    "source_feature_counts_fingerprint": EMPTY_SOURCE_FINGERPRINT,
    "terrain_surface_fingerprint": "2" * 64,
    "scene_character_fingerprint": "3" * 64,
    "scene_policy_fingerprint": "4" * 64,
}


def _valid_s0_context():
    return {
        "city": "contract_test",
        "mode": "full",
        "bbox_wgs84": [41.8, -87.7, 41.9, -87.6],
        "scale_mm_per_m": 0.008,
        "printer_profile_id": "bambu-0.4mm",
    }


def _valid_s1_context():
    return {
        "raw_feature_counts": dict(PROJECTED_SOURCE_COUNTS),
        "dem_evidence": {
            "status": "ready",
            "flat_fallback": False,
            "source": "fixture-dem.tif",
        },
        "osmium_backend": "native-osmium",
    }


def _valid_s8_context():
    return {
        **VALID_LINEAGE,
        "mesh_summary_version": "semantic-mesh-summary-v2",
        "mesh_summary": {
            "terrain": {
                "status": "ready", "vertices": 8, "faces": 12,
                "watertight": True, "winding_consistent": True,
                "bounds_mm": [[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]],
            },
        },
        "water_relief_status": "not_applicable",
    }


def test_contract_is_complete_ordered_and_context_chained():
    assert CONTRACT_VERSION == "generation-pipeline-contract-v3"
    assert [stage.id for stage in PIPELINE_STAGES] == [
        f"S{index}" for index in range(12)
    ]
    assert [stage.order for stage in PIPELINE_STAGES] == list(range(12))
    for previous, current in zip(PIPELINE_STAGES, PIPELINE_STAGES[1:]):
        assert previous.context_out == current.context_in


@pytest.mark.parametrize(("mode", "terminal", "count"), [
    ("fetch", "S1", 2),
    ("styles", "S7", 8),
    ("review", "S7", 8),
    ("draft", "S7", 8),
    ("full", "S11", 12),
])
def test_mode_applicability_is_a_contiguous_pipeline_prefix(
        mode, terminal, count):
    stages = stages_for_mode(mode)
    assert len(stages) == count
    assert terminal_stage(mode).id == terminal
    assert [stage.order for stage in stages] == list(range(count))
    for stage in PIPELINE_STAGES:
        assert is_stage_applicable(stage.id, mode) is (stage.order < count)


def test_stage_specs_are_immutable_and_lookup_rejects_unknown_values():
    with pytest.raises(AttributeError):
        stage_by_id("S0").name = "mutated"
    with pytest.raises(KeyError):
        stage_by_id("S99")
    with pytest.raises(ValueError):
        stages_for_mode("unknown")


def test_contract_snapshot_is_json_safe_and_contains_ui_fields():
    report = contract_as_dict()
    assert report["terminal_stages"]["full"] == "S11"
    assert report["terminal_stages"]["review"] == "S7"
    first = report["stages"][0]
    assert first["id"] == "S0"
    assert first["progress_threshold"] == 0
    assert first["context_in"] == "RunRequest"
    assert first["inputs"]
    assert first["outputs"]


def test_terrain_surface_plan_has_one_owner_and_all_3d_consumers_reuse_it():
    assert "TerrainSurfacePlan" in stage_by_id("S2").outputs
    assert "TerrainSurfacePlan" in stage_by_id("S6").inputs
    assert "TerrainSurfacePlan" in stage_by_id("S7").inputs
    assert "TerrainSurfacePlan" in stage_by_id("S8").inputs
    assert "DEM" not in stage_by_id("S8").inputs


@pytest.mark.parametrize(("overrides", "message"), [
    ({"city": ""}, "city"),
    ({"mode": "preview-ish"}, "mode"),
    ({"bbox_wgs84": [41.9, -87.7, 41.8, -87.6]}, "bbox_wgs84"),
    ({"scale_mm_per_m": 0.0}, "scale_mm_per_m"),
    ({"printer_profile_id": ""}, "printer_profile_id"),
])
def test_s0_rejects_invalid_resolved_run_specs(overrides, message):
    context = _valid_s0_context()
    context.update(overrides)
    with pytest.raises(ValueError, match=message):
        validate_stage_context("S0", context)


@pytest.mark.parametrize(("overrides", "message"), [
    ({"raw_feature_counts": {
        "roads": 0, "water": 0, "buildings": 0, "vegetation": 0,
    }}, "entirely zero"),
    ({"raw_feature_counts": {
        "roads": -1, "water": 0, "buildings": 0, "vegetation": 0,
    }}, "non-negative"),
    ({"dem_evidence": {
        "status": "flat_fallback", "flat_fallback": False,
        "source": "fixture-dem.tif",
    }}, "inconsistent"),
    ({"osmium_backend": ""}, "osmium_backend"),
])
def test_s1_rejects_false_or_incomplete_source_evidence(overrides, message):
    context = _valid_s1_context()
    context.update(overrides)
    with pytest.raises(ValueError, match=message):
        validate_stage_context("S1", context)


@pytest.mark.parametrize(("overrides", "message"), [
    ({"mesh_summary": {}}, "terrain mesh"),
    ({"mesh_summary": {
        "terrain": {"status": "ready", "vertices": 0, "faces": 12},
    }}, "ready mesh"),
    ({"mesh_summary": {
        "terrain": {
            "status": "ready", "vertices": 8, "faces": 12,
            "watertight": True,
            "bounds_mm": [[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]],
        },
    }}, "winding_consistent"),
    ({"water_relief_status": "unknown"}, "water_relief_status"),
])
def test_s8_rejects_missing_mesh_or_unknown_water_evidence(
        overrides, message):
    context = _valid_s8_context()
    context.update(overrides)
    with pytest.raises(ValueError, match=message):
        validate_stage_context("S8", context)


def test_s8_accepts_pre_v2_persisted_summary_without_winding_evidence():
    context = _valid_s8_context()
    context.pop("mesh_summary_version")
    context["mesh_summary"]["terrain"].pop("winding_consistent")

    validate_stage_context("S8", context)


def test_stage_output_context_contract_rejects_missing_handoff_evidence():
    required = stage_by_id("S9").required_context_keys
    assert "passed" in required
    assert "mesh_metrics" in required
    with pytest.raises(ValueError, match="missing required keys"):
        validate_stage_context("S9", {"passed": True})
    valid = {key: f"evidence:{key}" for key in required}
    valid.update(
        VALID_LINEAGE,
        passed=True, errors=[], warnings=[],
        gate_version="semantic-mesh-gate-v1",
        required_roles=VALID_REQUIRED_ROLES,
        mesh_metrics=VALID_MESH_METRICS,
        source_feature_counts_fingerprint=EMPTY_SOURCE_FINGERPRINT,
        feature_survival=evaluate_feature_survival({}, {}),
    )
    validate_stage_context("S9", valid)


def test_s2_preprocess_parameter_fingerprint_is_content_bound():
    import hashlib
    import json

    parameters = {
        "schema_version": "preprocess-parameters-v1",
        "effective_overrides": {"road_tier_override": 2},
    }
    fingerprint = hashlib.sha256(json.dumps(
        parameters, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    context = {
        key: f"S2:{key}" for key in stage_by_id("S2").required_context_keys
    }
    context.update(
        projected_feature_counts=PROJECTED_SOURCE_COUNTS,
        source_feature_counts_fingerprint=PROJECTED_SOURCE_FINGERPRINT,
        bbox_local_m=[0.0, 0.0, 1000.0, 1000.0],
        terrain_surface_plan={"fingerprint": "a" * 64},
        terrain_surface_fingerprint="a" * 64,
        preprocess_parameters=parameters,
        preprocess_parameters_fingerprint=fingerprint,
    )
    validate_stage_context("S2", context)

    context["preprocess_parameters"]["effective_overrides"][
        "road_tier_override"] = 3
    with pytest.raises(ValueError, match="fingerprint"):
        validate_stage_context("S2", context)


def test_s2_source_counts_and_terrain_plan_are_content_bound():
    import hashlib
    import json

    parameters = {
        "schema_version": "preprocess-parameters-v1",
        "effective_overrides": {},
    }
    context = {
        key: f"S2:{key}" for key in stage_by_id("S2").required_context_keys
    }
    context.update({
        "projected_feature_counts": dict(PROJECTED_SOURCE_COUNTS),
        "source_feature_counts_fingerprint": PROJECTED_SOURCE_FINGERPRINT,
        "bbox_local_m": [0.0, 0.0, 1000.0, 1000.0],
        "terrain_surface_plan": {"fingerprint": "a" * 64},
        "terrain_surface_fingerprint": "a" * 64,
        "preprocess_parameters": parameters,
        "preprocess_parameters_fingerprint": hashlib.sha256(json.dumps(
            parameters, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        ).encode()).hexdigest(),
    })
    validate_stage_context("S2", context)

    context["projected_feature_counts"]["roads"] = 2
    with pytest.raises(ValueError, match="source feature counts fingerprint"):
        validate_stage_context("S2", context)
    context["projected_feature_counts"]["roads"] = 1
    context["terrain_surface_plan"]["fingerprint"] = "b" * 64
    with pytest.raises(ValueError, match="terrain surface fingerprint"):
        validate_stage_context("S2", context)


@pytest.mark.parametrize("overrides", [
    {"passed": False},
    {"errors": ["non-manifold"]},
    {"warnings": ["clearance uncertain"]},
])
def test_s9_semantic_completion_is_fail_closed(overrides):
    context = {
        key: f"S9:{key}" for key in stage_by_id("S9").required_context_keys
    }
    context.update(
        VALID_LINEAGE,
        passed=True, errors=[], warnings=[],
        gate_version="semantic-mesh-gate-v1",
        required_roles=VALID_REQUIRED_ROLES,
        mesh_metrics=VALID_MESH_METRICS,
        source_feature_counts_fingerprint=EMPTY_SOURCE_FINGERPRINT,
        feature_survival=evaluate_feature_survival({}, {}),
    )
    context.update(overrides)
    with pytest.raises(ValueError, match="S9 cannot complete"):
        validate_stage_context("S9", context)


@pytest.mark.parametrize("survival", [
    {"passed": True, "errors": []},
    evaluate_feature_survival({}, {}) | {"policy_version": "invented"},
    evaluate_feature_survival({}, {}) | {"scope": "retention_quality"},
    evaluate_feature_survival({}, {}) | {"roles": {}},
    evaluate_feature_survival({}, {}) | {
        "intentional_omissions": ["roads"]},
])
def test_s9_rejects_incomplete_or_inconsistent_survival_evidence(survival):
    context = {
        key: f"S9:{key}" for key in stage_by_id("S9").required_context_keys
    }
    context.update(
        VALID_LINEAGE,
        passed=True, errors=[], warnings=[],
        gate_version="semantic-mesh-gate-v1",
        required_roles=VALID_REQUIRED_ROLES,
        mesh_metrics=VALID_MESH_METRICS,
        source_feature_counts_fingerprint=EMPTY_SOURCE_FINGERPRINT,
        feature_survival=survival)

    with pytest.raises(ValueError, match="feature_survival"):
        validate_stage_context("S9", context)


def test_s9_survival_must_be_bound_to_the_carried_s2_source_identity():
    context = {
        key: f"S9:{key}" for key in stage_by_id("S9").required_context_keys
    }
    context.update(
        VALID_LINEAGE,
        passed=True, errors=[], warnings=[],
        gate_version="semantic-mesh-gate-v1",
        required_roles=VALID_REQUIRED_ROLES,
        mesh_metrics=VALID_MESH_METRICS,
        source_feature_counts_fingerprint="f" * 64,
        feature_survival=evaluate_feature_survival({}, {}),
    )

    with pytest.raises(ValueError, match="not bound to the S2 source counts"):
        validate_stage_context("S9", context)


@pytest.mark.parametrize(("overrides", "message"), [
    ({"gate_version": "invented"}, "gate_version"),
    ({"required_roles": "terrain"}, "required_roles"),
    ({"required_roles": ["roads"]}, "include terrain"),
    ({"required_roles": ["terrain", "terrain"]}, "unique"),
    ({"required_roles": ["terrain", "unknown"]}, "unknown roles"),
    ({"mesh_metrics": {}}, "exactly match"),
    ({"mesh_metrics": {"terrain": {"present": True}}}, "vertices"),
    ({"mesh_metrics": {"terrain": {
        "present": True, "vertices": 8, "faces": 12,
        "watertight": False, "winding_consistent": True,
        "bounds_mm": [[0, 0, 0], [1, 1, 1]],
    }}}, "not watertight"),
    ({"mesh_metrics": {"terrain": {
        "present": True, "vertices": 8, "faces": 12,
        "watertight": True, "winding_consistent": True,
        "bounds_mm": [[1, 0, 0], [0, 1, 1]],
    }}}, "inverted"),
])
def test_s9_mesh_gate_requires_complete_role_bound_geometry_evidence(
        overrides, message):
    context = {
        key: f"S9:{key}" for key in stage_by_id("S9").required_context_keys
    }
    context.update(
        VALID_LINEAGE,
        gate_version="semantic-mesh-gate-v1",
        passed=True,
        required_roles=VALID_REQUIRED_ROLES,
        errors=[],
        warnings=[],
        mesh_metrics=VALID_MESH_METRICS,
        source_feature_counts_fingerprint=EMPTY_SOURCE_FINGERPRINT,
        feature_survival=evaluate_feature_survival({}, {}),
    )
    context.update(overrides)

    with pytest.raises(ValueError, match=message):
        validate_stage_context("S9", context)


@pytest.mark.parametrize("overrides", [
    {"status": "completed"},
    {"artifact_bundle": ["3mf", "design_spec"]},
    {"s9_errors": 1},
    {"s9_warnings": 1},
])
def test_s10_semantic_completion_requires_strict_artifact_bundle(overrides):
    context = {
        key: f"S10:{key}"
        for key in stage_by_id("S10").required_context_keys
    }
    context.update(
        VALID_LINEAGE,
        status="generated_pending_validation",
        artifact_bundle=list(S10_REQUIRED_ARTIFACT_BUNDLE),
        s9_errors=0,
        s9_warnings=0,
    )
    context.update(overrides)
    with pytest.raises(ValueError, match="S10"):
        validate_stage_context("S10", context)


@pytest.mark.parametrize("overrides", [
    {"accepted": False},
    {"validator_strict_passed": False},
    {"slicer_status": "failed"},
    {"errors": ["validator error"]},
    {"warnings": ["validator warning"]},
])
def test_s11_semantic_completion_requires_strict_acceptance(overrides):
    context = {
        key: f"S11:{key}"
        for key in stage_by_id("S11").required_context_keys
    }
    context.update(
        accepted=True,
        errors=[],
        warnings=[],
        artifact_sha256="a" * 64,
        validator_strict_passed=True,
        slicer_status="passed",
    )
    context.update(overrides)
    with pytest.raises(ValueError, match="S11 cannot complete"):
        validate_stage_context("S11", context)
