"""Bounded pre-S6 planar width experiment; does not change production defaults."""
import argparse, json, pickle, sys
from pathlib import Path
import manifold3d as m
from PIL import Image, ImageDraw, ImageFont
from shapely.geometry import box, GeometryCollection
from shapely.ops import unary_union
from shapely import affinity, set_precision
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tools.experiment_negative_road_width import nearby, cut_carrier, draw_negative
from _TEXTURE_STYLE_OF_DEEPSEEK.block_base import _polygon_parts
from _TEXTURE_STYLE_OF_DEEPSEEK._geom_utils import shapely_poly_to_crosssection, manifold64_to_mesh
from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--line-width-comparison', action='store_true')
    parser.add_argument('--sparse-road-comparison', action='store_true')
    parser.add_argument('--balanced-road-comparison', action='store_true')
    args=parser.parse_args()
    folder='paris_readability_local_v2' if args.line_width_comparison else 'paris_readability_local_v1'
    if args.sparse_road_comparison: folder='paris_readability_local_v3'
    if args.balanced_road_comparison: folder='paris_readability_local_v4'
    out=ROOT/'output'/folder;out.mkdir(exist_ok=True)
    source=ROOT/'output/paris_BC_full_20260914_v9_inputs/surface_inputs.pkl'
    layers,kw=pickle.load(source.open('rb')) # trusted local pipeline snapshot
    scale=kw['scale'];core=box(-3000,-1800,1500,2700);work=core.buffer(250)
    def region(items):return unary_union(nearby(list(items),work)).intersection(work)
    carrier=set_precision(region(list(layers.block_base)+list(layers.BO)), .001)
    heroes=region([p for p,h in layers.BL]).intersection(core)
    water=region(list(layers.WL)+list(layers.WO)).intersection(core)
    parks=region(list(layers.VL)+list(layers.VO))
    local=nearby(layers.block_base_cut_lines,work);major=nearby(layers.block_base_major_cut_lines,work)
    # Closing only proposes infill outside the original carrier. Protected
    # vegetation/water/landmarks cannot acquire new infill. Recut every road.
    closed=set_precision(carrier.buffer(.06/scale,join_style=2).buffer(-.06/scale,join_style=2), .001)
    additions=closed.difference(carrier).difference(unary_union([parks,water,heroes]))
    merged=unary_union([carrier,additions])
    print(f'ROI local={len(local)} major={len(major)}',flush=True)
    selection={}
    chosen=local
    if args.sparse_road_comparison or args.balanced_road_comparison:
        from collections import defaultdict, Counter
        source_roads=kw['source_roads']
        roi_source=source_roads.iloc[source_roads.sindex.query(work,predicate='intersects')]
        tags={r.geometry.wkb:(str(r.highway),str(r['name'] or '')) for _,r in roi_source.iterrows()}
        tunnels={r.geometry.wkb for _,r in roi_source.iterrows() if str(r.get('tunnel') or '').lower() not in ('','none','nan','no','false','0')}
        main_types={'primary','primary_link','secondary','secondary_link','tertiary','tertiary_link','trunk','motorway'}
        chosen=[g for g in local if tags.get(g.wkb,('', ''))[0] in main_types and g.wkb not in tunnels]
        groups=defaultdict(list)
        for g in local:
            h,n=tags.get(g.wkb,('', ''))
            if h not in main_types and n and n not in ('None','nan') and g.wkb not in tunnels:
                groups[n].append(g)
        occupied=unary_union(chosen).buffer(.8/scale)
        selected_names=[]
        for n,parts in sorted(groups.items(),key=lambda pair:sum(g.length for g in pair[1]),reverse=True):
            route=unary_union(parts)
            if route.length>=600 and route.difference(occupied).length/route.length>=.55:
                chosen.extend(parts);selected_names.append(n)
                occupied=unary_union([occupied,route.buffer(.8/scale)])
        selection={'policy':'all primary/secondary/tertiary; named minor route groups >=600m and >=55% length outside 0.8mm spacing buffer',
                   'before_features':len(local),'after_features':len(chosen),'retained_minor_names':selected_names,
                   'before_classes':dict(Counter(tags.get(g.wkb,('unmatched',''))[0] for g in local)),
                   'after_classes':dict(Counter(tags.get(g.wkb,('unmatched',''))[0] for g in chosen)),
                   'local_length_before_m':sum(g.intersection(core).length for g in local),
                   'local_length_after_m':sum(g.intersection(core).length for g in chosen)}
        print(selection,flush=True)
        if args.balanced_road_comparison:
            # E strictly extends D's selected road set; no previously selected
            # named group can be displaced by a different greedy ordering.
            expanded=list(chosen)
            occupied=unary_union(expanded).buffer(.55/scale)
            extra_names=[]
            for n,parts in sorted(groups.items(),key=lambda pair:sum(g.length for g in pair[1]),reverse=True):
                if n in selected_names: continue
                route=unary_union(parts)
                if route.length>=400 and route.difference(occupied).length/route.length>=.40:
                    expanded.extend(parts);extra_names.append(n)
                    occupied=unary_union([occupied,route.buffer(.55/scale)])
            assert {g.wkb for g in chosen}.issubset({g.wkb for g in expanded})
            selection={'D':selection,'E':{'policy':'retain D, add whole named minor groups >=400m with >=40% length outside 0.55mm spacing buffer',
                'after_features':len(expanded),'added_minor_names':extra_names,
                'after_classes':dict(Counter(tags.get(g.wkb,('unmatched',''))[0] for g in expanded)),
                'local_length_after_m':sum(g.intersection(core).length for g in expanded)}}
            print(selection['E'],flush=True)
    rows=[]
    variants=[('B',merged,.14,.42),('C',merged,.42,.42)] if args.line_width_comparison else [('A',carrier,.14,.21),('B',merged,.14,.42)]
    if args.sparse_road_comparison: variants=[('C',merged,.42,.42),('D',merged,.42,.42)]
    if args.balanced_road_comparison: variants=[('D',merged,.42,.42),('E',merged,.30,.42)]
    for name,geom,local_gap,gap in variants:
        roads=expanded if name=='E' else chosen if name=='D' else local
        (out/f'{name}_local_roads.wkb').write_bytes(GeometryCollection(roads).wkb)
        (out/f'{name}_major_roads.wkb').write_bytes(GeometryCollection(major).wkb)
        city,evidence=cut_carrier(geom,roads,major,scale,local_gap,gap)
        city=city.intersection(core).difference(unary_union([water,heroes]))
        # No tiny-fragment deletion: keep the urban texture for this first comparison.
        assert city.is_valid
        draw_negative(city,heroes,water,GeometryCollection(),core.bounds,out/f'{name}.png',pixels=1400)
        (out/f'{name}.wkb').write_bytes(city.wkb)
        def extrude(g,z,h):
            mm=affinity.translate(affinity.scale(g,xfact=scale,yfact=scale,origin=(0,0)),xoff=3000*scale,yoff=1800*scale)
            mm=set_precision(mm, .0001)
            parts=[shapely_poly_to_crosssection(p).extrude(h).translate((0,0,z)) for p in _polygon_parts(mm) if p.area > 1e-8]
            assert all(p.status() == m.Error.NoError for p in parts), (z,h,"invalid extrusion")
            return m.Manifold.batch_boolean(parts,m.OpType.Add) if parts else m.Manifold()
        # One fused solid per coupon. Water is recessed; all parts share a base.
        solid=extrude(core,0,.56)+extrude(core.difference(water),.56,.24)
        solid=solid+extrude(city,.8,.48)+extrude(heroes.difference(water),.8,.96)
        mesh=manifold64_to_mesh(solid)
        assert mesh.is_watertight and mesh.is_winding_consistent and mesh.volume>0
        mesh.export(out/f'{name}_same_scale.stl')
        export_deepseek_3mf({'buildings':mesh},str(out/f'{name}_same_scale.3mf'))
        rows.append({'variant':name,'local_gap_mm':local_gap,'major_gap_mm':gap,'urban_parts':len(list(_polygon_parts(city))),
                     'urban_area_mm2':city.area*scale**2,'faces':len(mesh.faces),'watertight':bool(mesh.is_watertight),
                     'bounds_mm':mesh.bounds.tolist(),'clearance':evidence})
        print(rows[-1]|{'clearance':'recorded'},flush=True)
    sheet=Image.new('RGB',(2800,1530),'#f5f5f2');d=ImageDraw.Draw(sheet)
    font=ImageFont.truetype('/System/Library/Fonts/STHeiti Medium.ttc',34)
    for i,(name,geom,local_gap,gap) in enumerate(variants):
        title=f'{name}  主路 {gap:.2f} mm / 普通街缝 {local_gap:.2f} mm'
        if args.sparse_road_comparison: title=f'{name}  '+('全路网加宽 0.42 mm' if name=='C' else '筛选路网 · 留白 0.42 mm')
        if args.balanced_road_comparison: title=('D  较强筛路 · 街缝 0.42 mm' if name=='D' else 'E  多保留次要街道 · 0.30 / 0.42 mm')
        sheet.paste(Image.open(out/f'{name}.png'),(i*1400,75));d.text((i*1400+25,20),title,font=font,fill='#202020')
    d.text((25,1484),'同区域、同平面比例；S6 前快照重裁切示意。试片约 34.9 mm，地形与高度已统一，非切片预览。',font=font,fill='#333333')
    sheet.save(out/'comparison.png')
    manifest={'source':str(source),'scope':'pre-S6 planar replay, not final Paris geometry or slicer output',
              'bounds_m':list(core.bounds),'scale_mm_per_m':scale,'closing_radius_mm':.06,'road_selection':selection,
              'added_area_before_road_recut_mm2':additions.area*scale**2,'variants':rows,
              'limits':['Normalized terrain/building heights; no bridge reconstruction in snapshot.',
                        'Not sliced or physically printed. No production defaults changed.']}
    (out/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2,default=str))
    (out/'README.md').write_text('巴黎局部道路可读性试验\n\nA 为当前宽度重裁切，B 主路由 0.21 加宽到 0.42 mm，普通街缝保持 0.14 mm。B 用 0.06 mm 半径轻微整合后重新裁切全部道路，新增填充避让绿地、水面及地标；没有删除碎建筑。\n\n来自 v9 的 S6 前快照，非最终巴黎网格的精确复刻。图片是平面几何示意，非切片效果。试片原比例约 34.9 mm 见方，统一地形和建筑高度，水面下凹；快照无桥梁复原，不用于桥梁验收。STL 和 3MF 是同一份单色融合实体，导入其一即可，请勿自动缩放。尚未完成 Bambu 切片或实物验证。生产默认参数未改。\n')
    if args.line_width_comparison:
        (out/'README.md').write_text('巴黎 B/C 街缝对比\n\nB：普通街缝 0.14 mm，主路 0.42 mm。C：普通街缝与主路均为 0.42 mm，按原模型配置的挤出线宽取值，不代表喷嘴的物理最小间隙。两者使用相同的 0.06 mm 半径街区整合。\n\n图片为 S6 前快照重裁切的平面几何示意，非切片预览。试片保持全城原平面比例，34.9 mm 见方，统一地形与建筑高度，无桥梁复原。STL/3MF 为相同单色实体，导入其一且勿缩放。网格闭合检查已通过，尚未实际切片或打印。生产默认参数未改。\n')
    if args.sparse_road_comparison:
        (out/'README.md').write_text('巴黎局部 C/D 筛路试验。C 完整街道加宽，D 保留主次干道与三级道路，次要道路按名称分组，长度至少600m且至少55%长度距离已选路网超过模型0.8mm时保留。两者留白0.42mm、整合半径0.06mm相同。筛选仅针对本地缓冲窗口，不是全城定版；预先存在的细街区边界仍可能保留，没有重建全部街区。水面地标一致。图为平面几何示意，3MF统一地形/高度、无桥梁复原，未实际切片/打印。')
    if args.balanced_road_comparison:
        (out/'README.md').write_text('巴黎 D/E 局部对比。D复现上一轮强筛选、0.42mm街缝。E保留D所有已选道路，再按完整名称组增加次要街道：至少400m、至少40%长度距离已选路网超过模型0.55mm；普通街缝0.30mm，既有主干保护线0.42mm。源数据、窗口、街区轻微整合均不变。\n\n这是同时调整筛选和线宽的风格对比，不能把变化归因于单一参数。统一地形和高度的34.9mm原比例试片，非最终全城网格。没有复原桥梁，旧细边界可能仍保留。网格已检查闭合及绕序，尚未切片或打印。生产默认未改。')
if __name__=='__main__':main()
