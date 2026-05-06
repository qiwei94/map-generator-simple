"""Building processor — 4mm simplified block extrusions with terrain embedding.

Strategy B (simplified block extrusion) matching Hangzhou/Chicago reference style.
Buildings are embedded 0.5mm into terrain for FDM fusion.
"""

import numpy as np
import trimesh
from shapely.geometry import Polygon, MultiPolygon
from shapely.ops import unary_union
import geopandas as gpd
from concurrent.futures import ThreadPoolExecutor, as_completed
import os

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.terrain import sample_terrain_z

from _TEXTURE_STYLE_OF_DEEPSEEK.config import (
    BUILDING_HEIGHT_MM,
    BUILDING_HEIGHT_MIN_MM,
    BUILDING_HEIGHT_MAX_MM,
    BUILDING_HEIGHT_OSM_MIN_M,
    BUILDING_HEIGHT_OSM_MAX_M,
    Z_BUILDING_EMBED_MM,
    BUILDING_SIMPLIFY_TOL_M,
    BUILDING_MIN_AREA,
    BUILDING_DEFAULT_HEIGHT_M,
    BUILDING_LEVEL_HEIGHT_M,
    BUILDING_COLOR,
    TERRAIN_THICKNESS_MM,
    Z_TERRAIN_BASE,
    get_area_class,
    estimate_building_height_from_area,
)


def _compress_height(est_height_m: float, area_m2: float) -> float:
    """Compress real-world building height to model mm range (3.0-5.3mm)."""
    if est_height_m > 0:
        effective = est_height_m
    else:
        effective = estimate_building_height_from_area(area_m2)

    h_min = BUILDING_HEIGHT_OSM_MIN_M
    h_max = BUILDING_HEIGHT_OSM_MAX_M
    m_min = BUILDING_HEIGHT_MIN_MM
    m_max = BUILDING_HEIGHT_MAX_MM

    compressed = m_min + (effective - h_min) / (h_max - h_min) * (m_max - m_min)
    return max(m_min, min(m_max, compressed))


def _extrude_footprint(footprint: Polygon, height_mm: float,
                       terrain_z: float, scale: float = 1.0) -> trimesh.Trimesh:
    """Extrude a single building footprint into a solid block.

    Args:
        footprint: building footprint polygon (simplified, in meters)
        height_mm: model building height in mm
        terrain_z: terrain Z at building centroid (model mm)

    Returns:
        Solid trimesh block, embedded 0.5mm into terrain.
    """
    # Scale XY from meters to mm
    exterior_m = np.array(footprint.exterior.coords)
    exterior_mm = exterior_m.copy()
    exterior_mm[:, :2] *= scale

    # Compute Z bounds
    # Match reference model: building bottom is ~0.04mm below terrain surface
    # (reference: terrain bottom 2.61mm, building bottom 2.57mm, difference 0.04mm)
    # This means buildings sit almost ON terrain, slightly embedded for FDM fusion
    z_bottom = terrain_z - 0.04  # minimal embedding like reference model
    z_top = z_bottom + height_mm

    # Build vertices: top face + bottom face (each vertex duplicated for Z planes)
    n_verts = len(exterior_mm) - 1  # closed ring, exclude last duplicate
    top_verts = np.column_stack([exterior_mm[:-1, :2], np.full(n_verts, z_top)])
    bot_verts = np.column_stack([exterior_mm[:-1, :2], np.full(n_verts, z_bottom)])

    all_verts = np.vstack([top_verts, bot_verts])

    # Build faces
    faces = []
    # Top face: fan triangulation
    for i in range(1, n_verts - 1):
        faces.append([0, i, i + 1])

    # Bottom face: fan triangulation (reversed winding)
    for i in range(1, n_verts - 1):
        faces.append([n_verts, n_verts + i + 1, n_verts + i])

    # Side walls: two triangles per edge
    for i in range(n_verts):
        j = (i + 1) % n_verts
        # Top edge: i, j; Bottom edge: n_verts+i, n_verts+j
        faces.append([i, j, n_verts + i])
        faces.append([j, n_verts + j, n_verts + i])

    mesh = trimesh.Trimesh(vertices=all_verts, faces=np.array(faces, dtype=np.int64))
    mesh.merge_vertices()
    mesh.update_faces(mesh.nondegenerate_faces())
    mesh.update_faces(mesh.unique_faces())

    if len(mesh.faces) == 0:
        return None

    return mesh


def _build_one_building(args) -> trimesh.Trimesh:
    """Build one building mesh (for parallel processing).

    Args:
        args: (idx, polygon, height_mm, terrain_z)

    Returns:
        Single building trimesh or None.
    """
    idx, polygon, height_mm, terrain_z = args
    try:
        return _extrude_footprint(polygon, height_mm, terrain_z)
    except Exception:
        return None


def build_deepseek_buildings(gdf: gpd.GeoDataFrame,
                             terrain_mesh: trimesh.Trimesh,
                             area_km2: float = 0,
                             scale: float = 1.0) -> trimesh.Trimesh:
    """Build deepseek-style building blocks.

    Args:
        gdf: GeoDataFrame of building footprints in local UTM meters
        terrain_mesh: scaled terrain mesh (already in model mm)
        area_km2: area for LOD filtering

    Returns:
        Merged trimesh of all building blocks, or None if no buildings.
    """
    if gdf is None or len(gdf) == 0:
        return None

    area_class = get_area_class(area_km2)
    min_area = BUILDING_MIN_AREA.get(area_class, 30)

    # Filter and prepare buildings
    valid_buildings = []
    for idx, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        if isinstance(geom, MultiPolygon):
            polys = list(geom.geoms)
        else:
            polys = [geom]

        for poly in polys:
            if poly.area < min_area:
                continue
            # Simplify footprint
            simplified = poly.simplify(BUILDING_SIMPLIFY_TOL_M, preserve_topology=True)
            if simplified.is_empty or simplified.area < 1.0:
                continue
            # Keep as Polygon (ignore MultiPolygon from simplify)
            if isinstance(simplified, MultiPolygon):
                simplified = max(simplified.geoms, key=lambda g: g.area)
            if not isinstance(simplified, Polygon) or simplified.is_empty:
                continue

            valid_buildings.append((idx, simplified))

    if not valid_buildings:
        return None

    # Batch sample terrain Z at centroids
    centroids_x = np.array([p.centroid.x for _, p in valid_buildings])
    centroids_y = np.array([p.centroid.y for _, p in valid_buildings])
    terrain_z = sample_terrain_z(terrain_mesh, centroids_x * scale, centroids_y * scale)

    # Prepare building tasks
    building_meshes = []
    for i, ((idx, poly), tz) in enumerate(zip(valid_buildings, terrain_z)):
        if np.isnan(tz):
            continue
        # OSM height
        est_height = gdf.loc[idx].get("est_height", 0)
        area_m2 = poly.area
        height_mm = _compress_height(est_height, area_m2)
        building_meshes.append(_extrude_footprint(poly, height_mm, tz, scale))

    # Filter None results
    building_meshes = [m for m in building_meshes if m is not None and len(m.faces) > 0]

    if not building_meshes:
        return None

    # Merge all buildings
    merged = trimesh.util.concatenate(building_meshes)
    merged.merge_vertices()
    merged.update_faces(merged.nondegenerate_faces())
    merged.update_faces(merged.unique_faces())

    return merged
