import numpy as np
import pytest
import trimesh
from shapely.geometry import box
from shapely.geometry import LineString
from shapely.ops import unary_union

from _TEXTURE_STYLE_OF_DEEPSEEK._layer_preprocess import LayerPolygons
from _TEXTURE_STYLE_OF_DEEPSEEK.block_base import build_deepseek_block_base_v3
from _TEXTURE_STYLE_OF_DEEPSEEK.print_profile import DEFAULT_PRINTER_PROFILE as PROFILE
from aesthetic.city_surface_plan import finalize_city_surfaces, surface_heights


def test_prepared_textured_route_cannot_silently_drop_small_approved_polygon():
    layers = LayerPolygons(
        block_base=[box(-20, -10, 0, 10), box(10, 0, 12, 2)],
        block_base_classes=['unclassified', 'unclassified'])
    evidence = finalize_city_surfaces(layers, bbox_local=(-30, -30, 30, 30),
                                      scale=1., printer_profile=PROFILE)
    terrain = trimesh.creation.box(extents=[80, 80, 2])
    mesh, proof = build_deepseek_block_base_v3(
        layers.block_base, terrain, 1., brick_style=False,
        block_classes=layers.block_base_classes,
        prepared_surface_evidence=evidence,
        polygon_thicknesses_mm=surface_heights(layers), return_clearance_evidence=True)
    assert len(mesh.split(only_watertight=True)) == 2
    assert proof['materialization']['verified_polygons'] == 2
    assert proof['materialization']['lost_polygons'] == 0


def test_actual_mesh_check_rejects_wrong_footprint_and_height():
    from _TEXTURE_STYLE_OF_DEEPSEEK.prepared_surface import verify_flat_polygon
    mesh = trimesh.creation.box(extents=[2, 2, .5])
    mesh.apply_translation([0, 0, .25])
    verify_flat_polygon(mesh, box(-1, -1, 1, 1), 1., 0., .5)
    with pytest.raises(ValueError, match='footprint'):
        verify_flat_polygon(mesh, box(-2, -1, 2, 1), 1., 0., .5)
    with pytest.raises(ValueError, match='height'):
        verify_flat_polygon(mesh, box(-1, -1, 1, 1), 1., 0., 1.)


def test_shared_extrusion_fails_on_missing_height_sample():
    from _TEXTURE_STYLE_OF_DEEPSEEK.prepared_surface import materialize_flat_surfaces
    with pytest.raises(ValueError, match='terrain'):
        materialize_flat_surfaces([box(0, 0, 2, 2)], [.5], 1.,
                                  lambda x, y: np.full(len(x), np.nan))


def test_shared_extrusion_preserves_small_footprints_far_from_origin():
    from _TEXTURE_STYLE_OF_DEEPSEEK.prepared_surface import materialize_flat_surfaces
    # Paris-scale local metres: the old float32 bridge produced ~1.9e-6 mm²
    # of symmetric-difference error. Do not weaken the footprint tolerance.
    polygon = box(10000.123, 10000.456, 10040.987, 10015.321)
    mesh, proof = materialize_flat_surfaces(
        [polygon], [.48], .007762010307696369,
        lambda x, y: np.zeros(len(x)))
    assert mesh.is_watertight
    assert proof['verified_polygons'] == 1
    assert proof['max_footprint_error_mm2'] < 1e-9


def test_approved_road_plan_includes_existing_gaps_not_suppressed_lines():
    from aesthetic.city_surface_plan import verify_surface_plan
    layers = LayerPolygons(BO=[box(-20, -20, 20, -1), box(-20, 1, 20, 20)],
        block_base_cut_lines=[LineString([(-20, 0), (20, 0)])],
        roads_lines=[(LineString([(0, -20), (0, 20)]), 'primary', False)])
    evidence = finalize_city_surfaces(layers, bbox_local=(-25, -25, 25, 25),
                                      scale=1., printer_profile=PROFILE)
    assert not layers.surface_road_reveals  # old streets existed before final cut
    roads = unary_union(layers.surface_road_polygons)
    assert roads.bounds[2] - roads.bounds[0] == pytest.approx(40.)
    assert roads.area == pytest.approx(40 * PROFILE.surface_road_gap_mm)
    assert evidence['road_surface_plan']['polygon_count'] == 1
    layers.surface_road_polygons = [box(-20, -20, 20, 20)]
    with pytest.raises(ValueError, match='road surface'):
        verify_surface_plan(layers, 1.)


def test_only_explicit_approved_bridges_can_cross_water():
    def make(bridge):
        from aesthetic.bridge_sources import extract_bridge_sources
        physical, _ = extract_bridge_sources([dict(
            geometry=LineString([(-3, 0), (3, 0)]), highway='primary',
            bridge='yes' if bridge else 'no', name='Pont Neuf')])
        layers = LayerPolygons(WL=[box(-2, -10, 2, 10)],
            block_base_cut_lines=[LineString([(-20, 0), (20, 0)])],
            bridge_lines=physical, roads_lines=[])
        finalize_city_surfaces(layers, bbox_local=(-25, -25, 25, 25),
                              scale=1., printer_profile=PROFILE)
        return unary_union(layers.surface_road_polygons)
    assert make(False).intersection(box(-2, -10, 2, 10)).area == 0
    assert make(True).intersection(box(-2, -10, 2, 10)).area > 0


