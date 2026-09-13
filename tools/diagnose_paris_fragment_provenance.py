"""Diagnostic replay only: rank visual differences and observe frozen production cuts.

Does not modify generation policy. Hooks live only in this process. Replays
selected blocks with the production helpers and checks against actual output.
"""
from pathlib import Path
import sys, json, time, hashlib
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy import ndimage as ndi
import shapely
from shapely.geometry import box, GeometryCollection
from shapely.strtree import STRtree

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tools.experiment_negative_road_width import draw_negative
from aesthetic.building_mass_strategy import _parts, _safe_union
from aesthetic.organization_experiment import protected_open_spaces, organize_block

OUT=ROOT/'output/paris_C_fragment_provenance_20260907_v1'
CURRENT=ROOT/'output/paris_25km_complete_streets_C_20260907_v1/paris_25km_complete_streets_C_20260907_v1_topdown.png'
COMPARE=ROOT/'output/paris_C_reference_25km_20260907_v1'
FONT='/System/Library/Fonts/STHeiti Medium.ttc'
NAMES=['原始街区覆盖','有建筑占用的街区','扣开放空间','扣水体','扣地标占位',
       '大街区占用掩膜','核心建筑处理','正式挖缝前','正式挖缝后']


def local_union(polys, roi):
    return _safe_union([p.intersection(roi) for p in polys if p.intersects(roi)])


def measure(g):
    return {'area_m2':g.area,'components':len(_parts(g))}


