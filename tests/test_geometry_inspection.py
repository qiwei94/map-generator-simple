from types import SimpleNamespace
import json
import numpy as np
from shapely.geometry import box
from aesthetic.geometry_inspection import build_geometry_inspection
from aesthetic.city_surface_plan import finalize_city_surfaces, materialize_city_role
from aesthetic.measurement_report import build_measurement_report, write_measurement_report
from _TEXTURE_STYLE_OF_DEEPSEEK._layer_preprocess import LayerPolygons
from _TEXTURE_STYLE_OF_DEEPSEEK.print_profile import DEFAULT_PRINTER_PROFILE
from _TEXTURE_STYLE_OF_DEEPSEEK.render_glb import _TerrainSurfacePlanSampler
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import resolve_terrain_surface_plan


def fixture():
    layers = LayerPolygons(BO=[box(-1,-1,1,1)], BO_heights=[.4])
    terrain = resolve_terrain_surface_plan(np.zeros((3,3)), 4., 4., 1.)
    finalize_city_surfaces(layers, bbox_local=(-2,-2,2,2), scale=1.,
        printer_profile=DEFAULT_PRINTER_PROFILE, terrain_surface_plan=terrain)
    sampler = _TerrainSurfacePlanSampler(terrain, (-2,-2,2,2), 1.)
    mesh, _ = materialize_city_role(layers, 'city', 1., sampler.z_mm_vec)
    return layers, mesh


def states(report):
    return {x['id']: x['status'] for x in report['checks']}


def test_s7_plan_cannot_pretend_mesh_or_slice_has_passed():
    layers, _ = fixture()
    report = build_geometry_inspection(layers)
    s = states(report)
    assert s['city.plan'] == 'passed'
    assert s['city.materialization'] == s['city.shell'] == 'pending'
    assert s['slice_survival'] == s['final_contact'] == 'pending'
    assert report['print_acceptance'] == 'pending'


def test_actual_mesh_passes_only_its_scope_and_tampering_fails():
    layers, mesh = fixture()
    report = build_geometry_inspection(layers, {'block_base': mesh})
    assert states(report)['city.materialization'] == 'passed'
    assert states(report)['city.shell'] == 'passed'
    assert states(report)['landmarks.materialization'] == 'not_applicable'
    assert report['status'] == 'partial'
    mesh.vertices[0,2] += .1
    bad = build_geometry_inspection(layers, {'block_base': mesh})
    assert states(bad)['city.materialization'] == 'failed'
    assert bad['status'] == 'error'
    assert states(build_geometry_inspection(layers, {}))['city.materialization'] == 'failed'


def test_missing_legacy_and_road_evidence_never_passes():
    report = build_geometry_inspection(SimpleNamespace())
    assert set(states(report).values()) == {'pending'}
    layers, mesh = fixture()
    layers.surface_grounding.pop('roads', None)
    layers.surface_plan_evidence['grounding'].pop('roads', None)
    layers.surface_plan_evidence['road_surface_plan']['polygon_count'] = 1
    layers.surface_plan_evidence['road_surface_plan']['bridge_source_line_parts'] = 1
    s = states(build_geometry_inspection(layers, {'block_base':mesh, 'roads':mesh}))
    assert s['road_contact'] == s['bridge_support'] == 'pending'


def test_inspection_is_chinese_searchable_report_family(tmp_path):
    layers, mesh = fixture()
    inspection = build_geometry_inspection(layers, {'block_base': mesh})
    report = build_measurement_report(run={'city':'诊断小样'}, source_features={},
        scene_character={}, scene_policy={}, generation_outcomes={'geometry_inspection':inspection})
    family = report['measurements']['outcomes']['geometry_inspection']
    assert family['status'] == 'partial'
    assert any(x['family'] == 'geometry_inspection' for x in report['indexes']['outcomes'])
    paths = write_measurement_report(tmp_path, report)
    from pathlib import Path
    html = Path(paths['html']).read_text()
    assert '几何完整性与接地检测' in html and 'geometry-checks' in html
    assert json.loads(Path(paths['json']).read_text())['generation_outcomes']['geometry_inspection'] == inspection


def test_admin_projection_keeps_compact_checks_not_full_evidence():
    from webapp.pipeline_console import _measurement_report_context, _artifact_stage, _attempt_sidecar_without_authority
    layers, mesh = fixture()
    inspection = build_geometry_inspection(layers, {'block_base':mesh})
    summary = _measurement_report_context({'generation_outcomes':{'geometry_inspection':inspection}})
    checks = summary['geometry_inspection']['checks']
    assert checks and all('evidence' not in x for x in checks)
    assert summary['geometry_inspection']['counts'] == inspection['counts']
    name = 'pipeline_measurement_report_s8.run-attempt.html'
    assert _artifact_stage(name) == 'S8'
    assert _attempt_sidecar_without_authority(name) is True
