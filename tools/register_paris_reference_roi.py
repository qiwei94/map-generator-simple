"""Image-only affine registration and frozen small comparison windows."""
import json
import hashlib
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy import ndimage as ndi
from scipy.optimize import minimize

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / 'output/paris_organization_20260905_v1'
OUT = BASE / 'registered_roi_v1'


def main():
    OUT.mkdir(exist_ok=False)
    srcpath = BASE / 'review_2d_AC/paris_C_topdown.png'
    refpath = BASE / 'review_2d_AC/reference_comparison/reference_original.png'
    src, ref = Image.open(srcpath).convert('RGB'), Image.open(refpath).convert('RGB')
    n = 800
    sm = np.max(np.asarray(src.resize((n,n))), axis=2) < 55
    rm = np.max(np.asarray(ref.resize((n,n))), axis=2) < 55
    # Ignore tiny marks and borders. Fit broad water features, not street style.
    def clean(mask):
        labels, _ = ndi.label(mask)
        sizes = np.bincount(labels.ravel())
        out = mask & (sizes[labels] >= 15)
        out[:20] = False; out[-20:] = False
        out[:,:20] = False; out[:,-20:] = False
        return out
    sm, rm = clean(sm), clean(rm)
    sd, rd = ndi.distance_transform_edt(~sm), ndi.distance_transform_edt(~rm)
    sy, sx = np.nonzero(sm); ry, rx = np.nonzero(rm)
    s = np.stack([sx,sy],1)[::3]; r = np.stack([rx,ry],1)[::3]
    def unpack(p):
        return np.array([[p[0],p[1]],[p[2],p[3]]]), np.array(p[4:])
    def residual(p, ss=s, rr=r):
        a,t = unpack(p)
        forward = ss @ a.T + t
        back = (rr-t) @ np.linalg.inv(a).T
        d1 = ndi.map_coordinates(rd, [forward[:,1],forward[:,0]], order=1, mode='constant',cval=40)
        d2 = ndi.map_coordinates(sd, [back[:,1],back[:,0]], order=1, mode='constant',cval=40)
        return np.r_[d1,d2]
    fit = minimize(lambda p: np.mean(np.minimum(residual(p),12)**2),
                   [.93,0,0,.986,67,29], method='Powell',
                   bounds=[(.85,1.05),(-.04,.04),(-.04,.04),(.9,1.08),(25,100),(-10,65)],
                   options={'maxiter':100,'xtol':1e-6,'ftol':1e-7})
    a,t = unpack(fit.x)
    # In full source pixels, map to native reference pixels.
    source_to_ref = np.diag(np.array(ref.size)/n) @ a @ np.diag(n/np.array(src.size))
    offset = np.array(ref.size)/n*t
    def ref_crop(bounds, size):
        x0,y0,x1,y1 = bounds
        step = np.diag([(x1-x0)/size,(y1-y0)/size])
        mat = source_to_ref @ step
        off = source_to_ref @ np.array([x0,y0]) + offset
        return ref.transform((size,size),Image.Transform.AFFINE,
            (mat[0,0],mat[0,1],off[0],mat[1,0],mat[1,1],off[1]),Image.Resampling.BICUBIC)
    # All three windows lie inside the previously used 5 km core.
    manifest = json.loads((BASE/'capture_manifest.json').read_text())
    xmin,ymin,xmax,ymax = manifest['bbox_local_m']
    def pixels(b):
        l,bottom,r,top=b
        return [(l-xmin)/(xmax-xmin)*src.width,(ymax-top)/(ymax-ymin)*src.height,
                (r-xmin)/(xmax-xmin)*src.width,(ymax-bottom)/(ymax-ymin)*src.height]
    regions = [('R1','北侧街区',[-2100,600,-500,2200]),
               ('R2','河道与岛屿',[-300,-1700,1300,-100]),
               ('R3','南侧街区',[-2200,-2300,-600,-700])]
    font = ImageFont.truetype('/System/Library/Fonts/STHeiti Medium.ttc',30)
    small = ImageFont.truetype('/System/Library/Fonts/STHeiti Medium.ttc',22)
    sheet = Image.new('RGB',(1740,3*670+90),'#f7f7f5'); draw=ImageDraw.Draw(sheet)
    draw.text((20,10),'固定基准窗口｜左：当前 C 原图　中：参考 demo 配准局部　右：水体叠合检查',font=font,fill='#222222')
    records=[]
    for i,(rid,title,b) in enumerate(regions):
        bounds=pixels(b); native=src.crop(tuple(round(v) for v in bounds)).resize((550,550))
        aligned=ref_crop(bounds,550)
        overlay=np.full((550,550,3),245,dtype=np.uint8)
        ms=np.max(np.asarray(native),axis=2)<55
        mr=np.max(np.asarray(aligned),axis=2)<55
        overlay[ms]=(0,160,210); overlay[mr]=(240,80,135); overlay[ms&mr]=(35,35,35)
        for j,im in enumerate([native,aligned,Image.fromarray(overlay)]):
            sheet.paste(im,(20+j*580,110+i*670))
        draw.text((20,75+i*670),f'{rid} {title}｜1.6 km × 1.6 km',font=small,fill='#222222')
        draw.text((20,675+i*670),'水体检查：青＝当前；粉＝参考；黑＝重叠。无河区域以路口和街区边界人工核验。',font=small,fill='#555555')
        aligned.save(OUT/f'{rid}_reference.png'); native.save(OUT/f'{rid}_current.png')
        corners=np.array([[bounds[0],bounds[1]],[bounds[2],bounds[1]],[bounds[2],bounds[3]],[bounds[0],bounds[3]]])
        records.append({'id':rid,'label':title,'bbox_local_m':b,'source_pixel_bounds':bounds,
                        'reference_pixel_quad':(corners@source_to_ref.T+offset).tolist()})
    sheet.save(OUT/'roi_baselines.png')
    # Locate the exact same windows on each original, without modifying originals.
    overview=Image.new('RGB',(1660,900),'#f7f7f5'); od=ImageDraw.Draw(overview)
    for j,im in enumerate([src,ref]):
        thumb=im.resize((800,800)); d=ImageDraw.Draw(thumb)
        for rec in records:
            if j==0:
                x0,y0,x1,y1=rec['source_pixel_bounds']; points=np.array([[x0,y0],[x1,y0],[x1,y1],[x0,y1]])
            else: points=np.array(rec['reference_pixel_quad'])
            points=points/np.array(im.size)*800
            xy=[tuple(p) for p in points]; d.line(xy+[xy[0]],fill='#ff582d',width=3)
            d.text(xy[0],rec['id'],font=font,fill='#e23c10')
        overview.paste(thumb,(20+j*820,65))
        od.text((20+j*820,15),['当前 C：仅定位，不重跑','参考 demo：同一窗口映射'][j],font=font,fill='#222222')
    overview.save(OUT/'roi_locations.png')
    report={'purpose':'diagnostic','verdict':'human_review','fit_success':bool(fit.success),
      'source_to_reference_affine':source_to_ref.tolist(),'offset':offset.tolist(),
      'registration':'bounded affine; bidirectional water-mask distance; no elastic warping',
      'water_mask_distance_p50_p90_p95_work_pixels':np.percentile(residual(fit.x),[50,90,95]).tolist(),
      'work_image_size':n,'regions':records,
      'sources':[{'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in [srcpath,refpath]],
      'limitations':['Raster-derived approximate registration, not surveyed georeferencing',
        'Water-width and bridge-rendering differences persist; mask distances are not positional accuracy',
        'Street intersections require independent visual checks; do not use reference to claim mm print accuracy'],
      'no_geometry_generation':True}
    (OUT/'registration.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':main()
