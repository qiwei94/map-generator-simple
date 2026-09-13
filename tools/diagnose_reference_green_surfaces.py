"""Read existing reference 3MFs: transformed per-object top/bottom ROI samples.

Object semantics are spatially identified, never inferred from extruder alone.
Thickness is local top-minus-bottom, not whole-object Z range. This cannot
identify an author's procedural random seed or prove their source DEM.
"""
import ctypes
import hashlib
import json
from pathlib import Path
import sys
import numpy as np
from scipy.ndimage import gaussian_filter, maximum_filter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.render_reference_actual_mesh import meshes_from_file, raster_library


def raster(v, f, box, step=.10, sign=1.):
    x0,y0,x1,y1 = box
    width, height = int(round((x1-x0)/step)), int(round((y1-y0)/step))
    t = v[f]
    mask = ((t[:,:,0].max(1)>=x0)&(t[:,:,0].min(1)<=x1)&
            (t[:,:,1].max(1)>=y0)&(t[:,:,1].min(1)<=y1))
    t = t[mask].copy()
    t[:,:,0] = (t[:,:,0]-x0)/step
    t[:,:,1] = (y1-t[:,:,1])/step
    t[:,:,2] *= sign
    t = np.ascontiguousarray(t, dtype=np.float64)
    c = np.full(len(t), 150, dtype=np.uint8)
    depth = np.full((height,width), -np.inf, dtype=np.float64)
    rgb = np.zeros((height,width,3), dtype=np.uint8)
    lib = ctypes.CDLL(str(raster_library()))
    lib.raster.argtypes = [ctypes.c_void_p,ctypes.c_void_p,ctypes.c_size_t,
        ctypes.c_int,ctypes.c_int,ctypes.c_void_p,ctypes.c_void_p]
    lib.raster(t.ctypes.data,c.ctypes.data,len(t),width,height,depth.ctypes.data,rgb.ctypes.data)
    depth[~np.isfinite(depth)] = np.nan
    return depth*sign


def describe(a):
    a = a[np.isfinite(a)]
    return None if not len(a) else dict(zip(('p05','p50','p95','min','max'),
        map(float, np.r_[np.percentile(a,[5,50,95]),a.min(),a.max()])))


def main():
    out = ROOT/'output/reference_green_audit_20260906'
    out.mkdir(parents=True,exist_ok=True)
    cases = [('杭州','xixi_interior',(-93.,-6.,-82.,1.)),
             ('纽约','central_park_north',(12.,26.,18.,33.))]
    evidence=[]
    for city, label, box in cases:
        path=ROOT.parent/'reference_archive/city_demo'/city/(city+'25Km城市肌理P.3mf')
        meshes=meshes_from_file(path,True)
        low=np.min([v.min(0) for _,v,_,_ in meshes],axis=0)
        high=np.max([v.max(0) for _,v,_,_ in meshes],axis=0)
        origin=np.array([(low[0]+high[0])/2,(low[1]+high[1])/2,0.])
        tops=[]; bottoms=[]; objects=[]
        for name,v,f,role in meshes:
            top=raster(v-origin,f,box)
            bottom=raster(v-origin,f,box,sign=-1.)
            tops.append(top);bottoms.append(bottom)
            objects.append({'name':name,'display_role_only':role,
                'roi_coverage':float(np.isfinite(top).mean()),
                'top_z_mm':describe(top),'local_shell_thickness_mm':describe(top-bottom)})
        tops=np.array(tops);bottoms=np.array(bottoms)
        finite=np.isfinite(tops)
        visible=np.max(np.where(finite,tops,-np.inf),axis=0)
        owner=np.argmax(np.where(finite,tops,-np.inf),axis=0)
        for i,o in enumerate(objects):
            o['visible_fraction']=float((owner==i).mean())
        yy,xx=np.indices(visible.shape)
        valid=np.isfinite(visible)
        design=np.column_stack([xx[valid]*.1,yy[valid]*.1,np.ones(valid.sum())])
        coef=np.linalg.lstsq(design,visible[valid],rcond=None)[0]
        residual=np.full(visible.shape,np.nan);residual[valid]=visible[valid]-design@coef
        # Residual contains real landform + facets + surface transitions, not vegetation truth.
        row={'city':city,'sample':label,'source':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
             'roi_centered_model_xy_mm':box,'sample_spacing_mm':.1,
             'objects':objects,'visible_z_mm':describe(visible),
             'plane_detrended_z_mm':describe(residual),
             'plane_detrended_rms_mm':float(np.sqrt(np.nanmean(residual**2))),
             'limitations':['ROI由模型图上空间位置识别，非地理配准边界。',
              '物体材质不是地物分类；局部厚度不等于覆盖层高于邻接地面的高度。',
              '起伏和三角面明暗不能证明随机噪声算法；无作者生成代码。']}
        evidence.append(row)
        np.savez_compressed(out/(label+'_samples.npz'),top=tops,bottom=bottoms,visible=visible,owner=owner,residual=residual)
        print(json.dumps(row,ensure_ascii=False,indent=2),flush=True)
    (out/'green_surface_measurements.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
