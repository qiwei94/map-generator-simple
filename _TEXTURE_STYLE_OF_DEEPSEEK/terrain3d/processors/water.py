"""Generate 3D water feature meshes."""

import logging
import numpy as np
import trimesh
import geopandas as gpd
from shapely.geometry import Polygon, MultiPolygon, LineString, MultiLineString
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.config import (COLORS, WATER_Z_OFFSET, WATERWAY_WIDTHS,
                               ROAD_Z_OFFSET, WATER_MIN_AREA_M2,
                               WATER_PRINT_VERTEX_SPACING_M,
                               WATER_MAX_EDGE_M,
                               get_mesh_workers,
                               get_water_polygon_simplify_params,
                               get_water_smooth_enabled, get_water_smooth_iterations,
                               get_water_high_detail)

logger = logging.getLogger(__name__)


def _chaikin_smooth_ring(coords: np.ndarray, iterations: int = 2) -> np.ndarray:
    """Smooth a closed ring (exterior or interior) using Chaikin's corner cutting.
    Reduces sharp turns so rivers/lakes have smoother boundaries.
    """
    if coords.shape[0] < 4 or iterations <= 0:
        return coords
    pts = coords[:, :2].astype(np.float64)
    for _ in range(iterations):
        n = len(pts)
        new_pts = []
        for i in range(n):
            p0 = pts[i]
            p1 = pts[(i + 1) % n]
            new_pts.append(0.75 * p0 + 0.25 * p1)
            new_pts.append(0.25 * p0 + 0.75 * p1)
        pts = np.array(new_pts)
    return pts


def _chaikin_smooth_line(coords: np.ndarray, iterations: int = 2) -> np.ndarray:
    """Smooth an open line (river centerline) using Chaikin; endpoints preserved."""
    if coords.shape[0] < 3 or iterations <= 0:
        return coords[:, :2].astype(np.float64)
    pts = coords[:, :2].astype(np.float64)
    for _ in range(iterations):
        n = len(pts)
        new_pts = [pts[0]]
        for i in range(n - 1):
            p0 = pts[i]
            p1 = pts[i + 1]
            new_pts.append(0.75 * p0 + 0.25 * p1)
            new_pts.append(0.25 * p0 + 0.75 * p1)
        new_pts.append(pts[-1])
        pts = np.array(new_pts)
    return pts


def build_water_features(gdf: gpd.GeoDataFrame,
                         terrain_mesh: trimesh.Trimesh,
                         for_print: bool = False,
                         relief: bool = False) -> trimesh.Trimesh:
    """Build 3D meshes for all water features.

    Args:
        gdf: GeoDataFrame with water geometries in local coordinates
        terrain_mesh: terrain mesh for Z sampling
        for_print: if True, densify boundaries to avoid radiating lines
                   and place water on terrain surface
        relief: if True, use recessed water at terrain Z level
                (no extra offset, appears dark when printed thin)

    Returns:
        Combined trimesh of all water features
    """
    if gdf.empty:
        logger.info("No water features to process")
        return None

    def build_one(item):
        geom, kind, width = item
        try:
            if kind == "poly":
                return _build_water_polygon(geom, terrain_mesh, for_print=for_print, relief=relief)
            return _build_water_line(geom, width, terrain_mesh, relief=relief)
        except Exception:
            return None

    items = []
    for idx, row in gdf.iterrows():
        geom = row.geometry
        if isinstance(geom, (Polygon, MultiPolygon)):
            items.append((geom, "poly", None))
        elif isinstance(geom, (LineString, MultiLineString)):
            waterway = row.get("waterway", "stream")
            if isinstance(waterway, list):
                waterway = waterway[0]
            width = WATERWAY_WIDTHS.get(waterway, 5)
            items.append((geom, "line", width))

    workers = get_mesh_workers()
    meshes = []
    # Sequential only: terrain_mesh/sample_terrain_z in workers is not thread-safe (double free)
    if workers <= 1 or len(items) < 15:
        for item in tqdm(items, desc="Water meshes", leave=False):
            m = build_one(item)
            if m is not None:
                meshes.append(m)
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(build_one, item): i for i, item in enumerate(items)}
            for fut in tqdm(as_completed(futures), total=len(futures),
                            desc="Water meshes", leave=False):
                try:
                    m = fut.result()
                    if m is not None:
                        meshes.append(m)
                except Exception:
                    pass

    if not meshes:
        return None

    combined = trimesh.util.concatenate(meshes)

    # Clip to terrain XY bounds — smoothing and ribbon widths can push
    # water geometry past the original clipped bbox.
    combined = _clip_mesh_to_terrain_bounds(combined, terrain_mesh)
    if combined is None or len(combined.faces) == 0:
        return None

    if relief:
        from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.relief import get_relief_color
        relief_rgb = tuple(int(get_relief_color("water")[i:i+2], 16)
                           for i in (1, 3, 5)) + (255,)
        combined.visual.vertex_colors = np.tile(
            [relief_rgb], (len(combined.vertices), 1)
        ).astype(np.uint8)
    else:
        combined.visual.vertex_colors = np.tile(
            COLORS["water"], (len(combined.vertices), 1)
        ).astype(np.uint8)

    logger.info(f"Water mesh: {len(combined.vertices)} vertices, "
                f"{len(combined.faces)} faces")
    return combined


