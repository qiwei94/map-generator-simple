from dataclasses import dataclass, field
from types import SimpleNamespace

import numpy as np
import pytest

from aesthetic.pipeline_contract import (
    S10_REQUIRED_ARTIFACT_BUNDLE,
    feature_source_counts_fingerprint,
)
from aesthetic.pipeline_domain import (
    CarriedFingerprints,
    ContextFingerprintMismatch,
    DomainContextError,
    PipelineContextV3Runtime,
    PipelineContextV4Runtime,
    PipelineContextV5Runtime,
    PipelineContextV8Runtime,
    PipelineContextV9Runtime,
    PipelineContextV10Runtime,
    ProjectedSources,
    RuntimeIdentity,
    RuntimeInputs,
    WrongPredecessorContext,
    context_handoff_ledger_value,
    run_s4_observation,
    run_s5_policy,
    run_s6_building_roles,
    run_s7_review,
    run_s8_mesh_materialization,
    run_s9_mesh_gate,
    require_s10_export_context,
    run_s10_artifact_bundle,
    thaw_json,
)


SOURCE_COUNTS = {
    "roads": 1,
    "buildings": 1,
    "water": 0,
    "vegetation": 0,
}


@dataclass
class _Layers:
    BL: list = field(default_factory=lambda: [("hero", 2.0)])
    BL_categories: list = field(default_factory=lambda: [None])
    BL_height_roles: list = field(default_factory=lambda: ["identity_anchor"])
    BO: list = field(default_factory=lambda: ["quiet"])
    BO_heights: list = field(default_factory=lambda: [0.4])
    VL: list = field(default_factory=list)
    VO: list = field(default_factory=list)
    WL: list = field(default_factory=list)
    WO: list = field(default_factory=list)
    block_base: list = field(default_factory=lambda: ["block"])
    block_base_classes: list = field(default_factory=lambda: ["urban"])
    block_base_cut_lines: list = field(default_factory=lambda: ["road-cut"])
    city_blocks: list = field(default_factory=lambda: ["city-block"])
    roads_lines: list = field(default_factory=lambda: ["road"])
    road_roles: dict = field(default_factory=lambda: {
        "scale_aware_topology": {"policy_version": "topology-v1"},
    })
    water_roles: dict = field(default_factory=lambda: {"policy_version": "water-v1"})
    building_height_evidence: dict = field(default_factory=dict)
    nozzle_real_m: float = 40.0
    min_area_m2: float = 1600.0


def _context_v3():
    terrain_fingerprint = "c" * 64
    runtime = RuntimeInputs(
        identity=RuntimeIdentity("run-fixture", "attempt-1"),
        city="fixture-city",
        sources=ProjectedSources(
            roads=["source-road"],
            buildings=["source-building"],
            water=["source-water"],
        ),
        bbox_local_m=(0.0, 0.0, 1000.0, 800.0),
        bbox_wgs84=(30.0, 120.0, 30.1, 120.1),
        elevation_grid=[[1.0, 2.0], [3.0, 4.0]],
        scale_mm_per_m=0.2,
        printer_profile=SimpleNamespace(
            profile_id="fixture-printer",
            extrusion_width_mm=0.42,
            layer_height_mm=0.2,
            min_surface_height_mm=0.4,
        ),
        terrain_surface_plan=SimpleNamespace(fingerprint=terrain_fingerprint),
        amap_reference=None,
        amap_evidence={"status": "unavailable", "reason": "fixture"},
        fingerprints=CarriedFingerprints(
            preprocess_parameters="a" * 64,
            source_feature_counts=feature_source_counts_fingerprint(
                SOURCE_COUNTS),
            terrain_surface=terrain_fingerprint,
        ),
    )
    return PipelineContextV3Runtime(runtime=runtime, layers=_Layers())


def _observe(context):
    def analyze(*_args, **_kwargs):
        return {
            "version": "scene-character-v1",
            "status": "ready",
            "grid_size": 8,
            "metrics": {"buildings": {"count": 1}},
            "summary": {},
        }

    def refresh(scene):
        scene["metrics"]["building_data_quality"] = {"status": "ready"}

    return run_s4_observation(
        context,
        analyze=analyze,
        summarize_external=lambda *_args, **_kwargs: {},
        refresh_quality=refresh,
    )


