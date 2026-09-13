#!/usr/bin/env python3
"""Offline S6 organization experiment using an explicit trusted S5 snapshot.

All alternatives reuse run_s6_building_roles and final surface materialization.
This is not a canonical full/S11 job and never changes generation defaults.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import pickle
import sys
import time
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.capture_organization_baseline import digest, restore, save_pickle


def load_checked(path):
    expected = path.with_suffix('.sha256').read_text().strip()
    if digest(path) != expected:
        raise ValueError(f'checksum mismatch: {path}')
    with path.open('rb') as f:
        return pickle.load(f)


def run_variant(root, variant):
    from aesthetic.pipeline_domain import run_s6_building_roles, thaw_layer_containers, thaw_json
    from aesthetic.organization_experiment import adapter
    out = root / variant
    out.mkdir(exist_ok=True)
    if (out / 'layers.pkl').exists():
        raise ValueError('Refusing to overwrite an existing candidate')
    start = time.monotonic()
    context = restore(load_checked(root / 's5_input.pkl'))
    print(f'[{variant}] S5 快照已校验；开始独立 S6', flush=True)
    result = run_s6_building_roles(context, merge_block_layers=True,
        apply_mass=adapter(variant, context.runtime.sources))
    save_pickle(out / 'layers.pkl', thaw_layer_containers(result.layers))
    (out / 's6_evidence.json').write_text(json.dumps({
        'variant': variant, 'elapsed_seconds': time.monotonic()-start,
        'building_mass': thaw_json(result.building_mass_evidence),
        'height_hierarchy': thaw_json(result.height_hierarchy_evidence),
        'scope': 'experimental S6 only; not a full pipeline job',
        'input_sha256': digest(root / 's5_input.pkl'),
    }, ensure_ascii=False, indent=2))
    print(f'[{variant}] 最终几何已保存，{time.monotonic()-start:.1f}s', flush=True)


def fixed_digest(layers):
    h = hashlib.sha256()
    for name in ('WL','WO','block_base_cut_lines','block_base_major_cut_lines'):
        h.update(name.encode())
        for p in getattr(layers, name): h.update(p.wkb)
    for p, z in layers.BL:
        h.update(p.wkb); h.update(float(z).hex().encode())
    return h.hexdigest()


def base_thickness_from_plan(plan):
    from _TEXTURE_STYLE_OF_DEEPSEEK.config import Z_WATER_BASE_MM
    # Undoing -2.0 + 0.4 otherwise yields 0.3999999999999999, which fails
    # the water builder's exact lower bound. Only normalize FP cancellation.
    return round(plan.terrain_base_z_mm - Z_WATER_BASE_MM, 12)


def measurements(layers, runtime):
    from shapely.ops import unary_union
    from shapely.strtree import STRtree
    from aesthetic.building_mass_strategy import _rotated_axes, _distribution
    from aesthetic.city_surface_plan import surface_heights
    polygons = list(layers.block_base) + list(layers.BO)
    scale = runtime.scale_mm_per_m
    sample = polygons[::max(1,int(np.ceil(len(polygons)/5000)))]
    axes = np.array([_rotated_axes(p) for p in sample]) * scale
    tree = STRtree(polygons)
    distances = []
    if polygons:
        for p in sample:
            _ids, ds = tree.query_nearest(p, exclusive=True, return_distance=True)
            if len(ds): distances.append(float(ds.min()) * scale)
    area = unary_union(polygons).area if polygons else 0.
    x0,y0,x1,y1 = runtime.bbox_local_m
    heights = surface_heights(layers)
    return {'polygon_count': len(polygons), 'sample_count': len(sample),
        'union_coverage_of_frame': area/((x1-x0)*(y1-y0)),
        'union_area_m2': area, 'summed_area_m2': sum(p.area for p in polygons),
        'short_axis_mm': _distribution(axes[:,0] if len(axes) else []),
        'nearest_other_component_gap_mm': _distribution(distances),
        'gap_measurement_limit': 'sampled nearest component distance, not internal corridor width or slicer survival',
        'below_colored_strip_sample_fraction': float(np.mean(axes[:,0]<runtime.printer_profile.min_colored_strip_mm)) if len(axes) else None,
        'elongated_over_4_sample_fraction': float(np.mean(axes[:,1]/np.maximum(axes[:,0],1e-9)>4)) if len(axes) else None,
        'ordinary_relief_mm': _distribution(heights), 'fixed_elements_sha256': fixed_digest(layers)}


def render_variant(root, variant):
    import trimesh
    from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import materialize_terrain_surface_plan
    from _TEXTURE_STYLE_OF_DEEPSEEK.water import prepare_deepseek_water_relief, build_deepseek_water_v3
    from _TEXTURE_STYLE_OF_DEEPSEEK.prepared_surface import materialize_flat_surfaces
    from _TEXTURE_STYLE_OF_DEEPSEEK.render_glb import _TerrainSurfacePlanSampler
    from aesthetic.city_surface_plan import materialize_road_surfaces, surface_heights, verify_surface_plan
    from tools.render_reference_actual_mesh import render
    start = time.monotonic()
    out = root / variant
    context = restore(load_checked(root / 's5_input.pkl'))
    runtime = context.runtime
    layers = load_checked(out / 'layers.pkl')
    scale = runtime.scale_mm_per_m
    verify_surface_plan(layers, scale)
    metrics = measurements(layers, runtime)
    (out / 'measurements.json').write_text(json.dumps(metrics, ensure_ascii=False, indent=2))
    common_path = root / 'common_meshes.pkl'
    if common_path.exists():
        common = load_checked(common_path)
        if common['fixed_elements_sha256'] != fixed_digest(layers):
            raise ValueError('roads/water/landmarks are not identical across variants')
    else:
        plan = runtime.terrain_surface_plan
        terrain = materialize_terrain_surface_plan(plan)
        base = base_thickness_from_plan(plan)
        relief = prepare_deepseek_water_relief(terrain, layers.WL, layers.WO, scale,
            base_thickness_mm=base, surface_thickness_mm=runtime.printer_profile.min_surface_height_mm)
        water = build_deepseek_water_v3(layers.WL, layers.WO, *runtime.bbox_local_m,
            scale=scale, flat_only=False, base_thickness_mm=base,
            surface_levels_mm=relief['surface_levels_mm'],
            surface_thickness_mm=runtime.printer_profile.min_surface_height_mm)
        sampler = _TerrainSurfacePlanSampler(plan, runtime.bbox_local_m, scale).z_mm_vec
        heroes, proof = materialize_flat_surfaces([p for p,h in layers.BL],
            [h for p,h in layers.BL], scale, sampler)
        common = {'terrain': terrain, 'water': water, 'heroes': heroes,
                  'hero_proof': proof, 'water_relief': relief,
                  'fixed_elements_sha256': fixed_digest(layers)}
        save_pickle(common_path, common)
    sampler = _TerrainSurfacePlanSampler(runtime.terrain_surface_plan, runtime.bbox_local_m, scale).z_mm_vec
    print(f'[{variant}] 严格挤出城市块面', flush=True)
    urban, urban_proof = materialize_flat_surfaces(list(layers.block_base)+list(layers.BO),
        surface_heights(layers), scale, sampler)
    print(f'[{variant}] 严格挤出共同道路走廊', flush=True)
    roads, road_proof = materialize_road_surfaces(layers, scale, sampler)
    parts = [(common['terrain'],'terrain'), (common['water'],'water'),
             (common['heroes'],'urban_relief'), (urban,'urban_relief'), (roads,'road')]
    meshes = [(f'{role}_{i}',m.vertices,m.faces,role) for i,(m,role) in enumerate(parts) if m is not None]
    scene = trimesh.Scene()
    palette = {'terrain':[167,167,167,255],'water':[17,17,17,255],
               'urban_relief':[248,248,248,255],'road':[102,102,102,255]}
    for i,(mesh,role) in enumerate(parts):
        if mesh is not None:
            mesh = mesh.copy(); mesh.visual.face_colors = palette[role]
            scene.add_geometry(mesh, node_name=f'{role}_{i}')
    (out/'experimental.glb').write_bytes(scene.export(file_type='glb'))
    views = []
    for name,elevation,crop in [('topdown',90,None),('oblique',30,None),
        ('center_detail',90,(-30,-30,30,30)),('northwest_detail',90,(-65,15,-15,65))]:
        print(f'[{variant}] 渲染 {name}', flush=True)
        views.append(render(meshes,out/f'{name}.png',elevation,pixel_size=2000,crop=crop))
    (out/'render_evidence.json').write_text(json.dumps({
        'variant':variant,'geometry':'actual verified extruded surfaces, not diagnostic layer colouring',
        'urban_materialization':urban_proof,'road_materialization':road_proof,
        'hero_materialization':common['hero_proof'],'views':views,
        'glb_sha256':digest(out/'experimental.glb'), 'elapsed_seconds':time.monotonic()-start,
        'formal_3mf_generated':False,'slicer_passed':False,
        'limitations':['shared centroid Z sampling; no terrain-contact or slicer acceptance',
                       'GLB is an experimental geometry carrier, not final product validation']},ensure_ascii=False,indent=2))
    print(f'[{variant}] 实际几何视图已完成',flush=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--experiment-dir',type=Path,required=True)
    p.add_argument('--variant',choices=['A','B','C'],required=True)
    p.add_argument('--render-only',action='store_true')
    a=p.parse_args()
    if a.render_only:
        render_variant(a.experiment_dir,a.variant)
    elif a.variant!='A':
        run_variant(a.experiment_dir,a.variant)
    else:
        raise ValueError('A must be recorded from the canonical run')


if __name__=='__main__': main()
