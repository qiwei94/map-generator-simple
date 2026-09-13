"""S6 freezes terrain-conforming footprint patches; S8 only materializes them.

Coordinates are model mm. Roads/bridges intentionally do not use this mass
policy: bridge decks require independent bank/support evidence.
"""
from __future__ import annotations

import hashlib
import json
import numpy as np
import trimesh
from shapely.affinity import scale as scale_geometry
from shapely.geometry import Polygon, LineString
from shapely.ops import unary_union

VERSION = 'terrain-conforming-mass-v1'


def require_terrain_binding(plan, sample_z_m):
    owner = getattr(sample_z_m, '__self__', None)
    if getattr(owner, 'surface_plan_fingerprint', None) != plan['terrain_fingerprint']:
        raise ValueError('grounding requires the same frozen terrain sampler as S6')


def _parts(geometry):
    if geometry.geom_type == 'Polygon':
        if geometry.area > 0:
            yield geometry
    elif hasattr(geometry, 'geoms'):
        for part in geometry.geoms:
            yield from _parts(part)


def _triangulate(poly, _allow_fallback=True):
    vertices, faces = trimesh.creation.triangulate_polygon(poly, engine='earcut')
    triangles = []
    area = 0.
    for face in faces:
        triangle = np.asarray(vertices[face], dtype=float)
        a, b = triangle[1] - triangle[0], triangle[2] - triangle[0]
        cross = a[0] * b[1] - a[1] * b[0]
        if cross < 0:
            triangle = triangle[::-1]
        if abs(cross) > 1e-16:
            triangles.append(triangle)
            area += abs(cross) / 2
    if abs(area - poly.area) > max(1e-12, poly.area * 1e-8):
        # Earcut can add an exterior triangle when a hole touches the outer
        # ring at one vertex. Partition by Delaunay cells, then clip each cell
        # to the *same* approved polygon. This neither fills holes nor changes
        # the terrain plane: every cell remains within this terrain triangle.
        if not _allow_fallback:
            raise ValueError('grounding constrained triangulation lost polygon area')
        from shapely.ops import triangulate
        for cell in triangulate(poly):
            for part in _parts(cell.intersection(poly)):
                yield from _triangulate(part, _allow_fallback=False)
        return
    yield from triangles


def _terrain_patches(poly, terrain):
    """Clip against the exact frozen grid triangles, not a new tessellation."""
    z = np.asarray(terrain.surface_z_grid_mm)
    ny, nx = z.shape
    width = terrain.width_m * terrain.scale_mm_per_m
    height = terrain.height_m * terrain.scale_mm_per_m
    xa, ya = np.linspace(-width / 2, width / 2, nx), np.linspace(-height / 2, height / 2, ny)
    lo_x, lo_y, hi_x, hi_y = poly.bounds
    if lo_x < xa[0] - 1e-8 or hi_x > xa[-1] + 1e-8 or lo_y < ya[0] - 1e-8 or hi_y > ya[-1] + 1e-8:
        raise ValueError('grounding footprint outside frozen terrain')
    x0 = max(0, np.searchsorted(xa, lo_x, side='right') - 1)
    x1 = min(nx - 2, np.searchsorted(xa, hi_x, side='left'))
    y0 = max(0, np.searchsorted(ya, lo_y, side='right') - 1)
    y1 = min(ny - 2, np.searchsorted(ya, hi_y, side='left'))
    # Flat/planar neighbourhoods need no extra vertices. This test examines
    # every grid value in the bounding rectangle, including interior peaks.
    xx, yy = np.meshgrid(xa[x0:x1+2], ya[y0:y1+2])
    patch = z[y0:y1+2, x0:x1+2]
    ax = (patch[0, -1] - patch[0, 0]) / (xx[0, -1] - xx[0, 0])
    ay = (patch[-1, 0] - patch[0, 0]) / (yy[-1, 0] - yy[0, 0])
    predicted = patch[0, 0] + ax * (xx - xx[0, 0]) + ay * (yy - yy[0, 0])
    if np.max(np.abs(predicted - patch)) <= 1e-10:
        yield from _triangulate(poly)
        return
    for iy in range(y0, y1 + 1):
        for ix in range(x0, x1 + 1):
            sw, se = (xa[ix], ya[iy]), (xa[ix+1], ya[iy])
            nw, ne = (xa[ix], ya[iy+1]), (xa[ix+1], ya[iy+1])
            for corners in ((sw, se, nw), (se, ne, nw)):
                for piece in _parts(poly.intersection(Polygon(corners))):
                    yield from _triangulate(piece)


