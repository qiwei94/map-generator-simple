"""Fixed complete Paris streets: width and bounded corner treatment, PNG only."""
from pathlib import Path
import sys, json, hashlib, time
import geopandas as gpd
from shapely.geometry import box, GeometryCollection
from PIL import Image, ImageDraw, ImageFont
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from _TEXTURE_STYLE_OF_DEEPSEEK.road_roles import select_block_partition_roads
from _TEXTURE_STYLE_OF_DEEPSEEK.buildings import ROAD_TIERS
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm,project_geodataframe
from tools.experiment_negative_road_width import cut_carrier,draw_negative
from aesthetic.block_edge_experiment import soften_block_edges

VARIANTS=[('A 当前',.28,.42,0),('B 收细',.20,.30,0),
          ('C 更细',.14,.21,0),('D B＋微切角',.20,.30,.04)]


def main():
    started=time.monotonic()
    out=ROOT/'output/paris_complete_street_finish_20260907_v3'
    out.mkdir(exist_ok=False)
    frame=bbox_to_utm(48.74355353,2.18153414,48.96964647,2.52286586)
    source=ROOT/'tmp/osmium_road_geometry_v1_ile-de-france-260902.osm.pbf-18e584daf7_48.7436_2.1815_48.9696_2.5229.geojson'
    roads=gpd.read_file(source,columns=['highway'])
    roads=project_geodataframe(roads,frame['utm_crs'],frame['origin'],clip_bbox=frame['utm_bbox'])
    scale=.007762010308
    board=Image.new('RGB',(3200,2640),'white')
    font=ImageFont.truetype('/System/Library/Fonts/STHeiti Medium.ttc',23)
    records=[]
    for row,(cx,cy) in enumerate([(0,0),(4500,3500),(-4500,-4000)]):
        roi=box(cx-1500,cy-1500,cx+1500,cy+1500)
        work=roi.buffer(100,join_style=2)
        local=roads.iloc[roads.sindex.query(work,predicate='intersects')].copy()
        complete,selection=select_block_partition_roads(local)
        lines=list(complete.geometry)
        major=list(complete.loc[complete.highway.isin(ROAD_TIERS[1])].geometry)
        fingerprint=hashlib.sha256(b''.join(g.wkb for g in lines)).hexdigest()
        cells=[]
        previous=None
        for col,(label,l,m,r) in enumerate(VARIANTS):
            tick=time.monotonic()
            urban,audit=cut_carrier(work,lines,major,scale,l,m)
            lost=0.
            if col<3:
                if previous is not None:
                    lost=previous.difference(urban).area
                    # Record non-monotone overlays, do not silently fix or
                    # label them numeric noise. They require geometry review.
                    print(f'{label} recut loss {lost:.8f} m2',flush=True)
                previous=urban
            edge=None
            if r:
                urban,edge=soften_block_edges(urban,scale=scale,radius_mm=r)
                assert edge['added_area_m2']<1e-6
                assert edge['input_blocks']==edge['output_blocks']
            urban=urban.intersection(roi)
            target=out/f'roi{row+1}_{"ABCD"[col]}.png'
            draw_negative(urban,GeometryCollection(),GeometryCollection(),GeometryCollection(),roi.bounds,target,pixels=1200)
            with Image.open(target) as im:
                board.paste(im.resize((800,800),Image.Resampling.LANCZOS),(col*800,row*880+80))
            draw=ImageDraw.Draw(board)
            draw.text((col*800+10,row*880+10),f'区域{row+1}｜{label}｜3 km',font=font,fill='black')
            draw.text((col*800+10,row*880+42),f'普通 {l:.2f} / 主干 {m:.2f} mm',font=font,fill='black')
            cells.append({'label':label,'gap_mm':[l,m],'road_fingerprint':fingerprint,
                'road_features':len(lines),'urban_fraction':urban.area/roi.area,
                'loss_from_wider_m2':lost,'overlay_tolerance_m2':roi.area/(1200*1200),
                'monotonic_area_gate':lost < roi.area/(1200*1200),
                'clearance':audit,'edge':edge,'seconds':time.monotonic()-tick})
        records.append({'bounds':list(roi.bounds),'selection':selection,'variants':cells})
        print(f'ROI {row+1} complete',flush=True)
    board.save(out/'comparison.png')
    (out/'evidence.json').write_text(json.dumps({'purpose':'diagnostic','verdict':'human_review',
        'scale_mm_per_m':scale,'city':'Paris','regions':records,'elapsed_seconds':time.monotonic()-started,
        'limitations':['Road-only carrier experiment, not a city render','No water or buildings in this isolation test',
            'No roads removed between variants','Sub-nozzle gaps are PNG candidates only',
            'Corner treatment is experimental; production defaults unchanged','Outer occupancy-grid boundary not tested']},ensure_ascii=False,indent=2))
    print(out,flush=True)

if __name__=='__main__':main()