def _build_water_polygon(geom, terrain_mesh: trimesh.Trimesh,
                         for_print: bool = False,
                         relief: bool = False) -> trimesh.Trimesh:
    """Build flat mesh for a water polygon at terrain-relative height."""
    polygons = []
    if isinstance(geom, MultiPolygon):
        polygons = list(geom.geoms)
    elif isinstance(geom, Polygon):
        polygons = [geom]

    meshes = []
    for polygon in polygons:
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        if polygon.is_empty or polygon.area < WATER_MIN_AREA_M2:
            continue

        mesh = _triangulate_polygon(polygon, terrain_mesh, for_print=for_print, relief=relief)
        if mesh is not None:
            meshes.append(mesh)

    if not meshes:
        return None

    return trimesh.util.concatenate(meshes)


def _polygon_to_rings(polygon: Polygon):
    """Build list of ring arrays (exterior + interiors) from a Polygon. Returns ([ring_arrays], None) or (None, None) if invalid."""
    if not isinstance(polygon, Polygon) or polygon.is_empty:
        return None, None
    exterior = np.array(polygon.exterior.coords[:-1], dtype=np.float64)
    if len(exterior) < 3:
        return None, None
    rings = [exterior]
    for interior in polygon.interiors:
        hole = np.array(interior.coords[:-1], dtype=np.float64)
        if len(hole) < 3:
            continue
        rings.append(hole)
    return rings, None


