"""Real Paris local B+C materialization regression; not full print acceptance."""
import argparse
import json
import pickle
from pathlib import Path
import sys
import time
import numpy as np
import trimesh
from shapely.geometry import box
from shapely.affinity import scale as scale_geometry

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from aesthetic.z_texture import (ZTexturePolicy,plan_ground_texture,materialize_textured_terrain,materialize_ground_texture,
    choose_roof,_polygons)
from aesthetic.surface_grounding import _patch_mesh,resolve_grounding,grounding_digest,materialize_grounding
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import sample_terrain_surface_plan_z
from tools.render_reference_actual_mesh import render
from tools.experiment_z_texture import digest


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False);start=time.monotonic()
    layer_path=Path('output/paris_C_surface_replay_20260908_v2/resolved_surfaces.pkl')
    input_path=Path('output/paris_C_formal_grounding_20260908_v2/surface_inputs.pkl')
    with layer_path.open('rb') as f:layers=pickle.load(f)
    with input_path.open('rb') as f:_,kw=pickle.load(f)
    source_report=json.loads(Path('output/paris_z_texture_R3_20260908_v4/report.json').read_text())
    bbox=source_report['detail_bbox_local_m'];core=box(*bbox)
    t=kw['terrain_surface_plan'];scale=kw['scale'];policy=ZTexturePolicy()
    print('Paris 约 1 km 实际网格回归，保留整城比例',flush=True)
    model=scale_geometry(core,xfact=scale,yfact=scale,origin=(0,0))
    xy,f,boundary=_patch_mesh(model,t)
    z=sample_terrain_surface_plan_z(t,xy[:,0],xy[:,1])
    base_plan=dict(version='diagnostic-cropped-terrain',terrain_fingerprint=t.fingerprint,
        input_geometry_fingerprint=digest(input_path),patches=[dict(mode='local_ground_microrelief',
            xy=xy,faces=f,boundary=boundary,bottom_z=np.full(len(xy),t.terrain_base_z_mm),top_z=z)])
    base_plan['fingerprint']=grounding_digest(base_plan)
    base=materialize_ground_texture(base_plan)
    texture=plan_ground_texture(layers,bbox,scale,t,policy.payload())
    layers.surface_grounding['ground_texture']=texture
    terrain=materialize_textured_terrain(base,layers,t)
    print('局部真实纹理：',texture['evidence']['surface_triangles'],'面',flush=True)
    polys=[];heights=[];roofs=[];before_roofs=[];modes=[]
    original=list(layers.block_base)+list(layers.BO)
    for poly,patch in zip(original,layers.surface_grounding['city']['patches']):
        if not poly.intersects(core):continue
        for part in _polygons(poly.intersection(core)):
            h=float(np.median(patch['top_z']-patch['bottom_z']))
            if patch['mode']!='draped_thickness':h=float(np.min(patch['top_z']-patch['bottom_z']))
            roof,_=choose_roof(patch['bottom_z'],patch['top_z'],patch['mode'],policy.max_block_relief_mm)
            if patch['mode']!='draped_thickness':roof=float(np.max(patch['top_z']))
            polys.append(part);heights.append(h);roofs.append(roof);modes.append(patch['mode'])
            before_roofs.append(float(np.max(patch['top_z'])) if patch['mode']!='draped_thickness' else None)
    g=resolve_grounding(polys,heights,scale,t,modes)
    import copy
    old=copy.deepcopy(g)
    for target,values in [(g,roofs),(old,before_roofs)]:
        for patch,roof in zip(target['patches'],values):
            if roof is not None:
                patch['top_z']=np.full(len(patch['xy']),roof);patch['mode']='flat_roof_above_highest_support'
        target['fingerprint']=grounding_digest(target)
    city,proof=materialize_grounding(g,polys,heights,scale)
    before,_=materialize_grounding(old,polys,heights,scale)
    meshes=[('terrain',terrain.vertices,terrain.faces,'terrain'),('city',city.vertices,city.faces,'urban_relief')]
    frame=(bbox[2]-bbox[0])*scale+1.
    render(meshes,a.output/'BC_actual_oblique.png',30,pixel_size=1400,frame_width_mm=frame)
    render([('terrain',base.vertices,base.faces,'terrain'),('city',before.vertices,before.faces,'urban_relief')],
           a.output/'A_actual_oblique.png',30,pixel_size=1400,frame_width_mm=frame)
    from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf
    export_deepseek_3mf({'terrain':terrain,'block_base':city},str(a.output/'diagnostic_local_BC.3mf'))
    report=dict(purpose='diagnostic',verdict='human_review',city='Paris',bbox_local_m=bbox,
        scale_mm_per_m=scale,elapsed_seconds=time.monotonic()-start,
        source_sha256={str(layer_path):digest(layer_path),str(input_path):digest(input_path)},
        terrain_texture=texture['evidence'],city_materialization=proof,
        terrain_watertight=bool(terrain.is_watertight),city_watertight=bool(city.is_watertight),
        volume_added_mm3=float(terrain.volume-base.volume),
        limitations=['local diagnostic only; excludes landmarks and water solids; not a full city model',
                     'texture is excluded from their source masks even though they are not drawn',
                     'no final water clipping, strict validator or actual slicing acceptance'])
    (a.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print('完成',report['elapsed_seconds'],'秒',flush=True)


if __name__=='__main__':main()