def _patch_mesh(poly, terrain, *, partition=None):
    points, lookup, faces = [], {}, []
    pieces = [poly] if partition is None else list(_parts(poly.intersection(partition))) + list(_parts(poly.difference(partition)))
    for triangle in (tri for piece in pieces for tri in _terrain_patches(piece, terrain)):
        face = []
        for xy in triangle:
            # Merge roundoff at adjacent clipped triangle boundaries, in mm.
            key = tuple(np.round(xy, 10))
            if key not in lookup:
                lookup[key] = len(points)
                points.append(key)
            face.append(lookup[key])
        if len(set(face)) == 3:
            faces.append(face)
    xy, faces = np.asarray(points, dtype=float), np.asarray(faces, dtype=np.int64)
    if not len(faces):
        raise ValueError('grounding lost approved footprint')
    # Earcut may omit a collinear point retained by the adjacent clipped
    # terrain triangle. Split affected faces, preserving every shared edge
    # vertex rather than repairing cracks with vertical internal walls.
    xy, faces = _conform_edges(xy, faces)
    xy, faces = _split_touching_boundary_fans(xy, faces)
    edges = np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    _, inverse, counts = np.unique(np.sort(edges, axis=1), axis=0, return_inverse=True, return_counts=True)
    if np.any(counts > 2):
        raise ValueError('grounding has non-manifold planar edges')
    boundary = edges[counts[inverse] == 1]
    actual_boundary = unary_union([LineString(xy[e]) for e in boundary])
    if actual_boundary.hausdorff_distance(poly.boundary) > 1e-7:
        raise ValueError('grounding triangulation has internal cracks')
    actual = unary_union([Polygon(xy[f]) for f in faces])
    if actual.symmetric_difference(poly).area > max(1e-9, poly.area * 1e-7):
        raise ValueError('grounding triangulation changed approved footprint')
    return xy, faces, boundary


def _split_touching_boundary_fans(xy, faces):
    """Duplicate coincident XY vertices at point contacts, not their geometry.

    A valid polygon hole may touch its exterior at one point. Extruding that
    shared vertex makes four walls share a vertical edge. Separate its local
    edge-connected triangle fans so each has its own closed vertical seam.
    """
    edges = np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    _, inverse, counts = np.unique(np.sort(edges, axis=1), axis=0,
                                   return_inverse=True, return_counts=True)
    boundary = edges[counts[inverse] == 1]
    vertices, degree = np.unique(boundary, return_counts=True)
    candidates = vertices[degree > 2]
    if not len(candidates):
        return xy, faces
    points, refined = xy.tolist(), faces.copy()
    for vertex in candidates:
        incident = np.flatnonzero(np.any(refined == vertex, axis=1))
        pending = set(map(int, incident))
        fans = []
        neighbours = {int(i): set(map(int, refined[i])) - {int(vertex)} for i in incident}
        while pending:
            fan, stack = [], [pending.pop()]
            while stack:
                face = stack.pop()
                fan.append(face)
                connected = {i for i in pending if neighbours[face] & neighbours[i]}
                pending -= connected
                stack.extend(connected)
            fans.append(fan)
        for fan in fans[1:]:
            replacement = len(points)
            points.append(xy[vertex].tolist())
            for i in fan:
                refined[i, refined[i] == vertex] = replacement
    return np.asarray(points), refined


