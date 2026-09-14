"""Fail-closed pre-export gates for semantic map meshes.

These checks run before a 3MF is published.  They deliberately complement,
not replace, the project validator: S9 rejects an incomplete in-memory mesh
bundle, while S11 re-opens the exported artifact and runs validator/slicer
acceptance.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import math

import numpy as np

from aesthetic.pipeline_contract import (
    FEATURE_SURVIVAL_FAMILIES,
    FEATURE_SURVIVAL_POLICY_VERSION,
    FEATURE_SURVIVAL_SCOPE,
    SEMANTIC_MESH_GATE_VERSION,
    feature_source_counts_fingerprint,
    normalize_feature_source_counts,
)


GATE_VERSION = SEMANTIC_MESH_GATE_VERSION
ALLOWED_INTENTIONAL_OMISSIONS = frozenset({"vegetation"})


def evaluate_feature_survival(
    source_feature_counts: Mapping[str, object],
    final_layer_counts: Mapping[str, object],
    *,
    intentional_omissions: Iterable[str] = (),
    scene_policy: Mapping | None = None,
) -> dict:
    """Prove that available semantic source families did not vanish.

    A formal artifact is not successful merely because every mesh that was
    *built* is well formed.  Roads, water, buildings or vegetation can be
    accidentally filtered to zero before mesh construction, which used to
    make the role disappear from ``required_roles`` and therefore evade S9.
    Only an explicit, recorded omission (currently used for disabled
    vegetation) may waive that survival check.
    """

    omitted = {str(value) for value in intentional_omissions}
    from aesthetic.landscape_runtime import is_active_landscape
    allowed = set(ALLOWED_INTENTIONAL_OMISSIONS)
    if is_active_landscape(scene_policy or {}):
        allowed.add('buildings')
    unsupported = omitted - allowed
    if unsupported:
        raise ValueError(
            "only vegetation may be intentionally omitted; unsupported: "
            + ", ".join(sorted(unsupported))
        )
    source = normalize_feature_source_counts(source_feature_counts)
    final = {
        "roads": max(0, int(final_layer_counts.get("roads", 0) or 0)),
        "water": (
            max(0, int(final_layer_counts.get("WL", 0) or 0))
            + max(0, int(final_layer_counts.get("WO", 0) or 0))
        ),
        "buildings": (
            max(0, int(final_layer_counts.get("BL", 0) or 0))
            + max(0, int(final_layer_counts.get("BO", 0) or 0))
        ),
        "vegetation": (
            max(0, int(final_layer_counts.get("VL", 0) or 0))
            + max(0, int(final_layer_counts.get("VO", 0) or 0))
        ),
    }
    roles = {}
    errors: list[str] = []
    for family in source:
        expected = source[family] > 0 and family not in omitted
        survived = not expected or final[family] > 0
        if family in omitted:
            status = "intentionally_omitted"
        elif not expected:
            status = "not_present_in_source"
        elif survived:
            status = "survived"
        else:
            status = "lost"
            errors.append(
                f"{family}: {source[family]} projected source features "
                "collapsed to zero final semantic layers"
            )
        roles[family] = {
            "source_count": source[family],
            "final_count": final[family],
            "expected": expected,
            "status": status,
        }
    return {
        "policy_version": FEATURE_SURVIVAL_POLICY_VERSION,
        "scope": FEATURE_SURVIVAL_SCOPE,
        "source_counts_fingerprint": feature_source_counts_fingerprint(source),
        "passed": not errors,
        "intentional_omissions": sorted(omitted),
        "omission_basis": ({'buildings': 'active_landscape_policy'}
                           if 'buildings' in omitted else {}),
        "roles": roles,
        "errors": errors,
    }


def _mesh_metrics(mesh) -> tuple[dict, list[str]]:
    errors: list[str] = []
    if mesh is None:
        return {"present": False}, ["mesh is missing"]
    vertices = np.asarray(getattr(mesh, "vertices", ()))
    faces = np.asarray(getattr(mesh, "faces", ()))
    metrics = {
        "present": True,
        "vertices": int(len(vertices)),
        "faces": int(len(faces)),
        "watertight": bool(getattr(mesh, "is_watertight", False)),
        "winding_consistent": bool(
            getattr(mesh, "is_winding_consistent", False)),
    }
    if vertices.ndim != 2 or vertices.shape[1:] != (3,) or not len(vertices):
        errors.append("mesh has no XYZ vertices")
    elif not np.isfinite(vertices).all():
        errors.append("mesh vertices contain non-finite values")
    if faces.ndim != 2 or faces.shape[1:] != (3,) or not len(faces):
        errors.append("mesh has no triangular faces")
    elif len(vertices) and (faces.min() < 0 or faces.max() >= len(vertices)):
        errors.append("mesh face indices are out of bounds")
    if not metrics["watertight"]:
        errors.append("mesh is not watertight")
    if not metrics["winding_consistent"]:
        errors.append("mesh winding is inconsistent")
    if len(vertices) and np.isfinite(vertices).all():
        bounds_min = vertices.min(axis=0)
        bounds_max = vertices.max(axis=0)
        metrics["bounds_mm"] = [
            [round(float(value), 6) for value in bounds_min],
            [round(float(value), 6) for value in bounds_max],
        ]
        if any(not math.isfinite(float(value))
               for value in (*bounds_min, *bounds_max)):
            errors.append("mesh bounds are not finite")
    return metrics, errors


def validate_semantic_mesh_bundle(
    meshes: Mapping[str, object],
    *,
    required_roles: Iterable[str],
    block_base_clearance: Mapping | None = None,
    require_block_base_clearance: bool = False,
    source_feature_counts: Mapping[str, object] | None = None,
    final_layer_counts: Mapping[str, object] | None = None,
    intentional_omissions: Iterable[str] = (),
    scene_policy: Mapping | None = None,
) -> dict:
    """Return explicit S9 evidence and never silently drop a required role."""

    required = tuple(dict.fromkeys(str(role) for role in required_roles))
    errors: list[str] = []
    warnings: list[str] = []
    metrics: dict[str, dict] = {}
    for role in required:
        role_metrics, role_errors = _mesh_metrics(meshes.get(role))
        metrics[role] = role_metrics
        errors.extend(f"{role}: {message}" for message in role_errors)

    clearance = dict(block_base_clearance or {})
    if require_block_base_clearance:
        if not clearance:
            errors.append("block_base: structural road-clearance evidence missing")
        else:
            target = float(clearance.get("target_gap_mm", 0.0) or 0.0)
            verified = float(
                clearance.get("verified_min_gap_mm", 0.0) or 0.0)
            tolerance = float(
                clearance.get("measurement_tolerance_m2", 1e-6) or 1e-6)
            intrusion = float(
                clearance.get("post_clip_intrusion_area_m2", math.inf))
            if clearance.get("status") != "checked":
                errors.append("block_base: clearance status is not checked")
            if clearance.get("passed") is not True:
                errors.append("block_base: clearance check did not pass")
            if target <= 0 or verified + 1e-9 < target:
                errors.append(
                    "block_base: verified road gap is below the target")
            if not math.isfinite(intrusion) or intrusion > tolerance:
                errors.append(
                    "block_base: post-transform road intrusion remains")

    feature_survival = evaluate_feature_survival(
        source_feature_counts or {},
        final_layer_counts or {},
        intentional_omissions=intentional_omissions,
        scene_policy=scene_policy,
    )
    errors.extend(feature_survival["errors"])

    return {
        "gate_version": GATE_VERSION,
        "status": "passed" if not errors and not warnings else "failed",
        "passed": not errors and not warnings,
        "required_roles": list(required),
        "mesh_metrics": metrics,
        "block_base_clearance": clearance,
        "feature_survival": feature_survival,
        "errors": errors,
        "warnings": warnings,
    }


def require_semantic_mesh_bundle(*args, **kwargs) -> dict:
    """Validate a bundle and raise before S10 if any gate is not clean."""

    evidence = validate_semantic_mesh_bundle(*args, **kwargs)
    if evidence["passed"] is not True:
        details = "; ".join(evidence["errors"] + evidence["warnings"])
        raise RuntimeError(f"S9 printable mesh gate rejected output: {details}")
    return evidence
