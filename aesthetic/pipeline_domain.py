"""Typed runtime Context adapters for the production generation pipeline.

The durable pipeline ledger intentionally stores only JSON-safe evidence.  A
real generation run, however, must also hand GeoDataFrames, Shapely geometry,
the resolved terrain surface and the printer profile from one Stage to the
next.  Keeping those objects as loose locals in ``generate_city_legacy`` made
the documented Context chain descriptive rather than executable.

This module is the runtime half of that contract.  It deliberately separates
non-serializable domain objects from ledger evidence and enforces an exact
``V3 -> V4 -> V5 -> V6 -> V7 -> V8 -> V9 -> V10`` predecessor chain.  S4
and S5 are read-only;
S6 receives an owned copy of the mutable ``LayerPolygons`` containers before
the existing, reviewed geometry algorithms are called.  Shapely geometries
are shared because they are immutable -- no geometry is regenerated here.
"""

from __future__ import annotations

from copy import copy, deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
import numpy as np
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

from aesthetic.pipeline_contract import (
    feature_source_counts_fingerprint,
    SEMANTIC_MESH_ROLES,
    SEMANTIC_MESH_SUMMARY_VERSION,
    S10_REQUIRED_ARTIFACT_BUNDLE,
)


DOMAIN_CONTEXT_VERSION = "pipeline-domain-context-v1"


class DomainContextError(RuntimeError):
    """Base error for an invalid production Context handoff."""


class WrongPredecessorContext(DomainContextError):
    """Raised before effects run when a Stage receives the wrong Context."""


class ContextFingerprintMismatch(DomainContextError):
    """Raised when a carried JSON payload no longer matches its identity."""


def freeze_json(value: Any) -> Any:
    """Recursively freeze a JSON-compatible value without losing Mapping API."""

    if isinstance(value, Mapping):
        return MappingProxyType({
            str(key): freeze_json(item) for key, item in value.items()
        })
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(item) for item in value)
    if isinstance(value, set):
        return tuple(sorted((freeze_json(item) for item in value), key=repr))
    return value


