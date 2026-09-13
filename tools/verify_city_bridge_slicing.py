"""Six actual Paris bridge coupons, unchanged model scale, offline slicing."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
import trimesh
from tools.verify_microrelief_slicing import settings
from tools.verify_road_width_slicing import parse_paths


def prepare(a):
    a.output.mkdir(parents=True,exist_ok=False); parts=[]; records=[]
    for row,root in enumerate((a.before,a.after)):
        for col,index in enumerate((1,22,23)):
            d=root/f'bridge_{index}'; path=d/'combined_single_material.stl'
            m=trimesh.load_mesh(path,process=False)
            delta=np.array([90+col*15,90+row*15,-m.bounds[0,2]])
            m.apply_translation(delta); parts.append(m)
            records.append(dict(variant='before' if row==0 else 'after',bridge=index,
                source_stl=str(path.resolve()),source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                delta_mm=delta.tolist(),bounds_mm=m.bounds.tolist(),geometry=json.loads((d/'report.json').read_text())))
    combined=trimesh.util.concatenate(parts); combined.export(a.output/'coupons.stl')
    for kind,name in [('machine','Bambu Lab X1 Carbon 0.4 nozzle'),('process','0.12mm Fine @BBL X1C'),('filament','Generic PLA')]:
        v=settings(kind,name)
        if kind=='process':v.update(ironing_type='no ironing',enable_support='0',enable_arc_fitting='0',brim_type='no_brim')
        (a.output/f'{kind}.json').write_text(json.dumps(v,indent=2))
    (a.output/'manifest.json').write_text(json.dumps(dict(samples=records,
        stl_sha256=hashlib.sha256((a.output/'coupons.stl').read_bytes()).hexdigest(),
        limitations=['真实巴黎道路/水体/DEM局部；不含城市建筑。','单材料组合切片，不代表多材料3MF或实体打印验收。']),ensure_ascii=False,indent=2))


def analyze(a):
    from shapely.geometry import LineString
    from shapely.ops import unary_union
    manifest=json.loads((a.output/'manifest.json').read_text())
    path=a.output/'plate_1.gcode'; text=path.read_text(); s=parse_paths(text)
    first=s[np.isclose(s[:,4],.2)][:,:4].reshape(-1,2)
    bounds=np.array([r['bounds_mm'] for r in manifest['samples']])
    delta=(first.min(0)+first.max(0))/2-(bounds[:,0,:2].min(0)+bounds[:,1,:2].max(0))/2
    s[:,[0,2]]-=delta[0];s[:,[1,3]]-=delta[1]
    out=[]
    for r in manifest['samples']:
        record=r['geometry']; shift=np.array(r['delta_mm']); lo,hi=np.array(r['bounds_mm'])
        middle=(s[:,:2]+s[:,2:4])/2
        local=s[((middle>=lo[:2])&(middle<=hi[:2])).all(1)]
        support=record['support'][0]
        bank=np.array(support['bank_xy_m'])*record['scale_mm_per_m']+shift[:2]
        z=np.array(support['bank_surface_z_mm'])+shift[2]
        center=LineString(bank)
        # Sample the actual depositing layers intersecting the approved deck.
        # Declared path envelopes are not physical extrusion measurements.
        levels=[]
        for level in sorted(set(local[:,4])):
            if not z.min()-.12-1e-5<=level<=z.max()+.12+1e-5:continue
            at=local[np.isclose(local[:,4],level)]
            envelope=unary_union([LineString([(x,y),(u,v)]).buffer(w/2) for x,y,u,v,_,w in at])
            levels.append(dict(z_mm=float(level),centerline_coverage=float(center.intersection(envelope).length/center.length),
                endpoints_covered=[bool(envelope.covers(__import__('shapely').geometry.Point(p))) for p in bank]))
        out.append(dict(variant=r['variant'],bridge=r['bridge'],bank_contact_status=record['bank_contact_status'],
            geometry_components=record['combined_components'],deck_layers=levels))
    report=dict(purpose='diagnostic',formal_print_acceptance=False,gcode_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        xy_slicer_translation_mm=delta.tolist(),samples=out,limitations=manifest['limitations'])
    (a.output/'slice_analysis.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(out,ensure_ascii=False,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','analyze'])
    p.add_argument('--before',type=Path);p.add_argument('--after',type=Path);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();prepare(a) if a.action=='prepare' else analyze(a)
