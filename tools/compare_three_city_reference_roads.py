"""Local raster-only reference overlays, with independently fitted water edges."""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy import ndimage as ndi
from scipy.optimize import differential_evolution, minimize
from compare_reference_road_mask import ROOT, extract

CASES = [
    ('westlake','西湖','four_cities_negative_20260905/westlake/city_topdown.png',
     'reference_green_audit_20260906/hangzhou/actual_topdown.png','four_cities_negative_20260905/westlake/report.json'),
    ('chicago','芝加哥','four_cities_negative_20260905/chicago/chicago-negative-v3.png',
     'chicago_reference_review_20260905/actual_mesh/actual_topdown.png','four_cities_negative_20260905/chicago/chicago-negative-v3-report.json'),
    ('new_york','纽约','density_four_cities_20260906/new_york.png',
     'reference_green_audit_20260906/new_york/actual_topdown.png','density_four_cities_20260906/new_york_render_report.json')]


def fit_water(current, reference):
    n=400
    def edge(im):
        water=np.asarray(im.resize((n,n),Image.Resampling.BILINEAR)).mean(axis=2)<60
        e=water & ~ndi.binary_erosion(water)
        e[:15]=False; e[-15:]=False; e[:,:15]=False; e[:,-15:]=False
        return e
    se,re=edge(current),edge(reference)
    sd,rd=ndi.distance_transform_edt(~se),ndi.distance_transform_edt(~re)
    def points(e):
        y,x=np.nonzero(e); p=np.c_[x,y]
        return p[::max(1,len(p)//1800)]
    s,r=points(se),points(re)
    def unpack(p): return np.array([[p[0],p[1]],[p[2],p[3]]]),np.array(p[4:])*n
    def residual(p):
        a,t=unpack(p); f=s@a.T+t; b=(r-t)@np.linalg.inv(a).T
        values=[]
        for xy,dist in [(f,rd),(b,sd)]:
            valid=(xy[:,0]>15)&(xy[:,0]<n-15)&(xy[:,1]>15)&(xy[:,1]<n-15)
            d=ndi.map_coordinates(dist,[xy[valid,1],xy[valid,0]],order=1)
            values.append(d)
        return values
    def cost(p):
        values=residual(p)
        # Trim unmatched coast segments, retaining penalties for mostly off-frame fits.
        loss=0
        for d,pts in zip(values,[s,r]):
            if len(d)<len(pts)*.35: return 1e6
            keep=np.sort(d)[:max(1,int(len(d)*.75))]
            loss+=np.mean(np.minimum(keep,20)**2)
        return loss
    bounds=[(.85,1.15),(-.045,.045),(-.045,.045),(.85,1.15),(-.30,.30),(-.35,.35)]
    fit=differential_evolution(cost,bounds,seed=20260906,popsize=7,maxiter=90,tol=.002,polish=False)
    final=minimize(cost,fit.x,method='Powell',bounds=bounds,options={'maxiter':100,'xtol':1e-6})
    p=final.x if final.fun<fit.fun else fit.x
    a,t=unpack(p)
    full=np.diag(np.array(reference.size)/n)@a@np.diag(n/np.array(current.size))
    off=np.array(reference.size)/n*t
    return full,off,{'method':'bounded affine, bidirectional water-edge fit, 75% trimmed objective; roads not fitted',
                     'work_size':n,'parameters':p.tolist(),'objective':cost(p),
                     'water_edge_residual_percentiles_px':[np.percentile(v,[50,75,90]).tolist() for v in residual(p)],
                     'limitations':'Water appearance differs. Local street alignment and omitted coast segments are not validated.'}


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--output',required=True,type=Path); args=ap.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    font=ImageFont.truetype('/System/Library/Fonts/STHeiti Medium.ttc',24)
    small=ImageFont.truetype('/System/Library/Fonts/STHeiti Medium.ttc',19)
    overview=Image.new('RGB',(2840,2270),'#f7f7f5'); d=ImageDraw.Draw(overview)
    d.text((20,10),'三城 25 km｜参考路网覆盖诊断（各城独立水岸配准；不是漏路统计）',font=font,fill='#222')
    d.text((20,45),'红：候选穿过白块；青：靠近灰区；黄：水岸或宽灰区；紫：参考覆盖不足。灰区不一定是道路。',font=small,fill='#444')
    reports=[]
    for row,(key,name,cpath,rpath,mpath) in enumerate(CASES):
        cp,rp,mp=[ROOT/'output'/p for p in [cpath,rpath,mpath]]
        current,reference=Image.open(cp).convert('RGB'),Image.open(rp).convert('RGB')
        a,t,registration=fit_water(current,reference)
        registration_warning=max(v[0] for v in registration['water_edge_residual_percentiles_px'])>3
        registration['status']='unreliable_do_not_score_roads' if registration_warning else 'approximate_human_review'
        print(key,registration,flush=True)
        coeff=tuple([a[0,0],a[0,1],t[0],a[1,0],a[1,1],t[1]])
        aligned=reference.transform(current.size,Image.Transform.AFFINE,coeff,Image.Resampling.BICUBIC,fillcolor=(235,225,245))
        def warp(mask): return np.asarray(Image.fromarray(mask.astype('uint8')*255).transform(current.size,Image.Transform.AFFINE,coeff,Image.Resampling.NEAREST))>127
        raw=np.asarray(reference); valid=np.ones(raw.shape[:2],bool); border=round(min(reference.size)*.028)
        valid[:border]=False; valid[-border:]=False; valid[:,:border]=False; valid[:,-border:]=False
        valid=warp(valid)
        candidate,uncertain=extract(raw,radius=3.3,min_component=2)
        candidate=warp(candidate)&valid; uncertain=warp(uncertain)&valid
        cr=np.asarray(current); rr=np.asarray(aligned); cg=cr.mean(axis=2)
        water=(cg<60)|(rr.mean(axis=2)<60); band=ndi.binary_dilation(water,iterations=2)
        uncertain|=candidate&band; candidate&=~band
        gap=(cg>=130)&(cg<=185); distance=ndi.distance_transform_edt(~gap)
        near=candidate&(distance<=2); inside=candidate&~near&(cg>230)
        unknown=uncertain|(candidate&~near&~inside)
        overlay=cr.astype(float)
        for mask,color in [(near,(0,165,205)),(inside,(235,45,75)),(unknown,(215,157,15))]: overlay[mask]=.18*overlay[mask]+.82*np.array(color)
        yy,xx=np.indices(valid.shape); hatch=~valid&(((xx+yy)//10)%2==0)
        overlay[hatch]=.45*overlay[hatch]+.55*np.array([165,120,200])
        # Independent water check: shared black, reference-only pink, ours-only cyan.
        watercheck=np.full_like(cr,245); ours_water=cg<60; ref_water=(rr.mean(axis=2)<60)&valid
        watercheck[ours_water]=(0,165,205); watercheck[ref_water]=(235,45,100)
        watercheck[ours_water&ref_water]=(20,20,20); watercheck[~valid]=(235,225,245)
        out=args.output/key; out.mkdir()
        panels=[current,aligned,Image.fromarray(watercheck),Image.fromarray(overlay.astype('uint8'))]
        titles=['现有负空间整图','参考 demo 同框映射','水面检查：黑共同／粉参考／青我方',
                '配准不可靠：不判断漏路' if registration_warning else '路网候选覆盖：仅供诊断']
        citysheet=Image.new('RGB',(2480,2570),'#f7f7f5'); cd=ImageDraw.Draw(citysheet)
        cd.text((20,10),name+' 25 km｜水岸与路网覆盖（配准需人工核验）',font=font,fill='#222')
        for col,(im,title) in enumerate(zip(panels,titles)):
            y=125+row*710; x=20+col*705
            d.text((x,y-32),name+'｜'+title,font=small,fill='#222')
            overview.paste(im.resize((690,690),Image.Resampling.LANCZOS),(x,y))
            xx=20+(col%2)*1230; yy=90+(col//2)*1230
            cd.text((xx,yy-32),title,font=font,fill='#222')
            citysheet.paste(im.resize((1200,1200),Image.Resampling.LANCZOS),(xx,yy))
        citysheet.save(out/'comparison.png'); panels[3].save(out/'overlay.png'); panels[2].save(out/'water_check.png')
        aligned.save(out/'reference_aligned.png'); Image.fromarray(candidate.astype('uint8')*255).save(out/'candidate_mask.png')
        metadata=json.loads(mp.read_text())
        report={'city':key,'purpose':'diagnostic','verdict':'rerun' if registration_warning else 'human_review','registration':registration,
                'source_to_reference_affine':a.tolist(),'offset':t.tolist(),'bbox_wgs84':metadata.get('bbox_wgs84'),
                'valid_fraction':float(valid.mean()),'no_model_changed':True,
                'sources':[{'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in [cp,rp,mp]],
                'limitations':['Gray is not necessarily road.','Water mismatch prevents interpreting near-water road coverage.','No same-bbox assertion; only overlapping registered area.']}
        (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)); reports.append(report)
    overview.save(args.output/'three_cities_comparison.png')
    (args.output/'report.json').write_text(json.dumps(reports,ensure_ascii=False,indent=2))


if __name__=='__main__': main()
