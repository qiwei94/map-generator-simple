from dataclasses import replace
import numpy as np
import pytest
from shapely.geometry import LineString, box
from shapely.ops import unary_union
from _TEXTURE_STYLE_OF_DEEPSEEK._layer_preprocess import LayerPolygons
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import resolve_terrain_surface_plan, sample_terrain_surface_plan_z
from _TEXTURE_STYLE_OF_DEEPSEEK.render_glb import _TerrainSurfacePlanSampler
from _TEXTURE_STYLE_OF_DEEPSEEK.print_profile import DEFAULT_PRINTER_PROFILE as PROFILE
from aesthetic.city_surface_plan import finalize_city_surfaces, materialize_road_surfaces


def scene(bridge=None, water=True, style='printer-default', cross_slope=0.):
    plan = resolve_terrain_surface_plan(np.zeros((3,3)), 4.,4.,1., max_surface_edge_mm=.4)
    ny,nx = plan.surface_z_grid_mm.shape
    yy,xx = np.mgrid[-2:2:complex(ny), -2:2:complex(nx)]
    z = 1 + .08*xx + cross_slope*yy - .8*np.maximum(0, 1-np.abs(xx)/.6)
    plan = replace(plan, surface_z_grid_mm=z)
    layers = LayerPolygons(WL=[box(-.5,-2,.5,2)] if water else [],
        block_base_cut_lines=[LineString([(-2,0),(2,0)])], bridge_lines=[bridge] if bridge else [])
    rows = [dict(geometry=bridge, bridge='yes', highway='primary')] if bridge else []
    finalize_city_surfaces(layers, bbox_local=(-2,-2,2,2), scale=1., printer_profile=PROFILE,
                          terrain_surface_plan=plan, road_style=style, source_roads=rows)
    sampler = _TerrainSurfacePlanSampler(plan, (-2,-2,2,2), 1.)
    return layers, plan, sampler


def test_ordinary_road_conforms_and_preserves_half_thickness_embedding():
    layers, terrain, sampler = scene(water=False)
    mesh, proof = materialize_road_surfaces(layers, 1., sampler.z_mm_vec)
    assert mesh.is_watertight
    for patch in layers.surface_grounding['roads']['patches']:
        xy = patch['xy'][patch['faces']].mean(axis=1)
        expected = sample_terrain_surface_plan_z(terrain, xy[:,0], xy[:,1])
        np.testing.assert_allclose(patch['bottom_z'][patch['faces']].mean(axis=1), expected-.12, atol=1e-9)
        np.testing.assert_allclose(patch['top_z'] - patch['bottom_z'], .24)
    assert proof['bridge_bank_connection'] == 'not_applicable'


@pytest.mark.parametrize('style', ['printer-default', 'negative-space-v1'])
def test_bridge_follows_bank_plane_not_riverbed_and_keeps_xy(style):
    bridge = LineString([(-1.5,0),(1.5,0)])
    layers, terrain, sampler = scene(bridge, style=style)
    ground = layers.surface_grounding['roads']
    assert ground['evidence']['status'] == 'ready'
    mesh, proof = materialize_road_surfaces(layers, 1., sampler.z_mm_vec)
    assert mesh.is_watertight and mesh.is_winding_consistent
    assert proof['bridge_bank_connection'] == 'verified_source_bank_interfaces'
    for index in layers.surface_road_bridge_indices:
        patch = ground['patches'][index]
        np.testing.assert_allclose(patch['bottom_z'], .88+.08*patch['xy'][:,0], atol=1e-8)
        assert patch['bottom_z'].min() > .7
    support = ground['support_evidence'][0]
    assert support['cap_deviation_max_mm'] < 1e-8
    assert support['min_bank_embedding_mm'] == pytest.approx(.12)
    assert support['final_boolean_contact'] == support['slicer_bridge_span'] == 'pending'
    assert unary_union(layers.surface_road_polygons).intersection(box(-.5,-2,.5,2)).area > 0


