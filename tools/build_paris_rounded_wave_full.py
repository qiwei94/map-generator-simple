"""Full cached Paris assembly using the approved E rounded-wave style.

Retains transformed terrain/water/bridge meshes from the user's previous full
model; rebuilds all city masses on its frozen DEM. No production defaults change.
"""
import argparse
import copy
import hashlib
import json
import pickle
import sys
import time
import heapq
from collections import defaultdict, Counter
from pathlib import Path

import numpy as np
import trimesh
from shapely import set_precision, make_valid
from shapely.geometry import box, GeometryCollection
from shapely.ops import unary_union
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.experiment_negative_road_width import cut_carrier
from tools.paris_block_relief_style import round_blocks
from tools.paris_soft_road_edges import soft_corridor
from tools.render_reference_actual_mesh import meshes_from_file, render
from _TEXTURE_STYLE_OF_DEEPSEEK.block_base import _polygon_parts
from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf
from aesthetic.surface_grounding import resolve_grounding, materialize_grounding

OUT = ROOT / 'output/paris_25km_rounded_wave_20260915'
SNAP = ROOT / 'output/paris_BC_full_20260914_v9_inputs/surface_inputs.pkl'
PRIOR = ROOT / 'output/paris_BC_full_20260914_v9'
CONTINUITY_REPAIR = False


def repair_short_road_gaps(source, chosen, max_length_m=280.0, max_edges=10):
    """Add short omitted chains between dangling retained endpoints in O(E log V).

    This deliberately works on endpoint topology instead of buffered geometry.
    It restores factual source segments only and refuses long or highly segmented
    paths, so it cannot grow into a second whole-city geometry-union pass.
    """
    key = lambda xy: (round(float(xy[0]), 1), round(float(xy[1]), 1))
    chosen_wkb = {g.wkb for g in chosen}
    degree = Counter()
    for g in chosen:
        if g.geom_type != 'LineString':
            continue
        degree[key(g.coords[0])] += 1
        degree[key(g.coords[-1])] += 1
    dangling = {node for node, count in degree.items() if count == 1}

    edges = []
    adjacency = defaultdict(list)
    for g in source:
        if g.wkb in chosen_wkb or g.geom_type != 'LineString':
            continue
        u, v = key(g.coords[0]), key(g.coords[-1])
        index = len(edges)
        edges.append((u, v, float(g.length), g))
        adjacency[u].append((v, index, float(g.length)))
        adjacency[v].append((u, index, float(g.length)))
    seeds = sorted(dangling.intersection(adjacency))

    # Multi-source Dijkstra. A collision between two seed fronts identifies the
    # cheapest omitted source chain joining two retained dangling endpoints.
    distance = {}
    owner = {}
    parent = {}
    heap = []
    for seed_id, node in enumerate(seeds):
        distance[node] = 0.0
        owner[node] = seed_id
        parent[node] = None
        heapq.heappush(heap, (0.0, node))
    candidates = []
    while heap:
        dist, node = heapq.heappop(heap)
        if dist != distance.get(node) or dist > max_length_m:
            continue
        for other, edge_id, length in adjacency[node]:
            nd = dist + length
            if nd <= max_length_m and (other not in distance or nd < distance[other]):
                distance[other] = nd
                owner[other] = owner[node]
                parent[other] = (node, edge_id)
                heapq.heappush(heap, (nd, other))
            elif other in owner and owner[other] != owner[node]:
                total = dist + length + distance[other]
                if total <= max_length_m:
                    candidates.append((total, node, other, edge_id))

    def trace(node):
        result = []
        while parent.get(node) is not None:
            node, edge_id = parent[node]
            result.append(edge_id)
        return result

    accepted = set()
    joined_seed_pairs = set()
    accepted_paths = 0
    for total, left, right, bridge_edge in sorted(candidates):
        pair = tuple(sorted((owner[left], owner[right])))
        if pair in joined_seed_pairs:
            continue
        path = trace(left) + [bridge_edge] + trace(right)
        path = list(dict.fromkeys(path))
        if len(path) > max_edges:
            continue
        joined_seed_pairs.add(pair)
        accepted.update(path)
        accepted_paths += 1
    added = [edges[i][3] for i in sorted(accepted)]
    evidence = {
        'algorithm': 'bounded multi-source endpoint Dijkstra',
        'complexity': 'O(E log V); no buffer or union in route search',
        'max_length_m': max_length_m,
        'max_edges_per_path': max_edges,
        'dangling_seed_nodes': len(seeds),
        'accepted_paths': accepted_paths,
        'added_features': len(added),
        'added_length_m': sum(g.length for g in added),
    }
    return chosen + added, evidence