def _triangulate_polygon(polygon: Polygon,
                        terrain_mesh: trimesh.Trimesh,
                        for_print: bool = False,
                        relief: bool = False) -> trimesh.Trimesh:
    """Triangulate a single polygon and place at water level.

    Args:
        polygon: Shapely polygon for the water feature.
        terrain_mesh: Terrain mesh for Z sampling.
        for_print: if True, densify boundary to avoid long triangulation edges
                   (radiating lines) and place water on terrain surface.
        relief: if True, use terrain Z at 25th percentile without extra offset
                (water sits at carved terrain level, appears dark when thin).
    """
    max_pts, tol_ratio = get_water_polygon_simplify_params()
    n_exterior = len(polygon.exterior.coords)
    has_holes = len(polygon.interiors) > 0
    # High-detail: skip extra simplify for polygons with holes to avoid damaging islands (e.g. 三潭印月).
    do_simplify = n_exterior > max_pts and not (get_water_high_detail() and has_holes)
    if do_simplify:
        tol = max(1.0, polygon.length / tol_ratio)
        polygon = polygon.simplify(tol, preserve_topology=True)
        if polygon.is_empty or polygon.area < 1.0:
            return None

    exterior_coords = np.array(polygon.exterior.coords[:-1], dtype=np.float64)
    if len(exterior_coords) < 3:
        return None

    if get_water_smooth_enabled():
        exterior_coords = _chaikin_smooth_ring(
            exterior_coords, iterations=get_water_smooth_iterations()
        )
    if len(exterior_coords) < 3:
        return None

    from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.terrain import sample_terrain_z

    # Sample terrain Z at boundary to determine water level.
    bx = exterior_coords[:, 0]
    by = exterior_coords[:, 1]
    z_at_boundary = sample_terrain_z(terrain_mesh, bx, by)
    if relief:
        z_base = float(np.nanpercentile(z_at_boundary, 25))
    else:
        z_base = float(np.nanpercentile(z_at_boundary, 25)) + WATER_Z_OFFSET

    # Build vertex array and ring info (smooth interiors too)
    rings = [exterior_coords]
    ring_end_indices = [len(exterior_coords)]
    for interior in polygon.interiors:
        hole = np.array(interior.coords[:-1], dtype=np.float64)
        if len(hole) < 3:
            continue
        if get_water_smooth_enabled():
            hole = _chaikin_smooth_ring(hole, iterations=get_water_smooth_iterations())
        if len(hole) < 3:
            continue
        rings.append(hole)
        ring_end_indices.append(ring_end_indices[-1] + len(hole))

    # Densify all rings so no boundary edge exceeds WATER_MAX_EDGE_M.
    # This prevents earcut from generating triangles that span across
    # the entire polygon (visible as "straight lines" in wireframe).
    for ri in range(len(rings)):
        rings[ri] = _densify_ring(rings[ri], WATER_MAX_EDGE_M)
    # Rebuild ring_end_indices after densification
    ring_end_indices = np.cumsum([len(r) for r in rings]).tolist()

    all_points_2d = np.vstack(rings).astype(np.float64)  # shape (N, 2)

    # Post-densification sanity check: warn if any edge still exceeds threshold
    _check_water_edges(all_points_2d, ring_end_indices)

    # Triangulate: earcut first; high-detail: retry with buffer(0) to fix invalid geometry before Delaunay.
    tri_indices = _earcut_triangulate(all_points_2d, ring_end_indices)
    if tri_indices is None and get_water_high_detail() and has_holes:
        fixed = polygon.buffer(0)
        if fixed.geom_type == "Polygon" and not fixed.is_empty and fixed.area >= 1.0:
            rings_f, _ = _polygon_to_rings(fixed)
            if rings_f is not None:
                all_points_2d = np.vstack(rings_f).astype(np.float64)
                ring_end_indices = np.cumsum([len(r) for r in rings_f]).tolist()
                tri_indices = _earcut_triangulate(all_points_2d, ring_end_indices)
    if tri_indices is None:
        tri_indices = _delaunay_triangulate(polygon, all_points_2d)
    if tri_indices is None or len(tri_indices) == 0:
        return None

    # Refine: split triangles with edges exceeding WATER_MAX_EDGE_M.
    # Densifying boundary alone doesn't prevent earcut from connecting
    # distant boundary vertices across the polygon interior.
    if WATER_MAX_EDGE_M > 0:
        all_points_2d, tri_indices = _refine_water_triangles(
            all_points_2d, tri_indices, WATER_MAX_EDGE_M
        )

    # Flat water surface at consistent elevation (lakes are level, not sloped).
    vertices = np.column_stack([
        all_points_2d[:, 0],
        all_points_2d[:, 1],
        np.full(len(all_points_2d), z_base)
    ])

    return trimesh.Trimesh(vertices=vertices, faces=tri_indices, process=False)


def _earcut_triangulate(all_points_2d: np.ndarray, ring_end_indices: list):
    """Earcut triangulation. Takes pre-built vertex array and ring end indices."""
    try:
        import mapbox_earcut
        coords = all_points_2d.astype(np.float64)
        rings = np.array(ring_end_indices, dtype=np.uint32)
        tri = mapbox_earcut.triangulate_float64(coords, rings)
        tri = np.array(tri).reshape(-1, 3)
        if len(tri) == 0:
            return None
        return tri
    except Exception:
        return None


def _densify_ring(coords: np.ndarray, max_spacing: float) -> np.ndarray:
    """Insert evenly-spaced vertices along a ring so no edge exceeds max_spacing.

    Breaks up long edges that would create visible radiating lines in the
    triangulation (e.g. across large lakes like 西湖).
    """
    if len(coords) < 2 or max_spacing <= 0:
        return coords
    result = [coords[0]]
    for i in range(len(coords)):
        a = coords[i]
        b = coords[(i + 1) % len(coords)]
        dist = np.linalg.norm(b - a)
        if dist > max_spacing:
            n_seg = max(2, int(np.ceil(dist / max_spacing)))
            for j in range(1, n_seg):
                t = j / n_seg
                result.append(a + t * (b - a))
        result.append(b)
    return np.array(result[:-1])  # last point == first point, remove duplicate


