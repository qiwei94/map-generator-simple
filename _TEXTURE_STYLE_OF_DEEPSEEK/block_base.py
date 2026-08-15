"""Block base — 3MF sub-mesh with per-region Z-texture displacement.

Builds city-block polygons as thin plates with textured top surfaces.
Each polygon's top face is subdivided with interior grid points and
displaced according to its semantic class (residential, forest, etc.).

Z range: [terrain_z, terrain_z + thickness + displacement]
Extruder: E6, warm beige #E8E2D4
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import numpy as np
import trimesh
import manifold3d
from shapely.geometry import Polygon, MultiPolygon
from scipy.spatial import Delaunay

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.terrain import sample_terrain_z
from _TEXTURE_STYLE_OF_DEEPSEEK._geom_utils import shapely_poly_to_crosssection
from _TEXTURE_STYLE_OF_DEEPSEEK.config import BLOCK_BASE_THICKNESS_MM
from _TEXTURE_STYLE_OF_DEEPSEEK._z_displacement import get_displacement


def filter_block_base_edges(
    polys: List[Polygon],
    bbox_local: "tuple[float, float, float, float]",
    scale: float,
    retreat_mm: float,
    transition_mm: float = 1.5,
    occupied_polys: "List[Polygon] | None" = None,
    min_coverage: float = 0.02,
) -> Tuple[List[Polygon], List[int], Dict[str, int]]:
    """Remove the artificial block-base carpet at the model boundary.

    Blocks touching the outer retreat ring are removed whole, so the result
    does not create a ruler-straight inset cut.  Inside the following
    transition band, only blocks with enough building/urban footprint are
    retained.  The returned indices let callers keep semantic classes aligned.
    All polygon coordinates are in source metres; the public controls are in
    printable model millimetres.
    """
    from shapely.geometry import box as shapely_box
    from shapely.ops import unary_union
    from shapely.strtree import STRtree

    if not polys or retreat_mm <= 0:
        indices = list(range(len(polys)))
        return list(polys), indices, {
            "input": len(polys),
            "kept": len(polys),
            "outer_removed": 0,
            "transition_removed": 0,
        }
    if scale <= 0:
        raise ValueError("scale must be positive")
    if transition_mm < 0:
        raise ValueError("transition_mm must be non-negative")
    if not 0 <= min_coverage <= 1:
        raise ValueError("min_coverage must be between 0 and 1")

    frame = shapely_box(*bbox_local)
    retreat_m = retreat_mm / scale
    transition_m = transition_mm / scale
    outer_safe = frame.buffer(-retreat_m, join_style=2)
    inner_safe = frame.buffer(-(retreat_m + transition_m), join_style=2)
    if outer_safe.is_empty:
        return [], [], {
            "input": len(polys),
            "kept": 0,
            "outer_removed": len(polys),
            "transition_removed": 0,
        }

    occupied = [
        poly for poly in (occupied_polys or [])
        if isinstance(poly, Polygon) and not poly.is_empty and poly.area > 0
    ]
    occupied_tree = STRtree(occupied) if occupied else None

    kept: List[Polygon] = []
    kept_indices: List[int] = []
    outer_removed = 0
    transition_removed = 0

    for index, poly in enumerate(polys):
        if not isinstance(poly, Polygon) or poly.is_empty:
            continue
        # Drop the complete polygon if any of it reaches the no-base ring.
        if not outer_safe.covers(poly):
            outer_removed += 1
            continue
        if inner_safe.is_empty or not inner_safe.covers(poly):
            coverage = 0.0
            if occupied_tree is not None:
                matches = occupied_tree.query(poly)
                if len(matches):
                    candidates = [occupied[int(i)] for i in matches]
                    covered = unary_union(candidates).intersection(poly).area
                    coverage = covered / poly.area if poly.area > 0 else 0.0
            if coverage < min_coverage:
                transition_removed += 1
                continue
        kept.append(poly)
        kept_indices.append(index)

    stats = {
        "input": len(polys),
        "kept": len(kept),
        "outer_removed": outer_removed,
        "transition_removed": transition_removed,
    }
    return kept, kept_indices, stats


def _polygon_to_manifold(poly: Polygon, scale: float, z_base: float,
                         thickness_mm: float = None) -> manifold3d.Manifold:
    """Legacy flat extrusion path (no texture)."""
    h = thickness_mm if thickness_mm is not None else BLOCK_BASE_THICKNESS_MM
    cs = shapely_poly_to_crosssection(poly)
    if cs.is_empty():
        return manifold3d.Manifold()
    try:
        cs = cs.scale((scale, scale))
        man = cs.extrude(height=h)
        return man.translate((0, 0, z_base))
    except Exception:
        return manifold3d.Manifold()


def _densify_ring(coords: np.ndarray, max_edge: float) -> np.ndarray:
    """Insert vertices so no edge exceeds max_edge."""
    if len(coords) < 2:
        return coords
    result = [coords[0]]
    for i in range(1, len(coords)):
        p0 = coords[i - 1]
        p1 = coords[i]
        seg_len = float(np.linalg.norm(p1 - p0))
        if seg_len > max_edge:
            n_splits = int(np.ceil(seg_len / max_edge))
            for j in range(1, n_splits + 1):
                t = j / n_splits
                result.append(p0 + t * (p1 - p0))
        else:
            result.append(p1)
    return np.array(result)


_MAX_GRID_POINTS = 10000


def _polygon_to_textured_mesh(
    poly: Polygon,
    scale: float,
    z_base: float,
    region: str,
    thickness_mm: float,
    grid_step_mm: float = 0.5,
    amp_scale: float = 2.0,
) -> Optional[trimesh.Trimesh]:
    """Build a textured mesh for one block_base polygon.

    Uses Delaunay triangulation with known-boundary side walls
    to guarantee watertight manifold output.
    """
    import shapely
    from shapely.geometry import Point as ShapelyPoint

    h = thickness_mm if thickness_mm is not None else BLOCK_BASE_THICKNESS_MM

    # Scale polygon to mm
    exterior_coords = np.array(poly.exterior.coords)[:, :2] * scale
    poly_mm = Polygon(exterior_coords)
    if poly_mm.is_empty or poly_mm.area < 0.01:
        return None

    # Adaptive grid step: cap max grid points per polygon
    minx, miny, maxx, maxy = poly_mm.bounds
    effective_step = grid_step_mm
    estimated_pts = poly_mm.area / (grid_step_mm ** 2)
    if estimated_pts > _MAX_GRID_POINTS:
        effective_step = np.sqrt(poly_mm.area / _MAX_GRID_POINTS)

    # Generate interior grid points
    xs = np.arange(minx + effective_step * 0.5, maxx, effective_step)
    ys = np.arange(miny + effective_step * 0.5, maxy, effective_step)
    if len(xs) == 0 or len(ys) == 0:
        return None

    gx, gy = np.meshgrid(xs, ys)
    grid_pts = np.column_stack([gx.ravel(), gy.ravel()])

    # Vectorized point-in-polygon via shapely 2.x
    shapely_pts = shapely.points(grid_pts[:, 0], grid_pts[:, 1])
    mask = shapely.contains(poly_mm, shapely_pts)
    interior_pts = grid_pts[mask]

    # Densify boundary
    boundary_coords = _densify_ring(exterior_coords, effective_step)
    if np.linalg.norm(boundary_coords[-1] - boundary_coords[0]) < 1e-6:
        boundary_coords = boundary_coords[:-1]

    n_boundary = len(boundary_coords)
    if n_boundary < 3:
        return None

    # Combine: boundary first, then interior
    all_pts = np.vstack([boundary_coords, interior_pts]) if len(interior_pts) > 0 else boundary_coords.copy()
    n_total = len(all_pts)
    if n_total < 3:
        return None

    # Delaunay triangulation
    try:
        tri = Delaunay(all_pts)
    except Exception:
        return None

    # Vectorized centroid-in-polygon filter
    simplices = tri.simplices
    centroids = all_pts[simplices].mean(axis=1)
    # Vectorized centroid-in-polygon filter via shapely
    centroid_pts = shapely.points(centroids[:, 0], centroids[:, 1])
    centroid_mask = shapely.contains(poly_mm, centroid_pts)
    faces_top = simplices[centroid_mask]

    if len(faces_top) == 0:
        return None

    # Compute displacement
    dz = get_displacement(region, all_pts[:, 0], all_pts[:, 1], amp_scale=amp_scale)
    dz = np.maximum(dz, 0.0)

    # Find actual boundary edges of the filtered face set (edges appearing in
    # exactly 1 face). These define where side walls go — guarantees watertight.
    edge_face_count: dict = {}
    for fi in range(len(faces_top)):
        a, b, c = int(faces_top[fi, 0]), int(faces_top[fi, 1]), int(faces_top[fi, 2])
        for e in [(a, b), (b, c), (c, a)]:
            canon = (min(e), max(e))
            if canon in edge_face_count:
                edge_face_count[canon] += 1
            else:
                edge_face_count[canon] = 1

    # Boundary edges: shared by exactly 1 face. Preserve original direction.
    boundary_edges = []
    for fi in range(len(faces_top)):
        a, b, c = int(faces_top[fi, 0]), int(faces_top[fi, 1]), int(faces_top[fi, 2])
        for e in [(a, b), (b, c), (c, a)]:
            canon = (min(e), max(e))
            if edge_face_count[canon] == 1:
                boundary_edges.append(e)

    # Build vertices: top [0..n_total-1], bottom [n_total..2*n_total-1]
    top_z = np.full(n_total, z_base + h) + dz
    top_verts = np.column_stack([all_pts[:, 0], all_pts[:, 1], top_z])
    bot_verts = np.column_stack([all_pts[:, 0], all_pts[:, 1], np.full(n_total, z_base)])
    vertices = np.vstack([top_verts, bot_verts])

    # Top faces (numpy batch)
    faces_top_arr = faces_top.astype(np.int32)
    # Bottom faces (reverse winding)
    faces_bot_arr = np.column_stack([
        n_total + faces_top_arr[:, 0],
        n_total + faces_top_arr[:, 2],
        n_total + faces_top_arr[:, 1],
    ])

    # Side walls from actual boundary edges (not assumed polygon ring)
    side_faces = []
    for a, b in boundary_edges:
        ba = n_total + a
        bb = n_total + b
        side_faces.append([a, ba, bb])
        side_faces.append([a, bb, b])

    faces_arr = np.vstack([
        faces_top_arr,
        faces_bot_arr,
        np.array(side_faces, dtype=np.int32),
    ])
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces_arr, process=False)
    return mesh


def build_deepseek_block_base_v3(
    polys: List[Polygon],
    terrain_mesh: trimesh.Trimesh,
    scale: float,
    brick_style: bool = True,
    bbox_local: "tuple[float,float,float,float] | None" = None,
    thickness_mm: float = None,
    block_classes: "List[str] | None" = None,
    grid_step_mm: float = 0.5,
    amp_scale: float = 2.0,
) -> Optional[trimesh.Trimesh]:
    """V3 block_base builder with optional Z-texture displacement.

    If block_classes is provided, uses textured mesh path.
    Otherwise falls back to flat manifold3d extrusion.
    """
    from shapely.geometry import box as shapely_box

    if not polys:
        return None

    if brick_style:
        import time as _time
        t_brick = _time.time()
        from _TEXTURE_STYLE_OF_DEEPSEEK._brick_transform import brick_transform_batch
        # brick_transform_batch filters out non-Polygon/empty inputs, track survivors
        pre_mask = [isinstance(p, Polygon) and not p.is_empty for p in polys]
        if block_classes is not None:
            block_classes = [c for c, k in zip(block_classes, pre_mask) if k]
        polys = brick_transform_batch(
            polys,
            corner_r_m=8.0, rot_deg=10.0, shift_m=8.0,
            perlin_amp=4.0, perlin_freq=0.15, resample_m=12.0,
            noise_seed=2026)
        if bbox_local:
            clip_box = shapely_box(*bbox_local)
            clipped = [p.intersection(clip_box) for p in polys]
            keep_mask = [isinstance(p, Polygon) and not p.is_empty for p in clipped]
            polys = [p for p, k in zip(clipped, keep_mask) if k]
            if block_classes is not None:
                block_classes = [c for c, k in zip(block_classes, keep_mask) if k]
        print(f"  BlockBase brick transform: {len(polys)} polys in {_time.time()-t_brick:.1f}s")
        if block_classes is not None and len(block_classes) != len(polys):
            block_classes = None

    use_texture = block_classes is not None and len(block_classes) == len(polys)

    if use_texture:
        return _build_textured(polys, terrain_mesh, scale, block_classes,
                               thickness_mm, grid_step_mm, amp_scale)
    else:
        return _build_flat(polys, terrain_mesh, scale, thickness_mm)


# ---------------------------------------------------------------------------
# Textured mesh — boundary-edge-aware approach
# ---------------------------------------------------------------------------


def _build_textured(
    polys: List[Polygon],
    terrain_mesh: trimesh.Trimesh,
    scale: float,
    block_classes: List[str],
    thickness_mm: float,
    grid_step_mm: float,
    amp_scale: float,
) -> Optional[trimesh.Trimesh]:
    """Textured path: per-polygon Delaunay + displacement."""
    import time as _time
    t0 = _time.time()

    h = thickness_mm if thickness_mm is not None else BLOCK_BASE_THICKNESS_MM
    meshes: list[trimesh.Trimesh] = []
    n_skipped = 0

    # Per-polygon random height jitter (seeded by index for reproducibility)
    rng = np.random.default_rng(2026)
    z_jitter = rng.uniform(-0.08, 0.08, size=len(polys))  # ±0.08mm

    # Batch terrain Z sampling for all polygon centroids
    valid_mask = np.array([
        not p.is_empty and p.area >= 10.0 for p in polys
    ])
    valid_indices = np.where(valid_mask)[0]
    centroids_x = np.array([polys[i].centroid.x for i in valid_indices]) * scale
    centroids_y = np.array([polys[i].centroid.y for i in valid_indices]) * scale
    all_tz = sample_terrain_z(terrain_mesh, centroids_x, centroids_y)

    t_sample = _time.time()
    print(f"  BlockBase terrain sampling: {len(valid_indices)} centroids in "
          f"{t_sample - t0:.1f}s")

    for vi_idx, i in enumerate(valid_indices):
        poly = polys[i]
        tz_val = all_tz[vi_idx] if vi_idx < len(all_tz) else float('nan')
        if np.isnan(tz_val):
            n_skipped += 1
            continue

        z_base = float(tz_val) + z_jitter[i] + 0.01
        region = block_classes[i] if i < len(block_classes) else "unclassified"

        mesh = _polygon_to_textured_mesh(
            poly, scale, z_base, region, h,
            grid_step_mm=grid_step_mm, amp_scale=amp_scale)
        if mesh is not None:
            meshes.append(mesh)
        else:
            man = _polygon_to_manifold(poly, scale, z_base, h)
            if not man.is_empty():
                md = man.to_mesh()
                verts = np.array(md.vert_properties, dtype=np.float64)
                faces = np.array(md.tri_verts, dtype=np.int64)
                meshes.append(trimesh.Trimesh(vertices=verts, faces=faces, process=False))
            else:
                n_skipped += 1

    n_skipped += int((~valid_mask).sum())
    print(f"  BlockBase(textured): {len(meshes)} built, {n_skipped} skipped, "
          f"{_time.time()-t0:.1f}s")

    if not meshes:
        return None

    t_merge = _time.time()
    combined = trimesh.util.concatenate(meshes)
    print(f"  BlockBase concat: {_time.time()-t_merge:.1f}s, "
          f"faces={len(combined.faces)}, bodies={len(meshes)}")

    # Manifold repair: merge duplicate vertices at shared block boundaries
    # (adjacent blocks share edges which become non-manifold after concat)
    t_repair = _time.time()
    from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.mesh_repair import (
        validate_and_repair_mesh_manifold,
    )
    combined = validate_and_repair_mesh_manifold(combined, name="block_base")
    print(f"  BlockBase Manifold repair: {_time.time()-t_repair:.1f}s, "
          f"watertight={combined.is_watertight}")

    return combined


def _build_flat(
    polys: List[Polygon],
    terrain_mesh: trimesh.Trimesh,
    scale: float,
    thickness_mm: float,
) -> Optional[trimesh.Trimesh]:
    """Flat extrusion path via manifold3d.

    Terrain height is sampled in one vectorized batch.  Besides being much
    faster for city-scale inputs, this keeps ``flat`` a genuinely lightweight
    alternative to the per-polygon textured path.
    """
    parts: list[manifold3d.Manifold] = []
    valid_indices = [
        i for i, poly in enumerate(polys)
        if not poly.is_empty and poly.area >= 10.0
    ]
    n_skipped = len(polys) - len(valid_indices)
    if not valid_indices:
        return None

    centroids_x = np.array([polys[i].centroid.x for i in valid_indices]) * scale
    centroids_y = np.array([polys[i].centroid.y for i in valid_indices]) * scale
    terrain_z = sample_terrain_z(terrain_mesh, centroids_x, centroids_y)

    for sample_i, poly_i in enumerate(valid_indices):
        if sample_i >= len(terrain_z) or np.isnan(terrain_z[sample_i]):
            n_skipped += 1
            continue

        poly = polys[poly_i]
        z_base = float(terrain_z[sample_i])
        man = _polygon_to_manifold(poly, scale, z_base, thickness_mm)
        if not man.is_empty():
            parts.append(man)
        else:
            n_skipped += 1

    print(f"  BlockBase(flat): {len(parts)} extruded, {n_skipped} skipped")

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
