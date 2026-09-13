"""Same-source local C kernel vs occupancy/edge-style PNG experiments."""
import argparse,json,time,hashlib,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from shapely.geometry import box
from shapely.ops import unary_union
from shapely.strtree import STRtree
from PIL import Image,ImageDraw,ImageFont
from tools.evaluate_urban_organization import load_checked
from tools.experiment_negative_road_width import nearby,cut_carrier,draw_negative,_polygon_parts
from aesthetic.organization_experiment import organize_block,protected_open_spaces
from aesthetic.block_occupancy import occupancy_support,inward_brick_edges,OccupancyPolicy
from aesthetic.bridge_sources import extract_bridge_sources,water_bridge_lines,bridge_corridor
from types import SimpleNamespace


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--root',type=Path,required=True);ap.add_argument('--output',type=Path,required=True)
    a=ap.parse_args();a.output.mkdir(parents=True,exist_ok=False)
    start=time.monotonic(); data=load_checked(a.root/'s5_input.pkl');layers=data['layers'];r=data['runtime']
    regions=json.loads((a.root/'registered_roi_3km_v1/registration.json').read_text())['regions']
    scale=r['scale_mm_per_m'];profile=r['printer_profile'];sources=r['sources']
    policy=OccupancyPolicy(support_mm=profile.min_colored_strip_mm/2)
    spaces=protected_open_spaces(sources)+list(layers.WL)+list(layers.WO)+[p for p,h in layers.BL]
    tree=STRtree(spaces);btree=STRtree(layers.city_blocks);buildings=sources.buildings
    manifest=dict(purpose='diagnostic',verdict='human_review',scale_mm_per_m=scale,node='controller',
        input_sha256=(a.root/'s5_input.sha256').read_text().strip(),load_seconds=time.monotonic()-start,
        limitations=['C内核统一对照，不含生产的稀疏区域B路由及完整S6。','耗时仅局部二维，不代表整城加速比。',
                     '参考沿用水体仿射配准，存在残差；不证明参考使用随机算法。','未生成3MF；切角需后续切片验证。'],regions=[])
    font=ImageFont.truetype('/System/Library/Fonts/STHeiti Medium.ttc',23)
    sheet=Image.new('RGB',(2400,3*690),'#f6f6f3');draw=ImageDraw.Draw(sheet)
    for row,reg in enumerate(regions):
        core=box(*reg['bbox_local_m']);work=core.buffer(200,join_style=2)
        ids=buildings.sindex.query(work,predicate='intersects');raw=list(buildings.iloc[ids].geometry)
        seeds=STRtree(raw);blocks=[layers.city_blocks[int(i)].intersection(work) for i in btree.query(work,predicate='intersects')]
        excluded=unary_union([spaces[int(i)].intersection(work) for i in tree.query(work,predicate='intersects')])
        t=time.monotonic();support,ev=occupancy_support(raw,work.bounds,scale,policy);occupancy_seconds=time.monotonic()-t
        exact=[];fast=[];t=time.monotonic()
        for block in blocks:
            for part in _polygon_parts(block):
                rows=[raw[int(i)] for i in seeds.query(part,predicate='intersects')]
                if not rows:continue
                made,_=organize_block(rows,part,excluded,variant='C',scale=scale,profile=profile)
                exact.extend(made)
        exact_seconds=time.monotonic()-t;t=time.monotonic()
        for block in blocks:
            fast.extend(_polygon_parts(block.intersection(support).difference(excluded)))
        fast_seconds=time.monotonic()-t+occupancy_seconds
        local=nearby(layers.block_base_cut_lines,work);major=nearby(layers.block_base_major_cut_lines,work)
        water=unary_union(nearby(list(layers.WL)+list(layers.WO),work))
        rid=sources.roads.sindex.query(work,predicate='intersects')
        rows=sources.roads.iloc[rid].to_dict('records')
        from aesthetic.road_identity_continuity import recover_identities
        restored,recovery=recover_identities(rows,unary_union(local+major))
        local+=list(getattr(restored,'geoms',[restored])) if not restored.is_empty else []
        br,_=extract_bridge_sources(rows);br=water_bridge_lines(SimpleNamespace(bridge_lines=br,WL=[water],WO=[]))
        major+=br;bridges=bridge_corridor(br,scale,.42,core)
        heroes=unary_union(nearby([p for p,h in layers.BL],work)).intersection(core)
        heroes,_=cut_carrier(heroes,local,major,scale,.28,.42)
        ready=[];cuts=[]
        for polys in (exact,fast):
            candidate,_=cut_carrier(unary_union(polys),local,major,scale,.28,.42)
            ready.append(candidate.intersection(core))
        t=time.monotonic();edged=[]
        for p in _polygon_parts(ready[1]):
            g,e=inward_brick_edges(p,scale,policy);edged.append(g);cuts.append(e)
        ready.append(unary_union(edged));edge_seconds=time.monotonic()-t
        for col,(title,g) in enumerate(zip(['精确建筑合并 C 内核','占用支撑街区','占用街区＋向内切角'],ready)):
            file=a.output/f'{reg["id"]}_{col}.png';draw_negative(g,heroes,water,bridges,core.bounds,file,pixels=1000)
            sheet.paste(Image.open(file).resize((580,580)),(col*600+10,row*690+80))
            draw.text((col*600+10,row*690+15),reg['label']+'｜'+title,font=font,fill='#222')
            (a.output/f'{reg["id"]}_{col}.wkb').write_bytes(g.wkb)
        ref=a.root/'registered_roi_bridges_v2'/f'{reg["id"]}_reference.png'
        sheet.paste(Image.open(ref).convert('RGB').resize((580,580)),(1810,row*690+80))
        draw.text((1810,row*690+15),'参考 demo｜原色',font=font,fill='#222')
        evidence=dict(region=reg,buildings=len(raw),blocks=len(blocks),occupancy=ev,
            exact_kernel_seconds=exact_seconds,occupancy_kernel_seconds=fast_seconds,edge_seconds=edge_seconds,
            area_m2=[g.area for g in ready],edge_corner_count=sum(e['accepted_corners'] for e in cuts),
            outward_area_m2=ready[2].difference(ready[1]).area,
            reference_sha256=hashlib.sha256(ref.read_bytes()).hexdigest())
        evidence['road_identity_recovery']=recovery
        manifest['regions'].append(evidence);print(reg['id'],evidence,flush=True)
    sheet.save(a.output/'comparison.png');(a.output/'report.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
