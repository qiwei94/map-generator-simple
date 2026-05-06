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
)

from _TEXTURE_STYLE_OF_DEEPSEEK.config import (
    INTERNAL_SPAN_MM,
    TERRAIN_THICKNESS_MM,
    Z_GAMMA,
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
    # Get boundary edges (edges that appear in exactly 1 face)
    boundary_edges = surface_mesh.edges[trimesh.grouping.group_rows(
        surface_mesh.edges_sorted, require_count=1
    )]

    if len(boundary_edges) == 0:
        return surface_mesh

    # Extract ordered boundary loop using adjacency walking (same as main pipeline)
    from collections import defaultdict, Counter
    edges_sorted = surface_mesh.edges_sorted
    edge_counts = Counter([tuple(e) for e in edges_sorted])
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

    # Convert to vertex coordinates (ordered boundary loop)
    boundary_loop = surface_mesh.vertices[best_loop]
    # Close the loop
    boundary_loop = np.vstack([boundary_loop, boundary_loop[0]])

    # Build walls: for each boundary edge, create a quad face
    n_surf_verts = len(surface_mesh.vertices)
    surf_verts = surface_mesh.vertices

    # Find which original vertices are on the boundary
    surf_vert_indices = {}
    for i, v in enumerate(surf_verts):
        key = (round(v[0], 4), round(v[1], 4), round(v[2], 4))
        if key not in surf_vert_indices:
            surf_vert_indices[key] = i

    # Build bottom vertices (duplicate XY, Z=bottom_z)
    bottom_verts = []
    wall_faces = []

    # Use boundary loop to create walls
    n_boundary = len(boundary_loop) - 1  # exclude closing vertex
    for i in range(n_boundary):
        v_top = boundary_loop[i]
        v_next = boundary_loop[(i + 1) % n_boundary]

        # Find the vertex indices in the surface mesh
        key_i = (round(v_top[0], 4), round(v_top[1], 4), round(v_top[2], 4))
        key_j = (round(v_next[0], 4), round(v_next[1], 4), round(v_next[2], 4))

        idx_i = surf_vert_indices.get(key_i)
        idx_j = surf_vert_indices.get(key_j)

        if idx_i is None or idx_j is None:
            continue

        # Bottom vertices
        b_i = np.array([v_top[0], v_top[1], bottom_z])
        b_j = np.array([v_next[0], v_next[1], bottom_z])

        bottom_verts.append(b_i)
        bottom_verts.append(b_j)

        bi_idx = n_surf_verts + len(bottom_verts) - 2
        bj_idx = n_surf_verts + len(bottom_verts) - 1

        # Two triangles per quad
        wall_faces.append([idx_i, idx_j, bi_idx])
        wall_faces.append([idx_j, bj_idx, bi_idx])

    if not bottom_verts:
        return surface_mesh

    bottom_verts = np.array(bottom_verts)

    # Build bottom cap: earcut triangulation of boundary polygon
    # (avoids radial spoke artifacts from centroid fan triangulation)
    boundary_xy = np.array([[v[0], v[1]] for v in boundary_loop[:-1]])
    n_bot_cap = len(boundary_xy)
    bottom_faces = []

    # Create bottom cap vertices: boundary XY at Z=bottom_z
    bot_cap_verts = np.column_stack([boundary_xy, np.full(n_bot_cap, bottom_z)])
    cap_vert_offset = n_surf_verts + len(bottom_verts)

    try:
        from mapbox_earcut import triangulate_float64 as earcut
        ring_end = np.array([n_bot_cap], dtype=np.int32)
        ear_faces = earcut(boundary_xy, ring_end)
        if ear_faces is not None and len(ear_faces) >= 3:
            ear_faces = ear_faces.reshape(-1, 3)
            for tri in ear_faces:
                # Reverse winding for downward-facing bottom
                bottom_faces.append([int(tri[0]) + cap_vert_offset,
                                    int(tri[2]) + cap_vert_offset,
                                    int(tri[1]) + cap_vert_offset])
    except (ImportError, Exception):
        pass

    # Fallback if earcut unavailable: simple fan (still produces some radial
    # edges but only as a last resort)
    if not bottom_faces:
        for i in range(1, n_bot_cap - 1):
            bottom_faces.append([0 + cap_vert_offset,
                                i + 1 + cap_vert_offset,
                                i + cap_vert_offset])

    # Combine all vertices and faces
    all_surf_verts = surf_verts.copy()
    all_verts = np.vstack([all_surf_verts, bottom_verts, bot_cap_verts])

    surf_faces = surface_mesh.faces.copy()
    all_faces_list = [surf_faces]
    if wall_faces:
        all_faces_list.append(np.array(wall_faces, dtype=np.int64))
    if bottom_faces:
        all_faces_list.append(np.array(bottom_faces, dtype=np.int64))

    all_faces = np.vstack(all_faces_list)

    # Remove duplicate vertices and update faces
    solid = trimesh.Trimesh(vertices=all_verts, faces=np.array(all_faces, dtype=np.int64))
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
    #   Large meshes (>100K faces): Manifold-backed repair (fast C++ kernel)
    #   Small meshes: trimesh-native repair
    n_faces = len(solid.faces)
    if n_faces > 100_000:
        print(f"[terrain] Large mesh ({n_faces} faces) — using Manifold-backed repair")
        solid = validate_and_repair_mesh_manifold(solid, name="terrain")
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
