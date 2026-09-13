"""Same-scale 3 km road-only cuts: old backbone vs complete block streets."""
from pathlib import Path
import sys
import json
from time import monotonic
import geopandas as gpd
from shapely.geometry import box, GeometryCollection
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from _TEXTURE_STYLE_OF_DEEPSEEK.road_roles import select_block_partition_roads
from _TEXTURE_STYLE_OF_DEEPSEEK.buildings import ROAD_TIERS
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm, project_geodataframe
from tools.experiment_negative_road_width import cut_carrier, draw_negative


def main():
    started = monotonic()
    out = ROOT/'output/paris_complete_streets_3km_20260907'
    out.mkdir(exist_ok=False)
    frame = bbox_to_utm(48.74355353,2.18153414,48.96964647,2.52286586)
    source = ROOT/'tmp/osmium_road_geometry_v1_ile-de-france-260902.osm.pbf-18e584daf7_48.7436_2.1815_48.9696_2.5229.geojson'
    roads = gpd.read_file(source, columns=['highway'])
    roads = project_geodataframe(roads, frame['utm_crs'], frame['origin'], clip_bbox=frame['utm_bbox'])
    scale = .007762010308
    board = Image.new('RGB',(1600,2520),'white')
    font = ImageFont.truetype('/System/Library/Fonts/STHeiti Medium.ttc',24)
    records = []
    for row,(cx,cy) in enumerate([(0,0),(4500,3500),(-4500,-4000)]):
        roi = box(cx-1500,cy-1500,cx+1500,cy+1500)
        local = roads.iloc[roads.sindex.query(roi, predicate='intersects')].copy()
        complete, evidence = select_block_partition_roads(local)
        major = list(complete.loc[complete.highway.isin(ROAD_TIERS[1])].geometry)
        values = []
        for col,(label,lines) in enumerate([
            ('原主骨架',list(complete.loc[complete.highway.isin(ROAD_TIERS[2])].geometry)),
            ('完整街道分割',list(complete.geometry))]):
            tick = monotonic()
            urban, audit = cut_carrier(roi, lines, major, scale, .28, .42)
            path = out/f'roi{row+1}_{col}.png'
            draw_negative(urban,GeometryCollection(),GeometryCollection(),GeometryCollection(),roi.bounds,path,pixels=800)
            board.paste(Image.open(path),(col*800,row*840+40))
            ImageDraw.Draw(board).text((col*800+12,row*840+8),f'区域{row+1}｜{label}｜3 km',font=font,fill='black')
            values.append({'variant':label,'roads':len(lines),'components':len(getattr(urban,'geoms',[urban])),
                           'seconds':monotonic()-tick,'passed':audit['local']['passed']})
        records.append({'bounds_local_m':list(roi.bounds),'selection':evidence,'results':values})
        print(records[-1]['results'],flush=True)
    board.save(out/'comparison.png')
    (out/'evidence.json').write_text(json.dumps({'scope':'road-only planar cut; no building, water or print acceptance',
        'scale_mm_per_m':scale,'gap_mm':[.28,.42],'regions':records,'total_seconds':monotonic()-started},ensure_ascii=False,indent=2))
    print(out,flush=True)


if __name__=='__main__':
    main()