def _conform_edges(xy, faces):
    from scipy.spatial import cKDTree
    edges = np.concatenate([faces[:,[0,1]], faces[:,[1,2]], faces[:,[2,0]]])
    _, inverse, counts = np.unique(np.sort(edges,axis=1),axis=0,return_inverse=True,return_counts=True)
    candidates = edges[counts[inverse] == 1]
    tree, splits = cKDTree(xy), {}
    for a,b in candidates:
        delta = xy[b]-xy[a]
        length = np.linalg.norm(delta)
        ids = np.asarray(tree.query_ball_point((xy[a]+xy[b])/2, length/2+1e-9), dtype=int)
        t = (xy[ids]-xy[a]) @ delta / (length*length)
        error = np.linalg.norm(xy[ids] - (xy[a] + t[:,None]*delta), axis=1)
        keep = (t>1e-9/length) & (t<1-1e-9/length) & (error<1e-9)
        if np.any(keep):
            interior = ids[keep][np.argsort(t[keep])].tolist()
            splits[(int(a),int(b))] = interior
            splits[(int(b),int(a))] = interior[::-1]
    if not splits:
        return xy, faces
    points, refined = xy.tolist(), []
    for face in faces:
        ring = []
        for a,b in zip(face, np.roll(face,-1)):
            ring.append(int(a))
            ring.extend(splits.get((int(a),int(b)), []))
        if len(ring) == 3:
            refined.append(face.tolist())
        else:
            center = len(points)
            points.append(xy[face].mean(axis=0).tolist())
            refined.extend([[a,b,center] for a,b in zip(ring, ring[1:]+ring[:1])])
    return np.asarray(points), np.asarray(refined, dtype=np.int64)


def grounding_digest(plan):
    digest = hashlib.sha256(json.dumps(
        {k: plan[k] for k in ('version', 'terrain_fingerprint', 'input_geometry_fingerprint')},
        sort_keys=True).encode())
    if 'support_evidence' in plan:
        digest.update(json.dumps(plan['support_evidence'], sort_keys=True, allow_nan=False).encode())
    for patch in plan['patches']:
        digest.update(patch['mode'].encode())
        for key in ('xy', 'faces', 'boundary', 'bottom_z', 'top_z'):
            arr = np.asarray(patch[key], dtype='<i8' if key in ('faces', 'boundary') else '<f8')
            digest.update(str(arr.shape).encode())
            digest.update(arr.tobytes())
    return digest.hexdigest()


def resolve_grounding(polys, heights, scale, terrain, modes, *, base_offset_mm=0.,
                      flat_block_relief_limit_mm=None):
    from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import sample_terrain_surface_plan_z
    if flat_block_relief_limit_mm is not None and (not np.isfinite(flat_block_relief_limit_mm) or flat_block_relief_limit_mm<=0):
        raise ValueError('flat block relief limit must be finite and positive')
    from aesthetic.city_surface_plan import surface_fingerprint
    if (len(modes) != len(polys) or not np.isfinite(base_offset_mm)
            or not np.isclose(scale, terrain.scale_mm_per_m, rtol=0, atol=1e-12)):
        raise ValueError('grounding mode/terrain scale mismatch')
    plan = dict(version=VERSION, terrain_fingerprint=str(terrain.fingerprint),
                input_geometry_fingerprint=surface_fingerprint(polys, scale, heights), patches=[])
    reliefs, lifts = [], []
    flat_blocks, steep_blocks = 0, 0
    for poly, h, mode in zip(polys, heights, modes):
        if mode not in ('draped_thickness', 'flat_roof_above_highest_support'):
            raise ValueError('unknown S6 grounding mode')
        expected = scale_geometry(poly, xfact=scale, yfact=scale, origin=(0, 0))
        xy, faces, boundary = _patch_mesh(expected, terrain)
        bottom = sample_terrain_surface_plan_z(terrain, xy[:, 0], xy[:, 1]) + base_offset_mm
        top = bottom + h if mode == 'draped_thickness' else np.full(len(bottom), bottom.max() + h)
        if flat_block_relief_limit_mm is not None and mode == 'draped_thickness':
            from aesthetic.z_texture import choose_roof
            roof, reason = choose_roof(bottom, top, mode, flat_block_relief_limit_mm)
            if roof is not None:
                top = np.full(len(bottom), roof)
                mode = 'flat_roof_above_highest_support'
                flat_blocks += 1
            else:
                steep_blocks += 1
        centroid = expected.centroid
        zc = float(sample_terrain_surface_plan_z(terrain, centroid.x, centroid.y))
        reliefs.append(float(np.ptp(bottom)))
        lifts.append(float(bottom.max() - zc) if mode != 'draped_thickness' else 0.)
        patch = dict(mode=mode)
        for key, arr in dict(xy=xy, faces=faces, boundary=boundary,
                             bottom_z=bottom, top_z=top).items():
            # Immutable byte-backed arrays survive the runtime Context without
            # millions of Python float objects or an alias that can change S6.
            patch[key] = np.frombuffer(arr.tobytes(), dtype=arr.dtype).reshape(arr.shape)
        plan['patches'].append(patch)
    plan['fingerprint'] = grounding_digest(plan)
    plan['evidence'] = dict(version=VERSION, owner_stage='S6',
        terrain_fingerprint=plan['terrain_fingerprint'], fingerprint=plan['fingerprint'],
        polygon_count=len(polys), patch_triangles=sum(len(p['faces']) for p in plan['patches']),
        draped_polygons=modes.count('draped_thickness'),
        flat_roof_polygons=modes.count('flat_roof_above_highest_support'),
        low_relief_blocks_flattened=flat_blocks, steep_blocks_kept_draped=steep_blocks,
        flat_block_relief_limit_mm=flat_block_relief_limit_mm,
        base_offset_mm=float(base_offset_mm),
        max_underfoot_relief_mm=max(reliefs, default=0.),
        max_roof_raise_vs_centroid_mm=max(lifts, default=0.),
        validation_scope='frozen_uncarved_terrain_geometry',
        final_water_boolean_contact='pending', slicing='pending',
        说明='街块随地形保持厚度；建筑底面贴地、屋顶位于脚下最高地形加批准高度；桥面不使用此策略。')
    plan['evidence']['draped_polygons'] -= flat_blocks
    plan['evidence']['flat_roof_polygons'] += flat_blocks
    if flat_block_relief_limit_mm is not None:
        plan['evidence']['说明']='低坡街块按最高支撑加原厚度抬平；陡坡保持随地形；核心和地标不变。'
    return plan