@pytest.mark.parametrize('bridge,reason', [
    (LineString([(-.2,0),(1.5,0)]), '两岸'),
    (LineString([(-1.5,0),(0,.2),(1.5,0)]), '弯桥')])
def test_unsupported_bridges_are_blocked_not_deleted_or_draped(bridge, reason):
    layers, _, sampler = scene(bridge)
    ground = layers.surface_grounding['roads']
    assert ground['evidence']['status'] == 'blocked'
    assert reason in ground['support_evidence'][0]['reason_zh']
    assert layers.surface_road_bridge_indices  # footprint has not disappeared
    with pytest.raises(ValueError, match='bridge support unresolved'):
        materialize_road_surfaces(layers, 1., sampler.z_mm_vec)


def test_no_source_bridge_never_invents_water_crossing():
    layers, _, sampler = scene()
    assert not layers.surface_road_bridge_indices
    assert unary_union(layers.surface_road_polygons).intersection(box(-.5,-2,.5,2)).area == 0
    materialize_road_surfaces(layers, 1., sampler.z_mm_vec)


def test_bank_cross_slope_is_bounded_not_hidden_by_raising_bridge():
    layers, _, sampler = scene(LineString([(-1.5,0),(1.5,0)]), cross_slope=.8)
    support = layers.surface_grounding['roads']['support_evidence'][0]
    assert support['status'] == 'blocked' and '横坡' in support['reason_zh']
    with pytest.raises(ValueError, match='bridge support unresolved'):
        materialize_road_surfaces(layers, 1., sampler.z_mm_vec)


def test_bridge_report_distinguishes_plan_from_actual_and_final_contact():
    from aesthetic.geometry_inspection import build_geometry_inspection
    layers, _, sampler = scene(LineString([(-1.5,0),(1.5,0)]))
    states = lambda r: {c['id']:c['status'] for c in r['checks']}
    assert states(build_geometry_inspection(layers))['bridge_support'] == 'pending'
    mesh, _ = materialize_road_surfaces(layers, 1., sampler.z_mm_vec)
    report = states(build_geometry_inspection(layers, {'roads':mesh}))
    assert report['road_contact'] == report['bridge_support'] == 'passed'
    assert report['final_contact'] == report['slice_survival'] == 'pending'
    layers, _, _ = scene(LineString([(-.2,0),(1.5,0)]))
    report = states(build_geometry_inspection(layers))
    assert report['road_plan'] == report['bridge_support'] == 'failed'


def test_actual_glb_and_3mf_use_the_same_bank_deck(tmp_path):
    import trimesh, zipfile
    import xml.etree.ElementTree as ET
    from _TEXTURE_STYLE_OF_DEEPSEEK.render_glb import render_glb_preview
    from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf
    layers, terrain, sampler = scene(LineString([(-1.5,0),(1.5,0)]))
    mesh, _ = materialize_road_surfaces(layers, 1., sampler.z_mm_vec)
    path = tmp_path/'bridge.glb'
    render_glb_preview(layers, {'bbox_local':(-2,-2,2,2),'scale':1.}, str(path),
                       terrain_surface_plan=terrain, preview_quality='fast')
    actual = trimesh.load(path,force='scene',process=False).geometry['roads']
    np.testing.assert_allclose(actual.vertices,mesh.vertices,atol=1e-6)
    np.testing.assert_array_equal(actual.faces,mesh.faces)
    path = tmp_path/'bridge.3mf'
    export_deepseek_3mf({'roads':mesh},str(path))
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read('3D/Objects/object_1.model'))
    ns={'m':'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'}
    obj=root.find("m:resources/m:object[@id='3']/m:mesh",ns)
    vertices=[[float(v.attrib[k]) for k in ('x','y','z')] for v in obj.findall('m:vertices/m:vertex',ns)]
    faces=[[int(v.attrib[k]) for k in ('v1','v2','v3')] for v in obj.findall('m:triangles/m:triangle',ns)]
    np.testing.assert_allclose(vertices,mesh.vertices,atol=1e-6)
    np.testing.assert_array_equal(faces,mesh.faces)