def save(name, value):
    with (OUT / name).open('wb') as f:
        pickle.dump(value, f, protocol=5)


def load(name):
    with (OUT / name).open('rb') as f:
        return pickle.load(f)


def polygonal(g):
    if isinstance(g,(list,tuple)):
        return unary_union([part for item in g for part in _polygon_parts(item)])
    return unary_union(list(_polygon_parts(g)))


def repair_factual_water_corridors(layers, kw):
    """Replay the pipeline fix against the cached projected water source."""
    from _TEXTURE_STYLE_OF_DEEPSEEK._layer_preprocess import (
        _close_unprintable_water_gaps,
    )
    from _TEXTURE_STYLE_OF_DEEPSEEK.water_roles import has_printable_water_mass
    from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import (
        bbox_to_utm, project_geodataframe,
    )
    import pandas as pd

    spec=json.loads((PRIOR/'design_spec.json').read_text())
    raw_cache=ROOT/'cache/pipeline/showcase_paris_25km_aesthetic/gdfs_v1_8e6d275a4c00.pkl'
    source=pickle.load(raw_cache.open('rb'))['water']
    projection=bbox_to_utm(*spec['bbox_wgs84'])
    source=project_geodataframe(source,projection['utm_crs'],projection['origin'])
    frame=box(*kw['bbox_local']);existing=polygonal(list(layers.WL)+list(layers.WO))
    additions=[];source_rows=[]
    candidates=source.iloc[source.sindex.query(frame,predicate='intersects')]
    for index,row in candidates.iterrows():
        def value(key):
            v=row.get(key)
            return '' if v is None or pd.isna(v) else str(v).strip().casefold()
        if value('water') not in {'river','canal','riverbank'} and value('waterway') not in {'river','canal','riverbank'}:
            continue
        for raw in _polygon_parts(row.geometry):
            poly=_close_unprintable_water_gaps(raw, layers.nozzle_real_m).intersection(frame)
            if (poly.is_empty or not has_printable_water_mass(
                    poly,nozzle_real_m=layers.nozzle_real_m)):
                continue
            missing=polygonal(poly.difference(existing,grid_size=.01))
            for part in _polygon_parts(missing):
                if part.area <= 1e-3:continue
                additions.append(part);source_rows.append(int(index))
    fixed=copy.copy(layers)
    fixed.WL=list(layers.WL)+additions
    evidence={'policy_version':'factual-surface-corridor-v1','source_cache':str(raw_cache),
              'source_cache_sha256':hashlib.sha256(raw_cache.read_bytes()).hexdigest(),
              'added_polygons':len(additions),'source_rows':sorted(set(source_rows)),
              'added_area_m2':sum(p.area for p in additions),
              'condition':'printable polygon tagged water/waterway=river|canal|riverbank and absent from frozen visible water'}
    fixed.water_roles={**dict(layers.water_roles or {}),'factual_surface_corridor_repair':evidence}
    print('Factual water corridor repair',evidence,flush=True)
    return fixed,evidence


