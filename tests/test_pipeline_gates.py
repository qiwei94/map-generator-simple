import numpy as np
import pytest
import trimesh

from aesthetic.pipeline_gates import (
    evaluate_feature_survival,
    require_semantic_mesh_bundle,
    validate_semantic_mesh_bundle,
)


def _box():
    return trimesh.creation.box(extents=(10.0, 10.0, 1.0))


def test_required_role_must_exist_and_be_closed():
    evidence = validate_semantic_mesh_bundle(
        {"terrain": _box(), "roads": None},
        required_roles=("terrain", "roads"),
    )
    assert evidence["passed"] is False
    assert any(item.startswith("roads:") for item in evidence["errors"])


def test_non_watertight_role_blocks_export():
    open_mesh = trimesh.Trimesh(
        vertices=np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], float),
        faces=np.array([[0, 1, 2]], int),
        process=False,
    )
    with pytest.raises(RuntimeError, match="not watertight"):
        require_semantic_mesh_bundle(
            {"terrain": _box(), "water": open_mesh},
            required_roles=("terrain", "water"),
        )


def test_structural_clearance_is_an_explicit_gate():
    evidence = validate_semantic_mesh_bundle(
        {"terrain": _box(), "block_base": _box()},
        required_roles=("terrain", "block_base"),
        require_block_base_clearance=True,
        block_base_clearance={
            "status": "checked",
            "passed": True,
            "target_gap_mm": 0.84,
            "verified_min_gap_mm": 0.84,
            "post_clip_intrusion_area_m2": 0.0,
            "measurement_tolerance_m2": 1e-6,
        },
    )
    assert evidence["passed"] is True
    assert evidence["warnings"] == []


def test_clearance_gap_below_target_is_rejected():
    evidence = validate_semantic_mesh_bundle(
        {"terrain": _box(), "block_base": _box()},
        required_roles=("terrain", "block_base"),
        require_block_base_clearance=True,
        block_base_clearance={
            "status": "checked",
            "passed": True,
            "target_gap_mm": 0.84,
            "verified_min_gap_mm": 0.42,
            "post_clip_intrusion_area_m2": 0.0,
        },
    )
    assert evidence["passed"] is False
    assert any("below the target" in item for item in evidence["errors"])


def test_source_family_cannot_silently_disappear_before_mesh_gate():
    evidence = validate_semantic_mesh_bundle(
        {"terrain": _box()},
        required_roles=("terrain",),
        source_feature_counts={"roads": 217, "water": 4},
        final_layer_counts={"roads": 0, "WL": 0, "WO": 0},
    )

    assert evidence["passed"] is False
    assert evidence["feature_survival"]["roles"]["roads"]["status"] == "lost"
    assert any("roads: 217" in item for item in evidence["errors"])
    assert any("water: 4" in item for item in evidence["errors"])


def test_only_explicitly_disabled_feature_family_may_be_omitted():
    result = evaluate_feature_survival(
        {"roads": 8, "vegetation": 12},
        {"roads": 3, "VL": 0, "VO": 0},
        intentional_omissions=("vegetation",),
    )

    assert result["passed"] is True
    assert result["roles"]["roads"]["status"] == "survived"
    assert result["roles"]["vegetation"]["status"] == "intentionally_omitted"


@pytest.mark.parametrize("family", ["roads", "water", "buildings"])
def test_structural_feature_families_cannot_be_declared_omitted(family):
    with pytest.raises(ValueError, match="unsupported intentional"):
        evaluate_feature_survival(
            {family: 12}, {}, intentional_omissions=(family,))


def test_active_landscape_may_explicitly_omit_roads_and_buildings():
    result = evaluate_feature_survival(
        {"roads": 2, "buildings": 4, "water": 1},
        {"roads": 0, "BL": 0, "BO": 0, "WL": 1, "WO": 0},
        intentional_omissions=("roads", "buildings"),
        scene_policy={
            "activation": "active",
            "landscape_strategy": {"enabled": True},
        },
    )

    assert result["passed"] is True
    assert result["roles"]["roads"]["status"] == "intentionally_omitted"
    assert result["roles"]["buildings"]["status"] == "intentionally_omitted"


def test_binary_survival_does_not_claim_retention_quality():
    result = evaluate_feature_survival(
        {"roads": 10_000}, {"roads": 1})

    assert result["passed"] is True
    assert result["scope"] == "binary_nonzero_survival"
    assert result["roles"]["roads"] == {
        "source_count": 10_000,
        "final_count": 1,
        "expected": True,
        "status": "survived",
    }
