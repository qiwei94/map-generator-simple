"""Generate 3D road ribbon meshes from OSM road data."""

import logging
import numpy as np
import trimesh
import geopandas as gpd
from shapely.geometry import LineString, MultiLineString, GeometryCollection
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.config import (ROAD_WIDTHS, ROAD_Z_OFFSET, COLORS,
                               ROAD_MIN_LENGTH_M,
                               get_mesh_workers, get_road_densify_segment_m)

logger = logging.getLogger(__name__)


def _extract_linestrings(geom) -> list:
    """Extract all LineStrings from any geometry type."""
    if isinstance(geom, LineString):
        return [geom]
    elif isinstance(geom, MultiLineString):
        return list(geom.geoms)
    elif isinstance(geom, GeometryCollection):
        lines = []
        for g in geom.geoms:
            lines.extend(_extract_linestrings(g))
        return lines
    return []


def _densified_road_points(line, width):
    """Return (line, width, coords_2d) with coords_2d densified as in _polyline_to_ribbon."""
    coords = np.array(line.coords)
    if len(coords) < 2:
        return None
    coords_2d = coords[:, :2]
    diffs = np.diff(coords_2d, axis=0)
    dists = np.sqrt((diffs ** 2).sum(axis=1))
    keep = np.concatenate([[True], dists > 0.01])
    coords_2d = coords_2d[keep]
    if len(coords_2d) < 2:
        return None
    coords_2d = _densify_line(coords_2d, max_segment_length=get_road_densify_segment_m())
    return (line, width, coords_2d)


def build_all_roads(gdf: gpd.GeoDataFrame,
                    terrain_mesh: trimesh.Trimesh,
                    relief: bool = False,
                    ridge_height_mm: float = None) -> trimesh.Trimesh:
    """Build 3D ribbon meshes for all roads.

    Args:
        gdf: GeoDataFrame with road geometries in local coordinates.
        terrain_mesh: terrain mesh for Z sampling.
        relief: if True, use raised ridge mode instead of terrain-draped ribbon.
        ridge_height_mm: ridge height above terrain in mm (default: 2.0).
            Only used when relief=True.
    """
    if gdf.empty:
        logger.info("No roads to process")
        return None

    items = []
    for idx, row in gdf.iterrows():
        geom = row.geometry
        highway = row.get("highway", "residential")
        if isinstance(highway, list):
            highway = highway[0]
        width = ROAD_WIDTHS.get(highway, 6)
        try:
            for line in _extract_linestrings(geom):
                if line.length >= ROAD_MIN_LENGTH_M:
                    items.append((line, width))
        except Exception:
            pass

    from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.terrain import sample_terrain_z
    # Batch sample terrain Z on main thread to avoid sharing terrain_mesh across threads
    # (trimesh ray casting is not thread-safe; causes double free when parallel)
    items_with_z = []
    if len(items) >= 30 and get_mesh_workers() > 1:
        with_coords = []
        for line, width in items:
            r = _densified_road_points(line, width)
            if r is not None:
                with_coords.append(r)
        if with_coords:
            all_x = np.concatenate([c[:, 0] for _, _, c in with_coords])
            all_y = np.concatenate([c[:, 1] for _, _, c in with_coords])
            logger.info("Sampling terrain Z for road points (batch)...")
            all_z = sample_terrain_z(terrain_mesh, all_x, all_y)
            lengths = [len(c) for _, _, c in with_coords]
            z_arrays = np.split(all_z, np.cumsum(lengths)[:-1])
            items_with_z = [(line, width, z) for (line, width, _), z in zip(with_coords, z_arrays)]
        for line, width in items:
            if _densified_road_points(line, width) is None:
                items_with_z.append((line, width, None))
    if not items_with_z:
        items_with_z = [(line, width, None) for line, width in items]

    workers = get_mesh_workers()
    meshes = []

    if workers <= 1 or len(items_with_z) < 30:
        for item in tqdm(items_with_z, desc="Road meshes", leave=False):
            line, width, z_arr = item
            try:
                m = _polyline_to_ribbon(line, width, terrain_mesh if z_arr is None else None,
                                        z_array=z_arr, relief=relief, ridge_height_mm=ridge_height_mm)
                if m is not None:
                    meshes.append(m)
            except Exception:
                pass
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {}
            for i, (line, width, z_arr) in enumerate(items_with_z):
                if z_arr is not None:
                    futures[ex.submit(_polyline_to_ribbon, line, width, None, z_arr,
                                      relief=relief, ridge_height_mm=ridge_height_mm)] = i
                else:
                    try:
                        m = _polyline_to_ribbon(line, width, terrain_mesh, None,
                                                relief=relief, ridge_height_mm=ridge_height_mm)
                        if m is not None:
                            meshes.append(m)
                    except Exception:
                        pass
            for fut in tqdm(as_completed(futures), total=len(futures),
                            desc="Road meshes", leave=False):
                try:
                    m = fut.result()
                    if m is not None:
                        meshes.append(m)
                except Exception:
                    pass

    if not meshes:
        return None

    combined = trimesh.util.concatenate(meshes)
    if relief:
        from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.relief import get_relief_color
        relief_rgb = tuple(int(get_relief_color("roads")[i:i+2], 16)
                           for i in (1, 3, 5)) + (255,)
        combined.visual.vertex_colors = np.tile(
            [relief_rgb], (len(combined.vertices), 1)
        ).astype(np.uint8)
    else:
        combined.visual.vertex_colors = np.tile(
            COLORS["road"], (len(combined.vertices), 1)
        ).astype(np.uint8)

    logger.info(f"Roads mesh: {len(combined.vertices)} vertices, "
                f"{len(combined.faces)} faces")
    return combined


