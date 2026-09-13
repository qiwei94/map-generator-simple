"""S6 owns final urban XY surfaces; S7 draws them and S8 only extrudes them.

No random rotation, second road selection or geometry simplification is
permitted after this plan. The original S6 mass statistics remain available
alongside the post-cut measurements, rather than silently changing meaning.
"""
from __future__ import annotations

import hashlib
import json
import math
from time import monotonic

import numpy as np
from shapely.geometry import box
from shapely.ops import unary_union
from aesthetic.bridge_sources import water_bridge_lines, bridge_corridor

from _TEXTURE_STYLE_OF_DEEPSEEK.config import (
    BLOCK_BASE_THICKNESS_MM, BUILDING_AGGREGATE_HEIGHT_MM,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.block_base import (
    _polygon_parts, enforce_final_block_base_clearance,
)

POLICY_VERSION = "shared-city-surface-v4"


def _clean_overlay_roundoff(polys, owners, scale):
    """Remove sub-nanometre overlay spurs before freezing S6 geometry.

    This is numerical precision, not a printability/visual area filter. Keep
    source ownership parallel when a polygon splits or disappears.
    """
    from shapely import set_precision
    precision_mm = 1e-8
    result, ids = [], []
    changed_area, vanished_area, vanished = 0., 0., 0
    for poly, owner in zip(polys, owners):
        clean = set_precision(poly, precision_mm / scale)
        parts = list(_polygon_parts(clean))
        error = poly.symmetric_difference(clean).area * scale * scale
        # Area displacement of a quantized boundary scales with perimeter,
        # not footprint area (important for extremely thin overlay slivers).
        allowed = max(1e-8, poly.length * scale * precision_mm * 2)
        if error > allowed:
            raise ValueError('numerical surface cleanup changed approved footprint: '
                             f'{error} mm2 > {allowed} mm2')
        changed_area += error
        if not parts:
            vanished += 1
            vanished_area += poly.area * scale * scale
        result.extend(parts)
        ids.extend([owner] * len(parts))
    if vanished_area > 1e-8:
        raise ValueError('numerical surface cleanup removed measurable geometry')
    return result, ids, dict(precision_mm=precision_mm,
        vanished_polygons=vanished, vanished_area_mm2=vanished_area,
        symmetric_difference_area_mm2=changed_area,
        policy='numerical_overlay_roundoff_only')


def prepare_negative_roads(layers, source_roads, *, scale=None, bridge_gap_mm=None):
    from aesthetic.bridge_sources import extract_bridge_sources
    if source_roads is None:
        raise ValueError('negative-space-v1 requires source road evidence')
    # S6 is a realization stage.  It must not silently reselect or append
    # source roads after S3 has frozen the seam graph; doing so made identical
    # city blocks render differently depending on which output path ran.
    seam_graph = list(getattr(layers, 'seam_graph', ()) or ())
    if seam_graph:
        # The only permitted S6 road input is the S3-frozen graph.  This is a
        # handoff, not a new selection: retaining both fields keeps legacy
        # consumers compatible while making the chosen ownership explicit.
        layers.block_base_cut_lines = seam_graph
    evidence = {
        'policy': 's3_frozen_seam_graph_v1', 'recovered_route_length_m': 0.0,
        'invented_connectors': 0, 'selected_seam_features': len(seam_graph),
        'mutation': 'none',
    }
    layers.bridge_lines, bridges = extract_bridge_sources(source_roads)
    if scale is not None:
        from aesthetic.bridge_approaches import recover_bridge_approaches
        approaches, proof = recover_bridge_approaches(layers.bridge_lines, source_roads,
            unary_union(list(layers.WL) + list(layers.WO)), scale, bridge_gap_mm)
        layers.bridge_lines.extend(approaches)
        bridges['approach_recovery'] = proof
    return {**evidence, 'owner_stage': 'S6', 'bridge_sources': bridges}


def road_surface_fingerprint(polys, scale, height, offset):
    return hashlib.sha256((surface_fingerprint(polys, scale, [height] * len(polys))
                           + float(offset).hex()).encode()).hexdigest()


def _resolve_road_surfaces(layers, clip, scale, profile, *, negative_space=False, split_grounding=False):
    # Continuous, already-approved corridors, not only the bits removed by
    # this cut. This includes pre-existing street gaps without reviving roads
    # suppressed by S3's topology decision.
    corridors = []
    for lines, gap in ((layers.block_base_cut_lines, profile.surface_road_gap_mm),
                       (layers.block_base_major_cut_lines, profile.final_block_base_gap_mm)):
        corridors.extend(line.buffer(gap / scale / 2, cap_style=2, join_style=2)
                         for line in lines if not line.is_empty)
    corridor = unary_union(corridors).intersection(clip)
    # Source bridge tags provide independent structural approval. The old
    # roads_lines[2] means *landmark bridge*, not physical water crossing.
    bridge_lines = water_bridge_lines(layers)
    bridge = bridge_corridor(bridge_lines, scale, profile.final_block_base_gap_mm, clip)
    corridor = unary_union([corridor, bridge])
    if negative_space:
        # Ordinary streets are missing urban material, never raised ribbons.
        corridor = bridge
    water = unary_union(list(layers.WL) + list(layers.WO))
    occupied = unary_union(list(layers.block_base) + list(layers.BO)
                           + [item[0] for item in layers.BL])
    road = corridor.difference(water.difference(bridge)).difference(occupied)
    layers.surface_road_polygons = list(_polygon_parts(road))
    if split_grounding:
        from shapely import set_precision
        # Overlay can leave zero-area spikes at angled bridge/road junctions.
        # Resolve numerical topology in S6, before freezing the footprint;
        # never ask S8 to add walls along a non-area floating-point spur.
        precision_m = 1e-9 / scale
        ordinary = list(_polygon_parts(set_precision(road.difference(bridge), precision_m)))
        bridge_parts = list(_polygon_parts(set_precision(road.intersection(bridge), precision_m)))
        overlay_error_mm2 = unary_union(ordinary + bridge_parts).symmetric_difference(road).area * scale * scale
        if overlay_error_mm2 > max(1e-8, road.area * scale * scale * 1e-7):
            raise ValueError('road support partition changed approved corridor area')
        layers.surface_road_polygons = ordinary + bridge_parts
        layers.surface_road_bridge_indices = list(range(len(ordinary), len(ordinary) + len(bridge_parts)))
    height = profile.min_surface_height_mm
    offset = -height / 2  # lower than the minimum approved urban relief
    return {'policy_version': 'approved-road-surfaces-v2',
            'representation': 'lower_surface_in_approved_corridor',
            'water_crossing': 'explicit_bridges_only',
            'bridge_source_line_parts': len(bridge_lines),
            'bridge_water_area_m2': road.intersection(water).area,
            'support_partition_precision_mm': 1e-9 if split_grounding else None,
            'polygon_count': len(layers.surface_road_polygons),
            'height_mm': height, 'base_offset_mm': offset,
            'geometry_fingerprint': road_surface_fingerprint(
                layers.surface_road_polygons, scale, height, offset)}


def surface_fingerprint(polys, scale, heights):
    if len(polys) != len(heights):
        raise ValueError("surface heights must stay parallel to geometry")
    digest = hashlib.sha256()
    digest.update(json.dumps([POLICY_VERSION, float(scale)]).encode())
    for poly, height in zip(polys, heights):
        if not math.isfinite(height) or height <= 0:
            raise ValueError("surface relief must be finite and positive")
        digest.update(poly.wkb)
        digest.update(float(height).hex().encode())
    return digest.hexdigest()


def surface_heights(layers):
    bo_heights = list(getattr(layers, "BO_heights", ()) or ())
    if bo_heights and len(bo_heights) != len(layers.BO):
        raise ValueError("BO heights must stay parallel to geometry")
    return ([float(BLOCK_BASE_THICKNESS_MM)] * len(layers.block_base)
            + (bo_heights or [float(BUILDING_AGGREGATE_HEIGHT_MM)]
               * len(layers.BO)))


def _measure(polys, scale):
    # Fixed deterministic sample: bounded cost even for a 70万 building city.
    sample = polys[::max(1, math.ceil(len(polys) / 1024))]
    widths = []
    for poly in sample:
        coords = np.asarray(poly.minimum_rotated_rectangle.exterior.coords)
        widths.append(float(np.linalg.norm(np.diff(coords, axis=0), axis=1).min())
                      * scale)
    return {
        "polygon_count": len(polys),
        "summed_area_m2": float(sum(p.area for p in polys)),
        "area_semantics": "sum of layer polygons; overlaps are not dissolved",
        "width_sample_count": len(sample),
        "short_axis_p50_mm": float(np.median(widths)) if widths else None,
        "short_axis_p10_mm": float(np.percentile(widths, 10)) if widths else None,
    }


def finalize_city_surfaces(layers, *, bbox_local, scale, printer_profile,
                           road_style='printer-default', source_roads=None,
                           terrain_surface_plan=None, z_texture_policy=None):
    """Freeze XY and optional Z patches in S6; no source IO or solid creation."""
    started = monotonic()
    timings = {}
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError("surface scale must be finite and positive")
    existing = getattr(layers, "surface_plan_evidence", {}) or {}
    if z_texture_policy is not None:
        from aesthetic.z_texture import ZTexturePolicy
        z_texture_policy = ZTexturePolicy(**z_texture_policy).payload()
    if existing.get("status") == "finalized":
        if existing.get('z_texture_policy') != z_texture_policy:
            raise ValueError('cannot restyle finalized Z texture; restart from S5')
        if existing.get('road_style', 'printer-default') != road_style:
            raise ValueError('cannot restyle a finalized surface; restart from S5')
        if terrain_surface_plan is not None:
            grounding = getattr(layers, 'surface_grounding', {}) or {}
            if not grounding or grounding['city']['terrain_fingerprint'] != str(terrain_surface_plan.fingerprint):
                raise ValueError('cannot replace finalized grounding; restart from S5')
        verify_surface_plan(layers, scale)
        return existing
    if road_style not in {'printer-default', 'negative-space-v1', 'negative-space-fine-v1'}:
        raise ValueError('unknown surface road style')
    negative = road_style in {'negative-space-v1', 'negative-space-fine-v1'}
    from aesthetic.road_width_contract import resolve_road_width_contract
    road_profile, width_contract = resolve_road_width_contract(printer_profile, road_style)
    recovery = prepare_negative_roads(layers, source_roads, scale=scale,
        bridge_gap_mm=road_profile.final_block_base_gap_mm) if negative else None

    original = list(layers.block_base) + list(layers.BO)
    heights = surface_heights(layers)
    nbase = len(layers.block_base)
    classes = list(layers.block_base_classes or [])
    if classes and len(classes) != nbase:
        raise ValueError("block base classes must stay parallel to geometry")
    classes = (classes or ["unclassified"] * nbase) + ["unclassified"] * len(layers.BO)
    clip = box(*bbox_local)
    polys, owners = [], []
    for index, poly in enumerate(original):
        for part in _polygon_parts(poly.intersection(clip)):
            polys.append(part)
            owners.append(index)
    before = _measure(polys, scale)
    timings['prepare'] = monotonic() - started
    tick = monotonic()
    reveals = []
    polys, owners, local = enforce_final_block_base_clearance(
        polys, owners, layers.block_base_cut_lines, scale,
        road_profile.surface_road_gap_mm, removed_geometry=reveals,
        min_piece_area_m2=0. if negative else 10.)
    polys, owners, major = enforce_final_block_base_clearance(
        polys, owners, list(layers.block_base_major_cut_lines) + water_bridge_lines(layers), scale,
        road_profile.final_block_base_gap_mm, removed_geometry=reveals,
        min_piece_area_m2=0. if negative else 10.)
    passed = all(e.get("passed") or e["status"] == "not_applicable"
                 for e in (local, major))
    if not passed:
        raise ValueError("shared urban surface clearance failed")
    polys, owners, urban_cleanup = _clean_overlay_roundoff(polys, owners, scale)
    timings['urban_road_cuts'] = monotonic() - tick
    print(f'[surface] urban cuts {timings["urban_road_cuts"]:.1f}s', flush=True)
    tick = monotonic()

    layers.block_base, layers.block_base_classes = [], []
    layers.BO, layers.BO_heights = [], []
    for poly, owner in zip(polys, owners):
        if owner < nbase:
            layers.block_base.append(poly)
            layers.block_base_classes.append(classes[owner])
        else:
            layers.BO.append(poly)
            layers.BO_heights.append(heights[owner])
    final = list(layers.block_base) + list(layers.BO)
    landmark_owners = []
    landmark_cleanup = None
    if negative:
        from aesthetic.road_identity_continuity import is_covered
        from shapely.strtree import STRtree
        rows = (r._asdict() for r in source_roads.itertuples(index=False)) if hasattr(source_roads, 'itertuples') else iter(source_roads)
        covered_parts = [r['geometry'].buffer(.05) for r in rows if is_covered(r)]
        covered_tree = STRtree(covered_parts)
        def exposed(line):
            hits = covered_tree.query(line, predicate='intersects')
            if not len(hits):
                return line
            return line.difference(unary_union([covered_parts[int(i)] for i in hits]))
        lm_original = list(layers.BL)
        lm_polys, lm_ids = [], []
        for idx, (poly, _) in enumerate(lm_original):
            for part in _polygon_parts(poly.intersection(clip)):
                lm_polys.append(part); lm_ids.append(idx)
        for lines, gap in ((layers.block_base_cut_lines, road_profile.surface_road_gap_mm),
                           (list(layers.block_base_major_cut_lines) + water_bridge_lines(layers),
                            road_profile.final_block_base_gap_mm)):
            lm_polys, lm_ids, _ = enforce_final_block_base_clearance(
                lm_polys, lm_ids, [exposed(g) for g in lines], scale,
                gap, min_piece_area_m2=0.)
        lm_polys, lm_ids, landmark_cleanup = _clean_overlay_roundoff(lm_polys, lm_ids, scale)
        layers.BL = [(p, lm_original[i][1]) for p, i in zip(lm_polys, lm_ids)]
        for attribute in ('BL_categories', 'BL_height_roles'):
            values = list(getattr(layers, attribute, ()) or ())
            if values:
                if len(values) != len(lm_original):
                    raise ValueError(f'{attribute} must stay parallel to landmarks')
                setattr(layers, attribute, [values[i] for i in lm_ids])
        landmark_owners = lm_ids
    timings['landmark_road_cuts'] = monotonic() - tick
    print(f'[surface] landmark cuts {timings["landmark_road_cuts"]:.1f}s', flush=True)
    tick = monotonic()
    road_plan = _resolve_road_surfaces(layers, clip, scale, road_profile, negative_space=negative,
                                     split_grounding=terrain_surface_plan is not None)
    after = _measure(final, scale)
    timings['road_surfaces_and_measurement'] = monotonic() - tick
    evidence = dict(major if major["status"] == "checked" else local)
    evidence.update({
        "policy_version": POLICY_VERSION,
        "timings_seconds": timings,
        "status": "finalized",
        "owner_stage": "S6",
        "road_style": road_style,
        "z_texture_policy": z_texture_policy,
        "z_texture_status": 'pending_3d_materialization' if z_texture_policy else 'legacy',
        "road_width_contract": width_contract,
        "source_road_recovery": recovery,
        "print_acceptance": "pending_actual_slicing",
        "surface_owners": [
            {'source_index': int(owner), 'source_role': 'block_base' if owner < nbase else 'BO',
             'source_geometry_sha256': hashlib.sha256(original[owner].wkb).hexdigest(),
             'height_mm': heights[owner]} for owner in owners],
        "landmark_owners": landmark_owners,
        "numerical_cleanup": {'urban': urban_cleanup, 'landmarks': landmark_cleanup},
        "consumers": ["S7", "S8"],
        "language": "zh-CN",
        "说明": "道路决策固定；裁切只执行一次；预览与网格复用同一平面及高度。",
        "passed": passed,
        "surface_roads": local,
        "major_roads": major,
        "road_surface_plan": road_plan,
        "scale_mm_per_m": float(scale),
        "bbox_local_m": list(bbox_local),
        "before": before,
        "after": after,
        "area_retention": (after["summed_area_m2"] / before["summed_area_m2"]
                           if before["summed_area_m2"] else None),
        "input_polygons": len(original),
        "output_polygons": len(final),
        "xy_deformation": "disabled_after_s6",
        "geometry_fingerprint": surface_fingerprint(final, scale, surface_heights(layers)),
        "landmark_fingerprint": surface_fingerprint(
            [p for p, h in layers.BL], scale, [float(h) for p, h in layers.BL]),
    })
    layers.surface_road_reveals = reveals
    layers.surface_plan_evidence = evidence
    if terrain_surface_plan is not None:
        from aesthetic.surface_grounding import resolve_grounding
        city = resolve_grounding(list(layers.block_base) + list(layers.BO),
            surface_heights(layers), scale, terrain_surface_plan,
            ['draped_thickness'] * len(layers.block_base)
            + ['flat_roof_above_highest_support'] * len(layers.BO),
            flat_block_relief_limit_mm=(z_texture_policy['max_block_relief_mm'] if z_texture_policy else None))
        landmarks = resolve_grounding([p for p, h in layers.BL],
            [float(h) for p, h in layers.BL], scale, terrain_surface_plan,
            ['flat_roof_above_highest_support'] * len(layers.BL))
        layers.surface_grounding = {'city': city, 'landmarks': landmarks}
        evidence['grounding'] = {role: plan['evidence'] for role, plan in layers.surface_grounding.items()}
        from aesthetic.road_grounding import resolve_road_grounding
        road_grounding = resolve_road_grounding(layers, scale, terrain_surface_plan)
        layers.surface_grounding['roads'] = road_grounding
        evidence['grounding']['roads'] = road_grounding['evidence']
        if z_texture_policy:
            from aesthetic.z_texture import plan_ground_texture
            texture = plan_ground_texture(layers, bbox_local, scale, terrain_surface_plan, z_texture_policy)
            layers.surface_grounding['ground_texture'] = texture
            evidence['grounding']['ground_texture'] = texture['evidence']
            evidence['z_texture_status'] = 'frozen_in_s6'
    return evidence


def verify_surface_plan(layers, scale):
    evidence = getattr(layers, "surface_plan_evidence", {}) or {}
    actual = surface_fingerprint(
        list(layers.block_base) + list(layers.BO), scale, surface_heights(layers))
    if (evidence.get("status") != "finalized"
            or actual != evidence.get("geometry_fingerprint")):
        raise ValueError("final surface plan changed after S6; refuse stale preview/mesh")
    road = evidence.get('road_surface_plan')
    if not road or road_surface_fingerprint(
            layers.surface_road_polygons, scale, road['height_mm'],
            road['base_offset_mm']) != road['geometry_fingerprint']:
        raise ValueError('road surface plan changed after S6')
    if surface_fingerprint([p for p, h in layers.BL], scale,
                           [float(h) for p, h in layers.BL]) != evidence.get('landmark_fingerprint'):
        raise ValueError('landmark surface plan changed after S6')
    if evidence.get('grounding'):
        from aesthetic.surface_grounding import grounding_digest
        ground = getattr(layers, 'surface_grounding', {}) or {}
        for role in evidence['grounding']:
            if (role not in ground or grounding_digest(ground[role]) !=
                    evidence['grounding'][role]['fingerprint']):
                raise ValueError('grounding plan changed after S6')
    return evidence


def materialize_city_role(layers, role, scale, sample_z_m):
    """Common GLB/3MF mass entry; old snapshots explicitly keep legacy policy."""
    from _TEXTURE_STYLE_OF_DEEPSEEK.prepared_surface import materialize_flat_surfaces
    verify_surface_plan(layers, scale)
    if role == 'city':
        polys, heights = list(layers.block_base) + list(layers.BO), surface_heights(layers)
    elif role == 'landmarks':
        polys, heights = [p for p, h in layers.BL], [float(h) for p, h in layers.BL]
    else:
        raise ValueError('unknown mass grounding role')
    grounding = (getattr(layers, 'surface_grounding', {}) or {}).get(role)
    if grounding is not None:
        from aesthetic.surface_grounding import materialize_grounding, require_terrain_binding
        require_terrain_binding(grounding, sample_z_m)
        return materialize_grounding(grounding, polys, heights, scale)
    return materialize_flat_surfaces(polys, heights, scale, sample_z_m)


def materialize_road_surfaces(layers, scale, sample_z_m):
    from _TEXTURE_STYLE_OF_DEEPSEEK.prepared_surface import materialize_flat_surfaces
    plan = verify_surface_plan(layers, scale)['road_surface_plan']
    grounding = (getattr(layers, 'surface_grounding', {}) or {}).get('roads')
    if grounding is not None:
        from aesthetic.road_grounding import materialize_road_grounding
        mesh, proof = materialize_road_grounding(grounding, layers, scale, sample_z_m)
    else:
        mesh, proof = materialize_flat_surfaces(
            layers.surface_road_polygons, [plan['height_mm']] * plan['polygon_count'],
            scale, sample_z_m, base_offset_mm=plan['base_offset_mm'])
    proof = {**proof, 'plan_fingerprint': plan['geometry_fingerprint']}
    if mesh is not None:
        mesh.metadata['surface_materialization'] = proof
    return mesh, proof


def verify_materialized_city(layers, meshes, block_proof, scale):
    """S8/S9 boundary: bind verified actual meshes to this exact S6 plan."""
    surface = getattr(layers, 'surface_plan_evidence', {}) or {}
    if surface.get('policy_version') != POLICY_VERSION:
        return  # historical, unprepared route is explicitly outside this contract
    from _TEXTURE_STYLE_OF_DEEPSEEK.prepared_surface import mesh_digest
    verify_surface_plan(layers, scale)
    texture = (getattr(layers, 'surface_grounding', {}) or {}).get('ground_texture')
    if texture is not None:
        terrain = meshes.get('terrain')
        proof = terrain.metadata.get('ground_texture', {}) if terrain is not None else {}
        if (not proof.get('materialized') or proof.get('fingerprint') != texture['fingerprint']
                or proof.get('mesh_sha256') != mesh_digest(terrain)):
            raise ValueError('terrain: missing or stale S6 ground texture materialization')
    for role, count in (('block_base', len(layers.block_base) + len(layers.BO)),
                        ('roads', len(layers.surface_road_polygons)),
                        ('landmarks', len(layers.BL))):
        mesh = meshes.get(role)
        if not count:
            if mesh is not None:
                raise ValueError(f'{role}: unplanned mesh')
            continue
        proof = ((block_proof or {}).get('materialization', {}) if role == 'block_base'
                 else (mesh.metadata.get('surface_materialization', {}) if mesh is not None else {}))
        if (mesh is None or not proof.get('passed') or proof.get('verified_polygons') != count
                or proof.get('lost_polygons') != 0 or proof.get('mesh_sha256') != mesh_digest(mesh)):
            raise ValueError(f'{role}: actual mesh is not bound to verified S6 materialization')
        if role == 'roads':
            if proof.get('plan_fingerprint') != surface['road_surface_plan']['geometry_fingerprint']:
                raise ValueError('roads: wrong S6 road plan')
            if surface.get('grounding', {}).get('roads') and proof.get('grounding_fingerprint') != surface['grounding']['roads']['fingerprint']:
                raise ValueError('roads: wrong S6 grounding plan')
        elif proof.get('input_geometry_fingerprint') != surface[
                'landmark_fingerprint' if role == 'landmarks' else 'geometry_fingerprint']:
            raise ValueError(f'{role}: wrong S6 surface plan')
        if surface.get('grounding') and role in ('block_base', 'landmarks'):
            ground_role = 'city' if role == 'block_base' else 'landmarks'
            if proof.get('grounding_fingerprint') != surface['grounding'][ground_role]['fingerprint']:
                raise ValueError(f'{role}: wrong S6 grounding plan')
