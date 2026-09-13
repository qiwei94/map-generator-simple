"""Compare frozen half-gap PNGs before/after source-identity recovery."""
import argparse
import json
from pathlib import Path
from collections import Counter

from PIL import Image,ImageDraw,ImageFont
from shapely import wkb
from shapely.geometry import box,Point
from shapely.ops import unary_union
from tools.evaluate_urban_organization import load_checked
from tools.experiment_negative_road_width import nearby


def endpoints(lines,core):
    degree=Counter()
    for line in getattr(lines,'geoms',[lines]):
        if line.geom_type!='LineString':continue
        for xy in (line.coords[0],line.coords[-1]):
            degree[tuple(round(x,2) for x in xy)]+=1
    inside=core.buffer(-30)
    return sum(d==1 and inside.contains(Point(*xy)) for xy,d in degree.items())


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--before',type=Path,required=True)
    p.add_argument('--after',type=Path,required=True)
    a=p.parse_args()
    report=json.loads((a.after/'evidence.json').read_text())
    layers=load_checked(a.after.parent/'C/layers.pkl')
    fontpath='/System/Library/Fonts/STHeiti Medium.ttc'
    font=ImageFont.truetype(fontpath,32); small=ImageFont.truetype(fontpath,24)
    board=Image.new('RGB',(2520,3*930+80),'#f7f7f5'); d=ImageDraw.Draw(board)
    d.text((20,12),'道路连续性｜相同 3 km 窗口、相同半宽：普通 0.28 / 主干 0.42 mm',font=font,fill='#222222')
    metrics=[]
    for i,r in enumerate(report['regions']):
        rid=r['region']['id']; core=box(*r['region']['bbox_local_m'])
        before=unary_union(nearby(list(layers.block_base_cut_lines)+list(layers.block_base_major_cut_lines),core)).intersection(core)
        recovered=wkb.loads((a.after/f'{rid}_recovered_source_routes.wkb').read_bytes()).intersection(core)
        after=unary_union([before,recovered])
        metric={'id':rid,'before_interior_dangling_nodes':endpoints(before,core),
                'after_interior_dangling_nodes':endpoints(after,core),
                'recovered_extra_source_length_core_m':recovered.difference(before.buffer(.05)).length}
        metrics.append(metric)
        print(metric,flush=True)
        row=Image.new('RGB',(2520,930),'#f7f7f5'); rd=ImageDraw.Draw(row)
        for j,(label,path) in enumerate([
            ('修复前',a.before/f'{rid}_medium.png'),
            ('连续性修复后',a.after/f'{rid}_medium.png'),
            ('参考 demo',a.after/f'{rid}_reference.png')]):
            rd.text((j*840+20,10),f'{rid} {r["region"]["label"]}｜{label}',font=font,fill='#222222')
            rd.text((j*840+20,55),'3 km × 3 km · 固定位置与比例',font=small,fill='#555555')
            with Image.open(path) as im:
                row.paste(im.resize((800,800),Image.Resampling.LANCZOS),(j*840+20,100))
        row.save(a.after/f'{rid}_before_after_reference.png')
        board.paste(row,(0,60+i*930))
    d.text((20,2835),'局部实验：恢复源数据中的相连道路身份，处理建筑遮缝；水体不改。参考仍有配准残差；不代表打印验收。',font=small,fill='#555555')
    board.save(a.after/'before_after_reference.png')
    (a.after/'continuity_comparison.json').write_text(json.dumps({
        'verdict':'human_review','purpose':'diagnostic','regions':metrics,
        'endpoint_metric_limit':'Degree-1 nodes excluding 30 m border; real dead ends also counted, not error count',
        'method':'source-only identity recovery; no invented connecting geometry',
        'unchanged':'region centers, gap widths, water stencil, palette, reference registration',
        'scope':'local PNG only; no deployment; not all roads or printability accepted'},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
