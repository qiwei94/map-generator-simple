"""Bounded slice coupons from measured reference height samples; no printer IO."""
from pathlib import Path
import hashlib
import json
import numpy as np
import trimesh

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'output/microrelief_slice_20260906'
PROFILES=Path('/Applications/BambuStudio.app/Contents/Resources/profiles/BBL')

def settings(kind,name):
    p=PROFILES/kind/(name+'.json')
    d=json.loads(p.read_text()); result={}
    if d.get('inherits'):result.update(settings(kind,d['inherits']))
    for inc in d.get('include',[]):result.update(settings(kind,inc))
    result.update(d)
    for k in ('inherits','include'):result.pop(k,None)
    return result

def solid(z):
    ny,nx=z.shape
    yy,xx=np.indices(z.shape)
    n=nx*ny
    v=np.vstack([np.c_[xx.ravel()*.1,yy.ravel()*.1,z.ravel()],
                 np.c_[xx.ravel()*.1,yy.ravel()*.1,np.zeros(n)]])
    f=[]
    for y in range(ny-1):
        for x in range(nx-1):
            a=y*nx+x;b=a+1;c=a+nx;d=c+1
            f.extend([(a,b,d),(a,d,c),(a+n,d+n,b+n),(a+n,c+n,d+n)])
    edge=list(range(nx))+[y*nx+nx-1 for y in range(1,ny)]+list(range(n-2,n-nx-1,-1))+[y*nx for y in range(ny-2,0,-1)]
    for a,b in zip(edge,edge[1:]+edge[:1]):f.extend([(a,a+n,b+n),(a,b+n,b)])
    m=trimesh.Trimesh(vertices=v,faces=f,process=False)
    assert m.is_watertight and m.is_winding_consistent and m.volume>0
    return m

def main():
    OUT.mkdir(parents=True,exist_ok=False)
    src=ROOT/'output/reference_green_audit_20260906/xixi_interior_samples.npz'
    z=np.load(src)['visible'][::-1].copy()
    assert np.isfinite(z).all()
    parts=[]; manifest=[]
    for row,kind in enumerate(('reference','flat')):
        for col,phase in enumerate((0.,.03,.06,.09)):
            surface=(z if kind=='reference' else np.full(z.shape,z.mean()))+phase
            m=solid(surface);m.apply_translation([100+col*14,100+row*10,0])
            parts.append(m)
            manifest.append({'kind':kind,'phase_mm':phase,'bounds':m.bounds.tolist(),
                'surface_min_mm':float(surface.min()),'surface_max_mm':float(surface.max())})
    trimesh.util.concatenate(parts).export(OUT/'eight_coupons.stl')
    machine=settings('machine','Bambu Lab X1 Carbon 0.4 nozzle')
    process=settings('process','0.12mm Fine @BBL X1C')
    filament=settings('filament','Generic PLA')
    process.update({'ironing_type':'no ironing','enable_support':'0','layer_height':'0.12'})
    for name,obj in [('machine',machine),('process',process),('filament',filament)]:
        (OUT/(name+'.json')).write_text(json.dumps(obj,indent=2))
    (OUT/'manifest.json').write_text(json.dumps({'samples':manifest,'source_npz_sha256':hashlib.sha256(src.read_bytes()).hexdigest(),
        'sample_spacing_mm':.1,'layer_height_mm':.12,'nozzle_mm':.4,
        'initial_layer_height':process.get('initial_layer_print_height'),
        'limits':['参考表面以0.1mm网格重采样，并非逐三角面原样裁切。',
            '平面对照与参考表面均值相同；phase仅改变基底厚度，不改变微起伏幅度。',
            '未发送打印任务；切片样片不是完整城市3MF验收。']},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
