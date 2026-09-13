import copy

import numpy as np
import pytest
import trimesh
from shapely.geometry import LineString, box
from shapely.ops import unary_union

from _TEXTURE_STYLE_OF_DEEPSEEK._layer_preprocess import LayerPolygons
from _TEXTURE_STYLE_OF_DEEPSEEK.print_profile import DEFAULT_PRINTER_PROFILE as PROFILE
from _TEXTURE_STYLE_OF_DEEPSEEK.block_base import build_deepseek_block_base_v3
from aesthetic.city_surface_plan import (
    finalize_city_surfaces, surface_heights, verify_surface_plan,
)


def _fixture():
    return LayerPolygons(
        BO=[box(-20, -20, 0, 20), box(0, -20, 20, 20)],
        BO_heights=[0.24, 0.84],
        block_base_cut_lines=[LineString([(-30, 0), (30, 0)])],
        block_base_major_cut_lines=[LineString([(0, -30), (0, 30)])],
    )


def _finalize(layers):
    return finalize_city_surfaces(
        layers, bbox_local=(-25, -25, 25, 25), scale=1.0,
        printer_profile=PROFILE)


def test_single_cut_plan_preserves_heights_and_exact_two_tier_gaps():
    layers = _fixture()
    evidence = _finalize(layers)
    assert evidence['passed']
    assert evidence['before']['polygon_count'] == 2
    assert evidence['after']['polygon_count'] == 4
    assert layers.BO_heights == [0.24, 0.24, 0.84, 0.84]
    left = unary_union([p for p in layers.BO if p.centroid.x < 0])
    right = unary_union([p for p in layers.BO if p.centroid.x > 0])
    assert left.distance(right) == pytest.approx(PROFILE.final_block_base_gap_mm)
    bottom = unary_union([p for p in layers.BO if p.centroid.y < 0])
    top = unary_union([p for p in layers.BO if p.centroid.y > 0])
    assert bottom.distance(top) == pytest.approx(PROFILE.surface_road_gap_mm)
    assert evidence['area_retention'] > 0.95
    assert _finalize(layers) == evidence  # no second retreat


@pytest.mark.parametrize('change', ['geometry', 'height', 'scale'])
def test_prepared_surface_rejects_stale_geometry_or_scale(change):
    layers = _fixture()
    _finalize(layers)
    if change == 'geometry':
        layers.BO[0] = layers.BO[0].buffer(-0.1)
    elif change == 'height':
        layers.BO_heights[0] += 0.12
    with pytest.raises(ValueError, match='changed after S6'):
        verify_surface_plan(layers, 0.5 if change == 'scale' else 1.0)


def test_s8_extrudes_the_same_footprints_and_keeps_height_roles():
    layers = _fixture()
    evidence = _finalize(layers)
    terrain = trimesh.creation.box(extents=[60, 60, 2])
    mesh, proof = build_deepseek_block_base_v3(
        layers.BO, terrain, scale=1.0, brick_style=False,
        prepared_surface_evidence=evidence,
        polygon_thicknesses_mm=surface_heights(layers),
        return_clearance_evidence=True)
    assert proof['geometry_fingerprint'] == evidence['geometry_fingerprint']
    assert proof['status'] == 'checked'
    assert mesh.is_watertight
    parts = mesh.split(only_watertight=True)
    assert sorted(round(p.extents[2], 5) for p in parts) == [0.24, 0.24, 0.84, 0.84]
    from shapely.geometry import Polygon
    tops = mesh.triangles[mesh.face_normals[:, 2] > 0.9, :, :2]
    footprint = unary_union([Polygon(t) for t in tops])
    assert footprint.symmetric_difference(unary_union(layers.BO)).area < 1e-3
    from aesthetic.pipeline_gates import validate_semantic_mesh_bundle
    gate = validate_semantic_mesh_bundle(
        {'block_base': mesh}, required_roles=['block_base'],
        block_base_clearance=proof, require_block_base_clearance=True)
    assert gate['passed'], gate
    with pytest.raises(ValueError, match='may not deform'):
        build_deepseek_block_base_v3(
            layers.BO, terrain, 1.0, brick_style=True,
            prepared_surface_evidence=evidence,
            polygon_thicknesses_mm=surface_heights(layers))