def _polyline_to_ribbon(line: LineString, width: float,
                        terrain_mesh: trimesh.Trimesh = None,
                        z_array: np.ndarray = None,
                        relief: bool = False,
                        ridge_height_mm: float = None) -> trimesh.Trimesh:
    """Convert a LineString to a 3D ribbon mesh draped on terrain.

    Either terrain_mesh (for sampling) or z_array (precomputed Z per point) must be provided.

    Args:
        relief: if True, raise road to fixed ridge above terrain.
        ridge_height_mm: ridge height above terrain in mm (default: 2.0).
    """
    coords = np.array(line.coords)
    if len(coords) < 2:
        return None

    # Only use X, Y (drop Z if present from shapely)
    coords_2d = coords[:, :2]

    # Remove duplicate consecutive points
    diffs = np.diff(coords_2d, axis=0)
    dists = np.sqrt((diffs ** 2).sum(axis=1))
    keep = np.concatenate([[True], dists > 0.01])  # keep points > 1cm apart
    coords_2d = coords_2d[keep]

    if len(coords_2d) < 2:
        return None

    # Densify: insert points so no segment exceeds max length (larger = fewer triangles)
    coords_2d = _densify_line(coords_2d, max_segment_length=get_road_densify_segment_m())

    x = coords_2d[:, 0]
    y = coords_2d[:, 1]
    n = len(x)

    if n < 2:
        return None

    if relief:
        z_offset = ridge_height_mm if ridge_height_mm is not None else 2.0
    else:
        z_offset = ROAD_Z_OFFSET

    if z_array is not None:
        z = np.asarray(z_array, dtype=np.float64) + z_offset
    elif terrain_mesh is not None:
        from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.terrain import sample_terrain_z
        z = sample_terrain_z(terrain_mesh, x, y) + z_offset
    else:
        return None

    half_w = width / 2.0

    # Compute direction vectors along the road
    dx = np.diff(x)
    dy = np.diff(y)

    # Segment lengths
    lengths = np.sqrt(dx ** 2 + dy ** 2)
    lengths = np.maximum(lengths, 1e-6)

    # Perpendicular normals (rotate direction 90 degrees)
    nx = -dy / lengths
    ny = dx / lengths

    # Average normals at interior vertices for smooth ribbon
    # Use miter-limited joints to prevent fan artifacts at sharp turns
    nx_avg = np.zeros(n)
    ny_avg = np.zeros(n)
    offset_scale = np.ones(n)  # per-vertex width scale for miter compensation

    nx_avg[0] = nx[0]
    ny_avg[0] = ny[0]
    nx_avg[-1] = nx[-1]
    ny_avg[-1] = ny[-1]

    for i in range(1, n - 1):
        ax = nx[i - 1] + nx[i]
        ay = ny[i - 1] + ny[i]
        al = np.sqrt(ax ** 2 + ay ** 2)

        # Dot product of adjacent segment directions (not normals)
        # to detect sharp turns
        dot = (dx[i - 1] * dx[i] + dy[i - 1] * dy[i]) / (lengths[i - 1] * lengths[i])
        dot = np.clip(dot, -1.0, 1.0)

        if al > 1e-6 and dot > -0.5:
            # Normal case: average normals and compensate miter width
            nx_avg[i] = ax / al
            ny_avg[i] = ay / al
            # Miter compensation: the averaged normal is shorter than unit
            # at turns, so scale the offset to maintain constant road width
            cos_half = al / 2.0  # cos(half-angle) = |avg| / 2
            if cos_half > 0.3:   # limit miter expansion to ~3.3x
                offset_scale[i] = 1.0 / cos_half
            else:
                offset_scale[i] = 1.0
        else:
            # Sharp turn (> ~120 degrees) or degenerate: use bevel joint
            # Just use the incoming segment's normal (no miter)
            nx_avg[i] = nx[i - 1]
            ny_avg[i] = ny[i - 1]

    # Left and right vertices with miter-compensated offset
    left_x = x + nx_avg * half_w * offset_scale
    left_y = y + ny_avg * half_w * offset_scale
    right_x = x - nx_avg * half_w * offset_scale
    right_y = y - ny_avg * half_w * offset_scale

    # Build vertices: left strip then right strip
    left_verts = np.column_stack([left_x, left_y, z])
    right_verts = np.column_stack([right_x, right_y, z])
    vertices = np.vstack([left_verts, right_verts])

    # Build faces: quads between left and right strips
    faces = []
    for i in range(n - 1):
        l0 = i
        l1 = i + 1
        r0 = n + i
        r1 = n + i + 1
        faces.append([l0, r0, l1])
        faces.append([l1, r0, r1])

    faces = np.array(faces, dtype=np.int64)

    return trimesh.Trimesh(vertices=vertices, faces=faces, process=False)


def _densify_line(coords: np.ndarray, max_segment_length: float) -> np.ndarray:
    """Insert intermediate points so no segment exceeds max_segment_length."""
    result = [coords[0]]

    for i in range(1, len(coords)):
        p0 = coords[i - 1]
        p1 = coords[i]
        dist = np.sqrt((p1[0] - p0[0]) ** 2 + (p1[1] - p0[1]) ** 2)

        if dist > max_segment_length:
            n_segments = int(np.ceil(dist / max_segment_length))
            for j in range(1, n_segments):
                t = j / n_segments
                pt = p0 + t * (p1 - p0)
                result.append(pt)

        result.append(p1)

    return np.array(result)
