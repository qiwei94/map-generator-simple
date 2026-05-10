"""Vegetation processor — flat colored plates for parks, forests, and green spaces.

Uses Manifold library for guaranteed-watertight output (like water module).
"""

import numpy as np
import trimesh
import manifold3d
from shapely.geometry import Polygon, MultiPolygon
import geopandas as gpd

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.terrain import sample_terrain_z

from _TEXTURE_STYLE_OF_DEEPSEEK.config import (
    VEGETATION_COLOR,
    VEGETATION_Z_OFFSET_MM,
    VEGETATION_MIN_AREA_M2,
    VEGETATION_MAX_EDGE_M,
    VEGETATION_SIMPLIFY_TOL_M,
)


def _signed_area_2d(contour: np.ndarray) -> float:
    """Signed area of a 2D closed contour (positive = CCW)."""
    x = contour[:, 0]
    y = contour[:, 1]
    return 0.5 * float(np.sum(x[:-1] * y[1:] - x[1:] * y[:-1]))


def _ensure_ccw(contour: np.ndarray) -> np.ndarray:
    """Reverse contour if it is clockwise."""
    if _signed_area_2d(contour) < 0:
        return contour[::-1]
    return contour


def _ensure_cw(contour: np.ndarray) -> np.ndarray:
    """Reverse contour if it is counter-clockwise."""
    if _signed_area_2d(contour) > 0:
        return contour[::-1]
    return contour


def _shapely_poly_to_crosssection(poly: Polygon) -> manifold3d.CrossSection:
    """Convert a Shapely Polygon to a Manifold CrossSection."""
    if poly.is_empty or len(poly.exterior.coords) < 4:
        return manifold3d.CrossSection()

    try:
        exterior = np.array(poly.exterior.coords, dtype=np.float64)
        if exterior.shape[1] >= 3:
            exterior = exterior[:, :2]
        exterior = _ensure_ccw(exterior)

        contours = [exterior]

        for interior in poly.interiors:
            hole = np.array(interior.coords, dtype=np.float64)
            if hole.shape[1] >= 3:
                hole = hole[:, :2]
            if len(hole) >= 3:
                hole = _ensure_cw(hole)
                contours.append(hole)

        return manifold3d.CrossSection(contours)
    except Exception:
        return manifold3d.CrossSection()


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

    if np.linalg.norm(result[-1][:2] - result[0][:2]) > 1e-10:
        result.append(result[0])
    return np.array(result)


def _simplify_polygon(polygon: Polygon, tolerance_m: float) -> Polygon:
    """Simplify polygon using Douglas-Peucker to reduce vertex count."""
    if polygon.is_empty or len(polygon.exterior.coords) < 4:
        return polygon
    
    try:
        simplified = polygon.simplify(tolerance=tolerance_m, preserve_topology=True)
        if simplified.is_empty or not isinstance(simplified, Polygon):
            return polygon
        return simplified
    except Exception:
        return polygon


def _extrude_vegetation_manifold(poly: Polygon, height: float) -> manifold3d.Manifold:
    """Extrude a single vegetation polygon using Manifold.
    
    Returns empty Manifold on failure.
    """
    cs = _shapely_poly_to_crosssection(poly)
    if cs.is_empty():
        return manifold3d.Manifold()
    try:
        return cs.extrude(height=height)
    except Exception:
        return manifold3d.Manifold()


def build_deepseek_vegetation(gdf: gpd.GeoDataFrame,
                              terrain_mesh: trimesh.Trimesh,
                              scale: float = 1.0) -> trimesh.Trimesh:
    """Build deepseek-style vegetation features using Manifold.

    Each vegetation patch is extruded separately using Manifold (watertight),
    then concatenated as separate shells (no boolean union to avoid non-manifold edges).

    Args:
        gdf: GeoDataFrame of vegetation features in local UTM meters
        terrain_mesh: scaled terrain mesh (model mm) - used for Z sampling
        scale: mm per meter scale factor

    Returns:
        Single trimesh of all vegetation plates (multiple shells).
    """
    if gdf is None or len(gdf) == 0:
        return None

    # Vegetation plate thickness in model mm (applied after scaling)
    vegetation_thickness_mm = 0.2
    trimesh_parts = []
    n_processed = 0
    n_skipped_small = 0

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
                    n_skipped_small += 1
                    continue

                # Simplify polygon
                poly = _simplify_polygon(poly, VEGETATION_SIMPLIFY_TOL_M)
                if poly.area < VEGETATION_MIN_AREA_M2:
                    n_skipped_small += 1
                    continue

                # Sample terrain Z (returns model mm)
                centroid = poly.centroid
                if terrain_mesh is not None:
                    tz = sample_terrain_z(terrain_mesh,
                                          np.array([centroid.x]) * scale,
                                          np.array([centroid.y]) * scale)
                    vegetation_z = float(tz[0]) + VEGETATION_Z_OFFSET_MM
                    if np.isnan(vegetation_z):
                        continue
                else:
                    vegetation_z = VEGETATION_Z_OFFSET_MM

                # Extrude using Manifold (geometry in UTM meters, extrude in model mm)
                # First scale XY to model mm, then extrude in model mm
                cs = _shapely_poly_to_crosssection(poly)
                if cs.is_empty():
                    continue
                
                # Scale cross section to model mm before extruding
                cs_scaled = cs.scale((scale, scale))
                man = cs_scaled.extrude(height=vegetation_thickness_mm)
                if man.is_empty():
                    continue
                
                # Position at vegetation Z
                man = man.translate((0, 0, vegetation_z))
                
                # Convert to trimesh
                mesh_data = man.to_mesh()
                verts = np.array(mesh_data.vert_properties, dtype=np.float64)
                faces = np.array(mesh_data.tri_verts, dtype=np.int64)
                mesh = trimesh.Trimesh(vertices=verts, faces=faces, process=False)
                
                trimesh_parts.append(mesh)
                n_processed += 1

    if not trimesh_parts:
        print(f"  Vegetation: 0 features processed, {n_skipped_small} skipped (too small)")
        return None

    # Use manifold batch_boolean for proper watertight union
    manifold_parts = []
    for mesh in trimesh_parts:
        # Convert trimesh to manifold mesh
        mesh_req = manifold3d.Mesh(
            vert_properties=mesh.vertices.astype(np.float32),
            tri_verts=mesh.faces.astype(np.uint32)
        )
        man = manifold3d.Manifold(mesh_req)
        if not man.is_empty():
            manifold_parts.append(man)
    
    if not manifold_parts:
        return None
    
    if len(manifold_parts) == 1:
        combined_man = manifold_parts[0]
    else:
        combined_man = manifold3d.Manifold.batch_boolean(
            manifold_parts, manifold3d.OpType.Add,
        )
    
    if combined_man.is_empty():
        print("  Vegetation: Manifold union produced empty result")
        return None
    
    mesh_data = combined_man.to_mesh()
    verts = np.array(mesh_data.vert_properties, dtype=np.float64)
    faces = np.array(mesh_data.tri_verts, dtype=np.int64)
    merged = trimesh.Trimesh(vertices=verts, faces=faces, process=False)

    print(f"  Vegetation: {n_processed} features processed, {n_skipped_small} skipped (too small)")
    return merged