def materialize_grounding(plan, polys, heights, scale):
    from aesthetic.city_surface_plan import surface_fingerprint
    from _TEXTURE_STYLE_OF_DEEPSEEK.prepared_surface import mesh_digest
    if (plan['fingerprint'] != grounding_digest(plan) or
            plan['input_geometry_fingerprint'] != surface_fingerprint(polys, scale, heights)
            or len(plan['patches']) != len(polys)):
        raise ValueError('grounding plan changed after S6')
    parts = []
    for patch in plan['patches']:
        xy, f, edges = patch['xy'], patch['faces'], patch['boundary']
        n = len(xy)
        vertices = np.vstack([np.column_stack([xy, patch['bottom_z']]),
                              np.column_stack([xy, patch['top_z']])])
        walls = []
        for a, b in edges:
            walls.extend([[a, b, b+n], [a, b+n, a+n]])
        mesh = trimesh.Trimesh(vertices=vertices,
            faces=np.vstack([f[:, ::-1], f+n, walls]), process=False)
        if not mesh.is_watertight or not mesh.is_winding_consistent or mesh.volume <= 0:
            raise ValueError('grounded footprint did not produce a closed positive shell')
        # Exact integral of piecewise-linear vertical thickness; no constant
        # extrusion-volume test that would accidentally reject a flat roof.
        a, b = xy[f[:, 1]]-xy[f[:, 0]], xy[f[:, 2]]-xy[f[:, 0]]
        area = np.abs(a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0]) / 2
        expected_volume = np.sum(area * (patch['top_z']-patch['bottom_z'])[f].mean(axis=1))
        if not np.isclose(mesh.volume, expected_volume, rtol=1e-7, atol=1e-9):
            raise ValueError('grounding volume differs from frozen thickness')
        parts.append(mesh)
    result = trimesh.util.concatenate(parts) if parts else None
    proof = dict(policy_version=VERSION, passed=True, input_geometry_fingerprint=plan['input_geometry_fingerprint'],
                 grounding_fingerprint=plan['fingerprint'], approved_polygons=len(polys),
                 verified_polygons=len(parts), lost_polygons=0,
                 z_policy='s6_frozen_role_grounding',
                 validation_scope='frozen_uncarved_terrain_geometry',
                 whole_footprint_grounding='verified_against_frozen_patches',
                 final_water_boolean_contact='not_verified', bridge_bank_connection='not_applicable',
                 mesh_sha256=mesh_digest(result) if result is not None else None)
    if result is not None:
        result.metadata['surface_materialization'] = proof
    return result, proof
