"""Local E road layout: clean footprints, frozen DEM, production flat-roof rule.

Diagnostic actual meshes, not a full-city rebuild or print acceptance.
"""
import argparse,hashlib,json,pickle,sys,time
from pathlib import Path
import numpy as np
import trimesh
from PIL import Image,ImageDraw,ImageFont
from shapely import from_wkb,set_precision
from shapely.geometry import box
from shapely.ops import unary_union
from shapely.affinity import scale as scale_geometry
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.experiment_negative_road_width import nearby,cut_carrier,draw_negative
from _TEXTURE_STYLE_OF_DEEPSEEK.block_base import _polygon_parts
from _TEXTURE_STYLE_OF_DEEPSEEK.config import BLOCK_BASE_THICKNESS_MM
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import sample_terrain_surface_plan_z
from aesthetic.surface_grounding import _patch_mesh,resolve_grounding,grounding_digest,materialize_grounding
from aesthetic.z_texture import materialize_ground_texture
from tools.render_reference_actual_mesh import render
from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf

def main():
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--soft-edges',action='store_true');parser.add_argument('--strong-edges',action='store_true');parser.add_argument('--carved-style',action='store_true');parser.add_argument('--rounded-style',action='store_true');parser.add_argument('--pronounced-edges',action='store_true');args=parser.parse_args()
 if args.pronounced_edges: args.rounded_style=True
 if args.rounded_style: args.carved_style=True
 if args.strong_edges: args.soft_edges=True
 start=time.monotonic();folder='output/paris_E_soft_edges_v1' if args.soft_edges else 'output/paris_E_brick_relief_v1'
 if args.strong_edges: folder='output/paris_E_soft_edges_v2'
 if args.carved_style: folder='output/paris_E_carved_style_v1'
 if args.rounded_style: folder='output/paris_E_rounded_style_v1'
 if args.pronounced_edges: folder='output/paris_E_rounded_wave_v1'
 out=ROOT/folder;out.mkdir(exist_ok=True)
 src=ROOT/'output/paris_readability_local_v4'
 layers,kw=pickle.load((ROOT/'output/paris_BC_full_20260914_v9_inputs/surface_inputs.pkl').open('rb'))
 t=kw['terrain_surface_plan'];s=kw['scale'];core=box(-3000,-1800,1500,2700)
 def union(items):return set_precision(unary_union(nearby(list(items),core)).intersection(core),.002)
 water=union(list(layers.WL)+list(layers.WO));parks=union(list(layers.VL)+list(layers.VO));heroes=union([p for p,h in layers.BL])
 old=set_precision(from_wkb((src/'E.wkb').read_bytes()),.002)
 local=list(from_wkb((src/'E_local_roads.wkb').read_bytes()).geoms)
 major=list(from_wkb((src/'E_major_roads.wkb').read_bytes()).geoms)
 # Close only residual fine seams; keep parks, water and original landmark masks.
 # Recut frozen E corridors after every geometry regularization.
 rough=set_precision(old.buffer(.06/s,join_style=2).buffer(-.06/s,join_style=2),.002)
 delta=unary_union(list(_polygon_parts(rough.difference(old,grid_size=.01))))
 protected=unary_union([p for g in [water,parks,heroes] for p in _polygon_parts(g)])
 added=delta.difference(protected,grid_size=.01)
 clean=unary_union([old,added]).buffer(-.012/s,join_style=2).buffer(.012/s,join_style=2)
 clean=clean.simplify(.01/s,preserve_topology=True)
 clean=clean.difference(unary_union([water,heroes]))
 # Simplification may move edges slightly: forbid newly invading protected parks.
 clean=clean.difference(parks.difference(old))
 clean,clearance=cut_carrier(clean,local,major,s,.30,.42)
 clean=set_precision(clean.intersection(core),.002)
 assert clean.is_valid
 # Recut reference too for a numerical tolerance-based corridor invariance check.
 checked,_=cut_carrier(clean,local,major,s,.30,.42)
 assert clean.symmetric_difference(checked).area*s*s<.001
 (out/'E_clean.wkb').write_bytes(clean.wkb)
 softness={}
 if args.soft_edges:
  from tools.paris_soft_road_edges import soft_corridor
  clean=from_wkb((ROOT/'output/paris_E_brick_relief_v1/E_clean.wkb').read_bytes())
  extra_width=.12 if args.strong_edges else .04
  local_corridor,le=soft_corridor(local,s,.30,extra_width_mm=extra_width)
  major_corridor,me=soft_corridor(major,s,.42,extra_width_mm=extra_width)
  corridor=unary_union([local_corridor,major_corridor])
  soft=set_precision(clean.difference(corridor),.002)
  soft=unary_union(list(_polygon_parts(soft.intersection(clean)))) # no new occupation of road/park/water
  precision_error=soft.difference(clean).area*s*s
  precision_bound=clean.length*s*(.002*s)*2
  assert soft.is_valid and precision_error<=precision_bound
  recut,_=cut_carrier(soft,local,major,s,.30,.42)
  recut=unary_union(list(_polygon_parts(recut)))
  assert soft.symmetric_difference(recut,grid_size=.002).area*s*s<=precision_bound
  (out/'E_soft.wkb').write_bytes(soft.wkb)
  (out/'E_nominal.wkb').write_bytes(clean.wkb)
  softness={'local':le,'major':me,'overlay_added_area_mm2':precision_error,'overlay_area_bound_mm2':precision_bound,'precision_grid_m':.002,'removed_urban_area_mm2':(clean.area-soft.area)*s*s}
  print('Smooth road-edge variation ready',softness,flush=True)
 carved_evidence={}
 if args.carved_style:
  from tools.paris_block_relief_style import carve_blocks
  clean=from_wkb((ROOT/'output/paris_E_brick_relief_v1/E_clean.wkb').read_bytes())
  if args.rounded_style:
   from tools.paris_block_relief_style import round_blocks
   carved_polys,carved_heights,carved_evidence=round_blocks(clean,s)
  else: carved_polys,carved_heights,carved_evidence=carve_blocks(clean,s)
  if args.pronounced_edges:
   from tools.paris_soft_road_edges import soft_corridor
   lc,le=soft_corridor(local,s,.30,extra_width_mm=.28,wavelength_mm=1.2,step_mm=.10)
   mc,me=soft_corridor(major,s,.42,extra_width_mm=.28,wavelength_mm=1.2,step_mm=.10)
   wave=unary_union([lc,mc]);new_polys=[];new_heights=[]
   for poly,h in zip(carved_polys,carved_heights):
    q=poly.difference(wave)
    # Round new convex tips after cutting; never fill a protected road gap.
    q=q.buffer(-.025/s,resolution=8,join_style=1).buffer(.025/s,resolution=8,join_style=1)
    for part in _polygon_parts(set_precision(q,.002)):
     if part.area*s*s>=.005:
      new_polys.append(part);new_heights.append(h)
   carved_evidence['width_modulation']={'local':le,'major':me,'post_cut_round_radius_mm':.025,'removed_fragment_max_area_mm2':.005}
   carved_polys,carved_heights=new_polys,new_heights
   print('Pronounced smooth edge modulation ready',len(carved_polys),flush=True)
  carved=unary_union(carved_polys)
  assert carved.is_valid
  (out/'carved.wkb').write_bytes(carved.wkb)
 print('E footprint cleanup ready',len(list(_polygon_parts(old))),len(list(_polygon_parts(clean))),flush=True)
 def shell(geom,water_level=False):
  patches=[]
  for p in _polygon_parts(geom):
   xy,f,b=_patch_mesh(scale_geometry(p,xfact=s,yfact=s,origin=(0,0)),t)
   top=sample_terrain_surface_plan_z(t,xy[:,0],xy[:,1])
   if water_level:top=np.full(len(xy),top.min()-.02)
   patches.append(dict(mode='local_ground_microrelief',xy=xy,faces=f,boundary=b,bottom_z=np.full(len(xy),t.terrain_base_z_mm),top_z=top))
  plan=dict(version='diagnostic-local-DEM',terrain_fingerprint=t.fingerprint,input_geometry_fingerprint=hashlib.sha256(geom.wkb).hexdigest(),patches=patches)
  plan['fingerprint']=grounding_digest(plan)
  return materialize_ground_texture(plan)
 terrain=shell(core.difference(water));water_mesh=shell(water,True)
 print('Real DEM and recessed water constructed',flush=True)
 hero_polys=[];hero_heights=[]
 for poly,h in layers.BL:
  if not poly.intersects(core):continue
  for p in _polygon_parts(set_precision(poly.intersection(core).difference(water),.002)):
   hero_polys.append(p);hero_heights.append(float(h))
 hg=resolve_grounding(hero_polys,hero_heights,s,t,['flat_roof_above_highest_support']*len(hero_polys))
 hero_mesh,_=materialize_grounding(hg,hero_polys,hero_heights,s)
 carved_hero_mesh=hero_mesh
 if args.carved_style:
  compressed=[min(h,1.2,round((.60+.24*np.log1p(h))/.12)*.12) for h in hero_heights]
  hp,hh=hero_polys,compressed
  if args.rounded_style:
   hp=[];hh=[]
   for poly,h in zip(hero_polys,compressed):
    rounded,_,_=round_blocks(poly,s)
    hp.extend(rounded);hh.extend([h]*len(rounded))
   carved_evidence['landmark_corner_treatment']='same adaptive rounded opening; retain compressed heights'
  chg=resolve_grounding(hp,hh,s,t,['flat_roof_above_highest_support']*len(hp))
  carved_hero_mesh,_=materialize_grounding(chg,hp,hh,s)
  carved_evidence['landmark_heights_before_mm']=hero_heights
  carved_evidence['landmark_heights_after_mm']=compressed
 rows=[]
 variants=[('E_before',old,None),('E_brick',clean,kw['z_texture_policy']['max_block_relief_mm'])]
 if args.soft_edges: variants=[('E_nominal',clean,kw['z_texture_policy']['max_block_relief_mm']),('E_soft',soft,kw['z_texture_policy']['max_block_relief_mm'])]
 if args.strong_edges:
  previous=from_wkb((ROOT/'output/paris_E_soft_edges_v1/E_soft.wkb').read_bytes())
  variants=[('E_previous',previous,kw['z_texture_policy']['max_block_relief_mm']),('E_soft',soft,kw['z_texture_policy']['max_block_relief_mm'])]
 if args.carved_style:
  previous=from_wkb((ROOT/'output/paris_E_soft_edges_v2/E_soft.wkb').read_bytes())
  variants=[('E_wavy',previous,kw['z_texture_policy']['max_block_relief_mm']),('E_carved',carved,kw['z_texture_policy']['max_block_relief_mm'])]
 for name,geom,flat in variants:
  polys=list(_polygon_parts(geom));heights=[BLOCK_BASE_THICKNESS_MM]*len(polys)
  if name=='E_carved': polys,heights=carved_polys,carved_heights
  hm=carved_hero_mesh if name=='E_carved' else hero_mesh
  ground=resolve_grounding(polys,heights,s,t,['draped_thickness']*len(polys),flat_block_relief_limit_mm=flat)
  city,proof=materialize_grounding(ground,polys,heights,s)
  meshes=[('terrain',terrain.vertices,terrain.faces,'terrain'),('water',water_mesh.vertices,water_mesh.faces,'water'),('city',city.vertices,city.faces,'urban_relief'),('landmarks',hm.vertices,hm.faces,'urban_relief')]
  if args.rounded_style and name=='E_wavy':
   from tools.render_reference_actual_mesh import meshes_from_file
   baseline_path=ROOT/('output/paris_E_rounded_style_v1/E_carved_diagnostic.3mf' if args.pronounced_edges else 'output/paris_E_carved_style_v1/E_carved_diagnostic.3mf')
   meshes=meshes_from_file(baseline_path)
  views=[render(meshes,out/f'{name}_oblique.png',35,pixel_size=1700,frame_width_mm=36.5),render(meshes,out/f'{name}_topdown.png',90,pixel_size=1700,frame_width_mm=36.5)]
  if args.soft_edges or args.carved_style:
   views.append(render(meshes,out/f'{name}_detail.png',90,pixel_size=1500,crop=(-5.,4.,1.,10.)))
  export_deepseek_3mf({'terrain':terrain,'water':water_mesh,'buildings':city,'landmarks':hm},str(out/f'{name}_diagnostic.3mf'))
  if args.rounded_style and name=='E_wavy':
   import shutil
   shutil.copyfile(baseline_path,out/f'{name}_diagnostic.3mf')
  row={'id':name,'city_faces':len(city.faces),'watertight':bool(city.is_watertight),'grounding':ground['evidence'],'proof':proof,'views':views}
  rows.append(row);print(name,len(city.faces),ground['evidence']['low_relief_blocks_flattened'],flush=True)
 # Label actual mesh views at the same camera and frame width.
 for view in (['oblique','topdown','detail'] if (args.soft_edges or args.carved_style) else ['oblique','topdown']):
  ims=[Image.open(out/f'{name}_{view}.png') for name,_,_ in variants]
  w=ims[0].width;h=max(i.height for i in ims)
  sheet=Image.new('RGB',(w*2,h+120),'#f5f5f2');d=ImageDraw.Draw(sheet)
  font=ImageFont.truetype('/System/Library/Fonts/STHeiti Medium.ttc',32)
  labels=['E 原轮廓 · 随地形顶面','E 整理轮廓 · 低坡平顶']
  if args.soft_edges: labels=['E 平顶街块 · 固定切缝','E 平顶街块 · 轻微连续收放']
  if args.strong_edges: labels=['上一版 · 宽度变化 0.04 mm','增强版 · 宽度变化 0.12 mm']
  if args.carved_style: labels=['上一版 · 道路收放','刻版浮雕试验 · 整块退让与高低层次']
  if args.rounded_style: labels=['上一版 · 削角与旋转','修正版 · 连续圆滑边界']
  if args.pronounced_edges: labels=['上一版 · 圆滑街块','增强版 · 更明显的连续起伏']
  for i,(im,label) in enumerate(zip(ims,labels)):
   sheet.paste(im,(i*w,65+h-im.height));d.text((i*w+25,18),label,font=font,fill='#222222')
  d.text((25,h+77),'真实网格 / 同一 E 路网、DEM、光照与比例 / Z 未夸张 / 诊断小样，非切片预览',font=font,fill='#333333')
  sheet.save(out/f'comparison_{view}.png')
 report={'purpose':'diagnostic','verdict':'human_review','source':str(src),'terrain_fingerprint':t.fingerprint,'scale_mm_per_m':s,'bbox_local_m':list(core.bounds),'elapsed_seconds':time.monotonic()-start,
 'soft_edges':softness,'carved_style':carved_evidence,
 'cleanup':{'closing_radius_mm':.06,'opening_radius_mm':.012,'simplification_mm':.01,'parts_before':len(list(_polygon_parts(old))),'parts_after':len(list(_polygon_parts(clean))),'area_change_mm2':(clean.area-old.area)*s*s},
 'block_thickness_mm':BLOCK_BASE_THICKNESS_MM,'road_widths_mm':[.30,.42],'landmark_count':len(hero_polys),'variants':rows,
 'limitations':['Pre-S6 planar E replay placed on frozen real DEM; not a crop of final Paris mesh.','Uniform anonymous block thickness; original core BO tiers are not reconstructed.','Reference comparator deliberately draped; production pipeline already has a low-slope flat-roof policy.','Water uses per-component low-level diagnostic recess; bridges not reconstructed.','No new microtexture added. No slicing/printing or formal full-model acceptance.']}
 (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str));print('DONE',report['elapsed_seconds'],flush=True)
if __name__=='__main__':main()
