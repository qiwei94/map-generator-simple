"""PNG-only gap sweep on frozen registered regions, sharing one cached S6 load."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from shapely.geometry import box
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.experiment_negative_road_width import WIDTHS, nearby, cut_carrier, draw_negative, _polygon_parts
from tools.evaluate_urban_organization import load_checked
from aesthetic.review_render import _Rasterizer, _rasterize_mask


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment-dir', type=Path, required=True)
    parser.add_argument('--registration', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repair-continuity', action='store_true',
                        help='Opt-in source identity recovery plus surface-road clearance of heroes')
    parser.add_argument('--only-width', choices=[w[0] for w in WIDTHS])
    parser.add_argument('--repair-bridges', action='store_true',
                        help='Hydrate physical bridge tags from frozen raw source; use S6 road surfaces')
    args = parser.parse_args()
    started = time.monotonic()
    registration = json.loads(args.registration.read_text())
    manifest = json.loads((args.experiment_dir/'capture_manifest.json').read_text())
    scale = manifest['scale_mm_per_m']
    refsource = registration['sources'][1]
    refpath = Path(refsource['path'])
    assert hashlib.sha256(refpath.read_bytes()).hexdigest() == refsource['sha256']
    ref = Image.open(refpath).convert('RGB')
    args.output.mkdir(exist_ok=False)
    layers = load_checked(args.experiment_dir/'C/layers.pkl')
    source_roads=None
    if args.repair_continuity or args.repair_bridges:
        from aesthetic.road_identity_continuity import recover_identities, is_covered
        source_roads=load_checked(args.experiment_dir/'s5_input.pkl')['runtime']['sources'].roads
    loaded = time.monotonic()-started
    all_retained = list(layers.block_base)+list(layers.BO)
    fontpath = '/System/Library/Fonts/STHeiti Medium.ttc'
    font, small = ImageFont.truetype(fontpath,32), ImageFont.truetype(fontpath,24)
    widths=[w for w in WIDTHS if not args.only_width or w[0]==args.only_width]
    columns = [(label,f'普通 {l:.2f} / 主干 {m:.2f} mm') for _,label,l,m in widths]
    columns.append(('参考 demo','沿用固定配准 · 原色'))
    sheet_width=840*len(columns)
    board = Image.new('RGB',(sheet_width,3*930+90),'#f7f7f5')
    bd = ImageDraw.Draw(board)
    bd.text((20,12),'巴黎局部道路留缝实验｜每行同一 3 km × 3 km，沿用 25 km 成品比例',font=font,fill='#222222')
    regions=[]
    print(f'缓存加载 {loaded:.2f} 秒；开始三个局部',flush=True)
    for row,region in enumerate(registration['regions']):
        begin=time.monotonic()
        core=box(*region['bbox_local_m']); work=core.buffer(250,join_style=2)
        retained=unary_union(nearby(all_retained,work)).intersection(work)
        removed=unary_union(nearby(layers.surface_road_reveals,work)).intersection(work)
        carrier=unary_union([retained,removed])
        heroes=unary_union(nearby([p for p,h in layers.BL],work)).intersection(work)
        water=unary_union(nearby(list(layers.WL)+list(layers.WO),work)).intersection(work)
        bridges=unary_union(nearby(layers.surface_road_polygons,work)).intersection(water)
        # Hold the canonical review renderer's ordered water/reveal masks
        # fixed. A unary union can change overlapping polygon/hole display.
        # This experiment must not silently become a water-rendering change.
        raster=_Rasterizer(core.bounds,2000,2000)
        water_stencil=_rasterize_mask(raster,[p for p in list(layers.WL)+list(layers.WO)
                                             if p.intersects(work)]).astype(bool)
        if (getattr(layers,'surface_plan_evidence',{}) or {}).get('status')=='finalized':
            reveal_stencil=_rasterize_mask(raster,[p for p in layers.surface_road_polygons
                                                  if p.intersects(work)]).astype(bool)
            water_stencil &= ~reveal_stencil
        local=nearby(layers.block_base_cut_lines,work)
        major=nearby(layers.block_base_major_cut_lines,work)
        bridge_sources=[]; bridge_evidence=None
        if args.repair_continuity or args.repair_bridges:
            ids=source_roads.sindex.query(work,predicate='intersects')
            rows=source_roads.iloc[sorted(int(i) for i in ids)].to_dict('records')
        if args.repair_bridges:
            from aesthetic.bridge_sources import extract_bridge_sources, water_bridge_lines, bridge_corridor
            from aesthetic.city_surface_plan import _resolve_road_surfaces
            bridge_sources,bridge_evidence=extract_bridge_sources(rows)
            bridge_sources=water_bridge_lines(SimpleNamespace(bridge_lines=bridge_sources, WL=[water], WO=[]))
            bridge_sources=[g.intersection(work) for g in bridge_sources]
            major=major+bridge_sources
        recovery_evidence=None; covered=box(0,0,0,0)
        if args.repair_continuity:
            # Bridge repair must not broaden the identity-recovery seeds.
            approved=unary_union(local+nearby(layers.block_base_major_cut_lines,work)).intersection(work)
            restored,recovery_evidence=recover_identities(rows,approved)
            # Clip to bounded working window before adding cuts. Do not
            # propagate city-wide identities or invent endpoint connectors.
            restored=restored.intersection(work)
            local=local+[restored] if not restored.is_empty else local
            covered=unary_union([r['geometry'] for r in rows if is_covered(r)]).buffer(.05)
            (args.output/f'{region["id"]}_recovered_source_routes.wkb').write_bytes(restored.wkb)
        results=[]; panels=[]; previous=None
        for name,label,l,m in widths:
            tick=time.monotonic()
            city,clearance=cut_carrier(carrier,local,major,scale,l,m)
            city=city.intersection(core)
            rendered_heroes=heroes
            hero_clearance=None
            if args.repair_continuity:
                # Explicitly covered/tunnel source runs are not carved into
                # landmarks. Other selected surface corridors own the gap.
                hero_local=[g.difference(covered) for g in local]
                hero_major=[g.difference(covered) for g in major]
                rendered_heroes,hero_clearance=cut_carrier(heroes,hero_local,hero_major,scale,l,m)
                selected_surface=unary_union(hero_local+hero_major).intersection(core)
                blocked=selected_surface.intersection(rendered_heroes).length
                if blocked>1e-5:
                    raise ValueError(f'Remaining hero obstruction {blocked} m')
            lost=previous.difference(city).area if previous is not None else 0.
            if lost > core.area/1000**2:
                raise ValueError(f'{region["id"]}/{name}: non-nested change exceeds one diagnostic pixel: {lost}')
            difference=city.symmetric_difference(retained.intersection(core)).area if name=='wide' and not args.repair_continuity else None
            if difference is not None and difference > 1e-6:
                raise ValueError(f'Baseline reconstruction mismatch {difference}')
            previous=city
            target=args.output/f'{region["id"]}_{name}.png'
            visible_water=water_stencil.copy(); bridge_plan=None
            if args.repair_bridges:
                planned=SimpleNamespace(block_base_cut_lines=local, block_base_major_cut_lines=major,
                    bridge_lines=bridge_sources, WL=[water], WO=[], block_base=[city], BO=[],
                    BL=[(rendered_heroes, 1.)])
                profile=SimpleNamespace(surface_road_gap_mm=l, final_block_base_gap_mm=m,
                                        min_surface_height_mm=.12)
                bridge_plan=_resolve_road_surfaces(planned, core, scale, profile)
                resolved=unary_union(planned.surface_road_polygons)
                allowed=bridge_corridor(bridge_sources,scale,m,core)
                road_mask=_rasterize_mask(raster,list(_polygon_parts(resolved.intersection(allowed)))).astype(bool)
                allowed_mask=_rasterize_mask(raster,list(_polygon_parts(allowed))).astype(bool)
                # Freeze the old shoreline rasterization. New water occlusion
                # comes only from the actual S6 bridge faces, not re-rasterized
                # ordinary bank corridors (which can shift an edge pixel).
                visible_water &= ~(road_mask & allowed_mask)
                unexpected=(water_stencil & ~visible_water) & ~allowed_mask
                if unexpected.any():
                    raise ValueError(f'Non-bridge water changed: {int(unexpected.sum())} pixels')
                measured=[]
                for br in bridge_sources:
                    span=br.intersection(water).intersection(core)
                    if span.length > 1e-7:
                        measured.append({'water_span_m':span.length,
                            'missing_surface_m':span.difference(resolved.buffer(1e-6)).length})
                bridge_plan.update(source_spans=measured,
                    missing_source_water_span_m=sum(x['missing_surface_m'] for x in measured),
                    unexpected_water_pixels=int(unexpected.sum()),
                    changed_water_pixels=int((water_stencil & ~visible_water).sum()))
                (args.output/f'{region["id"]}_{name}_bridge_surfaces.wkb').write_bytes(resolved.intersection(water).wkb)
            draw_negative(city,rendered_heroes,water,bridges,core.bounds,target,pixels=1000,
                          visible_water_mask=visible_water)
            (args.output/f'{region["id"]}_{name}.wkb').write_bytes(city.wkb)
            panels.append(Image.open(target).convert('RGB'))
            result={'id':name,'local_gap_mm':l,'major_gap_mm':m,
                'hero_clearance':hero_clearance,
                'bridge_plan':bridge_plan,
                'hero_removed_area_m2':heroes.intersection(core).area-rendered_heroes.intersection(core).area,
                'remaining_surface_centerline_hero_obstruction_m':blocked if args.repair_continuity else None,
                'ordinary_city_frame_coverage':city.area/core.area,
                'loss_from_wider_variant_m2':lost,'diagnostic_pixel_area_m2':core.area/1000**2,
                'wide_reconstruction_difference_m2':difference,'clearance':clearance,
                'elapsed_seconds':time.monotonic()-tick,'path':str(target.resolve()),
                'sha256':hashlib.sha256(target.read_bytes()).hexdigest()}
            results.append(result)
            print(region['id'],name,round(result['elapsed_seconds'],2),flush=True)
        quad=np.array(region['reference_pixel_quad']); step_x=(quad[1]-quad[0])/1000; step_y=(quad[3]-quad[0])/1000
        reference=ref.transform((1000,1000),Image.Transform.AFFINE,
            (step_x[0],step_y[0],quad[0,0],step_x[1],step_y[1],quad[0,1]),Image.Resampling.BICUBIC)
        reference.save(args.output/f'{region["id"]}_reference.png'); panels.append(reference)
        strip=Image.new('RGB',(sheet_width,930),'#f7f7f5'); d=ImageDraw.Draw(strip)
        for col,(panel,(title,subtitle)) in enumerate(zip(panels,columns)):
            d.text((col*840+20,10),f'{region["id"]} {region["label"]}｜{title}',font=font,fill='#222222')
            d.text((col*840+20,55),subtitle,font=small,fill='#555555')
            strip.paste(panel.resize((800,800),Image.Resampling.LANCZOS),(col*840+20,100))
        strip.save(args.output/f'{region["id"]}_width_comparison.png')
        board.paste(strip,(0,60+row*930))
        regions.append({'region':region,'local_cut_lines':len(local),'major_cut_lines':len(major),
            'identity_recovery':recovery_evidence,
            'bridge_source_evidence':bridge_evidence,
            'fixed_water_sha256':hashlib.sha256(water.wkb).hexdigest(),
            'fixed_water_stencil_sha256':hashlib.sha256(water_stencil.tobytes()).hexdigest(),
            'fixed_heroes_sha256':hashlib.sha256(heroes.wkb).hexdigest(),
            'fixed_bridges_sha256':hashlib.sha256(bridges.wkb).hexdigest(),
            'results':results,'elapsed_seconds':time.monotonic()-begin})
    note=('源 bridge 标签恢复桥面；同宽同范围。仅局部 PNG，非桥梁结构或打印验收。' if args.repair_bridges else
          '源道路身份恢复＋建筑让缝；固定半宽前后比较。参考有配准残差；仅 PNG，非打印验收。'
          if args.repair_continuity else '仅重新切缝；灰色也包含留空区域。参考有配准与分辨率差异；非打印验收。')
    bd.text((20,2850),note,font=small,fill='#555555')
    board.save(args.output/'three_regions_width_reference.png')
    report={'purpose':'diagnostic','verdict':'human_review','node':'controller Mac',
        'cache':'existing local C S6 snapshot; one load; no download',
        'registration':str(args.registration.resolve()),
        'registration_sha256':hashlib.sha256(args.registration.read_bytes()).hexdigest(),
        'input_sha256':(args.experiment_dir/'C/layers.sha256').read_text().strip(),
        'scale_mm_per_m':scale,'buffer_m':250,'load_seconds':loaded,'regions':regions,
        'experimental_continuity_repair':args.repair_continuity,
        'physical_bridge_repair':args.repair_bridges,
        'elapsed_seconds':time.monotonic()-started,'production_defaults_changed':False,
        'production_bridge_algorithm_updated':args.repair_bridges,
        'limitations':['Inherited C carrier holes and previously discarded fragments remain',
          'Water uses canonical ordered raster masks; bridge mode removes only verified S6 bridge faces',
          'Only negative road corridors vary, no road stroke overlays',
          'Reference raster is lower resolution than new candidate raster and has affine residuals',
          'Reference palette unchanged; not a printability test']}
    (args.output/'evidence.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'全部完成 {report["elapsed_seconds"]:.2f} 秒：{args.output.resolve()}',flush=True)


if __name__=='__main__':main()
