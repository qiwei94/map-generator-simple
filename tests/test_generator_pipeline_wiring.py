"""Static wiring guards for the legacy generator's canonical stage shell.

These tests do not claim that the monolith is fully dependency-injected.  They
prevent the expensive consumers from drifting back across the explicit Stage
boundaries while the remaining domain Context refactor proceeds incrementally.
"""

import ast
from pathlib import Path

from aesthetic.pipeline_contract import stage_by_id


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "generate_city_legacy.py"
DOMAIN_SOURCE = ROOT / "aesthetic" / "pipeline_domain.py"


def _run_tree():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    return next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_run_pipeline"
    )


def _call_name(call):
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return ""


def _calls(name):
    return sorted(
        (node for node in ast.walk(_run_tree())
         if isinstance(node, ast.Call) and _call_name(node) == name),
        key=lambda node: node.lineno,
    )


def _stage_calls(name):
    result = []
    for call in _calls(name):
        first = call.args[0] if call.args else None
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            result.append((first.value, call.lineno))
    return result


def test_generator_starts_each_canonical_generation_stage_once_in_order():
    starts = _stage_calls("_ledger_stage_start")
    assert [stage for stage, _line in starts] == [
        f"S{index}" for index in range(11)
    ]
    assert [line for _stage, line in starts] == sorted(
        line for _stage, line in starts)


def test_domain_consumers_stay_inside_their_declared_stage_boundaries():
    starts = dict(_stage_calls("_ledger_stage_start"))
    completes = _stage_calls("_ledger_stage_complete")

    def first_complete(stage):
        return min(line for item, line in completes if item == stage)

    expected = {
        "run_s4_observation": "S4",
        "run_s5_policy": "S5",
        "run_s6_building_roles": "S6",
        "build_deepseek_terrain": "S8",
        "run_s8_mesh_materialization": "S8",
        "run_s9_mesh_gate": "S9",
        "export_deepseek_3mf": "S10",
        "run_s10_artifact_bundle": "S10",
    }
    for consumer, stage in expected.items():
        calls = _calls(consumer)
        assert len(calls) == 1, consumer
        assert starts[stage] < calls[0].lineno < first_complete(stage), consumer

    # S7 has three mutually-exclusive terminal paths (review-only, draft and
    # normal/full).  Every one must seal the exact V6 Context before ledger
    # completion or a downstream return.
    s7_calls = _calls("run_s7_review")
    assert len(s7_calls) == 3
    assert all(
        starts["S7"] < call.lineno < starts["S8"]
        for call in s7_calls
    )


def test_runtime_adapters_own_the_real_s4_s6_domain_consumers():
    generator_tree = _run_tree()
    generator_calls = {
        _call_name(node) for node in ast.walk(generator_tree)
        if isinstance(node, ast.Call)
    }
    assert not generator_calls & {
        "analyze_scene_character",
        "resolve_scene_policy",
        "apply_building_mass_to_layers",
        "route_sub_nozzle_heroes",
        "cap_building_heights_to_terrain",
    }

    domain_tree = ast.parse(DOMAIN_SOURCE.read_text(encoding="utf-8"))
    functions = {
        node.name: node for node in domain_tree.body
        if isinstance(node, ast.FunctionDef)
    }

    def names_in(function):
        return {
            _call_name(node) for node in ast.walk(functions[function])
            if isinstance(node, ast.Call)
        }

    assert "analyze" in names_in("run_s4_observation")
    assert "resolve" in names_in("run_s5_policy")
    assert {"route_heroes", "apply_mass", "cap_heights"} <= names_in(
        "run_s6_building_roles")
    assert "require_gate" in names_in("run_s9_mesh_gate")
    assert "_stable_file_claim" in names_in("run_s10_artifact_bundle")


def test_canonical_draft_preview_consumes_only_the_shared_surface_plan():
    calls = _calls("render_glb_preview")
    assert len(calls) == 1
    keywords = {item.arg for item in calls[0].keywords}
    assert "terrain_surface_plan" in keywords
    assert "elevation_grid" not in keywords
    assert "terrain_mesh" not in keywords


def test_pipeline_observation_links_only_published_measurement_reports():
    calls = _calls("build_pipeline_observation_report")
    assert len(calls) == 1
    source = ast.get_source_segment(SOURCE.read_text(encoding="utf-8"), calls[0])
    assert source is not None
    assert '_measurement_report_paths["json"]' not in source
    assert '_measurement_report_paths["html"]' not in source
    assert '_measurement_stem}.json' in source
    assert '_measurement_stem}.html' in source
    assert '"measurement_report_json"' in source
    assert '"measurement_report_html"' in source
    assert '"pipeline_measurement_report_json"' not in source
    assert '"pipeline_measurement_report_html"' not in source


