"""Whole-frame PNG using the approved local C/negative-road experiment.

Explicit trusted snapshots only. No mesh, remote download, or production
default changes. Uses complete-frame source identities, never joins tile edges.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from shapely.geometry import box
from shapely.ops import unary_union
from shapely.strtree import STRtree
from tools.evaluate_urban_organization import load_checked
from tools.experiment_negative_road_width import cut_carrier, draw_negative, _polygon_parts
from aesthetic.road_identity_continuity import recover_identities, is_covered
from aesthetic.bridge_sources import extract_bridge_sources, water_bridge_lines, bridge_corridor
from aesthetic.city_surface_plan import _resolve_road_surfaces
from aesthetic.review_render import _Rasterizer, _rasterize_mask


def render(root, out, pixels=2400):
    root, out = Path(root), Path(out)
    out.mkdir(parents=True, exist_ok=False)
    start = time.monotonic()
    def progress(stage):
        record = {'status':'running', 'stage':stage, 'elapsed_seconds':time.monotonic()-start}
        (out/'status.json').write_text(json.dumps(record,ensure_ascii=False,indent=2))
        print(stage, round(record['elapsed_seconds'],2), flush=True)
    progress('加载已校验 C 街区与原始道路')
    manifest = json.loads((root/'capture_manifest.json').read_text())
    layers = load_checked(root/'C/layers.pkl')
    source = load_checked(root/'s5_input.pkl')['runtime']['sources'].roads
    scale = manifest['scale_mm_per_m']; core = box(*manifest['bbox_local_m'])
    rows = source.to_dict('records')
    progress('按完整源道路身份恢复连续性')
    local = list(layers.block_base_cut_lines); major = list(layers.block_base_major_cut_lines)
    from shapely.geometry import GeometryCollection
    restored, recovery = recover_identities(rows, GeometryCollection(local+major), progress=progress)
    covered = unary_union([r['geometry'] for r in rows if is_covered(r)]).buffer(.05)
    physical, bridge_source = extract_bridge_sources(rows)
    layers.bridge_lines = physical
    physical = water_bridge_lines(layers)
    allowed = bridge_corridor(physical,scale,.42,core)
    local += list(getattr(restored,'geoms',[restored])) if not restored.is_empty else []
    major += physical
    progress('恢复可追溯街区载体，执行半宽负空间裁切')
    # Intersection distributes over union/difference. Clip raw carriers to
    # non-overlapping cells BEFORE union; never union the whole connected
    # city plus all restored street reveals into one giant polygon.
    carriers = list(layers.block_base)+list(layers.BO)+list(layers.surface_road_reveals)
    hero_sources = [p for p,h in layers.BL]
    trees = {key:STRtree(values) for key,values in (
        ('carrier',carriers),('hero',hero_sources),('local',local),('major',major))}
    def selected(key, values, cell):
        return [values[int(i)] for i in trees[key].query(cell,predicate='intersects')]
    xmin,ymin,xmax,ymax = core.bounds
    nx,ny = math.ceil((xmax-xmin)/4000),math.ceil((ymax-ymin)/4000)
    city_parts,hero_parts,clearance,hero_clearance=[],[],[],[]
    for iy in range(ny):
        for ix in range(nx):
            cell=box(xmin+(xmax-xmin)*ix/nx,ymin+(ymax-ymin)*iy/ny,
                     xmin+(xmax-xmin)*(ix+1)/nx,ymin+(ymax-ymin)*(iy+1)/ny)
            progress(f'整图空间块 {iy*nx+ix+1}/{nx*ny}（不改变源道路身份）')
            carrier=unary_union([p.intersection(cell) for p in selected('carrier',carriers,cell)])
            heroes=unary_union([p.intersection(cell) for p in selected('hero',hero_sources,cell)])
            # Query a gap-sized halo; keep full lines so no artificial caps at cell edges.
            halo=cell.buffer(.42/scale)
            ll=selected('local',local,halo); mm=selected('major',major,halo)
            local_covered=covered.intersection(halo)
            c,ce=cut_carrier(carrier,ll,mm,scale,.28,.42)
            h,he=cut_carrier(heroes,[g.difference(local_covered) for g in ll],
                           [g.difference(local_covered) for g in mm],scale,.28,.42)
            # Use the exact verified bridge corridor for both the cut and
            # the visible bridge face, including short hairpin source ways.
            bridge_cell = allowed.intersection(cell)
            if not bridge_cell.is_empty:
                c = c.difference(bridge_cell)
                h = h.difference(bridge_cell)
            city_parts.extend(_polygon_parts(c)); hero_parts.extend(_polygon_parts(h))
            clearance.append(ce); hero_clearance.append(he)
    city=unary_union(city_parts); heroes=unary_union(hero_parts)
    progress('解析真实桥面；保留原始水岸与岛屿遮罩')
    profile = SimpleNamespace(surface_road_gap_mm=.28, final_block_base_gap_mm=.42,
                              min_surface_height_mm=.12)
    water = unary_union(list(layers.WL)+list(layers.WO))
    # Only the bridge faces need a new water occlusion mask here. Normal
    # roads are already represented by negative city space above.
    plan = SimpleNamespace(block_base_cut_lines=[],block_base_major_cut_lines=[],
        bridge_lines=physical,WL=list(layers.WL),WO=list(layers.WO),
        block_base=list(_polygon_parts(city)),BO=[],BL=[(heroes,1.)])
    bridge_plan = _resolve_road_surfaces(plan,core,scale,profile)
    resolved = unary_union(plan.surface_road_polygons)
    spans = unary_union(physical).intersection(water).intersection(core)
    missing = spans.difference(resolved.buffer(1e-6)).length
    if missing > 1e-5:
        (out/'bridge_failure.json').write_text(json.dumps({
            'missing_m':missing,
            'missing_wkt':spans.difference(resolved.buffer(1e-6)).wkt,
            'missing_from_corridor_m':spans.difference(allowed.buffer(1e-6)).length,
        },ensure_ascii=False,indent=2))
        (out/'status.json').write_text(json.dumps({'status':'failed','stage':'bridge_gate',
            'missing_bridge_water_length_m':missing}))
        raise ValueError(f'真实桥面缺失 {missing} m; refuse false-success PNG')
    raster = _Rasterizer(core.bounds,pixels*2,pixels*2)
    water_mask = _rasterize_mask(raster,list(layers.WL)+list(layers.WO)).astype(bool)
    old_reveals = _rasterize_mask(raster,list(layers.surface_road_polygons)).astype(bool)
    water_mask &= ~old_reveals
    bridge_mask = _rasterize_mask(raster,list(_polygon_parts(resolved.intersection(allowed)))).astype(bool)
    allowed_mask = _rasterize_mask(raster,list(_polygon_parts(allowed))).astype(bool)
    water_mask &= ~(bridge_mask & allowed_mask)
    progress('输出完整 PNG 与中文运行报告')
    target = out/'city_topdown.png'
    draw_negative(city,heroes,water,allowed,core.bounds,target,pixels=pixels,visible_water_mask=water_mask)
    (out/'city.wkb').write_bytes(city.wkb)
    (out/'bridge_surfaces.wkb').write_bytes(resolved.intersection(water).wkb)
    report = {'purpose':'diagnostic','verdict':'human_review', 'city':manifest['city'],
        '说明':'完整25km母范围；C街区、负空间道路、源身份连续性、真实桥面修复；仅PNG，不是3MF或发布验收。',
        'bbox_wgs84':manifest['bbox_wgs84'], 'bbox_local_m':manifest['bbox_local_m'],
        'scale_mm_per_m':scale, 'widths_mm':{'local':.28,'major_and_bridge':.42},
        'input_s5_sha256':(root/'s5_input.sha256').read_text().strip(),
        'input_C_sha256':(root/'C/layers.sha256').read_text().strip(),
        'bridge_source':bridge_source,'bridge_plan':bridge_plan,
        'bridge_water_source_length_m':spans.length,'missing_bridge_water_length_m':missing,
        'recovery':recovery,'city_clearance':clearance,'hero_clearance':hero_clearance,
        'png':str(target.resolve()),'png_sha256':hashlib.sha256(target.read_bytes()).hexdigest(),
        'pixels':pixels, 'elapsed_seconds':time.monotonic()-start,
        'limitations':['not a canonical full S0-S11 or print run',
                      'previously discarded carrier fragments cannot be reconstructed',
                      'flat semantic PNG, not terrain-height or oblique mesh validation']}
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    (out/'status.json').write_text(json.dumps({'status':'complete','png':str(target.resolve()),
        'elapsed_seconds':report['elapsed_seconds']},ensure_ascii=False,indent=2))
    print('完成',str(target),round(report['elapsed_seconds'],2),flush=True)
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--experiment-dir',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--pixels',type=int,default=2400)
    a=p.parse_args()
    render(a.experiment_dir,a.output,a.pixels)


if __name__=='__main__':main()
