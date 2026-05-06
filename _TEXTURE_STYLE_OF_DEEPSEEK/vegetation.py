"""Vegetation processor — flat colored plates for parks, forests, and green spaces.

Matching reference model vegetation implementation: vegetation is a thin plate
sitting at terrain level, with slight elevation to distinguish from terrain.
"""

import numpy as np
import trimesh
from shapely.geometry import Polygon, MultiPolygon
import geopandas as gpd

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.terrain import sample_terrain_z

from _TEXTURE_STYLE_OF_DEEPSEEK.config import (
    VEGETATION_COLOR,
    VEGETATION_Z_OFFSET_MM,
    VEGETATION_MIN_AREA_M2,
    VEGETATION_MAX_EDGE_M,
)


def _densify_ring(coords: np.ndarray, max_edge_m: float) -> np.ndarray:
    """Insert vertices so no edge exceeds max_edge_m (meters)."""
    if len(coords) < 2:
        return coords

    result = [coords[0]]
    for i in range(1, len(coords)):
        p0 = coords[i - 1]
        p1 = coords[i]
        seg_len = np.linalg.norm(p1[:2] - p0[:2])
        if seg_len > max_edge_m:
            n_splits = int(np.ceil(seg_len / max_edge_m))
            for j in range(1, n_splits + 1):
                t = j / n_splits
                result.append(p0 + t * (p1 - p0))
        else:
            result.append(p1)

    # Ensure closure
    if np.linalg.norm(result[-1][:2] - result[0][:2]) > 1e-10:
        result.append(result[0])
    return np.array(result)


def _triangulate_flat_polygon(polygon: Polygon):
    """Triangulate a polygon into a flat mesh at Z=0 (2D triangulation).

    Returns:
        (vertices_2d, faces) or (None, None) on failure.
    """
    try:
        from mapbox_earcut import triangulate_float64 as earcut

        rings = [np.array(polygon.exterior.coords)[:, :2]]
        for interior in polygon.interiors:
            rings.append(np.array(interior.coords)[:, :2])

        vertices = np.vstack(rings)
        ring_end_indices = np.cumsum([len(r) for r in rings])

        faces = earcut(vertices, ring_end_indices)
        if faces is None or len(faces) == 0:
            return None, None
        faces = faces.reshape(-1, 3)
        return vertices, faces
    except ImportError:
        pass

    # Fallback: centroid fan triangulation
    try:
        exterior = np.array(polygon.exterior.coords)
        if len(exterior) < 4:
            return None, None
        centroid = np.array(polygon.centroid.coords[0])
        from shapely.geometry import Point
        if not polygon.contains(Point(centroid)):
            tris = []
            for i in range(1, len(exterior) - 2):
                tris.append(exterior[0])
                tris.append(exterior[i])
                tris.append(exterior[i + 1])
            if not tris:
                return None, None
            vertices = np.array(tris)
            faces = np.arange(len(vertices)).reshape(-1, 3)
            return vertices, faces

        verts = []
        for i in range(len(exterior) - 1):
            verts.append(exterior[i])
            verts.append(exterior[i + 1])
            verts.append(centroid)
        vertices = np.array(verts)
        faces = np.arange(len(vertices)).reshape(-1, 3)
        return vertices, faces
    except Exception:
        return None, None


def _build_vegetation_polygon(polygon: Polygon,
                              terrain_mesh: trimesh.Trimesh,
                              scale: float = 1.0) -> trimesh.Trimesh:
    """Build a vegetation plate from a single polygon.

    Returns a mesh with 2 Z planes (top and bottom), thin plate style.
    Z position is set at the terrain level + offset.
    """
    if polygon.area < VEGETATION_MIN_AREA_M2:
        return None

    # Sample terrain Z at centroid to determine vegetation level
    centroid = polygon.centroid
    tz = sample_terrain_z(terrain_mesh,
                          np.array([centroid.x]) * scale,
                          np.array([centroid.y]) * scale)
    vegetation_z = float(tz[0]) + VEGETATION_Z_OFFSET_MM

    if np.isnan(vegetation_z):
        return None

    # Densify boundary for smoother edges
    boundary = np.array(polygon.exterior.coords)
    dense_boundary = _densify_ring(boundary, VEGETATION_MAX_EDGE_M)

    # Create densified polygon
    try:
        dense_poly = Polygon(dense_boundary)
    except Exception:
        dense_poly = polygon

    # Triangulate
    verts_2d, faces_2d = _triangulate_flat_polygon(dense_poly)
    if verts_2d is None or faces_2d is None:
        return None

    # Scale XY
    verts_2d_mm = verts_2d.copy()
    verts_2d_mm[:, :2] *= scale

    n_verts = len(verts_2d_mm)
    z_top = vegetation_z
    z_bot = vegetation_z - 0.2  # 0.2mm thickness for vegetation plate

    # Top face vertices
    top_verts = np.column_stack([verts_2d_mm[:, :2], np.full(n_verts, z_top)])

    # Bottom face vertices
    bot_verts = np.column_stack([verts_2d_mm[:, :2], np.full(n_verts, z_bot)])

    # Combine: [top (0..n-1), bottom (n..2n-1)]
    all_verts = np.vstack([top_verts, bot_verts])

    # Top faces (original winding)
    top_faces = faces_2d.copy()

    # Bottom faces (reversed winding - facing downward)
    bot_faces = faces_2d.copy() + n_verts
    bot_faces = bot_faces[:, [0, 2, 1]]

    all_faces = np.vstack([top_faces, bot_faces])

    mesh = trimesh.Trimesh(vertices=all_verts, faces=np.array(all_faces, dtype=np.int64))
    mesh.merge_vertices()
    mesh.update_faces(mesh.nondegenerate_faces())
    mesh.update_faces(mesh.unique_faces())

    return mesh


def build_deepseek_vegetation(gdf: gpd.GeoDataFrame,
                              terrain_mesh: trimesh.Trimesh,
                              scale: float = 1.0) -> trimesh.Trimesh:
    """Build deepseek-style vegetation features.

    Args:
        gdf: GeoDataFrame of vegetation features in local UTM meters
        terrain_mesh: scaled terrain mesh (model mm)
        scale: mm per meter scale factor

    Returns:
        Merged trimesh of all vegetation plates, or None if no vegetation.
    """
    if gdf is None or len(gdf) == 0:
        return None

    vegetation_meshes = []

    for idx, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue

        if isinstance(geom, (Polygon, MultiPolygon)):
            if isinstance(geom, MultiPolygon):
                polys = list(geom.geoms)
            else:
                polys = [geom]

            for poly in polys:
                if poly.area < VEGETATION_MIN_AREA_M2:
                    continue
                mesh = _build_vegetation_polygon(poly, terrain_mesh, scale)
                if mesh is not None and len(mesh.faces) > 0:
                    vegetation_meshes.append(mesh)

    if not vegetation_meshes:
        return None

    merged = trimesh.util.concatenate(vegetation_meshes)
    merged.merge_vertices()
    merged.update_faces(merged.nondegenerate_faces())
    merged.update_faces(merged.unique_faces())

    return merged
