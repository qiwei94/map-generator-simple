"""Read-only source/raster investigation; reference appearance is not ground truth."""
from pathlib import Path
import sys,json,hashlib
import numpy as np
import geopandas as gpd
from scipy import ndimage as ndi
from PIL import Image,ImageDraw,ImageFont
from shapely.geometry import box
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
from _TEXTURE_STYLE_OF_DEEPSEEK.road_roles import select_block_partition_roads
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm,project_geodataframe

def main():
    out=ROOT/'output/paris_extra_seams_20260907_v1';out.mkdir(exist_ok=False)
    base=ROOT/'output/paris_C_reference_25km_20260907_v1'
    cp=ROOT/'output/paris_25km_complete_streets_C_20260907_v1/paris_25km_complete_streets_C_20260907_v1_topdown.png'
    current=Image.open(cp).convert('RGB'); ref=Image.open(base/'reference_aligned_25km.png').convert('RGB')
    cg=np.asarray(current).mean(2); rg=np.asarray(ref).mean(2)
    refgap=(rg>100)&(rg<190); cgap=(cg>130)&(cg<190)
    rd=ndi.distance_transform_edt(~refgap); cd=ndi.distance_transform_edt(~cgap)
    water=ndi.binary_dilation((cg<60)|(rg<60),iterations=3)
    manifest=json.loads((ROOT/'output/paris_organization_20260905_v1/capture_manifest.json').read_text())
    xmin,ymin,xmax,ymax=manifest['bbox_local_m']; w,h=current.size
    def pixel(x,y): return ((np.asarray(x)-xmin)/(xmax-xmin)*w,(ymax-np.asarray(y))/(ymax-ymin)*h)
    frame=bbox_to_utm(*manifest['bbox_wgs84'])
    source=ROOT/'tmp/osmium_road_geometry_v1_ile-de-france-260902.osm.pbf-18e584daf7_48.7436_2.1815_48.9696_2.5229.geojson'
    raw=gpd.read_file(source,columns=['name','highway','oneway','bridge','tunnel'])
    roads=project_geodataframe(raw,frame['utm_crs'],frame['origin'],clip_bbox=frame['utm_bbox'])
    regions=[('北侧密集街区',[-2800,-100,200,2900]),('市中心河岸',[-1500,-1500,1500,1500])]
    font=ImageFont.truetype('/System/Library/Fonts/STHeiti Medium.ttc',24)
    board=Image.new('RGB',(2400,1800),'white');draw=ImageDraw.Draw(board); reports=[]
    for row,(label,bounds) in enumerate(regions):
        roi=box(*bounds); local=roads.iloc[roads.sindex.query(roi,predicate='intersects')]
        chosen,selection=select_block_partition_roads(local)
        samples=[]
        for idx,entry in chosen.iterrows():
            geom=entry.geometry.intersection(roi)
            if geom.is_empty or geom.length<30:continue
            ds=np.arange(5,geom.length,10.)
            points=[geom.interpolate(float(d)) for d in ds]
            xs,ys=pixel([p.x for p in points],[p.y for p in points]); xs=np.clip(xs.astype(int),0,w-1); ys=np.clip(ys.astype(int),0,h-1)
            samples.append((idx,entry,geom,xs,ys))
        # Translation sensitivity: use only declared main streets. No fit to
        # the questionable local streets; don't elastically force agreement.
        main=[s for s in samples if s[1].highway in ['primary','secondary']]
        xx=np.concatenate([s[3] for s in main]); yy=np.concatenate([s[4] for s in main])
        alignment=[]
        for dy in range(-4,5):
            for dx in range(-4,5):
                values=rd[np.clip(yy+dy,0,h-1),np.clip(xx+dx,0,w-1)]
                alignment.append((float(np.minimum(values,6).mean()),dx,dy))
        best=min(alignment);dx,dy=best[1:]
        records=[]
        for idx,entry,geom,xs,ys in samples:
            xx=np.clip(xs+dx,0,w-1);yy=np.clip(ys+dy,0,h-1)
            valid=~water[ys,xs]&~water[yy,xx]
            ours=(cd[ys,xs]<=1)&valid
            # A stable interior candidate is far from reference gray under
            # both unchanged affine and the bounded main-road shift.
            stable=(rd[ys,xs]>2)&(rd[yy,xx]>2)&(rg[ys,xs]>200)&(rg[yy,xx]>200)&ours
            n=int(stable.sum())
            records.append({'source_row':int(idx),'name':str(entry.get('name') or '(unnamed)'),
                'highway':entry.highway,'oneway':str(entry.get('oneway')),'length_m':geom.length,
                'sampled_visible_ours_m':int(ours.sum())*10,'stable_reference_interior_m':n*10,
                'candidate_fraction':n/max(1,int(ours.sum())),'geometry':geom})
        records.sort(key=lambda x:x['stable_reference_interior_m'],reverse=True)
        px0,py0=pixel(bounds[0],bounds[3]);px1,py1=pixel(bounds[2],bounds[1]);crop=(round(px0),round(py0),round(px1),round(py1))
        oursimg=current.crop(crop).resize((800,800)); refimg=ref.crop(crop).resize((800,800)); marked=oursimg.copy();md=ImageDraw.Draw(marked)
        for num,r in enumerate(records[:6],1):
            geom=r['geometry'];parts=list(geom.geoms) if hasattr(geom,'geoms') else [geom]
            for part in parts:
                pts=[((x-bounds[0])/3000*800,(bounds[3]-y)/3000*800) for x,y in part.coords]
                md.line(pts,fill='#f04444',width=3)
            p=geom.interpolate(.5,normalized=True)
            md.text(((p.x-bounds[0])/3000*800,(bounds[3]-p.y)/3000*800),str(num),font=font,fill='#0044ff')
            r['marker']=num
        for col,(im,title) in enumerate([(oursimg,'C 原图'),(refimg,'参考：固定配准'),(marked,'候选边界（不是确认错误）')]):
            board.paste(im,(col*800,row*900+70));draw.text((col*800+8,row*900+10),f'{label}｜{title}',font=font,fill='black')
        total={}
        for r in records:
            t=total.setdefault(r['highway'],{'length_m':0.,'candidate_m':0})
            t['length_m']+=r['length_m'];t['candidate_m']+=r['stable_reference_interior_m']
            r.pop('geometry')
        reports.append({'label':label,'bounds_local_m':bounds,'selection':selection,
            'best_main_road_translation_pixels':[dx,dy],'alignment_objective':best[0],
            'class_summary':total,'candidates':records})
        print(label,records[:6],flush=True)
    board.save(out/'comparison.png')
    (out/'report.json').write_text(json.dumps({'purpose':'diagnostic','verdict':'human_review','regions':reports,
        'parameters':{'sample_step_m':10,'reference_gray':[100,190],'reference_interior_distance_px':2,'shift_search_px':4},
        'current_sha256':hashlib.sha256(cp.read_bytes()).hexdigest(),
        'limitations':['Candidate lengths are sampled estimates, not surveyed absent roads',
        'Reference raster styles and registration may create false candidates','Full source line is drawn for identification, only sampled interiors count',
        'No production geometry, source roads or defaults changed']},ensure_ascii=False,indent=2))
    print(out,flush=True)

if __name__=='__main__':main()
