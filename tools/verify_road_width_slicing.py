"""Offline paired groove/rib slicing experiment; never sends printer commands."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import numpy as np
import trimesh

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.verify_microrelief_slicing import settings

OUT = ROOT / 'output/road_width_slice_20260906'
WIDTHS = (.14, .28, .42, .55, .63, .84)


def prism(x0, x1, y0, y1, z0, z1):
    from _TEXTURE_STYLE_OF_DEEPSEEK._geom_utils import shapely_poly_to_crosssection
    from shapely.geometry import box
    return shapely_poly_to_crosssection(box(x0, y0, x1, y1)).extrude(z1-z0).translate((0,0,z0))


def prepare():
    OUT.mkdir(parents=True, exist_ok=False)
    meshes, samples = [], []
    for row, kind in enumerate(('negative_gap', 'positive_strip')):
        for col, w in enumerate(WIDTHS):
            solid = prism(0,12,0,8,0,.8)
            if kind == 'negative_gap':
                solid = solid + prism(0,6-w/2,0,8,.8,1.28) + prism(6+w/2,12,0,8,.8,1.28)
            else:
                solid = solid + prism(6-w/2,6+w/2,0,8,.8,1.28)
            raw=solid.to_mesh64()
            mesh=trimesh.Trimesh(vertices=np.asarray(raw.vert_properties),faces=np.asarray(raw.tri_verts),process=False)
            assert mesh.is_watertight and mesh.is_winding_consistent and mesh.volume>0
            mesh.apply_translation([70+col*15,90+row*12,0])
            samples.append({'kind':kind,'width_mm':w,'bounds':mesh.bounds.tolist()})
            meshes.append(mesh)
    path=OUT/'coupons.stl';trimesh.util.concatenate(meshes).export(path)
    for name, value in (
        ('machine', settings('machine','Bambu Lab X1 Carbon 0.4 nozzle')),
        ('process', settings('process','0.12mm Fine @BBL X1C')),
        ('filament', settings('filament','Generic PLA')),
    ):
        if name=='process':
            value.update({'ironing_type':'no ironing','enable_support':'0',
                          'enable_arc_fitting':'0','brim_type':'no_brim'})
        (OUT/(name+'.json')).write_text(json.dumps(value,indent=2))
    (OUT/'manifest.json').write_text(json.dumps({
        'samples':samples,'stl_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'purpose':'diagnostic','base_height_mm':.8,'relief_height_mm':.48,
        'limits':['直槽/直条同材质小样，不代表弯道、坡地、桥跨或多材料验收。',
                  '分析的是声明线宽的走线包络，不是实体挤出实测。']},ensure_ascii=False,indent=2))


def parse_paths(text):
    import re
    x=y=e=0.; z=None;relative_e=True;absolute_xy=True;feature='';width=.42; paths=[]
    for line in text.splitlines():
        if line.startswith('; Z_HEIGHT:'):z=float(line.split(':')[1])
        if line.startswith('; FEATURE:'):feature=line.split(':')[1].strip()
        if line.startswith('; LINE_WIDTH:'):width=float(line.split(':')[1])
        cmd=line.split(';')[0].strip()
        if cmd=='M83':relative_e=True
        if cmd=='M82':relative_e=False
        if cmd=='G90':absolute_xy=True
        if cmd=='G91':absolute_xy=False
        v={k:float(n) for k,n in re.findall(r'([XYZE])(-?\d*\.?\d+)',cmd)}
        if cmd.startswith('G92'):
            e=v.get('E',e); x=v.get('X',x);y=v.get('Y',y)
        if cmd.startswith(('G2 ','G3 ')) and v.get('E',0)>0 and z is not None and feature!='Custom':
            raise ValueError('Unexpected extruding arc: slice with arc fitting disabled')
        if cmd.startswith(('G0 ','G1 ')):
            a=v.get('X',x) if absolute_xy else x+v.get('X',0)
            b=v.get('Y',y) if absolute_xy else y+v.get('Y',0)
            de=v.get('E',0) if relative_e else v.get('E',e)-e
            e=v.get('E',e)
            if z is not None and feature!='Custom' and de>0 and np.hypot(a-x,b-y)>1e-9:
                paths.append((x,y,a,b,z,width))
            x,y=a,b
    return np.asarray(paths,dtype=float).reshape(-1,6)


def analyze():
    import re
    from shapely.geometry import LineString
    from shapely.ops import unary_union
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection
    from matplotlib.font_manager import FontProperties
    p=OUT/'plate_1.gcode'; text=p.read_text(); s=parse_paths(text)
    manifest=json.loads((OUT/'manifest.json').read_text())
    first=s[np.isclose(s[:,4],.2)][:,:4].reshape(-1,2)
    bounds=np.array([v['bounds'] for v in manifest['samples']])
    delta=(first.min(0)+first.max(0))/2-(bounds[:,0,:2].min(0)+bounds[:,1,:2].max(0))/2
    s[:,[0,2]]-=delta[0];s[:,[1,3]]-=delta[1]
    font=FontProperties(fname='/System/Library/Fonts/STHeiti Medium.ttc')
    fig,axes=plt.subplots(2,6,figsize=(17,6),layout='constrained');reports=[]
    for i,sample in enumerate(manifest['samples']):
        lo,hi=np.asarray(sample['bounds']);mid=(s[:,:2]+s[:,2:4])/2
        seg=s[((mid>=lo[:2])&(mid<=hi[:2])).all(1)].copy()
        seg[:,[0,2]]-=lo[0];seg[:,[1,3]]-=lo[1]
        layers=[]
        for z in sorted(set(seg[:,4])):
            if z<=.8+1e-6:continue
            at=seg[np.isclose(seg[:,4],z)]
            envelope=unary_union([LineString([(a,b),(c,d)]).buffer(w/2) for a,b,c,d,_,w in at])
            scans=[]
            for y in np.linspace(1,7,61):
                cross=LineString([(0,y),(12,y)])
                measured=cross.difference(envelope) if sample['kind']=='negative_gap' else cross.intersection(envelope)
                pieces=list(getattr(measured,'geoms',[measured]))
                if sample['kind']=='negative_gap':
                    center=[g.length for g in pieces if not g.is_empty and g.bounds[0]<=6<=g.bounds[2]]
                    scans.append(max(center,default=0.))
                else:
                    # Two adjacent extrusion lines may have a rounding-scale
                    # center gap; that is not disappearance of the positive strip.
                    scans.append(measured.length)
            layers.append({'z_mm':float(z),'measured_width_min_mm':float(min(scans)),
                           'measured_width_median_mm':float(np.median(scans)),
                           'width_semantics':'center_clear_gap' if sample['kind']=='negative_gap' else 'sum_of_deposited_intervals',
                           'scanline_survival_fraction':float(np.mean(np.asarray(scans)>1e-4)),
                           'extrusion_path_length_mm':float(np.linalg.norm(at[:,2:4]-at[:,:2],axis=1).sum())})
        top=seg[np.isclose(seg[:,4],1.28)]
        ax=axes.flat[i];ax.add_collection(LineCollection(top[:,:4].reshape(-1,2,2),colors='#2186af',linewidths=.7))
        ax.axvspan(6-sample['width_mm']/2,6+sample['width_mm']/2,color='#ed9b54',alpha=.25)
        label='负空间槽' if sample['kind']=='negative_gap' else '正实体条'
        ax.set_title(f'{label} {sample["width_mm"]:.2f} mm',fontproperties=font,fontsize=11)
        ax.set(xlim=(-.2,12.2),ylim=(-.2,8.2),aspect='equal')
        reports.append({**sample,'upper_layers':layers,'top_layer_present':bool(len(top))})
    fig.suptitle('相同名义宽度：空槽与凸条的实际切片走线\n0.4 mm 喷嘴 · 0.12 mm 层高 · 单材料 · Z=1.28 mm 顶层\n蓝色为走线中心；橙色为设计槽／条范围，未显示实体挤出宽度',fontproperties=font,fontsize=14)
    fig.savefig(OUT/'toolpaths.png',dpi=150);plt.close(fig)
    config={k:re.search(r'^; '+k+r' = (.+)$',text,re.M).group(1) for k in (
        'layer_height','initial_layer_print_height','ironing_type','nozzle_diameter','wall_generator',
        'detect_thin_wall','enable_arc_fitting')}
    report={'purpose':'diagnostic','verdict':'human_review','effective_settings':config,
            'gcode_sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'xy_translation_mm':delta.tolist(),
            'samples':reports,'limits':manifest['limits'],'multi_material':'not_tested','physical_print':'not_tested'}
    (OUT/'analysis.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    for r in reports:print(r['kind'],r['width_mm'],r['upper_layers'][-1] if r['upper_layers'] else 'no upper extrusion')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['prepare','analyze'])
    args=parser.parse_args();prepare() if args.action=='prepare' else analyze()
