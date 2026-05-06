"""Generate terrain mesh from elevation grid."""

import logging
import numpy as np
import trimesh

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.config import COLORS, get_area_class, TERRAIN_GRID, DECIMATION_TARGETS

logger = logging.getLogger(__name__)


def build_terrain_mesh(elevation_grid: np.ndarray,
                       width_m: float, height_m: float,
                       area_km2: float = 0) -> trimesh.Trimesh:
    """Build a 3D terrain mesh from a 2D elevation grid.

    Args:
        elevation_grid: 2D numpy array (rows, cols) of elevation in meters.
                       Rows: south->north, Cols: west->east.
        width_m: total width in meters (X axis)
        height_m: total height in meters (Y axis)
        area_km2: area for LOD decision

    Returns:
        trimesh.Trimesh with vertex colors encoding elevation
    """
    rows, cols = elevation_grid.shape
    logger.info(f"Building terrain mesh from {rows}x{cols} grid, "
                f"{width_m:.0f}m x {height_m:.0f}m")

    # Generate vertex positions
    # X: west->east (cols), Y: south->north (rows), Z: elevation
    x = np.linspace(-width_m / 2, width_m / 2, cols)
    y = np.linspace(-height_m / 2, height_m / 2, rows)
    xx, yy = np.meshgrid(x, y)

    vertices = np.column_stack([
        xx.ravel(),
        yy.ravel(),
        elevation_grid.ravel()
    ])

    # Generate triangle faces (two triangles per grid cell)
    faces = _generate_grid_faces(rows, cols)

    # Compute vertex colors based on elevation
    vertex_colors = _elevation_to_colors(elevation_grid.ravel())

    mesh = trimesh.Trimesh(
        vertices=vertices,
        faces=faces,
        vertex_colors=vertex_colors,
        process=False
    )

    # Decimate if needed
    area_class = get_area_class(area_km2)
    target = DECIMATION_TARGETS.get(area_class)
    if target and len(mesh.faces) > target:
        logger.info(f"Decimating terrain from {len(mesh.faces)} to ~{target} faces")
        try:
            mesh = mesh.simplify_quadric_decimation(face_count=target)
            # Re-apply colors after decimation (vertices may have changed)
            new_elevations = mesh.vertices[:, 2]
            mesh.visual.vertex_colors = _elevation_to_colors(new_elevations)
        except (TypeError, AttributeError, ImportError) as e:
            # fast_simplification may be incompatible with Python 3.9
            # Skip decimation and keep full-resolution mesh
            logger.warning(f"Terrain decimation failed ({e}), using full-resolution mesh "
                          f"({len(mesh.faces)} faces)")

    logger.info(f"Terrain mesh: {len(mesh.vertices)} vertices, {len(mesh.faces)} faces")
    return mesh


def _generate_grid_faces(rows: int, cols: int) -> np.ndarray:
    """Generate triangle face indices for a regular grid."""
    faces = []
    for i in range(rows - 1):
        for j in range(cols - 1):
            # Top-left vertex index
            tl = i * cols + j
            tr = tl + 1
            bl = (i + 1) * cols + j
            br = bl + 1

            # Two triangles per quad
            faces.append([tl, bl, tr])
            faces.append([tr, bl, br])

    return np.array(faces, dtype=np.int64)


def _elevation_to_colors(elevations: np.ndarray) -> np.ndarray:
    """Map elevation values to green-brown gradient colors."""
    low_color = np.array(COLORS["terrain_low"], dtype=np.float64)
    high_color = np.array(COLORS["terrain_high"], dtype=np.float64)

    e_min = np.nanmin(elevations)
    e_max = np.nanmax(elevations)

    if e_max - e_min < 1e-3:
        # Flat terrain - use low color
        colors = np.tile(low_color, (len(elevations), 1)).astype(np.uint8)
        return colors

    # Normalize to [0, 1]
    t = (elevations - e_min) / (e_max - e_min)
    t = np.clip(t, 0, 1)

    # Compress gradient for subtle monochrome terrain look
    t = t * 0.4

    # Interpolate colors
    colors = np.outer(1 - t, low_color) + np.outer(t, high_color)
    colors = np.clip(colors, 0, 255).astype(np.uint8)

    return colors