def _policy(context):
    return run_s5_policy(
        context,
        activation="active",
        resolve=lambda *_args, **_kwargs: {
            "policy_version": "scene-policy-v1",
            "activation": "active",
            "scene_class": "urban",
            "archetype": "grid_metropolis",
            "garden_city_strategy": {},
            "roles": {},
        },
    )


def test_accepted_z_style_is_s5_owned_only_for_active_C_route():
    c=_observe(_context_v3())
    def resolver(*args,**kwargs):return {'activation':kwargs['activation']}
    active=run_s5_policy(c,activation='active',urban_organization='C',resolve=resolver)
    assert active.scene_policy['z_texture']['amplitude_mm']==.07
    assert 'z_texture' not in run_s5_policy(c,activation='audit_only',urban_organization='C',resolve=resolver).scene_policy
    assert 'z_texture' not in run_s5_policy(c,activation='active',resolve=resolver).scene_policy


def test_landscape_policy_skips_city_texture_and_clears_s6_fill():
    source = _context_v3()
    context = run_s5_policy(_observe(source), activation='active',
        urban_organization='block-first',
        resolve=lambda *args, **kwargs: {
            'activation': 'active', 'landscape_strategy': {'enabled': True}})
    assert 'z_texture' not in context.scene_policy
    assert 'block_first' not in context.scene_policy
    final = _building(context)
    assert not final.layers.block_base and not final.layers.BO
    assert tuple(source.layers.block_base) == ('block',)
    assert final.layers.BL
    assert final.building_mass_evidence['landscape_geometry']['status'] == 'active'


def _building(context):
    def route(layers, **_kwargs):
        layers.BO.append("routed")
        return {
            "status": "active",
            "geometry_changed": True,
            "sub_nozzle_heroes_demoted_to_mass": 1,
            "minimum_independent_width_mm": 0.42,
        }

    def mass(layers, *_args, **_kwargs):
        layers.BO = ["final-quiet", "final-mass"]
        layers.BO_heights = [0.4, 0.8]
        return {"status": "active", "output_components": 2}

    def cap(layers, *_args, **_kwargs):
        layers.BL = [("hero", 1.8)]
        return {
            "status": "active",
            "hero_count": 1,
            "after_height_mm": {"max": 1.8},
        }

    return run_s6_building_roles(
        context,
        route_heroes=route,
        apply_mass=mass,
        cap_heights=cap,
        prepare_region=lambda *_args, **_kwargs: {"status": "prepared"},
        apply_emphasis=lambda *_args, **_kwargs: {"status": "active"},
    )


@pytest.mark.parametrize('planar_only', [False, True])
def test_s6_merge_mode_seals_surface_geometry_without_mutating_s3(planar_only):
    from dataclasses import replace
    from shapely.geometry import LineString, box
    from _TEXTURE_STYLE_OF_DEEPSEEK._layer_preprocess import LayerPolygons
    from _TEXTURE_STYLE_OF_DEEPSEEK.print_profile import DEFAULT_PRINTER_PROFILE
    original = LayerPolygons(
        BO=[box(0, 0, 40, 40)], BO_heights=[0.84],
        block_base_cut_lines=[LineString([(20, 0), (20, 40)])])
    v3 = _context_v3()
    v3 = PipelineContextV3Runtime(
        runtime=replace(v3.runtime, printer_profile=DEFAULT_PRINTER_PROFILE,
                        terrain_surface_plan=SimpleNamespace(
                            fingerprint=v3.runtime.terrain_surface_plan.fingerprint,
                            surface_z_grid_mm=np.zeros((3, 3)), width_m=1000.,
                            height_m=800., scale_mm_per_m=.2)),
        layers=original)
    v5 = _policy(_observe(v3))
    v6 = run_s6_building_roles(
        v5, merge_block_layers=True, planar_only=planar_only,
        route_heroes=lambda *_a, **_kw: {'status': 'active'},
        apply_mass=lambda *_a, **_kw: {'status': 'active'},
        cap_heights=lambda *_a, **_kw: {'status': 'active'},
        prepare_region=lambda *_a, **_kw: {},
        apply_emphasis=lambda *_a, **_kw: {})
    assert len(v3.layers.BO) == len(original.BO) == 1
    assert len(v6.layers.BO) == 2
    assert v6.layers.BO_heights == (0.84, 0.84)
    assert v6.building_mass_evidence['final_surface_plan']['owner_stage'] == 'S6'
    assert bool(v6.layers.surface_grounding) is (not planar_only)


