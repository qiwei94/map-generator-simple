"""Rebuild all Paris source bridge corridors on trusted local S6 snapshots.

Subsystem replay only, not a formal city generation or slicing acceptance.
"""
import argparse,copy,hashlib,json,pickle,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from shapely.geometry import box
from shapely.ops import unary_union
from aesthetic.city_surface_plan import prepare_negative_roads,_resolve_road_surfaces
from aesthetic.road_width_contract import resolve_road_width_contract
from aesthetic.road_grounding import resolve_road_grounding,materialize_road_grounding
from _TEXTURE_STYLE_OF_DEEPSEEK.render_glb import _TerrainSurfacePlanSampler
from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf

def main():
 p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
 t=time.monotonic()
 original=Path('output/paris_C_formal_grounding_20260908_v2/surface_inputs.pkl')
 with original.open('rb') as f: layers,kwargs=pickle.load(f)
 # Isolate road system. Full S6 will carve these corridors from city masses.
 layers=copy.copy(layers);layers.BL=[];layers.BO=[];layers.block_base=[]
 terrain=kwargs['terrain_surface_plan'];scale=kwargs['scale'];bbox=kwargs['bbox_local']
 profile,contract=resolve_road_width_contract(kwargs['printer_profile'],'negative-space-fine-v1')
 print('loaded',time.monotonic()-t,flush=True)
 recovery=prepare_negative_roads(layers,kwargs['source_roads'],scale=scale,bridge_gap_mm=profile.final_block_base_gap_mm)
 print('approaches',recovery['bridge_sources']['approach_recovery']['recovered_parts'],time.monotonic()-t,flush=True)
 road=_resolve_road_surfaces(layers,box(*bbox),scale,profile,negative_space=True,split_grounding=True)
 layers.surface_plan_evidence=dict(road_surface_plan=road,road_width_contract=contract,scale_mm_per_m=scale)
 ground=resolve_road_grounding(layers,scale,terrain);layers.surface_grounding={'roads':ground}
 report=dict(purpose='diagnostic',source_sha256=hashlib.sha256(original.read_bytes()).hexdigest(),recovery=recovery,grounding=ground['evidence'],seconds=time.monotonic()-t)
 with (a.output/'bridge_layers.pkl').open('wb') as f:pickle.dump((layers,terrain),f)
 (a.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
 print('status',ground['evidence']['status'],'bridges',ground['evidence']['bridge_polygon_count'],flush=True)
 from collections import Counter
 print(Counter(e.get('reason_zh',e['status']) for e in ground['support_evidence']),flush=True)
 if ground['evidence']['status']=='ready':
  sampler=_TerrainSurfacePlanSampler(terrain,bbox,scale)
  mesh,proof=materialize_road_grounding(ground,layers,scale,sampler.z_mm_vec)
  export_deepseek_3mf({'roads':mesh},str(a.output/'bridges_diagnostic.3mf'))
  report.update(materialization=proof,seconds=time.monotonic()-t,watertight=bool(mesh.is_watertight))
  (a.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
 print('done',time.monotonic()-t,flush=True)
if __name__=='__main__':main()