def _check_water_edges(all_points_2d: np.ndarray, ring_end_indices: list):
    """Warn if any boundary edge still exceeds WATER_MAX_EDGE_M after densification.

    Persistent long edges after densification usually indicate data loss
    (e.g. OSM way missing segments) rather than a triangulation issue.
    """
    if WATER_MAX_EDGE_M <= 0:
        return
    for ri in range(len(ring_end_indices)):
        start = ring_end_indices[ri - 1] if ri > 0 else 0
        end = ring_end_indices[ri]
        ring = all_points_2d[start:end]
        if len(ring) < 2:
            continue
        for i in range(len(ring)):
            a = ring[i]
            b = ring[(i + 1) % len(ring)]
            d = np.linalg.norm(b - a)
            if d > WATER_MAX_EDGE_M:
                logger.warning(
                    f"Water polygon ring[{ri}] has edge {d:.0f}m > "
                    f"threshold {WATER_MAX_EDGE_M}m after densification — "
                    f"possible data loss"
                )
                return  # warn once per polygon


def _refine_water_triangles(all_points_2d: np.ndarray,
                            tri_indices: np.ndarray,
                            max_edge_m: float,
                            max_iter: int = 5) -> tuple:
    """Iteratively split triangles with edges exceeding max_edge_m.

    Uses longest-edge bisection: for each face, find the longest edge,
    split it at midpoint, and replace the face with two new faces.
    Repeat until no edges exceed threshold.

    This approach correctly handles all cases (1, 2, or 3 long edges)
    and guarantees termination because each split reduces the longest edge.
    """
    points = all_points_2d.astype(np.float64)
    faces = tri_indices.copy()
    vi_next = len(points)

    for iteration in range(max_iter):
        # Find faces with long edges
        faces_to_split = []
        for idx, face in enumerate(faces):
            vi, vj, vk = int(face[0]), int(face[1]), int(face[2])
            e01 = np.linalg.norm(points[vj] - points[vi])
            e12 = np.linalg.norm(points[vk] - points[vj])
            e02 = np.linalg.norm(points[vk] - points[vi])
            longest = max(e01, e12, e02)
            if longest > max_edge_m:
                # Find which edge is longest
                if e01 >= e12 and e01 >= e02:
                    a, b, opp = vi, vj, vk
                elif e12 >= e01 and e12 >= e02:
                    a, b, opp = vj, vk, vi
                else:
                    a, b, opp = vi, vk, vj
                faces_to_split.append((idx, a, b, opp))

        if not faces_to_split:
            break

        # Batch-split: collect all unique long edges first
        edge_midpoints = {}  # (min_vi, max_vi) -> (midpoint_x, midpoint_y)
        for _, a, b, _ in faces_to_split:
            key = (min(a, b), max(a, b))
            if key not in edge_midpoints:
                mid = (points[a] + points[b]) / 2.0
                edge_midpoints[key] = mid

        # Assign new vertex indices
        for key in edge_midpoints:
            edge_midpoints[key] = (vi_next, edge_midpoints[key])
            vi_next += 1

        # Rebuild faces: split each flagged face along its longest edge
        new_face_list = []
        split_set = set(idx for idx, _, _, _ in faces_to_split)

        for idx, face in enumerate(faces):
            vi, vj, vk = int(face[0]), int(face[1]), int(face[2])

            if idx not in split_set:
                new_face_list.append([vi, vj, vk])
                continue

            # Find the longest edge for this face
            e01 = np.linalg.norm(points[vj] - points[vi])
            e12 = np.linalg.norm(points[vk] - points[vj])
            e02 = np.linalg.norm(points[vk] - points[vi])
            if e01 >= e12 and e01 >= e02:
                a, b, opp = vi, vj, vk
            elif e12 >= e01 and e12 >= e02:
                a, b, opp = vj, vk, vi
            else:
                a, b, opp = vi, vk, vj

            key = (min(a, b), max(a, b))
            mvi, _ = edge_midpoints[key]

            # Replace face with two: [a, opp, mvi] and [b, opp, mvi]
            new_face_list.append([a, opp, mvi])
            new_face_list.append([b, opp, mvi])

        faces = np.array(new_face_list, dtype=np.int64)

        # Add new vertices
        if edge_midpoints:
            new_verts = np.array([v for _, v in edge_midpoints.values()])
            points = np.vstack([points, new_verts])

        logger.debug(f"  Refine iteration {iteration + 1}: "
                     f"{len(faces_to_split)} faces split, "
                     f"{len(points)} vertices, {len(faces)} faces")

    return points, faces


