"""Read actual slicer extrusion paths; never connects to a printer."""
from pathlib import Path
import re, json, hashlib
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.collections import LineCollection
from matplotlib.font_manager import FontProperties

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'output/microrelief_slice_20260906'

def main():
    p=OUT/'plate_1.gcode'; text=p.read_text()
    x=y=e=0.; z=None; relative=True; feature=''; paths=[]; width=.42
    for line in text.splitlines():
        if line.startswith('; Z_HEIGHT:'):z=float(line.split(':')[1])
        if line.startswith('; FEATURE:'):feature=line.split(':')[1].strip()
        if line.startswith('; LINE_WIDTH:'):width=float(line.split(':')[1])
        cmd=line.split(';')[0].strip()
        if cmd=='M83':relative=True
        if cmd=='M82':relative=False
        v={k:float(n) for k,n in re.findall(r'([XYZEIJ])(-?\d*\.?\d+)',cmd)}
        if cmd.startswith('G92'):e=v.get('E',e)
        if cmd.startswith(('G0 ','G1 ')):
            a,b=v.get('X',x),v.get('Y',y)
            de=v.get('E',0) if relative else v.get('E',e)-e
            e=v.get('E',e)
            if z is not None and feature!='Custom' and de>0 and (a!=x or b!=y):
                paths.append((x,y,a,b,z,width))
            x,y=a,b
        if cmd.startswith(('G2 ','G3 ')):
            a,b=v.get('X',x),v.get('Y',y)
            de=v.get('E',0) if relative else v.get('E',e)-e
            e=v.get('E',e)
            if de>0 and z is not None and feature!='Custom':
                cx,cy=x+v.get('I',0),y+v.get('J',0)
                t0=np.arctan2(y-cy,x-cx);t1=np.arctan2(b-cy,a-cx)
                sweep=(t1-t0)%(2*np.pi)
                if cmd.startswith('G2 '):sweep-=2*np.pi
                radius=np.hypot(x-cx,y-cy)
                count=max(2,int(np.ceil(abs(sweep)*radius/.02))+1)
                theta=np.linspace(t0,t0+sweep,count)
                pts=np.c_[cx+radius*np.cos(theta),cy+radius*np.sin(theta)]
                pts[0]=[x,y];pts[-1]=[a,b]
                paths.extend((u[0],u[1],v2[0],v2[1],z,width) for u,v2 in zip(pts[:-1],pts[1:]))
            x,y=a,b
    s=np.array(paths)
    # Infer global XY translation from symmetric first-layer footprint centers.
    first=s[np.isclose(s[:,4],.2)]
    points=first[:,:4].reshape(-1,2)
    manifest=json.loads((OUT/'manifest.json').read_text())
    bounds=np.array([i['bounds'] for i in manifest['samples']])
    delta=(points.min(0)+points.max(0))/2-(bounds[:,0,:2].min(0)+bounds[:,1,:2].max(0))/2
    s[:,[0,2]]-=delta[0];s[:,[1,3]]-=delta[1]
    font=FontProperties(fname='/System/Library/Fonts/STHeiti Medium.ttc')
    fig,axes=plt.subplots(2,4,figsize=(15,6.7),layout='constrained')
    metrics=[]
    for idx,sample in enumerate(manifest['samples']):
        lo,hi=np.array(sample['bounds']);mid=(s[:,:2]+s[:,2:4])/2
        selected=s[((mid>=lo[:2]) & (mid<=hi[:2])).all(1)]
        bylayer={}
        ax=axes.flat[idx]
        for z in sorted(set(selected[:,4])):
            seg=selected[np.isclose(selected[:,4],z)]
            length=np.linalg.norm(seg[:,2:4]-seg[:,:2],axis=1).sum()
            bylayer[f'{z:.2f}']={'extrusion_length_mm':float(length),'segments':len(seg)}
            if z>=1.16:
                color={1.16:'#c9cdd1',1.28:'#2987b8',1.4:'#ed732d'}[round(z,2)]
                lines=seg[:,:4].reshape(-1,2,2)-lo[:2]
                ax.add_collection(LineCollection(lines,colors=color,linewidths=.8))
        ax.set(xlim=(-.3,11.2),ylim=(-.3,7.2),aspect='equal')
        label='参考微起伏' if sample['kind']=='reference' else '同均高平面'
        ax.set_title(f'{label}｜基底 +{sample["phase_mm"]:.2f} mm',fontproperties=font,fontsize=12)
        ax.set_xlabel('mm'); ax.set_ylabel('mm')
        metrics.append({**sample,'layers':bylayer})
    fig.suptitle('西溪湿地微起伏：Bambu Studio 实际挤出走线\n灰：Z=1.16 mm　蓝：Z=1.28 mm　橙：Z=1.40 mm\n0.4 mm 喷嘴 · 0.12 mm 层高 · 首层 0.20 mm · 无熨烫；线条显示走线中心，不代表挤出宽度',fontproperties=font,fontsize=14)
    fig.savefig(OUT/'actual_toolpaths.png',dpi=160);plt.close(fig)
    config={k:re.search(r'^; '+k+r' = (.+)$',text,re.M).group(1) for k in ('layer_height','initial_layer_print_height','ironing_type','nozzle_diameter')}
    report={'gcode_sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'effective_settings':config,
            'global_xy_translation_mm':delta.tolist(),'coupons':metrics,
            'status':'slicing_evidence_only; physical_texture_requires_human_review'}
    (OUT/'slice_analysis.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    for c in metrics:print(c['kind'],c['phase_mm'],{z:round(d['extrusion_length_mm'],2) for z,d in c['layers'].items() if float(z)>=1.16})

if __name__=='__main__':main()