def select_roads(layers, kw):
    local = list(layers.block_base_cut_lines)
    rows = kw['source_roads'].itertuples(index=False)
    tags = {r.geometry.wkb: (str(r.highway), str(r.name or ''), str(r.tunnel or '').lower()) for r in rows}
    mains = {'primary','primary_link','secondary','secondary_link','tertiary','tertiary_link','trunk','motorway'}
    chosen, groups = [], defaultdict(list)
    for g in local:
        h, n, tunnel = tags.get(g.wkb, ('', '', ''))
        if tunnel not in ('','none','nan','no','false','0'):
            continue
        if h in mains:
            chosen.append(g)
        elif n and n not in ('None','nan'):
            groups[n].append(g)
    ranked = sorted(groups.items(), key=lambda pair: sum(g.length for g in pair[1]), reverse=True)
    routes = [unary_union(parts) for _, parts in ranked]
    selected = set()
    # Local unions of buffered candidates equal the global occupied mask in
    # each query, avoiding a growing whole-city union on every named street.
    for spacing, min_length, fraction in [(.8,600,.55),(.55,400,.40)]:
        buffers = [g.buffer(spacing/kw['scale']) for g in chosen]
        candidate_buffers = [g.buffer(spacing/kw['scale']) for g in routes]
        all_buffers = buffers + candidate_buffers
        tree = STRtree(all_buffers)
        initial = len(buffers)
        before = len(selected)
        for i, route in enumerate(routes):
            if i in selected or route.length < min_length:
                continue
            hits = tree.query(route, predicate='intersects')
            occupied = unary_union([all_buffers[int(j)] for j in hits if j < initial or int(j)-initial in selected])
            if route.difference(occupied).length / route.length >= fraction:
                selected.add(i)
        print('Road selection pass', spacing, len(selected)-before, 'named groups added', flush=True)
    for i in sorted(selected):
        chosen.extend(ranked[i][1])
    continuity = None
    if CONTINUITY_REPAIR:
        chosen, continuity = repair_short_road_gaps(local, chosen)
    evidence = dict(input_features=len(local), selected_features=len(chosen), named_groups=len(selected),
                    original_length_m=sum(g.length for g in local), selected_length_m=sum(g.length for g in chosen),
                    classes=dict(Counter(tags.get(g.wkb, ('unmatched',))[0] for g in chosen)),
                    scope='full source window; same E thresholds, global name groups and ranking',
                    continuity_repair=continuity)
    return chosen, list(layers.block_base_major_cut_lines), evidence


def geometry(layers, kw):
    s = kw['scale']; frame = box(*kw['bbox_local'])
    def region(items):
        return set_precision(unary_union(list(items)).intersection(frame), .002)
    carrier = region(list(layers.block_base)+list(layers.BO))
    water = region(list(layers.WL)+list(layers.WO))
    parks = region(list(layers.VL)+list(layers.VO))
    heroes = region([p for p,h in layers.BL])
    local, major, selection = load('roads.pkl') if (OUT/'roads.pkl').exists() else select_roads(layers, kw)
    save('roads.pkl', (local, major, selection))
    print('Whole-city carrier and road selection ready', selection, flush=True)
    closed = set_precision(carrier.buffer(.06/s,join_style=2).buffer(-.06/s,join_style=2), .002)
    protected = unary_union([parks, water, heroes])
    additions = polygonal(closed.difference(carrier,grid_size=.01)).difference(protected,grid_size=.01)
    merged = unary_union([carrier,additions])
    old, _ = cut_carrier(merged,local,major,s,.30,.42)
    old = polygonal(old.intersection(frame).difference(unary_union([water,heroes]),grid_size=.01))
    rough = set_precision(old.buffer(.06/s,join_style=2).buffer(-.06/s,join_style=2),.002)
    added = polygonal(rough.difference(old,grid_size=.01)).difference(protected,grid_size=.01)
    clean = unary_union([old,added]).buffer(-.012/s,join_style=2).buffer(.012/s,join_style=2)
    clean = polygonal(clean.simplify(.01/s,preserve_topology=True)).difference(unary_union([water,heroes]),grid_size=.01)
    clean = polygonal(clean).difference(polygonal(parks.difference(old,grid_size=.01)),grid_size=.01)
    clean, _ = cut_carrier(clean,local,major,s,.30,.42)
    clean = set_precision(clean.intersection(frame),.002)
    save('clean.pkl',(clean,water,selection))
    print('Whole-city footprint cleanup ready',len(list(_polygon_parts(clean))),flush=True)
    return finish_geometry(layers,kw,clean,water,local,major,selection)