def _composition(v6):
    return {
        "schema_version": "1.0",
        "city": v6.runtime.city,
        "bbox_wgs84": list(v6.runtime.bbox_wgs84),
        "scene_policy": thaw_json(v6.scene_policy),
    }


def _measurement(v6, *, attempt_id=None):
    return {
        "schema_version": "pipeline-measurement-v2",
        "raw_measurements": {
            "run": {
                "run_id": v6.identity.run_id,
                "attempt_id": attempt_id or v6.identity.attempt_id,
                "city": v6.runtime.city,
                "terrain_surface_fingerprint": (
                    v6.runtime.fingerprints.terrain_surface),
            },
            "scene_character": thaw_json(v6.scene_character),
        },
        "resolved_decisions": {
            "scene_policy": thaw_json(v6.scene_policy),
        },
    }


class _Mesh:
    def __init__(self, offset=0.0):
        self.vertices = np.asarray([
            [offset + 0.0, 0.0, 0.0],
            [offset + 1.0, 0.0, 0.0],
            [offset + 0.0, 1.0, 0.0],
            [offset + 0.0, 0.0, 1.0],
        ], dtype=np.float64)
        self.faces = np.asarray([
            [0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3],
        ], dtype=np.int64)
        self.is_watertight = True
        self.is_winding_consistent = True

    @property
    def bounds(self):
        return np.asarray([
            self.vertices.min(axis=0), self.vertices.max(axis=0),
        ])


def _context_v7():
    v6 = _building(_policy(_observe(_context_v3())))
    return run_s7_review(
        v6,
        water_relief_intent={"status": "pending_s8_materialization"},
        composition_spec=_composition(v6),
        measurement_report=_measurement(v6),
        review_artifacts={"scene_character": "scene.json"},
        terminal_disposition="continue",
    )


def _mesh_bundle():
    return {
        "terrain": _Mesh(0.0),
        "landmarks": _Mesh(2.0),
        "buildings": _Mesh(4.0),
        "roads": _Mesh(6.0),
        "water": None,
        "vegetation": None,
        "block_base": _Mesh(8.0),
    }


def _context_v8():
    return run_s8_mesh_materialization(
        _context_v7(),
        meshes=_mesh_bundle(),
        water_relief={"status": "not_applicable"},
        block_base_clearance={
            "status": "checked",
            "passed": True,
            "target_gap_mm": 0.84,
            "verified_min_gap_mm": 0.84,
            "post_clip_intrusion_area_m2": 0.0,
            "measurement_tolerance_m2": 1e-6,
        },
        source_feature_counts=SOURCE_COUNTS,
        vegetation_enabled=False,
        block_base_enabled=True,
        merge_block_layers=False,
    )