def get_terrain_resolution(area_km2: float) -> int:
    """Get terrain grid resolution based on area size."""
    area_class = get_area_class(area_km2)
    return TERRAIN_GRID[area_class]


def carve_terrain_for_water(terrain_mesh: trimesh.Trimesh,
                            water_gdf) -> int:
    """Push terrain vertices inside water polygons below the water surface.

    SRTM elevation data includes noise over water bodies (radar reflects
    off water surface).  This function flattens the terrain inside water
    polygon areas to create visible river/lake basins.

    Modifies *terrain_mesh* in-place.  Returns the number of carved vertices.

    Args:
        terrain_mesh: open terrain surface mesh (Z-up, meters)
        water_gdf: GeoDataFrame of water features in local coordinates
                   (already projected and clipped)
    """
    from shapely.geometry import Polygon, MultiPolygon
    from shapely import Point as ShapelyPoint
    import shapely

    verts = terrain_mesh.vertices
    total_carved = 0
    min_poly_area = 500.0  # only carve polygons > 500 m2

    for _, row in water_gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue

        polygons = []
        if isinstance(geom, Polygon):
            polygons = [geom]
        elif isinstance(geom, MultiPolygon):
            polygons = list(geom.geoms)
        else:
            continue

        for poly in polygons:
            if poly.area < min_poly_area:
                continue

            # Compute water surface Z from boundary terrain heights
            ext = np.array(poly.exterior.coords)
            bz = sample_terrain_z(terrain_mesh, ext[:, 0], ext[:, 1])
            valid = ~np.isnan(bz)
            if valid.sum() < 3:
                continue
            water_z = float(np.nanpercentile(bz[valid], 25))

            # Fast bounds pre-filter
            bnd = poly.bounds  # (minx, miny, maxx, maxy)
            in_box = ((verts[:, 0] >= bnd[0]) & (verts[:, 0] <= bnd[2]) &
                      (verts[:, 1] >= bnd[1]) & (verts[:, 1] <= bnd[3]))
            candidates = np.where(in_box)[0]
            if len(candidates) == 0:
                continue

            # Vectorised point-in-polygon via shapely 2.x
            pts = shapely.points(verts[candidates, 0], verts[candidates, 1])
            inside = shapely.contains(poly, pts)

            carve_idx = candidates[inside]
            # Push down vertices that are above water surface
            above = verts[carve_idx, 2] > water_z - 1.0
            carve_idx = carve_idx[above]
            if len(carve_idx) == 0:
                continue

            verts[carve_idx, 2] = water_z - 1.0  # 1 m below water surface
            total_carved += len(carve_idx)

    if total_carved > 0:
        terrain_mesh.vertices = verts
        # Recolor carved vertices (use low-elevation color)
        new_colors = _elevation_to_colors(terrain_mesh.vertices[:, 2])
        terrain_mesh.visual.vertex_colors = new_colors
        logger.info(f"Carved terrain for water: {total_carved} vertices pushed down")

    return total_carved


def sample_terrain_z(mesh: trimesh.Trimesh, x: np.ndarray,
                     y: np.ndarray) -> np.ndarray:
    """Sample Z (elevation) values from terrain mesh at given X,Y positions.

    Uses cKDTree nearest-neighbor interpolation instead of ray casting,
    which is faster and avoids access violations on meshes with degenerate faces.
    """
    if len(x) == 0:
        return np.array([])

    from scipy.spatial import cKDTree

    tree = cKDTree(mesh.vertices[:, :2])
    k = min(8, len(mesh.vertices))
    dists, idxs = tree.query(np.column_stack([x, y]), k=k)
    if k == 1:
        idxs = idxs[:, np.newaxis]
    return mesh.vertices[idxs, 2].max(axis=1)