def finish_geometry(layers,kw,clean,water,local,major,selection):
    s=kw['scale']
    polys, heights, rounding = round_blocks(clean,s)
    if (OUT/'wave.pkl').exists():
        corridor_parts,wave_evidence=load('wave.pkl')
    else:
        corridor_parts=[]; wave_evidence=[]
        for name,lines,width in [('local',local,.30),('major',major,.42)]:
            for i in range(0,len(lines),1000):
                corridor,e=soft_corridor(lines[i:i+1000],s,width,extra_width_mm=.28,wavelength_mm=1.2,step_mm=.10)
                corridor_parts.extend(_polygon_parts(corridor));wave_evidence.append(e)
                print('Wave corridors',name,min(i+1000,len(lines)),'/',len(lines),flush=True)
        save('wave.pkl',(corridor_parts,wave_evidence))
    tree=STRtree(corridor_parts);result=[];hh=[]
    for i,(p,h) in enumerate(zip(polys,heights)):
        hits=tree.query(p,predicate='intersects')
        q=set_precision(p,0).difference(unary_union([corridor_parts[int(j)] for j in hits]))
        q=q.buffer(-.025/s,resolution=8,join_style=1).buffer(.025/s,resolution=8,join_style=1)
        q=polygonal(make_valid(q)).simplify(.003/s,preserve_topology=True)
        q=polygonal(make_valid(set_precision(q,0)))
        frame=box(*kw['bbox_local'])
        if not frame.covers(q):
            q=make_valid(q.intersection(frame))
        for part in _polygon_parts(q):
            if part.area*s*s>=.005:
                result.append(part);hh.append(h)
        if i%1000==0:print('Rounded block cuts',i,'/',len(polys),flush=True)
    hero_polys=[];hero_heights=[]
    for poly,h in layers.BL:
        clipped=polygonal(poly.intersection(box(*kw['bbox_local'])).difference(water,grid_size=.01))
        if clipped.is_empty:continue
        rounded,_,_=round_blocks(clipped,s)
        hero_polys.extend(rounded)
        compressed=min(h,1.2,round((.60+.24*np.log1p(h))/.12)*.12)
        hero_heights.extend([compressed]*len(rounded))
    assert result and all(p.is_valid for p in result)
    evidence=dict(selection=selection,rounding=rounding,width_modulation=wave_evidence,boundary_simplification_tolerance_mm=.003,
                  blocks=len(result),landmarks=len(hero_polys),urban_area_mm2=sum(p.area for p in result)*s*s)
    value=(result,hh,hero_polys,hero_heights,evidence)
    save('geometry.pkl',value)
    return value


def grounded(polys,heights,kw,landmark=False):
    parts=[];proofs=[]
    for i in range(0,len(polys),400):
        pp=polys[i:i+400];hh=heights[i:i+400]
        mode='flat_roof_above_highest_support' if landmark else 'draped_thickness'
        plan=resolve_grounding(pp,hh,kw['scale'],kw['terrain_surface_plan'],[mode]*len(pp),
                               flat_block_relief_limit_mm=None if landmark else .24)
        mesh,proof=materialize_grounding(plan,pp,hh,kw['scale'])
        parts.append(mesh);proofs.append(dict(grounding=plan['evidence'],proof=proof))
        print('Grounded', 'landmarks' if landmark else 'blocks',min(i+400,len(polys)),'/',len(polys),flush=True)
    return trimesh.util.concatenate(parts),proofs