def test_runtime_context_chain_is_exact_and_carries_one_identity():
    v3 = _context_v3()
    v4 = _observe(v3)
    v5 = _policy(v4)
    v6 = _building(v5)
    v7 = run_s7_review(
        v6,
        water_relief_intent={"status": "pending_s8_materialization"},
        composition_spec=_composition(v6),
        measurement_report=_measurement(v6),
        review_artifacts={"scene_character": "scene.json"},
        terminal_disposition="continue",
    )

    assert v4.predecessor is v3
    assert v5.predecessor is v4
    assert v6.predecessor is v5
    assert v7.predecessor is v6
    assert v7.identity == RuntimeIdentity("run-fixture", "attempt-1")
    ledger_value = v7.ledger_value()
    assert {
        key: ledger_value[key]
        for key in (
            "preprocess_parameters_fingerprint",
            "source_feature_counts_fingerprint",
            "terrain_surface_fingerprint",
            "domain_context_version",
            "scene_character_fingerprint",
            "scene_policy_fingerprint",
            "domain_context_in",
            "domain_context_out",
        )
    } == {
        "preprocess_parameters_fingerprint": "a" * 64,
        "source_feature_counts_fingerprint": (
            feature_source_counts_fingerprint(SOURCE_COUNTS)),
        "terrain_surface_fingerprint": "c" * 64,
        "domain_context_version": "pipeline-domain-context-v1",
        "scene_character_fingerprint": v4.scene_character_fingerprint,
        "scene_policy_fingerprint": v5.scene_policy_fingerprint,
        "domain_context_in": "PipelineContextV6Runtime",
        "domain_context_out": "PipelineContextV7Runtime",
    }
    for key in (
        "final_layers_fingerprint",
        "building_role_evidence_fingerprint",
        "composition_spec_fingerprint",
        "measurement_report_fingerprint",
        "s7_review_fingerprint",
    ):
        assert len(ledger_value[key]) == 64
    assert context_handoff_ledger_value(
        v7, run_id="run-fixture", attempt_id="attempt-1") == ledger_value


def test_s4_and_s5_do_not_mutate_s3_layers_and_s6_owns_a_clone():
    v3 = _context_v3()
    original = v3.layers
    original_bo = tuple(original.BO)
    original_bl = tuple(original.BL)
    original_roles = dict(original.road_roles)

    v4 = _observe(v3)
    v5 = _policy(v4)
    assert v4.layers is original
    assert v5.layers is original
    assert original.BO == original_bo
    assert original.BL == original_bl

    v6 = _building(v5)
    assert v6.layers is not original
    assert v6.layers.BO == ("final-quiet", "final-mass")
    assert v6.layers.BL == (("hero", 1.8),)
    assert original.BO == original_bo
    assert original.BL == original_bl
    assert original.road_roles == original_roles
    assert v6.layers.road_roles is not original.road_roles


def test_wrong_predecessor_is_rejected_before_any_stage_effect():
    calls = []
    with pytest.raises(WrongPredecessorContext, match="S5 requires exact"):
        run_s5_policy(
            _context_v3(),
            activation="active",
            resolve=lambda *_args, **_kwargs: calls.append("called"),
        )
    assert calls == []


def test_context_constructor_itself_rejects_wrong_predecessor():
    v3 = _context_v3()
    with pytest.raises(
            WrongPredecessorContext, match="S5 Context requires exact"):
        PipelineContextV5Runtime(
            predecessor=v3,
            scene_policy={"policy_version": "invalid-chain"},
            scene_policy_fingerprint=(
                "this fingerprint must never be evaluated"),
        )


def test_mutated_scene_character_is_rejected_before_policy_resolution():
    v4 = _observe(_context_v3())
    with pytest.raises(TypeError):
        v4.scene_character["status"] = "tampered"

    tampered = dict(v4.scene_character)
    tampered["status"] = "tampered"
    with pytest.raises(ContextFingerprintMismatch, match="SceneCharacter"):
        PipelineContextV4Runtime(
            predecessor=v4.predecessor,
            scene_character=tampered,
            scene_character_fingerprint=v4.scene_character_fingerprint,
        )


def test_s7_rejects_incomplete_or_wrong_context():
    v5 = _policy(_observe(_context_v3()))
    with pytest.raises(WrongPredecessorContext, match="S7 requires exact"):
        run_s7_review(
            v5,
            water_relief_intent={},
            composition_spec={"schema_version": "1.0"},
            measurement_report={"schema_version": "v1"},
            review_artifacts={},
            terminal_disposition="continue",
        )

    v6 = _building(v5)
    with pytest.raises(DomainContextError, match="non-empty CompositionSpec"):
        run_s7_review(
            v6,
            water_relief_intent={},
            composition_spec={},
            measurement_report={"schema_version": "v1"},
            review_artifacts={},
            terminal_disposition="continue",
        )