def thaw_json(value: Any) -> Any:
    """Return an ordinary JSON-safe copy of recursively frozen evidence."""

    if isinstance(value, Mapping):
        return {str(key): thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


def canonical_json_fingerprint(value: Mapping[str, Any]) -> str:
    """Return the stable SHA-256 identity used by the durable ledger."""

    try:
        payload = json.dumps(
            thaw_json(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DomainContextError(
            "domain Context evidence must be finite and JSON-compatible"
        ) from exc
    return hashlib.sha256(payload).hexdigest()


def _require_exact_context(value: Any, expected: type, stage_id: str) -> None:
    if type(value) is not expected:
        raise WrongPredecessorContext(
            f"{stage_id} requires exact {expected.__name__}, got "
            f"{type(value).__name__}"
        )


def _require_payload_fingerprint(
    *, stage_id: str, name: str, payload: Mapping[str, Any], expected: str
) -> None:
    actual = canonical_json_fingerprint(payload)
    if actual != expected:
        raise ContextFingerprintMismatch(
            f"{stage_id} received stale or mutated {name}: "
            f"expected {expected}, got {actual}"
        )


@dataclass(frozen=True)
class RuntimeIdentity:
    run_id: str
    attempt_id: str

    def __post_init__(self) -> None:
        if not str(self.run_id).strip() or not str(self.attempt_id).strip():
            raise ValueError("run_id and attempt_id must be non-empty")


@dataclass(frozen=True)
class CarriedFingerprints:
    preprocess_parameters: str
    source_feature_counts: str
    terrain_surface: str

    def ledger_value(self) -> dict[str, str]:
        return {
            "preprocess_parameters_fingerprint": self.preprocess_parameters,
            "source_feature_counts_fingerprint": self.source_feature_counts,
            "terrain_surface_fingerprint": self.terrain_surface,
        }


@dataclass(frozen=True)
class ProjectedSources:
    roads: Any
    buildings: Any
    water: Any
    vegetation: Any = None
    landuse: Any = None


@dataclass(frozen=True)
class RuntimeInputs:
    """Read-only inputs resolved no later than S3.

    These objects are not serialized into the ledger.  ``ledger_value`` on a
    Stage Context exposes only the bounded evidence that is safe to persist.
    """

    identity: RuntimeIdentity
    city: str
    sources: ProjectedSources
    bbox_local_m: tuple[float, float, float, float]
    bbox_wgs84: tuple[float, float, float, float]
    elevation_grid: Any
    scale_mm_per_m: float
    printer_profile: Any
    terrain_surface_plan: Any
    amap_reference: Any
    amap_evidence: Mapping[str, Any]
    fingerprints: CarriedFingerprints

    def __post_init__(self) -> None:
        if not str(self.city).strip():
            raise ValueError("runtime city must be non-empty")
        if len(self.bbox_local_m) != 4 or len(self.bbox_wgs84) != 4:
            raise ValueError("runtime bboxes must contain four coordinates")
        if not math.isfinite(float(self.scale_mm_per_m)):
            raise ValueError("scale_mm_per_m must be finite")
        if float(self.scale_mm_per_m) <= 0:
            raise ValueError("scale_mm_per_m must be positive")
        plan_fingerprint = getattr(
            self.terrain_surface_plan, "fingerprint", None)
        if (plan_fingerprint is not None
                and str(plan_fingerprint) != self.fingerprints.terrain_surface):
            raise ContextFingerprintMismatch(
                "RuntimeInputs terrain plan does not match the carried "
                "terrain_surface_fingerprint"
            )
        object.__setattr__(
            self, "amap_evidence", freeze_json(dict(self.amap_evidence)))


@dataclass(frozen=True)
class FrozenLayerPolygons:
    """Read-only snapshot of a LayerPolygons-compatible aggregate."""

    _template: Any
    _values: Mapping[str, Any]
    _kinds: Mapping[str, str]

    def __getattr__(self, name: str) -> Any:
        try:
            return self._values[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def freeze_layer_containers(layers: Any) -> FrozenLayerPolygons:
    if isinstance(layers, FrozenLayerPolygons):
        return layers
    try:
        attributes = vars(layers)
    except TypeError as exc:
        raise DomainContextError(
            "Stage layers must expose LayerPolygons-compatible attributes"
        ) from exc
    values: dict[str, Any] = {}
    kinds: dict[str, str] = {}
    for name, value in attributes.items():
        if isinstance(value, list):
            values[name] = tuple(value)
            kinds[name] = "list"
        elif isinstance(value, dict):
            values[name] = freeze_json(value)
            kinds[name] = "dict"
        elif isinstance(value, set):
            values[name] = frozenset(value)
            kinds[name] = "set"
        else:
            values[name] = value
            kinds[name] = "scalar"
    return FrozenLayerPolygons(
        _template=copy(layers),
        _values=MappingProxyType(values),
        _kinds=MappingProxyType(kinds),
    )


def thaw_layer_containers(layers: Any) -> Any:
    """Create a mutable LayerPolygons-compatible copy from a Stage snapshot."""

    frozen = freeze_layer_containers(layers)
    cloned = copy(frozen._template)
    for name, value in frozen._values.items():
        kind = frozen._kinds[name]
        if kind == "list":
            value = list(value)
        elif kind == "dict":
            value = thaw_json(value)
        elif kind == "set":
            value = set(value)
        setattr(cloned, name, value)
    return cloned


def _update_semantic_hash(digest: Any, value: Any) -> None:
    """Stream stable semantic content into a digest without copying geometry."""

    digest.update(type(value).__name__.encode("utf-8"))
    digest.update(b"\0")
    if value is None or isinstance(value, (str, int, float, bool)):
        digest.update(json.dumps(
            value, ensure_ascii=False, allow_nan=False,
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8"))
    elif isinstance(value, Mapping):
        for key in sorted(value, key=lambda item: str(item)):
            _update_semantic_hash(digest, str(key))
            _update_semantic_hash(digest, value[key])
    elif isinstance(value, (list, tuple)):
        digest.update(str(len(value)).encode("ascii"))
        for item in value:
            _update_semantic_hash(digest, item)
    elif isinstance(value, (set, frozenset)):
        item_digests = []
        for item in value:
            child = hashlib.sha256()
            _update_semantic_hash(child, item)
            item_digests.append(child.digest())
        for item_digest in sorted(item_digests):
            digest.update(item_digest)
    elif hasattr(value, "wkb"):
        digest.update(bytes(value.wkb))
    elif isinstance(value, np.ndarray):
        if value.dtype.hasobject:
            raise DomainContextError('object arrays are not valid stage geometry')
        digest.update(value.dtype.str.encode('ascii'))
        digest.update(str(value.shape).encode('ascii'))
        digest.update(np.ascontiguousarray(value).tobytes())
    elif hasattr(value, "item"):
        _update_semantic_hash(digest, value.item())
    elif hasattr(value, "name") and isinstance(value.name, str):
        digest.update(value.name.encode("utf-8"))
    else:
        # LayerPolygons contains primitives, enums and Shapely geometry.  The
        # fallback keeps custom categorical objects deterministic without
        # traversing opaque third-party internals.
        digest.update(str(value).encode("utf-8"))
    digest.update(b"\xff")


def layer_semantic_fingerprint(layers: Any) -> str:
    """Fingerprint every LayerPolygons field, including geometry WKB."""

    frozen = freeze_layer_containers(layers)
    digest = hashlib.sha256()
    for name in sorted(frozen._values):
        _update_semantic_hash(digest, name)
        _update_semantic_hash(digest, frozen._values[name])
    return digest.hexdigest()


def semantic_mesh_bundle_fingerprint(meshes: Mapping[str, Any]) -> str:
    """Fingerprint semantic mesh coordinates and topology without rewriting them.

    The Stage Context retains opaque mesh handles because copying a city-scale
    bundle would double peak memory.  This content fingerprint, rechecked at
    S9 and immediately before S10 export, makes that shared ownership explicit
    and detects an in-place geometry mutation between Stage boundaries.
    """

    import numpy as np

    digest = hashlib.sha256()
    for role in sorted(str(name) for name in meshes):
        digest.update(role.encode("utf-8"))
        digest.update(b"\0")
        mesh = meshes[role]
        if mesh is None:
            digest.update(b"absent\xff")
            continue
        for attribute in ("vertices", "faces"):
            values = np.asarray(getattr(mesh, attribute, ()))
            contiguous = np.ascontiguousarray(values)
            digest.update(attribute.encode("ascii"))
            digest.update(str(contiguous.dtype).encode("ascii"))
            digest.update(repr(tuple(contiguous.shape)).encode("ascii"))
            digest.update(memoryview(contiguous).cast("B"))
            digest.update(b"\xff")
    return digest.hexdigest()


def _stable_file_claim(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read one published file once and reject an identity-changing race."""

    requested = Path(path)
    resolved = requested.resolve(strict=True)
    digest = hashlib.sha256()
    with resolved.open("rb") as stream:
        before = os.fstat(stream.fileno())
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if any(getattr(before, key) != getattr(after, key)
           for key in stable_fields):
        raise DomainContextError(
            f"S10 artifact changed while hashing: {requested}")
    current_path = requested.resolve(strict=True)
    current = current_path.stat()
    if current_path != resolved or any(
            getattr(current, key) != getattr(after, key)
            for key in stable_fields):
        raise DomainContextError(
            f"S10 artifact path changed while hashing: {requested}")
    return {
        "filename": resolved.name,
        "size_bytes": int(after.st_size),
        "sha256": digest.hexdigest(),
    }


def context_handoff_ledger_value(
    context: Any, *, run_id: str, attempt_id: str,
) -> dict[str, Any]:
    """Return ledger evidence only after binding it to the active attempt."""

    supported = (
        PipelineContextV4Runtime,
        PipelineContextV5Runtime,
        PipelineContextV6Runtime,
        PipelineContextV7Runtime,
        PipelineContextV8Runtime,
        PipelineContextV9Runtime,
        PipelineContextV10Runtime,
    )
    if type(context) not in supported:
        raise DomainContextError(
            "ledger handoff requires a completed V4-V10 Runtime Context")
    expected = RuntimeIdentity(str(run_id), str(attempt_id))
    if context.identity != expected:
        raise ContextFingerprintMismatch(
            "Runtime Context identity does not match the active ledger: "
            f"context={context.identity.run_id}/{context.identity.attempt_id}, "
            f"ledger={expected.run_id}/{expected.attempt_id}"
        )
    return context.ledger_value()


@dataclass(frozen=True)
class PipelineContextV3Runtime:
    runtime: RuntimeInputs
    layers: Any

    def __post_init__(self) -> None:
        object.__setattr__(self, "layers", freeze_layer_containers(self.layers))

    @property
    def identity(self) -> RuntimeIdentity:
        return self.runtime.identity

    def ledger_value(self) -> dict[str, Any]:
        return {
            **self.runtime.fingerprints.ledger_value(),
            "domain_context_version": DOMAIN_CONTEXT_VERSION,
        }


@dataclass(frozen=True)
class PipelineContextV4Runtime:
    predecessor: PipelineContextV3Runtime
    scene_character: Mapping[str, Any]
    scene_character_fingerprint: str

    def __post_init__(self) -> None:
        _require_exact_context(
            self.predecessor, PipelineContextV3Runtime, "S4 Context")
        frozen = freeze_json(dict(self.scene_character))
        _require_payload_fingerprint(
            stage_id="S4",
            name="SceneCharacter",
            payload=frozen,
            expected=self.scene_character_fingerprint,
        )
        object.__setattr__(self, "scene_character", frozen)

    @property
    def runtime(self) -> RuntimeInputs:
        return self.predecessor.runtime

    @property
    def layers(self) -> Any:
        return self.predecessor.layers

    @property
    def identity(self) -> RuntimeIdentity:
        return self.predecessor.identity

    def ledger_value(self) -> dict[str, Any]:
        return {
            **self.predecessor.ledger_value(),
            "scene_character_fingerprint": self.scene_character_fingerprint,
            "domain_context_in": "PipelineContextV3Runtime",
            "domain_context_out": "PipelineContextV4Runtime",
        }


@dataclass(frozen=True)
class PipelineContextV5Runtime:
    predecessor: PipelineContextV4Runtime
    scene_policy: Mapping[str, Any]
    scene_policy_fingerprint: str

    def __post_init__(self) -> None:
        _require_exact_context(
            self.predecessor, PipelineContextV4Runtime, "S5 Context")
        frozen = freeze_json(dict(self.scene_policy))
        _require_payload_fingerprint(
            stage_id="S5",
            name="ScenePolicy",
            payload=frozen,
            expected=self.scene_policy_fingerprint,
        )
        object.__setattr__(self, "scene_policy", frozen)

    @property
    def runtime(self) -> RuntimeInputs:
        return self.predecessor.runtime

    @property
    def layers(self) -> Any:
        return self.predecessor.layers

    @property
    def scene_character(self) -> Mapping[str, Any]:
        return self.predecessor.scene_character

    @property
    def scene_character_fingerprint(self) -> str:
        return self.predecessor.scene_character_fingerprint

    @property
    def identity(self) -> RuntimeIdentity:
        return self.predecessor.identity

    def ledger_value(self) -> dict[str, Any]:
        return {
            **self.predecessor.ledger_value(),
            "scene_policy_fingerprint": self.scene_policy_fingerprint,
            "domain_context_in": "PipelineContextV4Runtime",
            "domain_context_out": "PipelineContextV5Runtime",
        }


@dataclass(frozen=True)
class PipelineContextV6Runtime:
    predecessor: PipelineContextV5Runtime
    layers: Any
    building_mass_evidence: Mapping[str, Any]
    height_hierarchy_evidence: Mapping[str, Any]
    height_emphasis_evidence: Mapping[str, Any]
    final_layers_fingerprint: str = field(init=False)
    building_role_evidence_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        _require_exact_context(
            self.predecessor, PipelineContextV5Runtime, "S6 Context")
        object.__setattr__(self, "layers", freeze_layer_containers(self.layers))
        object.__setattr__(
            self, "building_mass_evidence",
            freeze_json(dict(self.building_mass_evidence)))
        object.__setattr__(
            self, "height_hierarchy_evidence",
            freeze_json(dict(self.height_hierarchy_evidence)))
        object.__setattr__(
            self, "height_emphasis_evidence",
            freeze_json(dict(self.height_emphasis_evidence)))
        object.__setattr__(
            self, "final_layers_fingerprint",
            layer_semantic_fingerprint(self.layers))
        object.__setattr__(
            self, "building_role_evidence_fingerprint",
            canonical_json_fingerprint({
                "building_mass": self.building_mass_evidence,
                "height_hierarchy": self.height_hierarchy_evidence,
                "height_emphasis": self.height_emphasis_evidence,
            }))

    @property
    def runtime(self) -> RuntimeInputs:
        return self.predecessor.runtime

    @property
    def scene_character(self) -> Mapping[str, Any]:
        return self.predecessor.scene_character

    @property
    def scene_character_fingerprint(self) -> str:
        return self.predecessor.scene_character_fingerprint

    @property
    def scene_policy(self) -> Mapping[str, Any]:
        return self.predecessor.scene_policy

    @property
    def scene_policy_fingerprint(self) -> str:
        return self.predecessor.scene_policy_fingerprint

    @property
    def identity(self) -> RuntimeIdentity:
        return self.predecessor.identity

    def ledger_value(self) -> dict[str, Any]:
        return {
            **self.predecessor.ledger_value(),
            "domain_context_in": "PipelineContextV5Runtime",
            "domain_context_out": "PipelineContextV6Runtime",
            "final_layers_fingerprint": self.final_layers_fingerprint,
            "building_role_evidence_fingerprint": (
                self.building_role_evidence_fingerprint),
        }


@dataclass(frozen=True)
class PipelineContextV7Runtime:
    predecessor: PipelineContextV6Runtime
    water_relief_intent: Mapping[str, Any]
    composition_spec: Mapping[str, Any]
    measurement_report: Mapping[str, Any]
    review_artifacts: Mapping[str, str]
    terminal_disposition: str
    composition_spec_fingerprint: str = field(init=False)
    measurement_report_fingerprint: str = field(init=False)
    s7_review_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        _require_exact_context(
            self.predecessor, PipelineContextV6Runtime, "S7 Context")
        object.__setattr__(
            self, "water_relief_intent",
            freeze_json(dict(self.water_relief_intent)))
        object.__setattr__(
            self, "composition_spec", freeze_json(dict(self.composition_spec)))
        object.__setattr__(
            self, "measurement_report",
            freeze_json(dict(self.measurement_report)))
        object.__setattr__(
            self, "review_artifacts",
            freeze_json(dict(self.review_artifacts)))
        object.__setattr__(
            self, "composition_spec_fingerprint",
            canonical_json_fingerprint(self.composition_spec))
        object.__setattr__(
            self, "measurement_report_fingerprint",
            canonical_json_fingerprint(self.measurement_report))
        object.__setattr__(
            self, "s7_review_fingerprint",
            canonical_json_fingerprint({
                "identity": {
                    "run_id": self.identity.run_id,
                    "attempt_id": self.identity.attempt_id,
                },
                "final_layers_fingerprint": (
                    self.predecessor.final_layers_fingerprint),
                "water_relief_intent": self.water_relief_intent,
                "composition_spec_fingerprint": (
                    self.composition_spec_fingerprint),
                "measurement_report_fingerprint": (
                    self.measurement_report_fingerprint),
                "review_artifacts": self.review_artifacts,
                "terminal_disposition": self.terminal_disposition,
            }))

    @property
    def runtime(self) -> RuntimeInputs:
        return self.predecessor.runtime

    @property
    def layers(self) -> Any:
        return self.predecessor.layers

    @property
    def scene_character(self) -> Mapping[str, Any]:
        return self.predecessor.scene_character

    @property
    def scene_character_fingerprint(self) -> str:
        return self.predecessor.scene_character_fingerprint

    @property
    def scene_policy(self) -> Mapping[str, Any]:
        return self.predecessor.scene_policy

    @property
    def scene_policy_fingerprint(self) -> str:
        return self.predecessor.scene_policy_fingerprint

    @property
    def identity(self) -> RuntimeIdentity:
        return self.predecessor.identity

    def ledger_value(self) -> dict[str, Any]:
        return {
            **self.predecessor.ledger_value(),
            "domain_context_in": "PipelineContextV6Runtime",
            "domain_context_out": "PipelineContextV7Runtime",
            "composition_spec_fingerprint": self.composition_spec_fingerprint,
            "measurement_report_fingerprint": (
                self.measurement_report_fingerprint),
            "s7_review_fingerprint": self.s7_review_fingerprint,
        }


@dataclass(frozen=True)
class PipelineContextV8Runtime:
    """Immutable identity for the materialized semantic mesh bundle.

    Mesh instances are deliberately shared as opaque handles to avoid doubling
    city-scale memory.  The mapping, all bounded evidence and the full vertex /
    face content identity are immutable at the Context boundary.
    """

    predecessor: PipelineContextV7Runtime
    meshes: Mapping[str, Any]
    mesh_summary: Mapping[str, Any]
    water_relief: Mapping[str, Any]
    block_base_clearance: Mapping[str, Any]
    required_roles: tuple[str, ...]
    source_feature_counts: Mapping[str, Any]
    final_layer_counts: Mapping[str, Any]
    intentional_omissions: tuple[str, ...]
    require_block_base_clearance: bool
    mesh_bundle_fingerprint: str = field(init=False)
    mesh_summary_fingerprint: str = field(init=False)
    s8_materialization_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        _require_exact_context(
            self.predecessor, PipelineContextV7Runtime, "S8 Context")
        if self.predecessor.terminal_disposition != "continue":
            raise DomainContextError(
                "S8 cannot consume a terminal review/styles Context")
        mesh_map = {str(name): mesh for name, mesh in self.meshes.items()}
        from aesthetic.city_surface_plan import verify_materialized_city
        try:
            verify_materialized_city(self.predecessor.layers, mesh_map,
                                     self.block_base_clearance, self.predecessor.runtime.scale_mm_per_m)
        except ValueError as exc:
            raise DomainContextError(str(exc)) from exc
        if set(mesh_map) != set(SEMANTIC_MESH_ROLES):
            raise DomainContextError(
                "S8 semantic mesh bundle must declare exactly: "
                + ", ".join(SEMANTIC_MESH_ROLES))
        required = tuple(dict.fromkeys(str(role) for role in self.required_roles))
        unsupported = set(required) - set(SEMANTIC_MESH_ROLES)
        if unsupported or "terrain" not in required:
            raise DomainContextError(
                "S8 required mesh roles must include terrain and use only "
                f"canonical roles; unsupported={sorted(unsupported)}")
        summary = freeze_json(dict(self.mesh_summary))
        if set(summary) != set(SEMANTIC_MESH_ROLES):
            raise DomainContextError(
                "S8 mesh summary must cover the exact semantic bundle")
        for role in SEMANTIC_MESH_ROLES:
            expected = "absent" if mesh_map[role] is None else "ready"
            role_summary = summary.get(role)
            if (not isinstance(role_summary, Mapping)
                    or role_summary.get("status") != expected):
                raise DomainContextError(
                    f"S8 mesh summary disagrees with role {role!r}")
        block_clearance = freeze_json(dict(self.block_base_clearance or {}))
        source_counts = freeze_json(dict(self.source_feature_counts))
        layer_counts = freeze_json(dict(self.final_layer_counts))
        omissions = tuple(sorted(
            dict.fromkeys(str(item) for item in self.intentional_omissions)))
        object.__setattr__(self, "meshes", MappingProxyType(mesh_map))
        object.__setattr__(self, "mesh_summary", summary)
        object.__setattr__(self, "water_relief", freeze_json(
            dict(self.water_relief)))
        object.__setattr__(self, "block_base_clearance", block_clearance)
        object.__setattr__(self, "required_roles", required)
        object.__setattr__(self, "source_feature_counts", source_counts)
        object.__setattr__(self, "final_layer_counts", layer_counts)
        object.__setattr__(self, "intentional_omissions", omissions)
        mesh_fingerprint = semantic_mesh_bundle_fingerprint(mesh_map)
        summary_fingerprint = canonical_json_fingerprint(summary)
        object.__setattr__(
            self, "mesh_bundle_fingerprint", mesh_fingerprint)
        object.__setattr__(
            self, "mesh_summary_fingerprint", summary_fingerprint)
        object.__setattr__(
            self, "s8_materialization_fingerprint",
            canonical_json_fingerprint({
                "identity": {
                    "run_id": self.identity.run_id,
                    "attempt_id": self.identity.attempt_id,
                },
                "s7_review_fingerprint": (
                    self.predecessor.s7_review_fingerprint),
                "mesh_bundle_fingerprint": mesh_fingerprint,
                "mesh_summary_fingerprint": summary_fingerprint,
                "water_relief_status": self.water_relief.get("status"),
                "block_base_clearance": block_clearance,
                "required_roles": required,
                "source_feature_counts": source_counts,
                "final_layer_counts": layer_counts,
                "intentional_omissions": omissions,
                "require_block_base_clearance": bool(
                    self.require_block_base_clearance),
            }))

    @property
    def runtime(self) -> RuntimeInputs:
        return self.predecessor.runtime

    @property
    def layers(self) -> Any:
        return self.predecessor.layers

    @property
    def scene_character(self) -> Mapping[str, Any]:
        return self.predecessor.scene_character

    @property
    def scene_policy(self) -> Mapping[str, Any]:
        return self.predecessor.scene_policy

    @property
    def identity(self) -> RuntimeIdentity:
        return self.predecessor.identity

    def ledger_value(self) -> dict[str, Any]:
        return {
            **self.predecessor.ledger_value(),
            "domain_context_in": "PipelineContextV7Runtime",
            "domain_context_out": "PipelineContextV8Runtime",
            "mesh_summary_version": SEMANTIC_MESH_SUMMARY_VERSION,
            "mesh_summary": thaw_json(self.mesh_summary),
            "water_relief_status": self.water_relief.get("status"),
            "mesh_bundle_fingerprint": self.mesh_bundle_fingerprint,
            "mesh_summary_fingerprint": self.mesh_summary_fingerprint,
            "s8_materialization_fingerprint": (
                self.s8_materialization_fingerprint),
        }


@dataclass(frozen=True)
class PipelineContextV9Runtime:
    predecessor: PipelineContextV8Runtime
    geometry_gate: Mapping[str, Any]
    geometry_gate_fingerprint: str = field(init=False)
    verified_mesh_bundle_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        _require_exact_context(
            self.predecessor, PipelineContextV8Runtime, "S9 Context")
        actual_mesh = semantic_mesh_bundle_fingerprint(self.predecessor.meshes)
        if actual_mesh != self.predecessor.mesh_bundle_fingerprint:
            raise ContextFingerprintMismatch(
                "S9 received a mutated semantic mesh bundle")
        gate = freeze_json(dict(self.geometry_gate))
        if (gate.get("passed") is not True
                or gate.get("status") != "passed"
                or tuple(gate.get("errors") or ())
                or tuple(gate.get("warnings") or ())):
            raise DomainContextError(
                "S9 Runtime Context requires a clean fail-closed gate")
        if tuple(gate.get("required_roles") or ()) != (
                self.predecessor.required_roles):
            raise ContextFingerprintMismatch(
                "S9 gate required roles do not match the S8 Context")
        object.__setattr__(self, "geometry_gate", gate)
        object.__setattr__(
            self, "geometry_gate_fingerprint",
            canonical_json_fingerprint(gate))
        object.__setattr__(
            self, "verified_mesh_bundle_fingerprint", actual_mesh)

    @property
    def runtime(self) -> RuntimeInputs:
        return self.predecessor.runtime

    @property
    def meshes(self) -> Mapping[str, Any]:
        return self.predecessor.meshes

    @property
    def identity(self) -> RuntimeIdentity:
        return self.predecessor.identity

    def ledger_value(self) -> dict[str, Any]:
        gate = self.geometry_gate
        return {
            **self.predecessor.ledger_value(),
            "domain_context_in": "PipelineContextV8Runtime",
            "domain_context_out": "PipelineContextV9Runtime",
            "gate_version": gate["gate_version"],
            "passed": gate["passed"],
            "required_roles": thaw_json(gate["required_roles"]),
            "errors": thaw_json(gate["errors"]),
            "warnings": thaw_json(gate["warnings"]),
            "mesh_metrics": thaw_json(gate["mesh_metrics"]),
            "feature_survival": thaw_json(gate["feature_survival"]),
            "geometry_gate_fingerprint": self.geometry_gate_fingerprint,
            "verified_mesh_bundle_fingerprint": (
                self.verified_mesh_bundle_fingerprint),
        }


@dataclass(frozen=True)
class PipelineContextV10Runtime:
    predecessor: PipelineContextV9Runtime
    status: str
    artifact_manifest: Mapping[str, Any]
    artifact_bundle_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        _require_exact_context(
            self.predecessor, PipelineContextV9Runtime, "S10 Context")
        if self.status != "generated_pending_validation":
            raise DomainContextError(
                "S10 can only report generated_pending_validation; S11 owns "
                "formal acceptance")
        manifest = freeze_json(dict(self.artifact_manifest))
        if set(manifest) != set(S10_REQUIRED_ARTIFACT_BUNDLE):
            raise DomainContextError(
                "S10 artifact manifest must contain the exact six-piece "
                "auditable bundle")
        for name, claim in manifest.items():
            if not isinstance(claim, Mapping):
                raise DomainContextError(
                    f"S10 artifact claim {name!r} must be a Mapping")
            digest = str(claim.get("sha256", ""))
            if (int(claim.get("size_bytes", 0) or 0) <= 0
                    or len(digest) != 64
                    or any(char not in "0123456789abcdef" for char in digest)):
                raise DomainContextError(
                    f"S10 artifact claim {name!r} has no stable identity")
        object.__setattr__(self, "artifact_manifest", manifest)
        object.__setattr__(
            self, "artifact_bundle_fingerprint",
            canonical_json_fingerprint(manifest))

    @property
    def runtime(self) -> RuntimeInputs:
        return self.predecessor.runtime

    @property
    def identity(self) -> RuntimeIdentity:
        return self.predecessor.identity

    def ledger_value(self) -> dict[str, Any]:
        gate = self.predecessor.geometry_gate
        return {
            **self.predecessor.ledger_value(),
            "domain_context_in": "PipelineContextV9Runtime",
            "domain_context_out": "PipelineContextV10Runtime",
            "status": self.status,
            "artifact_bundle": sorted(self.artifact_manifest),
            "s9_gate_version": gate["gate_version"],
            "s9_errors": len(gate["errors"]),
            "s9_warnings": len(gate["warnings"]),
            "artifact_manifest": thaw_json(self.artifact_manifest),
            "artifact_bundle_fingerprint": self.artifact_bundle_fingerprint,
        }


def clone_layer_containers(layers: Any) -> Any:
    """Copy mutable layer containers while sharing immutable geometries.

    ``PipelineCache`` may return the same ``LayerPolygons`` object to another
    request.  S6 is allowed to change semantic ownership, but it must never
    mutate that cached S3 result.  A normal ``deepcopy`` would duplicate tens
    of thousands of immutable GEOS geometries; this bounded clone copies each
    list/set/dict container and preserves the geometry objects themselves.
    """

    return thaw_layer_containers(layers)


def run_s4_observation(
    context: PipelineContextV3Runtime,
    *,
    analyze: Callable[..., Mapping[str, Any]] | None = None,
    summarize_external: Callable[..., Mapping[str, Any]] | None = None,
    refresh_quality: Callable[[dict[str, Any]], Any] | None = None,
) -> PipelineContextV4Runtime:
    """Measure the scene without changing S3 layers or source geometry."""

    _require_exact_context(context, PipelineContextV3Runtime, "S4")
    if analyze is None:
        from aesthetic.scene_character import analyze_scene_character
        analyze = analyze_scene_character
    if summarize_external is None:
        from aesthetic.amap_salience import summarize_amap_urban_evidence
        summarize_external = summarize_amap_urban_evidence
    if refresh_quality is None:
        from aesthetic.scene_character import refresh_building_data_quality
        refresh_quality = refresh_building_data_quality

    runtime = context.runtime
    sources = runtime.sources
    span_m = max(
        float(runtime.bbox_local_m[2] - runtime.bbox_local_m[0]),
        float(runtime.bbox_local_m[3] - runtime.bbox_local_m[1]),
    )
    measured = analyze(
        sources.roads,
        sources.buildings,
        sources.water,
        runtime.bbox_local_m,
        grid_size=8,
        elevation_grid=runtime.elevation_grid,
        nozzle_real_m=float(context.layers.nozzle_real_m),
        model_span_mm=span_m * float(runtime.scale_mm_per_m),
    )
    if not isinstance(measured, Mapping):
        raise DomainContextError("S4 analyzer must return a Mapping")
    scene = deepcopy(dict(measured))
    scene["bbox_wgs84"] = [float(value) for value in runtime.bbox_wgs84]
    if runtime.amap_reference is not None:
        external = dict(summarize_external(
            runtime.amap_reference,
            grid_size=scene["grid_size"],
        ))
        external["reference_status"] = runtime.amap_evidence.get(
            "status", "ready")
        external["reference_frame"] = runtime.amap_evidence.get(
            "preprocess_frame", "exact")
    else:
        external = {
            "version": "amap-urban-evidence-v1",
            "status": runtime.amap_evidence.get("status", "unavailable"),
            "source": "amap_style7_cartographic_reference",
            "reason": runtime.amap_evidence.get(
                "reason", "AMap salience reference unavailable"),
            "constraint": "cross-source scene evidence only",
        }
    metrics = scene.get("metrics")
    if not isinstance(metrics, dict):
        raise DomainContextError("S4 SceneCharacter.metrics must be a dict")
    metrics["external_urban"] = external
    refresh_quality(scene)
    fingerprint = canonical_json_fingerprint(scene)
    return PipelineContextV4Runtime(
        predecessor=context,
        scene_character=scene,
        scene_character_fingerprint=fingerprint,
    )


def run_s5_policy(
    context: PipelineContextV4Runtime,
    *,
    activation: str,
    urban_organization: str = 'default',
    resolve: Callable[..., Mapping[str, Any]] | None = None,
) -> PipelineContextV5Runtime:
    """Resolve a bounded policy from the exact S4 observation."""

    _require_exact_context(context, PipelineContextV4Runtime, "S5")
    _require_payload_fingerprint(
        stage_id="S5",
        name="SceneCharacter",
        payload=context.scene_character,
        expected=context.scene_character_fingerprint,
    )
    if activation not in {"active", "audit_only"}:
        raise ValueError("S5 activation must be active or audit_only")
    if resolve is None:
        from aesthetic.scene_policy import resolve_scene_policy
        resolve = resolve_scene_policy
    # The resolver receives a fresh builder, never the frozen S4 payload.  A
    # few policy fields intentionally reuse nested measurement structures;
    # freezing the result below severs those aliases at the Stage boundary.
    policy = resolve(
        thaw_json(context.scene_character),
        printer_profile=context.runtime.printer_profile,
        activation=activation,
    )
    if not isinstance(policy, Mapping):
        raise DomainContextError("S5 resolver must return a Mapping")
    output = deepcopy(dict(policy))
    if urban_organization == 'block-first':
        from aesthetic.block_first import plan_blocks
        output['block_first'] = plan_blocks(context.layers, context.runtime.sources.buildings)
    if urban_organization in {'C', 'block-first'} and activation == 'active':
        from aesthetic.z_texture import ZTexturePolicy
        output['z_texture'] = ZTexturePolicy().payload()
    return PipelineContextV5Runtime(
        predecessor=context,
        scene_policy=output,
        scene_policy_fingerprint=canonical_json_fingerprint(output),
    )


def run_s6_building_roles(
    context: PipelineContextV5Runtime,
    *,
    height_emphasis_zones: bool = False,
    merge_block_layers: bool = False,
    surface_road_style: str = 'printer-default',
    urban_organization: str = 'default',
    planar_only: bool = False,
    prepare_region: Callable[..., Mapping[str, Any]] | None = None,
    route_heroes: Callable[..., Mapping[str, Any]] | None = None,
    apply_mass: Callable[..., Mapping[str, Any]] | None = None,
    apply_emphasis: Callable[..., Mapping[str, Any]] | None = None,
    cap_heights: Callable[..., Mapping[str, Any]] | None = None,
) -> PipelineContextV6Runtime:
    """Own and resolve final building polygons and relative height roles."""

    _require_exact_context(context, PipelineContextV5Runtime, "S6")
    if surface_road_style != 'printer-default' and not merge_block_layers:
        raise DomainContextError('negative surface style requires shared S6 surfaces')
    if urban_organization not in {'default', 'C', 'block-first'}:
        raise DomainContextError('unknown urban organization')
    if urban_organization == 'C':
        if not merge_block_layers or apply_mass is not None:
            raise DomainContextError('C requires shared surfaces and no conflicting mass override')
        from aesthetic.organization_experiment import adapter
        apply_mass = adapter('C', context.runtime.sources)
    if urban_organization == 'block-first':
        if not merge_block_layers or apply_mass is not None:
            raise DomainContextError('block-first requires shared surfaces and no mass override')
        from functools import partial
        from aesthetic.block_first import apply_block_first
        apply_mass = partial(apply_block_first, sources=context.runtime.sources)
    _require_payload_fingerprint(
        stage_id="S6",
        name="SceneCharacter",
        payload=context.scene_character,
        expected=context.scene_character_fingerprint,
    )
    _require_payload_fingerprint(
        stage_id="S6",
        name="ScenePolicy",
        payload=context.scene_policy,
        expected=context.scene_policy_fingerprint,
    )
    if prepare_region is None or apply_emphasis is None:
        from aesthetic.height_emphasis_zones import (
            apply_height_emphasis_zones,
            prepare_region_first_height_roles,
        )
        prepare_region = prepare_region or prepare_region_first_height_roles
        apply_emphasis = apply_emphasis or apply_height_emphasis_zones
    if route_heroes is None or cap_heights is None:
        from aesthetic.building_height_hierarchy import (
            cap_building_heights_to_terrain,
            route_sub_nozzle_heroes,
        )
        route_heroes = route_heroes or route_sub_nozzle_heroes
        cap_heights = cap_heights or cap_building_heights_to_terrain
    if apply_mass is None:
        from aesthetic.building_mass_strategy import apply_building_mass_to_layers
        apply_mass = apply_building_mass_to_layers
    from aesthetic.building_height_hierarchy import POLICY_VERSION as height_version
    from aesthetic.building_mass_strategy import ACTIVATION_VERSION as mass_version
    from aesthetic.height_emphasis_zones import POLICY_VERSION as emphasis_version

    runtime = context.runtime
    sources = runtime.sources
    layers = clone_layer_containers(context.layers)
    scene_character = thaw_json(context.scene_character)
    scene_policy = thaw_json(context.scene_policy)
    emphasis_evidence: Mapping[str, Any] = {
        "policy_version": emphasis_version,
        "status": "inactive",
        "reason": "experimental region-first policy not requested",
    }
    if height_emphasis_zones:
        emphasis_evidence = {
            **dict(prepare_region(layers)),
            "activation": "explicit_cli_experiment",
        }
        routing_evidence = {
            "policy_version": height_version,
            "status": "replaced_by_region_first_preparation",
            "sub_nozzle_heroes_demoted_to_mass": 0,
        }
    else:
        routing_evidence = dict(route_heroes(
            layers,
            scale_mm_per_m=runtime.scale_mm_per_m,
            scene_policy=scene_policy,
            printer_profile=runtime.printer_profile,
        ))

    garden = scene_policy.get("garden_city_strategy", {}) or {}
    span_m = max(
        float(runtime.bbox_local_m[2] - runtime.bbox_local_m[0]),
        float(runtime.bbox_local_m[3] - runtime.bbox_local_m[1]),
    )
    mass_evidence = dict(apply_mass(
        layers,
        sources.buildings,
        sources.roads,
        sources.water,
        runtime.bbox_local_m,
        printer_profile=runtime.printer_profile,
        scene_policy=scene_policy,
        topology_tier=int(garden.get("urban_mass_topology_tier") or 2),
        topology_blocks=(layers.city_blocks if layers.city_blocks else None),
        topology_evidence=(
            (layers.road_roles or {}).get("scale_aware_topology") or None),
        model_span_mm=span_m * float(runtime.scale_mm_per_m),
        downstream_final_clearance=bool(merge_block_layers),
    ))
    if not mass_evidence:
        mass_evidence = {
            "activation_version": mass_version,
            "status": "inactive",
            "reason": "building mass returned no evidence",
        }

    if height_emphasis_zones:
        emphasis_evidence = {
            **dict(emphasis_evidence),
            **dict(apply_emphasis(
                layers,
                sources.buildings,
                scene_character,
                scene_policy,
                printer_profile=runtime.printer_profile,
                scale_mm_per_m=runtime.scale_mm_per_m,
                building_mass_evidence=mass_evidence,
            )),
            "activation": "explicit_cli_experiment",
        }

    terrain_cap = dict(cap_heights(
        layers,
        runtime.terrain_surface_plan,
        scale_mm_per_m=runtime.scale_mm_per_m,
        scene_policy=scene_policy,
        printer_profile=runtime.printer_profile,
    ))
    hierarchy_evidence = {
        **terrain_cap,
        "routing": routing_evidence,
        "terrain_capping": terrain_cap,
        "geometry_changed": bool(routing_evidence.get("geometry_changed")),
        "sub_nozzle_heroes_demoted_to_mass": routing_evidence.get(
            "sub_nozzle_heroes_demoted_to_mass", 0),
        "minimum_independent_width_mm": routing_evidence.get(
            "minimum_independent_width_mm"),
    }
    if hierarchy_evidence.get("status") in {
            "invalid_terrain_evidence", "unavailable"}:
        raise DomainContextError(
            "Terrain-owned scene has no credible DEM relief; generation is "
            "blocked instead of silently generating a flat model. "
            f"Evidence: {hierarchy_evidence}"
        )
    if (height_emphasis_zones
            and emphasis_evidence.get("status") != "active"):
        raise DomainContextError(
            "Explicit height-emphasis A/B could not produce a valid "
            "region-first candidate; refusing to label the baseline as the "
            f"experiment. Evidence: {emphasis_evidence}"
        )
    if merge_block_layers:
        from aesthetic.city_surface_plan import finalize_city_surfaces
        mass_evidence["final_surface_plan"] = finalize_city_surfaces(
            layers, bbox_local=runtime.bbox_local_m,
            scale=runtime.scale_mm_per_m,
            printer_profile=runtime.printer_profile,
            road_style=surface_road_style, source_roads=sources.roads,
            terrain_surface_plan=None if planar_only else runtime.terrain_surface_plan,
            z_texture_policy=scene_policy.get('z_texture'))
        mass_evidence['final_surface_plan']['geometry_scope'] = (
            'planar_review_only; terrain_contact_and_slicing_pending' if planar_only
            else 'planar_and_terrain_contact')
    return PipelineContextV6Runtime(
        predecessor=context,
        layers=layers,
        building_mass_evidence=mass_evidence,
        height_hierarchy_evidence=hierarchy_evidence,
        height_emphasis_evidence=emphasis_evidence,
    )


def _require_s7_payload_binding(
    context: PipelineContextV6Runtime,
    *,
    composition_spec: Mapping[str, Any],
    measurement_report: Mapping[str, Any],
) -> None:
    """Reject stale S7 evidence from another run, crop or policy."""

    runtime = context.runtime
    identity = context.identity
    if str(composition_spec.get("city", "")) != str(runtime.city):
        raise ContextFingerprintMismatch(
            "S7 CompositionSpec city does not match the Runtime Context")
    try:
        composition_bbox = tuple(
            float(value) for value in composition_spec["bbox_wgs84"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DomainContextError(
            "S7 CompositionSpec requires a valid bbox_wgs84") from exc
    if composition_bbox != tuple(float(v) for v in runtime.bbox_wgs84):
        raise ContextFingerprintMismatch(
            "S7 CompositionSpec bbox does not match the Runtime Context")
    composition_policy = composition_spec.get("scene_policy")
    if not isinstance(composition_policy, Mapping):
        raise DomainContextError(
            "S7 CompositionSpec requires embedded ScenePolicy evidence")
    _require_payload_fingerprint(
        stage_id="S7 CompositionSpec",
        name="ScenePolicy",
        payload=composition_policy,
        expected=context.scene_policy_fingerprint,
    )

    raw_measurements = measurement_report.get("raw_measurements")
    decisions = measurement_report.get("resolved_decisions")
    if not isinstance(raw_measurements, Mapping):
        raise DomainContextError(
            "S7 measurement report requires raw_measurements")
    if not isinstance(decisions, Mapping):
        raise DomainContextError(
            "S7 measurement report requires resolved_decisions")
    report_run = raw_measurements.get("run")
    if not isinstance(report_run, Mapping):
        raise DomainContextError(
            "S7 measurement report requires raw_measurements.run")
    if (str(report_run.get("run_id", "")) != identity.run_id
            or str(report_run.get("attempt_id", "")) != identity.attempt_id):
        raise ContextFingerprintMismatch(
            "S7 measurement report identity does not match the Runtime Context")
    if str(report_run.get("city", "")) != str(runtime.city):
        raise ContextFingerprintMismatch(
            "S7 measurement report city does not match the Runtime Context")
    if str(report_run.get("terrain_surface_fingerprint", "")) != str(
            runtime.fingerprints.terrain_surface):
        raise ContextFingerprintMismatch(
            "S7 measurement report terrain fingerprint is stale")
    report_character = raw_measurements.get("scene_character")
    report_policy = decisions.get("scene_policy")
    if not isinstance(report_character, Mapping):
        raise DomainContextError(
            "S7 measurement report requires SceneCharacter evidence")
    if not isinstance(report_policy, Mapping):
        raise DomainContextError(
            "S7 measurement report requires ScenePolicy evidence")
    _require_payload_fingerprint(
        stage_id="S7 measurement report",
        name="SceneCharacter",
        payload=report_character,
        expected=context.scene_character_fingerprint,
    )
    _require_payload_fingerprint(
        stage_id="S7 measurement report",
        name="ScenePolicy",
        payload=report_policy,
        expected=context.scene_policy_fingerprint,
    )


def run_s7_review(
    context: PipelineContextV6Runtime,
    *,
    water_relief_intent: Mapping[str, Any],
    composition_spec: Mapping[str, Any],
    measurement_report: Mapping[str, Any],
    review_artifacts: Mapping[str, str],
    terminal_disposition: str,
) -> PipelineContextV7Runtime:
    """Seal S7 evidence into the Context consumed by S8 or a terminal mode.

    Rendering and atomic artifact IO remain effects of the production adapter;
    this boundary makes their complete result explicit and prevents S8/S10
    from reaching back into loose S4-S7 locals.
    """

    _require_exact_context(context, PipelineContextV6Runtime, "S7")
    _require_payload_fingerprint(
        stage_id="S7",
        name="SceneCharacter",
        payload=context.scene_character,
        expected=context.scene_character_fingerprint,
    )
    _require_payload_fingerprint(
        stage_id="S7",
        name="ScenePolicy",
        payload=context.scene_policy,
        expected=context.scene_policy_fingerprint,
    )
    if terminal_disposition not in {"continue", "review", "styles", "draft"}:
        raise ValueError("unsupported S7 terminal disposition")
    if not composition_spec:
        raise DomainContextError("S7 requires a non-empty CompositionSpec")
    if not measurement_report:
        raise DomainContextError("S7 requires a non-empty measurement report")
    _require_s7_payload_binding(
        context,
        composition_spec=composition_spec,
        measurement_report=measurement_report,
    )
    return PipelineContextV7Runtime(
        predecessor=context,
        water_relief_intent=thaw_json(water_relief_intent),
        composition_spec=thaw_json(composition_spec),
        measurement_report=thaw_json(measurement_report),
        review_artifacts=dict(review_artifacts),
        terminal_disposition=terminal_disposition,
    )


def run_s8_mesh_materialization(
    context: PipelineContextV7Runtime,
    *,
    meshes: Mapping[str, Any],
    water_relief: Mapping[str, Any],
    block_base_clearance: Mapping[str, Any] | None,
    source_feature_counts: Mapping[str, Any],
    vegetation_enabled: bool,
    block_base_enabled: bool,
    merge_block_layers: bool,
) -> PipelineContextV8Runtime:
    """Seal the exact S8 mesh products and derive the S9 gate request.

    Geometry builders remain the reviewed legacy implementation.  This
    adapter owns their output boundary: the next Stage can consume only this
    complete bundle, not loose mesh locals or recomputed layer predicates.
    """

    _require_exact_context(context, PipelineContextV7Runtime, "S8")
    _require_payload_fingerprint(
        stage_id="S8",
        name="ScenePolicy",
        payload=context.scene_policy,
        expected=context.predecessor.scene_policy_fingerprint,
    )
    from aesthetic.pipeline_observation import summarize_meshes

    layers = context.layers
    required_roles = ["terrain"]
    if layers.BL:
        required_roles.append("landmarks")
    if layers.BO and not merge_block_layers:
        required_roles.append("buildings")
    if final_layer_counts(layers)['roads']:
        required_roles.append("roads")
    if layers.WL or layers.WO:
        required_roles.append("water")
    if vegetation_enabled and (layers.VL or layers.VO):
        required_roles.append("vegetation")
    if block_base_enabled and (
            layers.block_base or (merge_block_layers and layers.BO)):
        required_roles.append("block_base")
    final_counts = final_layer_counts(layers)
    omissions = ("vegetation",) if not vegetation_enabled else ()
    source_counts = dict(source_feature_counts)
    source_fingerprint = feature_source_counts_fingerprint(source_counts)
    if source_fingerprint != context.runtime.fingerprints.source_feature_counts:
        raise ContextFingerprintMismatch(
            "S8 source feature counts do not match the carried S2 identity")
    clearance = dict(block_base_clearance or {})
    from aesthetic.city_surface_plan import POLICY_VERSION as SURFACE_POLICY_VERSION
    if (getattr(layers, 'surface_plan_evidence', {}) or {}).get('policy_version') == SURFACE_POLICY_VERSION:
        clearance['city_materialization'] = {
            role: dict(mesh.metadata.get('surface_materialization', {}))
            for role, mesh in meshes.items()
            if role in {'block_base', 'roads', 'landmarks'} and mesh is not None}
    return PipelineContextV8Runtime(
        predecessor=context,
        meshes=dict(meshes),
        mesh_summary=summarize_meshes(meshes),
        water_relief=dict(water_relief),
        block_base_clearance=clearance,
        required_roles=tuple(required_roles),
        source_feature_counts=source_counts,
        final_layer_counts=final_counts,
        intentional_omissions=omissions,
        require_block_base_clearance=bool(
            "block_base" in required_roles
            and getattr(layers, "block_base_cut_lines", None)),
    )


def run_s9_mesh_gate(
    context: PipelineContextV8Runtime,
    *,
    require_gate: Callable[..., Mapping[str, Any]] | None = None,
) -> PipelineContextV9Runtime:
    """Run the read-only printable bundle gate against the exact V8 object."""

    _require_exact_context(context, PipelineContextV8Runtime, "S9")
    actual_mesh = semantic_mesh_bundle_fingerprint(context.meshes)
    if actual_mesh != context.mesh_bundle_fingerprint:
        raise ContextFingerprintMismatch(
            "S9 received a mutated semantic mesh bundle")
    from aesthetic.city_surface_plan import verify_materialized_city
    try:
        verify_materialized_city(context.predecessor.layers, context.meshes,
                                 context.block_base_clearance, context.predecessor.runtime.scale_mm_per_m)
    except ValueError as exc:
        raise DomainContextError(str(exc)) from exc
    if require_gate is None:
        from aesthetic.pipeline_gates import require_semantic_mesh_bundle
        require_gate = require_semantic_mesh_bundle
    evidence = require_gate(
        context.meshes,
        required_roles=context.required_roles,
        block_base_clearance=thaw_json(context.block_base_clearance),
        require_block_base_clearance=context.require_block_base_clearance,
        source_feature_counts=thaw_json(context.source_feature_counts),
        final_layer_counts=thaw_json(context.final_layer_counts),
        intentional_omissions=context.intentional_omissions,
    )
    if not isinstance(evidence, Mapping):
        raise DomainContextError("S9 gate must return a Mapping")
    return PipelineContextV9Runtime(
        predecessor=context,
        geometry_gate=dict(evidence),
    )


def require_s10_export_context(
    context: PipelineContextV9Runtime,
) -> Mapping[str, Any]:
    """Recheck V9 immediately before exporter/file-system effects begin."""

    _require_exact_context(context, PipelineContextV9Runtime, "S10")
    actual_mesh = semantic_mesh_bundle_fingerprint(context.meshes)
    if actual_mesh != context.verified_mesh_bundle_fingerprint:
        raise ContextFingerprintMismatch(
            "S10 received a semantic mesh bundle changed after S9")
    _require_payload_fingerprint(
        stage_id="S10",
        name="GeometryGate",
        payload=context.geometry_gate,
        expected=context.geometry_gate_fingerprint,
    )
    return context.meshes


def run_s10_artifact_bundle(
    context: PipelineContextV9Runtime,
    *,
    artifact_paths: Mapping[str, str | os.PathLike[str]],
    status: str = "generated_pending_validation",
) -> PipelineContextV10Runtime:
    """Bind the six published files to the exact clean V9 Context."""

    require_s10_export_context(context)
    if set(artifact_paths) != set(S10_REQUIRED_ARTIFACT_BUNDLE):
        raise DomainContextError(
            "S10 must publish exactly the required six-piece artifact bundle")
    manifest = {
        str(name): _stable_file_claim(path)
        for name, path in artifact_paths.items()
    }
    return PipelineContextV10Runtime(
        predecessor=context,
        status=status,
        artifact_manifest=manifest,
    )


def final_layer_counts(layers: Any) -> dict[str, int]:
    """Return the canonical JSON-safe S6/S7 layer summary."""

    return {
        "BL": len(layers.BL),
        "BO": len(layers.BO),
        "WL": len(layers.WL),
        "WO": len(layers.WO),
        "VL": len(layers.VL),
        "VO": len(layers.VO),
        "roads": (len(layers.surface_road_polygons)
                  if (getattr(layers, 'surface_plan_evidence', {}) or {}).get('status') == 'finalized'
                  else len(layers.roads_lines)),
        "block_base": len(layers.block_base),
    }
