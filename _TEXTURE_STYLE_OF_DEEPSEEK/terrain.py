"""Terrain processor — watertight terrain solid.

Builds terrain in model mm space directly, avoiding Z-mapping issues from
mixing real-meter and model-mm coordinate systems.
"""

import numpy as np
import trimesh

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.terrain import (
    build_terrain_mesh,
    sample_terrain_z,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.mesh_repair import (
    validate_and_repair_mesh,
    validate_and_repair_mesh_manifold,
    optimize_and_repair_mesh,
)

from _TEXTURE_STYLE_OF_DEEPSEEK.config import (
    INTERNAL_SPAN_MM,
    Z_TERRAIN_BASE,
    TERRAIN_GRID,
    DECIMATION_TARGETS,
    get_area_class,
)


def _add_walls_and_bottom(surface_mesh: trimesh.Trimesh,
                          bottom_z: float) -> trimesh.Trimesh:
    """Convert an open surface mesh to a watertight solid.

    Adds vertical walls from boundary edges down to bottom_z,
    and a flat bottom cap at bottom_z.

    Args:
        surface_mesh: open surface trimesh (already in model mm)
        bottom_z: Z coordinate for the bottom face

    Returns:
        Watertight solid trimesh.
    """
    # Find boundary edges (those appearing in exactly one face)
    from collections import Counter, defaultdict
    edge_counts = Counter(tuple(e) for e in surface_mesh.edges_sorted)
    boundary_edges = [e for e, c in edge_counts.items() if c == 1]

    if len(boundary_edges) < 3:
        return surface_mesh

    # Build adjacency graph
    adj = defaultdict(list)
    for a, b in boundary_edges:
        adj[a].append(b)
        adj[b].append(a)

    # Walk the boundary loop
    visited_edges = set()
    best_loop = []

    for start_edge in boundary_edges:
        start = start_edge[0]
        if tuple(sorted(start_edge)) in visited_edges:
            continue

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
            if current == start:
                break

        if len(loop) > len(best_loop):
            best_loop = loop

    if len(best_loop) < 3:
        return surface_mesh

    # best_loop is already vertex indices — use directly (no dict lookup)
    loop_indices = np.array(best_loop, dtype=np.int64)
    n_boundary = len(loop_indices)
    n_surf_verts = len(surface_mesh.vertices)
    surf_verts = surface_mesh.vertices

    # Build wall quads: each boundary edge → 2 triangles connecting top→bottom
    # Bottom vertices: same XY as boundary, Z=bottom_z
    boundary_coords = surf_verts[loop_indices]
    bottom_verts = boundary_coords.copy()
    bottom_verts[:, 2] = bottom_z

    # Wall face indices (vectorized)
    # Top vertex i connects to bottom vertex i (at n_surf_verts + i)
    i_arr = np.arange(n_boundary, dtype=np.int64)
    j_arr = (i_arr + 1) % n_boundary

    top_i = loop_indices[i_arr]
    top_j = loop_indices[j_arr]
    bot_i = n_surf_verts + i_arr
    bot_j = n_surf_verts + j_arr

    # Two triangles per quad (CCW winding for outward faces)
    wall_faces = np.empty((n_boundary * 2, 3), dtype=np.int64)
    wall_faces[0::2, 0] = top_i
    wall_faces[0::2, 1] = top_j
    wall_faces[0::2, 2] = bot_i
    wall_faces[1::2, 0] = top_j
    wall_faces[1::2, 1] = bot_j
    wall_faces[1::2, 2] = bot_i

    # Bottom cap: earcut triangulation of boundary polygon
    boundary_xy = boundary_coords[:, :2].astype(np.float64)
    cap_vert_offset = n_surf_verts + n_boundary
    bot_cap_verts = np.column_stack([boundary_xy, np.full(n_boundary, bottom_z)])
    bottom_faces = None

    try:
        from mapbox_earcut import triangulate_float64 as earcut
        ring_end = np.array([n_boundary], dtype=np.int32)
        ear_indices = earcut(boundary_xy, ring_end)
        if ear_indices is not None and len(ear_indices) >= 3:
            ear_faces = ear_indices.reshape(-1, 3) + cap_vert_offset
            # Reverse winding for downward-facing bottom
            bottom_faces = ear_faces[:, ::-1]
    except (ImportError, Exception):
        pass

    # Fallback: simple fan
    if bottom_faces is None:
        fan = np.empty((n_boundary - 2, 3), dtype=np.int64)
        fan[:, 0] = cap_vert_offset
        fan[:, 1] = cap_vert_offset + np.arange(2, n_boundary, dtype=np.int64)
        fan[:, 2] = cap_vert_offset + np.arange(1, n_boundary - 1, dtype=np.int64)
        bottom_faces = fan

    # Combine all vertices and faces
    all_verts = np.vstack([surf_verts, bottom_verts, bot_cap_verts])
    all_faces = np.vstack([surface_mesh.faces, wall_faces, bottom_faces])

    solid = trimesh.Trimesh(vertices=all_verts, faces=all_faces, process=False)
    solid.merge_vertices()
    solid.update_faces(solid.nondegenerate_faces())
    solid.update_faces(solid.unique_faces())

    return solid


def build_deepseek_terrain(elevation_grid: np.ndarray,
                           width_m: float,
                           height_m: float,
                           area_km2: float,
                           scale: float,
                           water_gdf=None) -> trimesh.Trimesh:
    """Build the deepseek-style terrain: watertight terrain solid.

    Args:
        elevation_grid: 2D numpy array (rows, cols) in meters
        width_m: terrain width in meters (X)
        height_m: terrain height in meters (Y)
        area_km2: area in km^2 for LOD decisions
        water_gdf: unused (water is now a separate base plate object)

    Returns:
        Watertight trimesh scaled to model mm, Z-mapped to terrain thickness,
        positioned at Z_TERRAIN_BASE (-0.17mm).
    """
    # Step 1: Build heightfield surface mesh (real meters)
    area_class = get_area_class(area_km2)
    mesh = build_terrain_mesh(elevation_grid, width_m, height_m, area_km2)

    # Step 2: Scale XY from real meters to model mm
    mesh.vertices[:, :2] *= scale

    # Function-level import: allows runtime monkey-patch from auto-params
    from _TEXTURE_STYLE_OF_DEEPSEEK.config import Z_GAMMA, TERRAIN_THICKNESS_MM

    # Step 3: Map surface Z to model mm (0..TERRAIN_THICKNESS_MM + Z_TERRAIN_BASE)
    z_surface = mesh.vertices[:, 2]
    z_min, z_max = z_surface.min(), z_surface.max()
    z_range = z_max - z_min

    if z_range > 0.01:
        t = (z_surface - z_min) / z_range  # 0..1 normalized
        t = np.power(t, Z_GAMMA)            # power curve: <1 boosts low relief
        mesh.vertices[:, 2] = t * TERRAIN_THICKNESS_MM + Z_TERRAIN_BASE
    else:
        mesh.vertices[:, 2] = TERRAIN_THICKNESS_MM / 2 + Z_TERRAIN_BASE

    # Step 4: Build watertight solid (add walls + bottom in model mm)
    solid = _add_walls_and_bottom(mesh, Z_TERRAIN_BASE)

    # Step 5: Validate and repair
    # Use optimize_and_repair_mesh as safety net: it decimates large meshes
    # via fast_simplification C ext BEFORE sending to Manifold, preventing
    # OOM / timeout on huge inputs (e.g. 1.7M faces from 1024x1024 DEM).
    n_faces = len(solid.faces)
    if n_faces > 100_000:
        print(f"[terrain] Large mesh ({n_faces} faces) — "
              f"using optimize_and_repair_mesh (decimate + Manifold)")
        solid = optimize_and_repair_mesh(
            solid, max_faces=100_000, agg=7.0, name="terrain",
        )
    else:
        solid = validate_and_repair_mesh(solid, name="terrain",
                                         fix_watertight=True,
                                         fix_normals=True,
                                         fix_degenerate=True,
                                         fix_duplicates=True)

    return solid


def sample_deepseek_terrain_z(terrain_mesh: trimesh.Trimesh,
                              x: np.ndarray,
                              y: np.ndarray) -> np.ndarray:
    """Sample terrain Z at given XY positions.

    Wrapper around terrain3d's sample_terrain_z for convenience.
    """
    return sample_terrain_z(terrain_mesh, x, y)
