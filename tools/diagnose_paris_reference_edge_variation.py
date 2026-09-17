from pathlib import Path
import sys,json
import numpy as np
from PIL import Image,ImageDraw,ImageFont
from scipy.ndimage import map_coordinates
ROOT=Path('/Users/kiwi/Documents/ChatGPT/for_better_map/map-generator-simple');sys.path.insert(0,str(ROOT))
from tools.render_reference_actual_mesh import meshes_from_file,render
from tools.diagnose_reference_green_surfaces import raster
out=ROOT/'output/paris_reference_edge_check_v1';out.mkdir(exist_ok=True)
m=meshes_from_file(ROOT.parent/'reference_archive/city_demo/巴黎/巴黎25Km城市肌理P.3mf',True)
lo=np.min([v.min(0) for _,v,_,_ in m],0);hi=np.max([v.max(0) for _,v,_,_ in m],0);origin=np.r_[(lo[:2]+hi[:2])/2,0.]
roi=(1400/60-20,20-810/60,1660/60-20,20-490/60);step=1/240
render(m,out/'shaded.png',90,pixel_size=1040,crop=roi)
tops=np.array([raster(v-origin,f,roi,step=step) for _,v,f,_ in m]);owner=np.argmax(np.where(np.isfinite(tops),tops,-np.inf),0)
gray=owner==2;white=(owner==0)|(owner==1)
a=np.zeros((*owner.shape,3),dtype=np.uint8);a[gray]=160;a[white]=245
flat=Image.fromarray(a);flat.save(out/'unlit_geometry.png')
marked=flat.copy();d=ImageDraw.Draw(marked);font=ImageFont.truetype('/System/Library/Fonts/STHeiti Medium.ttc',25)
center=np.array([(1506-1400)*4,(611-490)*4]);normal=np.array([np.cos(np.deg2rad(4)),np.sin(np.deg2rad(4))]);tangent=np.array([-normal[1],normal[0]])
records=[]
for i,offset in enumerate(np.arange(-35,36,10)):
 p=center+tangent*offset*4;ends=[]
 for sign in (-1,1):
  r=np.arange(0,180,.5);q=p[None,:]+sign*r[:,None]*normal
  vals=map_coordinates(gray.astype(np.uint8),[q[:,1],q[:,0]],order=0);hits=np.flatnonzero(vals==0)
  if not len(hits):break
  j=hits[0];end=q[j];xi,yi=np.round(end).astype(int)
  if not white[yi,xi]:break
  ends.append((r[j],end))
 if len(ends)!=2:continue
 width=sum(e[0] for e in ends)*step
 d.line([tuple(e[1]) for e in ends],fill='#dd3030',width=3);d.text(tuple(ends[-1][1]+[18,-13]),f'{width:.3f} mm',font=font,fill='#b00000')
 records.append({'along_road_offset_mm':float(offset/60),'width_mm':width})
marked.save(out/'cross_sections.png')
ims=[Image.open(out/'shaded.png'),flat,marked];h=flat.height
sheet=Image.new('RGB',(3120,h+110),'#f5f5f2');d=ImageDraw.Draw(sheet)
for i,(im,title) in enumerate(zip(ims,['参考实际网格 · 有光照','同一几何 · 去掉光照','同一直路局部 · 横断面间距'])):
 sheet.paste(im,(1040*i,70));d.text((1040*i+15,20),title,font=font,fill='#222222')
sheet.save(out/'comparison.png')
(out/'report.json').write_text(json.dumps({'source':'reference_archive/city_demo/巴黎/巴黎25Km城市肌理P.3mf','model_crop_mm':roi,'pixel_mm':step,'sections':records,'limits':['Fixed transverse direction on a visually straight road segment; not a road centerline reconstruction.','Widths are top-visible gray corridor between white objects; cannot identify author intent or algorithm.','Includes possible neighborhood junction effect; not generalizable width statistics.']},indent=2));print(records)
