"""Vegetation processor — flat colored plates for parks, forests, green spaces.

Single Manifold pipeline: each qualifying patch becomes a CrossSection,
all CrossSections are extruded and Manifold-unioned. No trimesh round-trip
in the middle.
"""

from __future__ import annotations

import numpy as np
import trimesh
import manifold3d
from shapely.geometry import MultiPolygon, Polygon
import geopandas as gpd

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.terrain import sample_terrain_z
from _TEXTURE_STYLE_OF_DEEPSEEK._geom_utils import shapely_poly_to_crosssection
from _TEXTURE_STYLE_OF_DEEPSEEK.config import (
    VEGETATION_Z_OFFSET_MM,
    VEGETATION_THICKNESS_MM,
    VEGETATION_MIN_AREA_M2,
    VEGETATION_SIMPLIFY_TOL_M,
)


def _simplify_polygon(polygon: Polygon, tolerance_m: float) -> Polygon:
    """Douglas-Peucker simplification with safe fallback."""
    if polygon.is_empty or len(polygon.exterior.coords) < 4:
        return polygon
    try:
        simplified = polygon.simplify(tolerance=tolerance_m, preserve_topology=True)
        if simplified.is_empty or not isinstance(simplified, Polygon):
            return polygon
        return simplified
    except Exception:
        return polygon


def _polygon_to_manifold(poly: Polygon, scale: float, vegetation_z: float
                        ) -> manifold3d.Manifold:
    """Scale XY → mm, extrude to vegetation plate thickness, position at terrain Z."""
    cs = shapely_poly_to_crosssection(poly)
    if cs.is_empty():
        return manifold3d.Manifold()
    try:
        cs = cs.scale((scale, scale))
        man = cs.extrude(height=VEGETATION_THICKNESS_MM)
        return man.translate((0, 0, vegetation_z))
    except Exception:
        return manifold3d.Manifold()


def build_deepseek_vegetation(gdf: gpd.GeoDataFrame,
                              terrain_mesh: trimesh.Trimesh,
                              scale: float = 1.0) -> trimesh.Trimesh:
    """Build vegetation plates (flat colored patches above terrain).

    Args:
        gdf: GeoDataFrame of vegetation polygons in local UTM meters.
        terrain_mesh: scaled terrain mesh (model mm) for Z sampling.
        scale: mm-per-meter scale factor.

    Returns:
        Watertight trimesh of all vegetation plates (Manifold union).
    """
    if gdf is None or len(gdf) == 0:
        return None

    parts: list[manifold3d.Manifold] = []
    n_processed = 0
    n_skipped_small = 0

    for _, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        if not isinstance(geom, (Polygon, MultiPolygon)):
            continue

        polys = geom.geoms if isinstance(geom, MultiPolygon) else [geom]
        for poly in polys:
            if poly.area < VEGETATION_MIN_AREA_M2:
                n_skipped_small += 1
                continue

            poly = _simplify_polygon(poly, VEGETATION_SIMPLIFY_TOL_M)
            if poly.area < VEGETATION_MIN_AREA_M2:
                n_skipped_small += 1
                continue

            # Sample terrain Z at centroid (model mm) — terrain XY is already in mm
            if terrain_mesh is not None:
                centroid = poly.centroid
                tz = sample_terrain_z(
                    terrain_mesh,
                    np.array([centroid.x]) * scale,
                    np.array([centroid.y]) * scale,
                )
                if len(tz) == 0 or np.isnan(tz[0]):
                    continue
                vegetation_z = float(tz[0]) + VEGETATION_Z_OFFSET_MM
            else:
                vegetation_z = VEGETATION_Z_OFFSET_MM

            man = _polygon_to_manifold(poly, scale, vegetation_z)
            if not man.is_empty():
                parts.append(man)
                n_processed += 1

    print(f"  Vegetation: {n_processed} extruded, {n_skipped_small} skipped (too small)")

    if not parts:
        return None

    if len(parts) == 1:
        combined = parts[0]
    else:
        combined = manifold3d.Manifold.batch_boolean(parts, manifold3d.OpType.Add)

    if combined.is_empty():
        return None

    mesh_data = combined.to_mesh()
    verts = np.array(mesh_data.vert_properties, dtype=np.float64)
    faces = np.array(mesh_data.tri_verts, dtype=np.int64)
    return trimesh.Trimesh(vertices=verts, faces=faces, process=False)