def main():
    global OUT, CONTINUITY_REPAIR
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,default=OUT)
    parser.add_argument('--repair-water',action='store_true')
    parser.add_argument('--continuity-repair',action='store_true')
    args=parser.parse_args();OUT=args.output_dir.resolve();CONTINUITY_REPAIR=args.continuity_repair
    started=time.monotonic();OUT.mkdir(exist_ok=True)
    layers,kw=pickle.load(SNAP.open('rb'))
    water_repair=None
    if args.repair_water:
        layers,water_repair=repair_factual_water_corridors(layers,kw)
    if (OUT/'geometry.pkl').exists():
        polygons,heights,hp,hh,geometry_evidence=load('geometry.pkl')
    elif (OUT/'clean.pkl').exists():
        clean,water,selection=load('clean.pkl');local,major,_=load('roads.pkl')
        polygons,heights,hp,hh,geometry_evidence=finish_geometry(layers,kw,clean,water,local,major,selection)
    else:
        polygons,heights,hp,hh,geometry_evidence=geometry(layers,kw)
    if (OUT/'meshes.pkl').exists():
        meshes,proofs,source_evidence=load('meshes.pkl')
    else:
        baseline=next(PRIOR.glob('*.3mf'));raw=meshes_from_file(baseline,include_names={'terrain','water','roads'})
        prior_terrain=next(v for n,v,f,r in raw if n=='terrain')
        offset=np.array([(prior_terrain[:,0].min()+prior_terrain[:,0].max())/2,
                         (prior_terrain[:,1].min()+prior_terrain[:,1].max())/2,
                         prior_terrain[:,2].min()-kw['terrain_surface_plan'].terrain_base_z_mm])
        old={n:trimesh.Trimesh(vertices=v-offset,faces=f,process=False) for n,v,f,r in raw}
        if args.repair_water:
            from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import materialize_terrain_surface_plan
            from aesthetic.z_texture import materialize_textured_terrain
            from _TEXTURE_STYLE_OF_DEEPSEEK.water import prepare_deepseek_water_relief,build_deepseek_water_v3
            terrain=materialize_terrain_surface_plan(kw['terrain_surface_plan'])
            terrain=materialize_textured_terrain(terrain,layers,kw['terrain_surface_plan'])
            relief=prepare_deepseek_water_relief(terrain,layers.WL,layers.WO,kw['scale'],
                base_thickness_mm=.4,surface_thickness_mm=kw['printer_profile'].min_surface_height_mm,
                exact_boundary=True)
            xmin,ymin,xmax,ymax=kw['bbox_local']
            water=build_deepseek_water_v3(layers.WL,layers.WO,xmin,ymin,xmax,ymax,
                scale=kw['scale'],flat_only=False,base_thickness_mm=.4,
                surface_levels_mm=relief['surface_levels_mm'],
                surface_thickness_mm=kw['printer_profile'].min_surface_height_mm,support_to_base=True)
            meshes={'terrain':terrain,'roads':old['roads'],'water':water}
            source_evidence={'terrain':'rematerialized from frozen TerrainSurfacePlan and ground texture',
                'water':'rebuilt from corrected WL/WO with exact terrain recess','roads':'unchanged prior road/bridge triangles',
                'prior_path':str(baseline),'prior_sha256':hashlib.sha256(baseline.read_bytes()).hexdigest(),
                'prior_assembly_translation_removed_mm':offset.tolist(),'water_relief':relief,'water_repair':water_repair}
        else:
            meshes=old
            source_evidence=dict(path=str(baseline),sha256=hashlib.sha256(baseline.read_bytes()).hexdigest(),
                                 retained_objects=list(meshes),assembly_translation_removed_mm=offset.tolist(),
                                 policy='unchanged source triangles; common translation only')
        expected=np.array(kw['bbox_local']).reshape(2,2)*kw['scale']
        assert np.allclose(meshes['terrain'].bounds[:,:2],expected,atol=.002)
        print('Terrain, water and bridge assembly ready',flush=True)
        city,proof=grounded(polygons,heights,kw)
        heroes,hproof=grounded(hp,hh,kw,True)
        meshes.update(block_base=city,landmarks=heroes)
        proofs=dict(city=proof,landmarks=hproof)
        save('meshes.pkl',(meshes,proofs,source_evidence))
    artifact=OUT/'paris_25km_rounded_wave.3mf'
    checks={n:dict(faces=len(m.faces),watertight=bool(m.is_watertight),winding_consistent=bool(m.is_winding_consistent),
                  volume_mm3=float(m.volume),bounds_mm=m.bounds.tolist()) for n,m in meshes.items()}
    assert all(v['watertight'] and v['winding_consistent'] and v['volume_mm3']>0 for v in checks.values()),checks
    export_deepseek_3mf(meshes,str(artifact))
    print('Export complete',artifact.stat().st_size,checks,flush=True)
    report=dict(city='Paris',nominal_span_km=25,bbox_local_m=kw['bbox_local'],scale_mm_per_m=kw['scale'],
                source_snapshot_sha256=hashlib.sha256(SNAP.read_bytes()).hexdigest(),source_assembly=source_evidence,
                terrain_fingerprint=kw['terrain_surface_plan'].fingerprint,geometry=geometry_evidence,grounding=proofs,
                meshes=checks,artifact=dict(filename=artifact.name,sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),size_bytes=artifact.stat().st_size),
                generation='cached full-city style rebuild',water_repair=water_repair,slicing='not_run',views=[])
    (OUT/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    views=[(n,m.vertices,m.faces,'urban_relief' if n in ('block_base','landmarks') else 'road' if n=='roads' else n) for n,m in meshes.items()]
    for elevation,name,crop in [(90,'topdown',None),(35,'oblique',None),(90,'center_detail',(-23.286,-13.972,11.643,20.957))]:
        print('Rendering',name,flush=True)
        report['views'].append(render(views,OUT/f'{name}.png',elevation,pixel_size=2600,crop=crop,frame_width_mm=204))
    report['elapsed_seconds']=time.monotonic()-started
    (OUT/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print('DONE',report['elapsed_seconds'],flush=True)


if __name__=='__main__':
    main()