def _delaunay_triangulate(polygon: Polygon, all_points_2d: np.ndarray):
    """Fallback: Delaunay on the 2D point set, clipped to polygon interior."""
    try:
        from scipy.spatial import Delaunay
        from shapely.geometry import Point
        if len(all_points_2d) < 3:
            return None
        tri = Delaunay(all_points_2d)
        faces = tri.simplices

        # Keep only triangles whose centroid lies inside the polygon
        centroids = all_points_2d[faces].mean(axis=1)
        mask = np.array([polygon.contains(Point(c)) for c in centroids])
        faces = faces[mask]

        if len(faces) == 0:
            return None
        return faces
    except Exception:
        return None


def _build_water_line(geom, width: float,
                      terrain_mesh: trimesh.Trimesh,
                      relief: bool = False) -> trimesh.Trimesh:
    """Build ribbon mesh for water linestrings (rivers, streams).

    Args:
        relief: if True, water sits at terrain Z without extra offset.
    """
    from shapely.geometry import LineString as ShapelyLineString
    from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.roads import _polyline_to_ribbon

    lines = []
    if isinstance(geom, MultiLineString):
        lines = list(geom.geoms)
    elif isinstance(geom, LineString):
        lines = [geom]

    meshes = []
    for line in lines:
        coords = np.array(line.coords)
        if len(coords) < 2:
            continue
        if get_water_smooth_enabled():
            smooth_2d = _chaikin_smooth_line(coords, iterations=get_water_smooth_iterations())
        else:
            smooth_2d = coords[:, :2].astype(np.float64)
        line_smooth = ShapelyLineString(smooth_2d)
        if relief:
            mesh = _polyline_to_ribbon(line_smooth, width, terrain_mesh, relief=True, ridge_height_mm=0.0)
        else:
            mesh = _polyline_to_ribbon(line_smooth, width, terrain_mesh)
        if mesh is not None:
            if relief:
                # Water line at terrain Z (ridge_height_mm=0 means Z=terrain_Z)
                pass
            else:
                # Ribbon Z is terrain + ROAD_Z_OFFSET; adjust to terrain + WATER_Z_OFFSET
                mesh.vertices[:, 2] += (WATER_Z_OFFSET - ROAD_Z_OFFSET)
            meshes.append(mesh)

    if not meshes:
        return None

    return trimesh.util.concatenate(meshes)


def _clip_mesh_to_terrain_bounds(mesh: trimesh.Trimesh,
                                  terrain_mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """Remove faces whose centroid is outside the terrain XY bounding box.

    After Chaikin smoothing and ribbon-width expansion, water geometry may
    extend past the terrain edge.  This trims those overhangs so water
    stays within the map area.
    """
    tv = terrain_mesh.vertices
    x_min, x_max = float(tv[:, 0].min()), float(tv[:, 0].max())
    y_min, y_max = float(tv[:, 1].min()), float(tv[:, 1].max())

    verts = mesh.vertices
    faces = mesh.faces
    centroids = verts[faces].mean(axis=1)  # (N, 3)

    inside = ((centroids[:, 0] >= x_min) & (centroids[:, 0] <= x_max) &
              (centroids[:, 1] >= y_min) & (centroids[:, 1] <= y_max))

    n_removed = int((~inside).sum())
    if n_removed > 0:
        logger.info(f"Water clip: removed {n_removed} faces outside terrain bounds")
        mesh.update_faces(inside)
        mesh.remove_unreferenced_vertices()

    return mesh
