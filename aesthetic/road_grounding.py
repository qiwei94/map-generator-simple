"""S6 road draping and bounded, source-backed straight bridge deck plans.

Bank heights define a model deck, not a surveyed bridge elevation. Unsupported
spans remain explicit blockers, never disappear or fall back to riverbed Z.
"""
from types import SimpleNamespace
import hashlib
import numpy as np
from shapely.geometry import Point, LineString, box
from shapely.affinity import scale as scale_geometry
from shapely.ops import unary_union

from aesthetic.surface_grounding import (
    resolve_grounding, grounding_digest, materialize_grounding,
    require_terrain_binding, _patch_mesh,
)
from aesthetic.bridge_sources import water_bridge_lines, bridge_corridor

VERSION = 'road-grounding-and-bank-deck-v1'


def _bridge_patch(poly, lines, scale, terrain, height, offset, water, gap):
    from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import sample_terrain_surface_plan_z
    candidates = []
    for line in lines:
        corridor = bridge_corridor([line], scale, gap, box(*poly.bounds))
        if poly.difference(corridor).area <= max(1e-9, poly.area * 1e-8):
            candidates.append(line)
    if not candidates:
        raise ValueError('桥面重叠或无单一来源覆盖，需独立多桥衔接策略')
    source = sorted(candidates, key=lambda g: g.normalize().wkb)[0]
    coords = np.asarray(source.coords)[:, :2]
    start, end = coords[0], coords[-1]
    axis = end - start
    length = float(np.linalg.norm(axis))
    if length <= 1e-9:
        raise ValueError('桥梁源线没有有效端点')
    axis /= length
    lateral = np.array([-axis[1], axis[0]])
    if np.max(np.abs((coords - start) @ lateral)) * scale > 1e-6:
        raise ValueError('弯桥尚需沿线高程策略，拒绝使用直桥平面')
    span = source.intersection(poly)
    if span.geom_type != 'LineString' or span.length <= 1e-9:
        raise ValueError('桥面中心线被裁断或分裂，缺少连续支撑路径')
    a, b = np.asarray(span.coords[0])[:2], np.asarray(span.coords[-1])[:2]
    if (water.covers(Point(a)) or water.covers(Point(b))
            or span.intersection(water).length <= 1e-9):
        raise ValueError('桥面裁切后未保留水外的两岸支撑，不能猜测或延长端点')
    direction = b - a
    span_length = float(np.linalg.norm(direction))
    direction /= span_length
    expected = scale_geometry(poly, xfact=scale, yfact=scale, origin=(0,0))
    # Exact bank cross-section samples, including intersections with all DEM
    # grid triangles. Uniform sparse samples could miss a small bank peak.
    xy, _, _ = _patch_mesh(expected, terrain)
    t = ((xy / scale - a) @ direction) / span_length
    if t.min() < -1e-7 or t.max() > 1 + 1e-7:
        raise ValueError('桥头并非可验证的横断面，需专用接驳策略')
    caps = []
    for value in (0., 1.):
        cap = xy[np.abs(t-value) < 1e-7]
        if len(cap) < 2:
            raise ValueError('桥岸支撑横断面不足')
        along = cap @ np.array([-direction[1], direction[0]])
        cross_section = LineString([cap[along.argmin()] / scale, cap[along.argmax()] / scale])
        if cross_section.intersects(water):
            raise ValueError('桥岸横断面部分仍在水中，支撑证据不足')
        caps.append(cap)
    bank_xy = np.vstack([a, b]) * scale
    bank_z = sample_terrain_surface_plan_z(terrain, bank_xy[:,0], bank_xy[:,1])

    def plane_z(points):
        fraction = ((points - bank_xy[0]) @ direction) / (span_length * scale)
        return bank_z[0] + fraction * (bank_z[1] - bank_z[0])

    cap_xy = np.vstack(caps)
    cap_error = np.abs(plane_z(cap_xy) - sample_terrain_surface_plan_z(terrain, cap_xy[:,0], cap_xy[:,1]))
    # Old road offset is -height/2. A quarter-thickness deviation guarantees
    # a positive embedding interval and road/deck vertical overlap at banks.
    limit = height / 4
    if float(cap_error.max()) > limit + 1e-8:
        raise ValueError('桥岸横坡超出支撑厚度容差，需细分引道而非整体抬高')
    width, depth = terrain.width_m * scale, terrain.height_m * scale
    xx, yy = np.meshgrid([-width/2, width/2], [-depth/2, depth/2])
    plane = SimpleNamespace(width_m=terrain.width_m, height_m=terrain.height_m,
        scale_mm_per_m=scale, fingerprint=terrain.fingerprint,
        surface_z_grid_mm=plane_z(np.column_stack([xx.ravel(), yy.ravel()])).reshape(2,2))
    resolved = resolve_grounding([poly], [height], scale, plane,
                                 ['draped_thickness'], base_offset_mm=offset)
    patch = dict(resolved['patches'][0], mode='source_bridge_bank_plane')
    return patch, dict(status='ready', source_sha256=hashlib.sha256(source.wkb).hexdigest(),
        bank_xy_m=[a.tolist(), b.tolist()], bank_surface_z_mm=bank_z.tolist(),
        span_mm=span_length * scale, cap_deviation_max_mm=float(cap_error.max()),
        cap_deviation_limit_mm=limit,
        min_bank_embedding_mm=float(-offset-cap_error.max()),
        min_bank_vertical_overlap_mm=float(height-cap_error.max()),
        final_boolean_contact='pending', slicer_bridge_span='pending',
        说明='按源线两岸地形建立模型桥面；不是实测桥梁高度；尚未做水体裁切后的接触与跨桥切片验收。')