def test_s7_rejects_report_from_another_attempt():
    v6 = _building(_policy(_observe(_context_v3())))
    with pytest.raises(
            ContextFingerprintMismatch, match="identity does not match"):
        run_s7_review(
            v6,
            water_relief_intent={"status": "pending"},
            composition_spec=_composition(v6),
            measurement_report=_measurement(v6, attempt_id="attempt-2"),
            review_artifacts={},
            terminal_disposition="continue",
        )


def test_ledger_handoff_rejects_context_from_another_attempt():
    v4 = _observe(_context_v3())
    with pytest.raises(
            ContextFingerprintMismatch, match="active ledger"):
        context_handoff_ledger_value(
            v4, run_id="run-fixture", attempt_id="attempt-2")


def test_v8_v10_chain_binds_mesh_gate_and_six_published_files(tmp_path):
    v8 = _context_v8()
    assert type(v8) is PipelineContextV8Runtime
    assert v8.predecessor.terminal_disposition == "continue"
    assert v8.required_roles == (
        "terrain", "landmarks", "buildings", "roads", "block_base")
    assert len(v8.mesh_bundle_fingerprint) == 64
    assert len(v8.s8_materialization_fingerprint) == 64

    v9 = run_s9_mesh_gate(v8)
    assert type(v9) is PipelineContextV9Runtime
    assert v9.predecessor is v8
    assert v9.geometry_gate["passed"] is True
    assert v9.geometry_gate["errors"] == ()
    assert require_s10_export_context(v9) is v8.meshes

    paths = {}
    for index, name in enumerate(S10_REQUIRED_ARTIFACT_BUNDLE):
        path = tmp_path / f"{index}-{name}.artifact"
        path.write_bytes(f"fixture-{name}".encode("utf-8"))
        paths[name] = path
    v10 = run_s10_artifact_bundle(v9, artifact_paths=paths)
    assert type(v10) is PipelineContextV10Runtime
    assert v10.predecessor is v9
    assert set(v10.artifact_manifest) == set(S10_REQUIRED_ARTIFACT_BUNDLE)
    assert all(len(item["sha256"]) == 64
               for item in v10.artifact_manifest.values())
    ledger_value = context_handoff_ledger_value(
        v10, run_id="run-fixture", attempt_id="attempt-1")
    assert ledger_value["domain_context_in"] == "PipelineContextV9Runtime"
    assert ledger_value["domain_context_out"] == "PipelineContextV10Runtime"
    assert ledger_value["s9_errors"] == 0
    assert ledger_value["s9_warnings"] == 0
    assert len(ledger_value["artifact_bundle_fingerprint"]) == 64


def test_s9_rejects_mutated_v8_mesh_before_gate_effect():
    v8 = _context_v8()
    v8.meshes["roads"].vertices[0, 0] = 99.0
    calls = []
    with pytest.raises(
            ContextFingerprintMismatch, match="mutated semantic mesh"):
        run_s9_mesh_gate(
            v8,
            require_gate=lambda *_args, **_kwargs: calls.append("called"),
        )
    assert calls == []


def test_s9_constructor_rejects_dirty_gate_and_wrong_predecessor():
    v8 = _context_v8()
    with pytest.raises(DomainContextError, match="clean fail-closed"):
        PipelineContextV9Runtime(
            predecessor=v8,
            geometry_gate={
                "gate_version": "fixture",
                "status": "failed",
                "passed": False,
                "required_roles": list(v8.required_roles),
                "errors": ["broken"],
                "warnings": [],
            },
        )
    calls = []
    with pytest.raises(WrongPredecessorContext, match="S9 requires exact"):
        run_s9_mesh_gate(
            _context_v7(),
            require_gate=lambda *_args, **_kwargs: calls.append("called"),
        )
    assert calls == []


def test_s10_rejects_incomplete_bundle_before_hashing(tmp_path):
    v9 = run_s9_mesh_gate(_context_v8())
    with pytest.raises(DomainContextError, match="exactly the required"):
        run_s10_artifact_bundle(
            v9,
            artifact_paths={"3mf": tmp_path / "missing.3mf"},
        )