@pytest.mark.parametrize('road_style', ['printer-default', 'negative-space-v1'])
@pytest.mark.parametrize('grounded', [False, True])
def test_fast_glb_and_formal_consumers_materialize_identical_city_surfaces(tmp_path, road_style, grounded):
    from _TEXTURE_STYLE_OF_DEEPSEEK.render_glb import render_glb_preview, _TerrainSurfacePlanSampler
    from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import resolve_terrain_surface_plan
    from aesthetic.city_surface_plan import materialize_road_surfaces
    from aesthetic.city_surface_plan import materialize_city_role, verify_materialized_city
    layers = LayerPolygons(BO=[box(-20, -20, 20, 20)], BO_heights=[.84],
        BL=[(box(22, 2, 24, 4), 1.2)],
        block_base_cut_lines=[LineString([(-25, 0), (25, 0)])])
    plan = resolve_terrain_surface_plan(np.full((9, 9), 10.), 60., 60., 1., max_surface_edge_mm=5.)
    if grounded:
        from dataclasses import replace
        ny, nx = plan.surface_z_grid_mm.shape
        yy, xx = np.mgrid[-1:1:complex(ny), -1:1:complex(nx)]
        plan = replace(plan, surface_z_grid_mm=1. + .2 * xx + .1 * yy * yy)
    evidence = finalize_city_surfaces(layers, bbox_local=(-30, -30, 30, 30),
                                      scale=1., printer_profile=PROFILE, road_style=road_style,
                                      source_roads=[], terrain_surface_plan=plan if grounded else None)
    sampler = _TerrainSurfacePlanSampler(plan, (-30, -30, 30, 30), 1.)
    formal, proof = build_deepseek_block_base_v3(
        layers.BO, None, 1., brick_style=False, prepared_surface_evidence=evidence,
        polygon_thicknesses_mm=surface_heights(layers),
        prepared_sample_z_m=sampler.z_mm_vec, return_clearance_evidence=True,
        prepared_grounding_plan=layers.surface_grounding.get('city'))
    roads, _ = materialize_road_surfaces(layers, 1., sampler.z_mm_vec)
    landmarks, _ = materialize_city_role(layers, 'landmarks', 1., sampler.z_mm_vec)
    verify_materialized_city(layers, {'block_base': formal, 'roads': roads, 'landmarks': landmarks}, proof, 1.)
    path = tmp_path / 'prepared.glb'
    render_glb_preview(layers, {'bbox_local': (-30, -30, 30, 30), 'scale': 1.},
        str(path), terrain_surface_plan=plan, preview_quality='fast')
    result = trimesh.load(path, force='scene', process=False)
    for role, expected in [('block_base', formal), ('roads', roads), ('landmarks', landmarks)]:
        if expected is None:
            assert role not in result.geometry
            continue
        actual = result.geometry[role]
        np.testing.assert_allclose(actual.vertices, expected.vertices, atol=1e-5)
        np.testing.assert_array_equal(actual.faces, expected.faces)
    # Round-trip the actual production 3MF exporter, not a second mock mesh.
    import zipfile
    import xml.etree.ElementTree as ET
    from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf
    path3mf = tmp_path / 'prepared.3mf'
    export_deepseek_3mf({'block_base': formal, 'roads': roads, 'landmarks': landmarks}, str(path3mf))
    with zipfile.ZipFile(path3mf) as archive:
        root = ET.fromstring(archive.read('3D/Objects/object_1.model'))
    ns = {'m': 'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'}
    for oid, expected in [('7', formal), ('3', roads), ('6', landmarks)]:
        node = root.find(f"m:resources/m:object[@id='{oid}']/m:mesh", ns)
        if expected is None:
            assert node is None
            continue
        vertices = [[float(v.attrib[k]) for k in ('x', 'y', 'z')]
                    for v in node.findall('m:vertices/m:vertex', ns)]
        faces = [[int(v.attrib[k]) for k in ('v1', 'v2', 'v3')]
                 for v in node.findall('m:triangles/m:triangle', ns)]
        np.testing.assert_allclose(vertices, expected.vertices, atol=1e-5)
        np.testing.assert_array_equal(faces, expected.faces)


def test_stage_boundary_rejects_partial_or_mutated_actual_mesh():
    from aesthetic.city_surface_plan import verify_materialized_city
    layers = LayerPolygons(BO=[box(-10, -10, 0, 0), box(2, 2, 4, 4)], BO_heights=[.5, .5])
    plan = finalize_city_surfaces(layers, bbox_local=(-20, -20, 20, 20), scale=1., printer_profile=PROFILE)
    mesh, proof = build_deepseek_block_base_v3(layers.BO, None, 1., brick_style=False,
        prepared_surface_evidence=plan, polygon_thicknesses_mm=surface_heights(layers),
        prepared_sample_z_m=lambda x, y: np.zeros(len(x)), return_clearance_evidence=True)
    verify_materialized_city(layers, {'block_base': mesh}, proof, 1.)
    partial = mesh.split()[0]
    with pytest.raises(ValueError, match='actual mesh'):
        verify_materialized_city(layers, {'block_base': partial}, proof, 1.)
    mesh.vertices[0, 2] += .1
    with pytest.raises(ValueError, match='actual mesh'):
        verify_materialized_city(layers, {'block_base': mesh}, proof, 1.)