def build_terrain_with_base(terrain_mesh: trimesh.Trimesh,
                            base_thickness_m: float,
                            wall_color: tuple = None,
                            terrain_colors: dict = None) -> trimesh.Trimesh:
    """Build a watertight solid by adding walls and a flat bottom to the terrain.

    Args:
        terrain_mesh: open terrain surface mesh
        base_thickness_m: thickness of base below the lowest terrain point
        wall_color: RGBA tuple for walls and bottom (default: PRINT_COLORS["base_wall"])
        terrain_colors: dict with "terrain_low" and "terrain_high" keys for
                       re-coloring the terrain surface (None keeps current colors)

    Returns:
        Watertight trimesh with terrain top + walls + flat bottom
    """
    from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.mesh_repair import (
        validate_and_repair_mesh_manifold,
    )

    if wall_color is None:
        from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.config import PRINT_COLORS
        wall_color = PRINT_COLORS["base_wall"]

    verts = terrain_mesh.vertices
    bottom_z = float(verts[:, 2].min()) - base_thickness_m

    # --- Find boundary edges (edges belonging to exactly one face) ---
    boundary_loop = _get_boundary_loop(terrain_mesh)
    if boundary_loop is None:
        logger.warning("Could not extract terrain boundary; returning surface only")
        return terrain_mesh

    n_top = len(verts)
    n_boundary = len(boundary_loop)

    # --- Build wall vertices and faces ---
    # For each boundary vertex, create a corresponding bottom vertex
    wall_bottom_verts = verts[boundary_loop].copy()
    wall_bottom_verts[:, 2] = bottom_z  # flatten to base level

    # Wall faces: quad strip connecting top boundary to bottom boundary
    # Bottom vertex indices start at n_top
    wall_faces = []
    for i in range(n_boundary):
        j = (i + 1) % n_boundary
        top_i = boundary_loop[i]
        top_j = boundary_loop[j]
        bot_i = n_top + i
        bot_j = n_top + j
        # Two triangles per quad (winding: outward)
        wall_faces.append([top_i, top_j, bot_j])
        wall_faces.append([top_i, bot_j, bot_i])

    wall_faces = np.array(wall_faces, dtype=np.int64)

    # --- Build bottom cap ---
    # Project boundary vertices to 2D (X, Y) at bottom_z, triangulate
    bottom_pts_2d = wall_bottom_verts[:, :2].astype(np.float64)

    from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.water import _earcut_triangulate

    # Try earcut first, filter degenerate faces
    bottom_faces = _earcut_triangulate(bottom_pts_2d, [len(bottom_pts_2d)])

    if bottom_faces is None or len(bottom_faces) == 0:
        # Fallback: Use constrained Delaunay-style triangulation
        # Create interior points to avoid radial pattern from single centroid
        import random
        
        # Compute bounding box of boundary points
        min_x, min_y = bottom_pts_2d.min(axis=0)
        max_x, max_y = bottom_pts_2d.max(axis=0)
        
        # Create a grid of interior points (avoid edges)
        n_boundary = len(bottom_pts_2d)
        interior_points = []
        interior_idx_map = []  # Track which indices are interior
        
        # Add a few interior points in a grid pattern
        grid_size = max(2, int(np.sqrt(n_boundary / 50)))  # Scale with boundary complexity
        for i in range(1, grid_size):
            for j in range(1, grid_size):
                x = min_x + (max_x - min_x) * i / grid_size
                y = min_y + (max_y - min_y) * j / grid_size
                pt = np.array([x, y])
                
                # Check if point is roughly inside the boundary (simple bounding circle check)
                center = bottom_pts_2d.mean(axis=0)
                radius = np.max(np.linalg.norm(bottom_pts_2d - center, axis=1))
                if np.linalg.norm(pt - center) < radius * 0.9:  # 90% of radius to stay inside
                    interior_points.append([x, y])
        
        # Combine boundary and interior points
        if interior_points:
            all_pts = np.vstack([bottom_pts_2d, np.array(interior_points)])
            n_total = len(all_pts)
            
            # Try earcut again with interior points
            # First, create a polygon with holes: outer boundary + inner "holes" that are actually interior regions
            bottom_faces = _earcut_triangulate(all_pts, [n_total])
            
            if bottom_faces is not None and len(bottom_faces) > 0:
                # Validate no degenerate faces
                v0_temp = wall_bottom_verts[bottom_faces[:, 0]]
                v1_temp = wall_bottom_verts[bottom_faces[:, 1]]
                v2_temp = wall_bottom_verts[bottom_faces[:, 2]]
                cross_temp = np.cross(v1_temp - v0_temp, v2_temp - v0_temp)
                areas_temp = np.sqrt(np.sum(cross_temp ** 2, axis=1)) * 0.5
                
                good = areas_temp > 1e-10
                if not np.all(good):
                    bottom_faces = bottom_faces[good]
                
                # If still valid, add interior vertices to wall_bottom_verts
                if len(bottom_faces) > 0:
                    interior_verts = np.array([[p[0], p[1], bottom_z] for p in interior_points])
                    wall_bottom_verts = np.vstack([wall_bottom_verts, interior_verts])
                    n_boundary = n_total  # Now includes all points
            else:
                # Final fallback: centroid fan triangulation
                centroid_2d = bottom_pts_2d.mean(axis=0)
                angles = np.arctan2(bottom_pts_2d[:, 1] - centroid_2d[1],
                                   bottom_pts_2d[:, 0] - centroid_2d[0])
                sorted_indices = np.argsort(angles)
                
                bottom_faces_list = []
                n_sorted = len(sorted_indices)
                for i in range(n_sorted):
                    j = (i + 1) % n_sorted
                    bottom_faces_list.append([n_boundary, sorted_indices[i], sorted_indices[j]])
                
                bottom_faces = np.array(bottom_faces_list, dtype=np.int64)
                centroid_3d = np.array([[centroid_2d[0], centroid_2d[1], bottom_z]])
                wall_bottom_verts = np.vstack([wall_bottom_verts, centroid_3d])
                n_boundary += 1
        else:
            # Simple case: too small for interior points, use centroid fan
            centroid_2d = bottom_pts_2d.mean(axis=0)
            angles = np.arctan2(bottom_pts_2d[:, 1] - centroid_2d[1],
                               bottom_pts_2d[:, 0] - centroid_2d[0])
            sorted_indices = np.argsort(angles)
            
            bottom_faces_list = []
            n_sorted = len(sorted_indices)
            for i in range(n_sorted):
                j = (i + 1) % n_sorted
                bottom_faces_list.append([n_boundary, sorted_indices[i], sorted_indices[j]])
            
            bottom_faces = np.array(bottom_faces_list, dtype=np.int64)
            centroid_3d = np.array([[centroid_2d[0], centroid_2d[1], bottom_z]])
            wall_bottom_verts = np.vstack([wall_bottom_verts, centroid_3d])
            n_boundary += 1
    else:
        # Check if earcut produced degenerate faces and filter them
        v0_temp = wall_bottom_verts[bottom_faces[:, 0]]
        v1_temp = wall_bottom_verts[bottom_faces[:, 1]]
        v2_temp = wall_bottom_verts[bottom_faces[:, 2]]
        cross_temp = np.cross(v1_temp - v0_temp, v2_temp - v0_temp)
        areas_temp = np.sqrt(np.sum(cross_temp ** 2, axis=1)) * 0.5

        good = areas_temp > 1e-10
        if not np.all(good):
            n_bad = int(np.sum(~good))
            logger.info(f"Bottom cap: removing {n_bad} degenerate faces from earcut")
            bottom_faces = bottom_faces[good]

    # Bottom face indices reference the wall_bottom_verts which start at n_top
    bottom_faces_offset = bottom_faces + n_top
    # Reverse winding so bottom faces point downward
    bottom_faces_offset = bottom_faces_offset[:, ::-1]

    # --- Combine everything ---
    all_verts = np.vstack([verts, wall_bottom_verts])
    all_faces = np.vstack([terrain_mesh.faces, wall_faces, bottom_faces_offset])

    # --- Colors ---
    # Re-color terrain surface if requested
    if terrain_colors is not None:
        top_colors = _elevation_to_colors_custom(
            verts[:, 2],
            terrain_colors["terrain_low"],
            terrain_colors["terrain_high"]
        )
    else:
        top_colors = np.array(terrain_mesh.visual.vertex_colors[:, :4])

    wall_color_arr = np.tile(wall_color, (n_boundary, 1)).astype(np.uint8)
    all_colors = np.vstack([top_colors, wall_color_arr])

    solid = trimesh.Trimesh(
        vertices=all_verts,
        faces=all_faces,
        vertex_colors=all_colors,
        process=False
    )
    solid.fix_normals()

    # Validate and repair to ensure watertight
    # Use Manifold-backed repair for guaranteed watertight result
    solid = validate_and_repair_mesh_manifold(solid, name="terrain_base")

    if not solid.is_watertight:
        logger.warning("Terrain base mesh is still not fully watertight after repair")
    else:
        logger.info("Terrain base mesh is watertight")

    logger.info(f"Terrain+base: {len(solid.vertices)} verts, {len(solid.faces)} faces")
    return solid