def select_regions():
    current=Image.open(CURRENT).convert('RGB')
    ref=Image.open(COMPARE/'reference_aligned_25km.png').convert('RGB')
    valid=np.asarray(Image.open(COMPARE/'valid_mask.png'))>0
    cg=np.asarray(current).mean(axis=2); rg=np.asarray(ref).mean(axis=2)
    # Broad uniform missing-carrier appearance, excluding water. This ranks
    # investigation windows, not semantic error or an aesthetic score.
    gray=(cg>130)&(cg<185)
    broad=ndi.distance_transform_edt(gray)>3
    deviation=np.sqrt(np.maximum(0,ndi.uniform_filter(rg**2,11)-ndi.uniform_filter(rg,11)**2))
    score=broad*(deviation>8)*valid*(rg>65)
    manifest=json.loads((ROOT/'output/paris_organization_20260905_v1/capture_manifest.json').read_text())
    xmin,ymin,xmax,ymax=manifest['bbox_local_m']; w,h=current.size
    rx=round(3000/(xmax-xmin)*w); ry=round(3000/(ymax-ymin)*h)
    candidates=[]
    for y in range(ry//2,h-ry,ry//3):
        for x in range(rx//2,w-rx,rx//3):
            if valid[y:y+ry,x:x+rx].mean()<.99: continue
            candidates.append((float(score[y:y+ry,x:x+rx].mean()),x,y))
    candidates.sort(reverse=True)
    selected=[]
    for s,x,y in candidates:
        if all(abs(x-a[1])>=rx or abs(y-a[2])>=ry for a in selected):
            selected.append((s,x,y))
        if len(selected)==2: break
    selected.append((float('nan'),round(w/2-rx/2),round(h/2-ry/2)))
    overview=current.copy(); draw=ImageDraw.Draw(overview)
    regions=[]
    for i,(s,x,y) in enumerate(selected):
        rid=f'R{i+1}'
        bounds=[xmin+x/w*(xmax-xmin),ymax-(y+ry)/h*(ymax-ymin),
                xmin+(x+rx)/w*(xmax-xmin),ymax-y/h*(ymax-ymin)]
        regions.append({'id':rid,'bounds':bounds,'pixel_box':[x,y,x+rx,y+ry],
            'ranking_score':None if np.isnan(s) else s,
            'selection':'central control' if i==2 else 'largest broad-gray vs reference-texture discrepancy'})
        draw.rectangle((x,y,x+rx,y+ry),outline='#ff3636',width=5)
        draw.text((x,y),rid,font=ImageFont.truetype(FONT,40),fill='#ff3636')
        current.crop((x,y,x+rx,y+ry)).save(OUT/f'{rid}_current.png')
        ref.crop((x,y,x+rx,y+ry)).save(OUT/f'{rid}_reference.png')
    overview.save(OUT/'selected_regions.png')
    (OUT/'selection.json').write_text(json.dumps({'regions':regions,'ranking_candidates':len(candidates),
        'limitations':['Fixed affine registration; local alignment still uncertain',
         'Ranking targets broad gray versus reference texture, not every possible fragmentation mode'],
        'current_sha256':hashlib.sha256(CURRENT.read_bytes()).hexdigest()},ensure_ascii=False,indent=2))
    return regions


def main():
    OUT.mkdir(exist_ok=False)
    started=time.monotonic(); regions=select_regions(); traces={}
    import aesthetic.block_first as bf
    import aesthetic.city_surface_plan as surface
    import generate_city_legacy as engine
    from generate_model import canonical_arguments
    mass_original=bf.apply_block_first; final_original=surface.finalize_city_surfaces
    s6_original=engine.run_s6_building_roles

    def mass(layers,buildings,roads,water,bbox_local,*,printer_profile,scene_policy,sources,**kw):
        plan=scene_policy['block_first']; blocks=list(layers.city_blocks)
        support=bf.support_polygons(plan['oversized_block_support'])
        opens=protected_open_spaces(sources); waterpolys=list(layers.WL)+list(layers.WO)
        heroes=[p for p,h in layers.BL]
        exclusion_all=opens+waterpolys+heroes
        et=STRtree(exclusion_all); st=STRtree(support)
        for region in regions:
            roi=box(*region['bounds']); stages=[[] for _ in range(7)]
            block_ids=[]; cores=[]; oversized=[]
            for i,b in enumerate(blocks):
                if not b.intersects(roi): continue
                stages[0].append(b)
                if i not in plan['occupied_block_ids']: continue
                block_ids.append(i); stages[1].append(b)
                g=shapely.make_valid(b).difference(local_union(opens,b)); stages[2].extend(_parts(g))
                g=g.difference(local_union(waterpolys,b)); stages[3].extend(_parts(g))
                g=g.difference(local_union(heroes,b)); stages[4].extend(_parts(g))
                # Exact production union, not the diagnostic subtraction order.
                exc=_safe_union([exclusion_all[int(j)] for j in et.query(b)])
                allowed=shapely.make_valid(b).difference(exc)
                if b.area>plan['policy']['max_block_area_m2']:
                    oversized.append(i)
                    occupied=_safe_union([support[int(j)].intersection(b) for j in st.query(b)])
                    allowed=allowed.intersection(occupied)
                stages[5].extend(_parts(allowed))
                ids=plan['core_sources'].get(str(i))
                made=[]
                if ids:
                    cores.append(i); seeds=[]
                    for geom in buildings.iloc[ids].geometry: seeds.extend(_parts(geom))
                    made,_=organize_block(seeds,b,exc,variant='C',
                        scale=printer_profile.nozzle_diameter_mm/layers.nozzle_real_m,profile=printer_profile)
                stages[6].extend(made or _parts(allowed))
            traces[region['id']]={'geoms':[local_union(s,roi) for s in stages],
                'occupied_blocks':len(block_ids),'core_blocks':cores,'oversized_blocks':oversized}
        result=mass_original(layers,buildings,roads,water,bbox_local,printer_profile=printer_profile,
            scene_policy=scene_policy,sources=sources,**kw)
        for region in regions:
            trace=traces[region['id']]; roi=box(*region['bounds'])
            actual=local_union(list(layers.block_base)+list(layers.BO),roi)
            error=actual.symmetric_difference(trace['geoms'][-1]).area
            trace['replay_vs_actual_mass_difference_m2']=error
            if error>1.: raise ValueError(f'ROI replay mismatch: {error}')
        return result

    def finalize(layers,**kw):
        for region in regions:
            traces[region['id']]['geoms'].append(local_union(list(layers.block_base)+list(layers.BO),box(*region['bounds'])))
        result=final_original(layers,**kw)
        for region in regions:
            trace=traces[region['id']]; roi=box(*region['bounds'])
            trace['geoms'].append(local_union(list(layers.block_base)+list(layers.BO),roi))
        return result

    class Done(Exception): pass
    def s6(context,**kw):
        s6_original(context,**kw)
        raise Done()
    bf.apply_block_first=mass; surface.finalize_city_surfaces=finalize; engine.run_s6_building_roles=s6
    try:
        engine.main(canonical_arguments(['--bbox','48.74355353,2.18153414,48.96964647,2.52286586',
            '--pbf','pbf_cache/ile-de-france-260902.osm.pbf','--city','paris_fragment_diagnostic_20260907_v1',
            '--elevation-file','cache/srtm/N48E002.hgt','--no-snap','--amap-salience','cache','--no-vegetation',
            '--png','--review-png','--draft','--review-only','--urban-organization','block-first',
            '--surface-road-style','negative-space-fine-v1']))
    except Done: pass
    finally:
        bf.apply_block_first=mass_original; surface.finalize_city_surfaces=final_original; engine.run_s6_building_roles=s6_original
    font=ImageFont.truetype(FONT,22)
    records=[]
    for region in regions:
        rid=region['id']; trace=traces[rid]; geoms=trace.pop('geoms'); roi=box(*region['bounds'])
        board=Image.new('RGB',(2500,1100),'white'); d=ImageDraw.Draw(board)
        metrics=[]
        for j,g in enumerate(geoms):
            p=OUT/f'{rid}_stage{j}.png'; draw_negative(g,GeometryCollection(),GeometryCollection(),GeometryCollection(),roi.bounds,p,pixels=700)
            (OUT/f'{rid}_stage{j}.wkb').write_bytes(g.wkb)
            with Image.open(p) as im: board.paste(im.resize((500,500)),((j%5)*500,(j//5)*550+50))
            m=measure(g); m['stage']=NAMES[j]
            if j: m.update(removed_m2=geoms[j-1].difference(g).area,added_m2=g.difference(geoms[j-1]).area)
            metrics.append(m)
            d.text(((j%5)*500+8,(j//5)*550+5),f'{j} {NAMES[j]}',font=font,fill='black')
            d.text(((j%5)*500+8,(j//5)*550+28),f'覆盖 {g.area/roi.area:.1%}｜连通面 {m["components"]}',font=font,fill='black')
        with Image.open(OUT/f'{rid}_reference.png') as im: board.paste(im.resize((500,500)),(2000,600))
        d.text((2008,555),'参考 demo（近似配准）',font=font,fill='black')
        board.save(OUT/f'{rid}_stages.png')
        records.append({**region,**trace,'metrics':metrics})
    (OUT/'attribution.json').write_text(json.dumps({'purpose':'diagnostic','verdict':'human_review',
        'production_policy_changed':False,'regions':records,'elapsed_seconds':time.monotonic()-started,
        'limitations':['Removal attribution is ordered, overlapping exclusions are not additive independent effects',
        'Mass replay checked against actual production mass; final surface excludes separately rendered landmarks/water/bridges',
        'Diagnostic run intentionally stops after S6; its ledger may report interrupted/failed, not a gallery generation',
        'Core option C is building organization, distinct from visual gap choice C']},ensure_ascii=False,indent=2))
    print(OUT,flush=True)

if __name__=='__main__':main()
