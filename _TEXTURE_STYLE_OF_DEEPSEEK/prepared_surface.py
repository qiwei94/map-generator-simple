"""Strict, shared flat extrusion of approved S6 polygons. No filtering/repair.

Each returned shell is checked against its own approved footprint and Z range;
an invalid or missing part aborts the whole materialization. This is deliberately
not the legacy procedural-texture builder: any texture must first become an
explicit surface-plan parameter rather than a hidden S8 mutation.
"""
from __future__ import annotations

import hashlib
import numpy as np
import trimesh
from shapely.affinity import scale as scale_geometry
from shapely.geometry import Polygon
from shapely.ops import unary_union

from ._geom_utils import shapely_poly_to_crosssection


def mesh_digest(mesh):
    digest = hashlib.sha256()
    for array, dtype in ((mesh.vertices, '<f8'), (mesh.faces, '<i8')):
        values = np.asarray(array, dtype=dtype)
        digest.update(str(values.shape).encode())
        digest.update(values.tobytes())
    return digest.hexdigest()


def verify_flat_polygon(mesh, poly, scale, base_z, height):
    if mesh is None or not len(mesh.faces) or not mesh.is_watertight or not mesh.is_winding_consistent:
        raise ValueError('approved surface did not produce a closed, consistently wound mesh')
    if not np.isfinite(mesh.vertices).all():
        raise ValueError('non-finite surface coordinates')
    if not np.allclose(mesh.bounds[:, 2], [base_z, base_z + height], rtol=0, atol=1e-5):
        raise ValueError('actual surface height differs from approved relief')
    tops = mesh.triangles[mesh.face_normals[:, 2] > .99, :, :2]
    actual = unary_union([Polygon(t) for t in tops])
    expected = scale_geometry(poly, xfact=scale, yfact=scale, origin=(0, 0))
    error = actual.symmetric_difference(expected).area
    tolerance = max(1e-9, expected.area * 1e-5)
    if error > tolerance:
        raise ValueError(f'actual surface footprint differs from plan: {error} mm2')
    if actual.hausdorff_distance(expected) > max(1e-5, np.linalg.norm(mesh.extents[:2]) * 1e-6):
        raise ValueError('actual surface footprint boundary differs from plan')
    if abs(mesh.volume - expected.area * height) > max(1e-9, expected.area * height * 1e-5):
        raise ValueError('actual surface volume differs from approved extrusion')
    return float(error)


def materialize_flat_surfaces(polys, heights, scale, sample_z_m, *, base_offset_mm=0.):
    """sample_z_m accepts local-metre XY arrays and returns model-mm Z."""
    if len(polys) != len(heights) or not np.isfinite(scale) or scale <= 0:
        raise ValueError('invalid approved surface scale/heights')
    if not polys:
        return None, {'verified_polygons': 0, 'lost_polygons': 0}
    if any(p.is_empty or not p.is_valid or p.area <= 0 for p in polys):
        raise ValueError('invalid approved surface polygon')
    zs = np.asarray(sample_z_m(np.array([p.centroid.x for p in polys]),
                              np.array([p.centroid.y for p in polys])))
    if zs.shape != (len(polys),) or not np.isfinite(zs).all():
        raise ValueError('missing/non-finite terrain sample for approved surface')
    parts, max_error = [], 0.
    for index, (poly, height, z) in enumerate(zip(polys, heights, zs)):
        if not np.isfinite(height) or height <= 0:
            raise ValueError('invalid approved height')
        base = float(z) + base_offset_mm
        cs = shapely_poly_to_crosssection(poly).scale((scale, scale))
        solid = cs.extrude(float(height)).translate((0, 0, base))
        if solid.is_empty():
            raise ValueError(f'approved surface {index} disappeared during extrusion')
        # Keep Manifold's double precision. to_mesh() truncates to float32;
        # far-from-origin small footprints can then fail the unchanged S6
        # area contract even though the solid itself is geometrically exact.
        raw = solid.to_mesh64()
        mesh = trimesh.Trimesh(vertices=np.asarray(raw.vert_properties, dtype=float),
                               faces=np.asarray(raw.tri_verts), process=False)
        max_error = max(max_error, verify_flat_polygon(mesh, poly, scale, base, height))
        parts.append(mesh)
    mesh = trimesh.util.concatenate(parts)
    from aesthetic.city_surface_plan import surface_fingerprint
    proof = {'policy_version': 'verified-flat-surfaces-v1', 'passed': True,
             'input_geometry_fingerprint': surface_fingerprint(polys, scale, heights),
             'approved_polygons': len(polys), 'verified_polygons': len(parts),
             'lost_polygons': 0, 'max_footprint_error_mm2': max_error,
             'z_policy': 'shared_centroid_surface_sample',
             'validation_scope': 'closed_mesh_footprint_and_extrusion_only',
             'whole_footprint_grounding': 'not_verified',
             'bridge_bank_connection': 'not_verified',
             'texture': 'disabled_unplanned_displacement',
             'mesh_sha256': mesh_digest(mesh)}
    mesh.metadata['surface_materialization'] = proof
    return mesh, proof