def _get_boundary_loop(mesh: trimesh.Trimesh):
    """Extract ordered boundary vertex loop from an open surface mesh.

    Returns:
        numpy array of UNIQUE vertex indices forming a closed boundary loop,
        or None if extraction fails.
    """
    # Find edges that appear in exactly one face (= boundary)
    edges = mesh.edges_sorted
    # Count edge occurrences
    edge_tuples = [tuple(e) for e in edges]
    from collections import Counter, defaultdict
    edge_counts = Counter(edge_tuples)
    boundary_edges = [e for e, c in edge_counts.items() if c == 1]

    if not boundary_edges:
        return None

    # Build adjacency for boundary vertices
    adj = defaultdict(list)
    for a, b in boundary_edges:
        adj[a].append(b)
        adj[b].append(a)

    # Walk ALL boundary loops
    visited_edges = set()
    all_boundary_loops = []

    for start_edge in boundary_edges:
        start = start_edge[0]

        # Check if this edge was already visited
        if tuple(sorted(start_edge)) in visited_edges:
            continue

        # Walk the boundary
        loop = []
        current = start
        prev = None

        while True:
            loop.append(current)
            neighbors = [n for n in adj[current] if n != prev]

            if not neighbors:
                break

            next_v = neighbors[0]
            edge = tuple(sorted([current, next_v]))

            if edge in visited_edges:
                break

            visited_edges.add(edge)
            prev = current
            current = next_v

            # Closed loop - back to start
            if current == start:
                break

        # Only keep loops with at least 3 unique vertices
        if len(loop) >= 3:
            all_boundary_loops.append(np.array(loop, dtype=np.int64))

    # Return the longest loop (main outer boundary)
    if all_boundary_loops:
        longest = max(all_boundary_loops, key=len)
        return longest

    return None


def _elevation_to_colors_custom(elevations: np.ndarray,
                                low_color: tuple,
                                high_color: tuple) -> np.ndarray:
    """Map elevation values to a custom color gradient."""
    low = np.array(low_color, dtype=np.float64)
    high = np.array(high_color, dtype=np.float64)

    e_min = np.nanmin(elevations)
    e_max = np.nanmax(elevations)

    if e_max - e_min < 1e-3:
        return np.tile(low, (len(elevations), 1)).astype(np.uint8)

    t = (elevations - e_min) / (e_max - e_min)
    t = np.clip(t, 0, 1)

    colors = np.outer(1 - t, low) + np.outer(t, high)
    return np.clip(colors, 0, 255).astype(np.uint8)