def resolve_road_grounding(layers, scale, terrain):
    from aesthetic.city_surface_plan import surface_fingerprint
    road = layers.surface_plan_evidence['road_surface_plan']
    polys = list(layers.surface_road_polygons)
    h, offset = road['height_mm'], road['base_offset_mm']
    if not np.isclose(offset, -h/2, rtol=0, atol=1e-12):
        raise ValueError('road grounding requires the declared half-thickness embedding policy')
    bridges = set(getattr(layers, 'surface_road_bridge_indices', []) or [])
    water = unary_union(list(layers.WL) + list(layers.WO))
    sources = water_bridge_lines(layers)
    patches, supports = [], []
    for i, poly in enumerate(polys):
        if i not in bridges:
            p = resolve_grounding([poly], [h], scale, terrain,
                                   ['draped_thickness'], base_offset_mm=offset)
            patches.extend(p['patches'])
            continue
        try:
            # Bridge width was frozen by the road width contract in S6.
            gap = layers.surface_plan_evidence['road_width_contract']['resolved_visual_widths']['major']
            patch, evidence = _bridge_patch(poly, sources, scale, terrain, h, offset, water, gap)
            patches.append(patch)
            supports.append(dict(polygon_index=i, **evidence))
        except ValueError as exc:
            supports.append(dict(polygon_index=i, status='blocked', reason_zh=str(exc)))
    blocked = any(e['status'] == 'blocked' for e in supports)
    plan = dict(version=VERSION, terrain_fingerprint=str(terrain.fingerprint),
        input_geometry_fingerprint=surface_fingerprint(polys, scale, [h]*len(polys)),
        patches=patches, support_evidence=supports)
    plan['fingerprint'] = grounding_digest(plan)
    plan['evidence'] = dict(version=VERSION, owner_stage='S6', status='blocked' if blocked else 'ready',
        fingerprint=plan['fingerprint'], terrain_fingerprint=plan['terrain_fingerprint'],
        polygon_count=len(polys), ordinary_road_count=len(polys)-len(bridges),
        bridge_polygon_count=len(bridges), bridge_support=supports,
        base_offset_mm=offset, height_mm=h, patch_triangles=sum(len(p['faces']) for p in patches),
        final_boolean_contact='pending', slicing='pending',
        说明='道路沿冻结地形保持厚度并半厚嵌入；直桥按两岸建立桥面；不支持的桥保留阻断原因，不回退河床取高。')
    return plan


def materialize_road_grounding(plan, layers, scale, sample_z_m):
    require_terrain_binding(plan, sample_z_m)
    if any(e.get('status') != 'ready' for e in plan['support_evidence']):
        raise ValueError('bridge support unresolved: ' + '; '.join(
            e.get('reason_zh', '') for e in plan['support_evidence'] if e.get('status') != 'ready'))
    road = layers.surface_plan_evidence['road_surface_plan']
    mesh, proof = materialize_grounding(plan, layers.surface_road_polygons,
                                       [road['height_mm']]*len(layers.surface_road_polygons), scale)
    proof.update(validation_scope='frozen_terrain_roads_and_source_bank_decks',
                 whole_footprint_grounding='road_drape_and_bank_interfaces_only',
                 bridge_bank_connection='verified_source_bank_interfaces' if plan['support_evidence'] else 'not_applicable')
    if mesh is not None:
        mesh.metadata['surface_materialization'] = proof
    return mesh, proof