def test_preview_does_not_repaint_suppressed_roads_on_merged_blocks(tmp_path):
    from aesthetic.review_render import render_review_bundle
    layers = LayerPolygons(BO=[box(-20, -20, 20, 20)], BO_heights=[0.84])
    # Raw visible line survives as evidence, but is NOT an eligible cutter.
    layers.roads_lines = [(LineString([(-20, 0), (20, 0)]), 1, False)]
    _finalize(layers)
    bundle = render_review_bundle(
        layers, {'bbox_local': (-25, -25, 25, 25), 'scale': 1.0},
        1.0, str(tmp_path), 'shared-surface')
    assert bundle['road_mask'].max() == 0
    assert bundle['building_mask'][512, 512] == 1


def test_plan_clips_reveals_to_actual_city_support():
    layers = _fixture()
    _finalize(layers)
    reveals = unary_union(layers.surface_road_reveals)
    assert reveals.difference(box(-20, -20, 20, 20)).area == 0
    assert reveals.intersection(unary_union(layers.BO)).area < 1e-6


def test_snap_transform_moves_all_spatial_collections_and_preserves_source():
    from generate_city_legacy import _transform_layers_to_exact
    layers = _fixture()
    layers.city_blocks = [box(-20, -20, 20, 20)]
    source = copy.deepcopy(layers)
    transformed = _transform_layers_to_exact(layers, 100, 100, (75, 75, 125, 125))
    assert transformed.city_blocks[0].bounds == (80, 80, 120, 120)
    assert transformed.block_base_cut_lines[0].bounds == (75, 100, 125, 100)
    assert transformed.block_base_major_cut_lines[0].bounds == (100, 75, 100, 125)
    assert source.city_blocks[0].bounds == (-20, -20, 20, 20)


def test_expanded_cache_scale_is_rejected_before_measurement():
    from generate_city_legacy import _require_exact_layer_scale
    layers = LayerPolygons(nozzle_real_m=PROFILE.nozzle_diameter_mm / 0.00592186)
    with pytest.raises(ValueError, match='requested frame'):
        _require_exact_layer_scale(layers, 0.00776201, PROFILE)
    layers.nozzle_real_m = PROFILE.nozzle_diameter_mm / 0.00776201
    _require_exact_layer_scale(layers, 0.00776201, PROFILE)


def test_v14_accepts_shared_local_only_proof_without_weakening_major_gap(tmp_path):
    import hashlib
    import json
    from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf
    from _TEXTURE_STYLE_OF_DEEPSEEK.validator import validate_3mf
    layers = _fixture()
    layers.block_base_major_cut_lines = []
    plan = _finalize(layers)
    terrain = trimesh.creation.box(extents=[196, 176, 4])
    mesh, proof = build_deepseek_block_base_v3(
        layers.BO, terrain, 1.0, brick_style=False,
        polygon_thicknesses_mm=surface_heights(layers),
        prepared_surface_evidence=plan, return_clearance_evidence=True)
    artifact = tmp_path / 'shared.3mf'
    export_deepseek_3mf({'terrain': terrain, 'block_base': mesh}, str(artifact))
    proof.update(configured_min_gap_mm=0.55, extrusion_width_mm=0.42)
    spec = {
        'artifact': {'filename': artifact.name,
                     'sha256': hashlib.sha256(artifact.read_bytes()).hexdigest()},
        'block_base': {'resolved_mode': 'textured', 'final_clearance': proof},
    }
    def check():
        (tmp_path / 'design_spec.json').write_text(json.dumps(spec))
        result = validate_3mf(str(artifact))
        return next(r for r in result['rules'] if r['id'] == 'V14')['passed']
    assert check()
    # A claimed major road with only a local-road gap must still fail.
    proof['major_roads'] = {**proof['surface_roads'], 'status': 'checked'}
    assert not check()
