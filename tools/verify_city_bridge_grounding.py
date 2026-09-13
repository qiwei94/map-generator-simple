"""Offline real-source Paris bridge subsystem test; not full city acceptance."""
import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import trimesh
from shapely.geometry import box
from shapely.affinity import translate
from types import SimpleNamespace
from tools.evaluate_urban_organization import load_checked, base_thickness_from_plan
from aesthetic.bridge_sources import extract_bridge_sources, water_bridge_lines
from aesthetic.surface_grounding import _parts
from aesthetic.city_surface_plan import finalize_city_surfaces, materialize_road_surfaces
from aesthetic.geometry_inspection import build_geometry_inspection
from _TEXTURE_STYLE_OF_DEEPSEEK._layer_preprocess import LayerPolygons
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import materialize_terrain_surface_plan
from _TEXTURE_STYLE_OF_DEEPSEEK.render_glb import _TerrainSurfacePlanSampler
from _TEXTURE_STYLE_OF_DEEPSEEK.water import prepare_deepseek_water_relief, build_deepseek_water_v3
from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf
from _TEXTURE_STYLE_OF_DEEPSEEK._geom_utils import mesh_to_manifold64, manifold64_to_mesh


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--snapshot',type=Path,required=True)
    ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--bridges',default='1,22,23')
    ap.add_argument('--exact-water-boundary',action='store_true')
    args=ap.parse_args(); args.output.mkdir(parents=True,exist_ok=False)
    data=load_checked(args.snapshot); r=data['runtime']; old=r['terrain_surface_plan']
    scale=r['scale_mm_per_m']; base=base_thickness_from_plan(old)
    raw=r['sources'].roads.to_dict('records')
    sources,_=extract_bridge_sources(raw)
    full=data['layers']; waters=list(full.WL)+list(full.WO)
    bridges=water_bridge_lines(SimpleNamespace(bridge_lines=sources,WL=waters,WO=[]))
    ny,nx=old.surface_z_grid_mm.shape
    xs=np.linspace(-old.width_m/2,old.width_m/2,nx)
    ys=np.linspace(-old.height_m/2,old.height_m/2,ny)
    records=[]
    for index in map(int,args.bridges.split(',')):
        tick=time.monotonic(); br=bridges[index]; bounds=br.buffer(350).bounds
        ix0=max(0,np.searchsorted(xs,bounds[0])-1); ix1=min(nx-1,np.searchsorted(xs,bounds[2]))
        iy0=max(0,np.searchsorted(ys,bounds[1])-1); iy1=min(ny-1,np.searchsorted(ys,bounds[3]))
        cx=(xs[ix0]+xs[ix1])/2; cy=(ys[iy0]+ys[iy1])/2
        width=xs[ix1]-xs[ix0]; depth=ys[iy1]-ys[iy0]
        core=box(xs[ix0],ys[iy0],xs[ix1],ys[iy1]); local=(-width/2,-depth/2,width/2,depth/2)
        z=old.surface_z_grid_mm[iy0:iy1+1,ix0:ix1+1].copy()
        grid=old.regular_grid_m[iy0:iy1+1,ix0:ix1+1].copy()
        fingerprint=hashlib.sha256(old.fingerprint.encode()+np.array([ix0,ix1,iy0,iy1]).tobytes()+z.tobytes()).hexdigest()
        plan=replace(old,width_m=width,height_m=depth,surface_z_grid_mm=z,regular_grid_m=grid,
                     fingerprint=fingerprint)
        water=[translate(p,-cx,-cy) for w in waters if w.intersects(core) for p in _parts(w.intersection(core))]
        bridge=translate(br,-cx,-cy)
        layers=LayerPolygons(WL=water,bridge_lines=[bridge])
        bridge_rows=[dict(row,geometry=translate(row['geometry'],-cx,-cy)) for row in raw
                     if row['geometry'] is not None and row['geometry'].equals(br)]
        target=args.output/f'bridge_{index}';target.mkdir()
        record=dict(bridge_index=index,source_sha256=hashlib.sha256(br.wkb).hexdigest(),
            snapshot_sha256=args.snapshot.with_suffix('.sha256').read_text().strip(),
            source_bbox_local_m=list(core.bounds),source_crs=str(r['sources'].roads.crs),
            local_origin_m=[cx,cy],scale_mm_per_m=scale,model_size_mm=[width*scale,depth*scale],
            original_terrain_fingerprint=old.fingerprint,cropped_terrain_fingerprint=fingerprint,
            terrain_sampling='exact original grid subset, no Z renormalization',
            scope='one actual source bridge + actual water + DEM; other roads/buildings omitted',
            formal_print_acceptance=False)
        try:
            finalize_city_surfaces(layers,bbox_local=local,scale=scale,printer_profile=r['printer_profile'],
                terrain_surface_plan=plan,road_style='negative-space-v1',source_roads=bridge_rows)
            record['support']=layers.surface_grounding['roads']['support_evidence']
            sampler=_TerrainSurfacePlanSampler(plan,local,scale)
            roads,_=materialize_road_surfaces(layers,scale,sampler.z_mm_vec)
            terrain=materialize_terrain_surface_plan(plan)
            record['water_relief']=relief=prepare_deepseek_water_relief(terrain,water,[],scale,
                base_thickness_mm=base,surface_thickness_mm=r['printer_profile'].min_surface_height_mm,
                exact_boundary=args.exact_water_boundary)
            water_mesh=build_deepseek_water_v3(water,[],*local,scale,flat_only=False,
                base_thickness_mm=base,surface_levels_mm=relief['surface_levels_mm'],
                surface_thickness_mm=relief['surface_thickness_mm'],support_to_base=args.exact_water_boundary)
            meshes={'terrain':terrain,'water':water_mesh,'roads':roads}
            export_deepseek_3mf(meshes,str(target/'semantic_diagnostic.3mf'))
            trimesh.Scene(meshes).export(target/'semantic_diagnostic.glb')
            # Actual combined solid for single-material slicing, not a substituted
            # multi-material acceptance artifact. The semantic 3MF stays intact.
            solid=mesh_to_manifold64(terrain)+mesh_to_manifold64(water_mesh)+mesh_to_manifold64(roads)
            # Backend simplification bounds surface motion to 1e-7 model mm;
            # no nozzle-scale filtering or arbitrary component deletion.
            combined=manifold64_to_mesh(solid.simplify(1e-7))
            combined.export(target/'combined_single_material.stl')
            record['combined_components']=len(combined.split(only_watertight=False))
            record['component_volumes_mm3']=[float(m.volume) for m in combined.split(only_watertight=False)]
            record['assembly_numeric_simplification_tolerance_mm']=1e-7
            record['inspection']=build_geometry_inspection(layers,meshes)
            # Bank support is inspected against final recessed terrain, not
            # the frozen pre-water sampler. Positive volume is conservative.
            contact=[]
            for s in record['support']:
                for bank in s.get('bank_xy_m',[]):
                    center=np.array(bank)*scale
                    cutter=trimesh.creation.box(extents=[.1,.1,100],transform=trimesh.transformations.translation_matrix([*center,0]))
                    overlap=manifold64_to_mesh(mesh_to_manifold64(roads)^mesh_to_manifold64(terrain)^mesh_to_manifold64(cutter))
                    contact.append(float(overlap.volume))
            record['bank_contact_volume_mm3']=contact
            record['bank_contact_status']='passed' if contact and min(contact)>1e-9 else 'failed'
            record['status']='generated_pending_slice'
        except Exception as exc:
            record.update(status='blocked',error=str(exc))
        record['seconds']=time.monotonic()-tick
        (target/'report.json').write_text(json.dumps(record,ensure_ascii=False,indent=2,default=str))
        records.append(record)
        print(index,record['status'],record.get('error',record.get('bank_contact_status')),flush=True)
    (args.output/'summary.json').write_text(json.dumps(records,ensure_ascii=False,indent=2,default=str))


if __name__=='__main__': main()
