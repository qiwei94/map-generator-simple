"""Measure local visible corridors in transformed reference geometry, in mm."""
import sys,json,hashlib
from pathlib import Path
import numpy as np
from scipy import ndimage as ndi
from PIL import Image,ImageDraw,ImageFont
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.render_reference_actual_mesh import meshes_from_file
from tools.diagnose_reference_green_surfaces import raster

def main():
 out=ROOT/'output/paris_readability_local_v3';path=ROOT.parent/'reference_archive/city_demo/巴黎/巴黎25Km城市肌理P.3mf'
 meshes=meshes_from_file(path,True)
 lo=np.min([v.min(0) for _,v,_,_ in meshes],axis=0);hi=np.max([v.max(0) for _,v,_,_ in meshes],axis=0)
 origin=np.r_[(lo[:2]+hi[:2])/2,0.];step=1/60
 tops=np.array([raster(v-origin,f,(-20,-20,20,20),step=step) for _,v,f,_ in meshes])
 owner=np.argmax(np.where(np.isfinite(tops),tops,-np.inf),axis=0)
 road_index=[i for i,(n,v,f,r) in enumerate(meshes) if n=='Object_3'][0]
 gray=owner==road_index
 white=np.isin(owner,[i for i,(n,v,f,r) in enumerate(meshes) if r=='urban_relief'])
 dist=ndi.distance_transform_edt(gray)
 ridge=gray&(dist>=4)&(dist<45)&(dist==ndi.maximum_filter(dist,size=3))
 ridge[:100]=False;ridge[-100:]=False;ridge[:,:100]=False;ridge[:,-100:]=False
 points=np.argwhere(ridge);np.random.default_rng(20260915).shuffle(points);records=[]
 gray_u8=gray.astype(np.uint8)
 angles=np.arange(0,np.pi,np.pi/90);radii=np.arange(0,91,.5)
 for y,x in points[::max(1,len(points)//1200)]:
  if any((x-r['x_px'])**2+(y-r['y_px'])**2<100**2 for r in records):continue
  lengths=[]
  for a in angles:
   ends=[]
   for sign in (-1,1):
    xx=x+sign*np.cos(a)*radii;yy=y+sign*np.sin(a)*radii
    vals=ndi.map_coordinates(gray_u8,[yy,xx],order=0)
    idx=np.flatnonzero(vals==0)
    if not len(idx):break
    j=idx[0];end=(float(xx[j]),float(yy[j]));xi,yi=np.round(end).astype(int)
    if not white[yi,xi]:break
    ends.append((radii[j],end))
   if len(ends)==2:lengths.append((sum(e[0] for e in ends),a,[e[1] for e in ends]))
  if not lengths:continue
  length,a,ends=min(lengths,key=lambda q:q[0])
  if length*step>1.2:continue
  records.append({'x_px':int(x),'y_px':int(y),'width_mm':length*step,'endpoints_px':ends})
  if len(records)>=60:break
 im=Image.open(out/'reference_center.png').convert('RGB');d=ImageDraw.Draw(im);font=ImageFont.truetype('/System/Library/Fonts/STHeiti Medium.ttc',32)
 show=[records[i] for i in np.linspace(0,len(records)-1,12).astype(int)]
 for i,r in enumerate(show,1):
  d.line([tuple(p) for p in r['endpoints_px']],fill='#ff3333',width=4)
  d.text((r['x_px']+8,r['y_px']-30),f"{i}: {r['width_mm']:.2f}",font=font,fill='#bd0000',stroke_width=1,stroke_fill='white')
 im.save(out/'reference_width_measurements.png')
 vals=[r['width_mm'] for r in records]
 report={'source':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'reference_xy_span_mm':(hi-lo)[:2].tolist(),
 'method':'Actual transformed topmost object raster at 60 px/mm. Object_3 gray corridors visually checked. Distance-ridge seeds, shortest angular transect bounded by white relief on both sides; excludes water endpoints. Spatially separated convenience samples, not street class statistics.',
 'crop_centered_mm':[-20,-20,20,20],'pixel_mm':step,'sample_count':len(records),'p10_p50_p90_mm':np.percentile(vals,[10,50,90]).tolist(),
 'sample_min_max_mm':[min(vals),max(vals)],'samples':records,'annotated_samples':show,
 'limits':['Local visible gap estimates; not nozzle line width or physical extrusion measurements.','Sampling excludes widths above 1.2mm and does not describe all roads.','Reference center crop is not geographically registered to candidate.','Raster endpoint uncertainty approximately 0.03mm; faceting/occlusion may add uncertainty.']}
 (out/'reference_width_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print({k:v for k,v in report.items() if k not in ['samples','annotated_samples']},flush=True)
if __name__=='__main__':main()