def test_formal_mesh_materialization_cannot_start_before_s8():
    starts = dict(_stage_calls("_ledger_stage_start"))
    mesh_builders = (
        "build_deepseek_terrain",
        "build_deepseek_buildings_v3",
        "build_deepseek_roads_v3",
        "build_deepseek_water_v3",
        "build_deepseek_vegetation_v3",
        "build_deepseek_block_base_v3",
    )
    for name in mesh_builders:
        assert all(call.lineno > starts["S8"] for call in _calls(name)), name


def test_s2_resolves_the_preprocess_source_and_s3_only_consumes_it():
    starts = dict(_stage_calls("_ledger_stage_start"))
    completes = _stage_calls("_ledger_stage_complete")
    s2_end = min(line for stage, line in completes if stage == "S2")
    s3_end = min(line for stage, line in completes if stage == "S3")
    for name in ("_load_amap_salience_guide",
                 "_load_snap_amap_salience_guide"):
        calls = _calls(name)
        assert calls, name
        assert all(starts["S2"] < call.lineno < s2_end for call in calls)
        assert not any(starts["S3"] < call.lineno < s3_end for call in calls)

    source = SOURCE.read_text(encoding="utf-8")
    assert '"preprocess_parameters": _preprocess_parameters' in source
    assert '"preprocess_parameters_fingerprint": (' in source
    assert '_preprocess_parameters["effective_overrides"]' in source
    assert '"merge_mode": bool(MERGE_BLOCK_LAYERS)' in source


def test_cross_source_scene_summary_is_imported_in_the_pipeline_scope():
    """The optional AMap correction must not silently fall back on NameError."""

    run_tree = _run_tree()
    summary_calls = [
        node for node in ast.walk(run_tree)
        if isinstance(node, ast.Call)
        and _call_name(node) == "summarize_amap_urban_evidence"
    ]
    imports = [
        node for node in ast.walk(run_tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "aesthetic.amap_salience"
        and any(
            alias.name == "summarize_amap_urban_evidence"
            for alias in node.names
        )
    ]
    assert len(summary_calls) == 1
    assert len(imports) == 1
    assert imports[0].lineno < summary_calls[0].lineno


def test_review_artifacts_are_attempt_scoped_with_compatibility_aliases():
    source = SOURCE.read_text(encoding="utf-8")
    assert 'f"{CITY_NAME}_preview.{_artifact_identity}.png"' in source
    assert 'f"{CITY_NAME}_draft.{_artifact_identity}.glb"' in source
    assert 'f"{CITY_NAME}.{_artifact_identity}"' in source
    assert 'f"{CITY_NAME}_preview.png"' in source
    assert 'f"{CITY_NAME}_draft.glb"' in source
    assert 'f"{CITY_NAME}_topdown.png"' in source


def test_every_generator_stage_completion_emits_its_required_context_keys():
    runtime_carried_keys = {
        "preprocess_parameters_fingerprint",
        "source_feature_counts_fingerprint",
        "terrain_surface_fingerprint",
        "scene_character_fingerprint",
        "scene_policy_fingerprint",
    }
    for call in _calls("_ledger_stage_complete"):
        assert len(call.args) >= 2
        stage_node, context_node = call.args[:2]
        assert isinstance(stage_node, ast.Constant)
        stage_id = stage_node.value
        assert isinstance(context_node, ast.Dict), stage_id
        literal_keys = {
            key.value for key in context_node.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        missing = set(stage_by_id(stage_id).required_context_keys) - literal_keys
        if stage_id in {"S4", "S5", "S6", "S7", "S8", "S9", "S10"}:
            unpacked_calls = [
                value for key, value in zip(
                    context_node.keys, context_node.values)
                if key is None and isinstance(value, ast.Call)
            ]
            assert len(unpacked_calls) == 1, stage_id
            assert _call_name(unpacked_calls[0]) == (
                "context_handoff_ledger_value")
            if stage_id in {"S4", "S5", "S6", "S7"}:
                missing -= runtime_carried_keys
            else:
                # V8-V10 ledger_value owns the complete Stage-specific
                # contract, including mesh/gate/artifact evidence.
                missing.clear()
        assert not missing, f"{stage_id} missing {sorted(missing)}"
