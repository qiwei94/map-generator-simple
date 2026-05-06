"""Road processor — 0.4mm thin ribbon with >=90% faces pointing +Z.

Roads are raised 0.51mm above terrain surface, matching reference model placement.
Uses 2.5x width multiplier for visual prominence.

Bridge filtering support:
- filter_bridges_only=True: only keep road segments crossing water (bridges)
- Requires water_gdf parameter for bridge detection
"""

import numpy as np
import trimesh
from shapely.geometry import LineString, MultiLineString
import geopandas as gpd

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.terrain import sample_terrain_z
from _TEXTURE_STYLE_OF_DEEPSEEK.bridge_filter import filter_bridges_only as filter_bridge_roads

from _TEXTURE_STYLE_OF_DEEPSEEK.config import (
    ROAD_THICKNESS_MM,
    Z_ROAD_ABOVE_TERRAIN_MM,
    ROAD_WIDTH_MULTIPLIER,
    ROAD_DENSIFY_MAX_M,
    ROAD_WIDTHS,
    ROAD_FILTER,
    ROAD_COLOR,
    get_area_class,
)


def _densify_line(line: LineString, max_seg_m: float) -> LineString:
    """Densify a LineString so no segment exceeds max_seg_m."""
    coords = list(line.coords)
    if len(coords) < 2:
        return line

    new_coords = [coords[0]]
    for i in range(1, len(coords)):
        p0 = np.array(coords[i - 1])
        p1 = np.array(coords[i])
        seg_len = np.linalg.norm(p1 - p0)
        if seg_len > max_seg_m:
            n_splits = int(np.ceil(seg_len / max_seg_m))
            for j in range(1, n_splits + 1):
                t = j / n_splits
                new_coords.append(tuple(p0 + t * (p1 - p0)))
        else:
            new_coords.append(coords[i])
    return LineString(new_coords)


def _build_ribbon(line: LineString, width_m: float,
                  terrain_mesh: trimesh.Trimesh,
                  scale: float = 1.0) -> trimesh.Trimesh:
    """Build a road ribbon mesh from a LineString.

    The ribbon follows terrain elevation with faces oriented upward (+Z).
    Roads sit Z_ROAD_ABOVE_TERRAIN_MM above terrain.

    Args:
        line: densified LineString in local UTM meters
        width_m: road width in real meters
        terrain_mesh: scaled terrain mesh (model mm)

    Returns:
        Trimesh ribbon with top and bottom faces.
    """
    coords = np.array(line.coords)
    n = len(coords)
    if n < 2:
        return None

    half_w = width_m / 2

    # Compute tangent directions and perpendiculars
    left_pts = []
    right_pts = []

    for i in range(n):
        if i == 0:
            tangent = coords[1] - coords[0]
        elif i == n - 1:
            tangent = coords[-1] - coords[-2]
        else:
            tangent = coords[i + 1] - coords[i - 1]

        tangent_len = np.linalg.norm(tangent)
        if tangent_len < 1e-10:
            # Use previous perpendicular if available
            if left_pts:
                left_pts.append(coords[i] + (left_pts[-1] - coords[i]))
                right_pts.append(coords[i] + (right_pts[-1] - coords[i]))
                continue
            tangent = np.array([1.0, 0.0])
        else:
            tangent = tangent / tangent_len

        # Perpendicular (rotate 90 deg CCW)
        perp = np.array([-tangent[1], tangent[0]]) * half_w

        left_pts.append(coords[i] + perp)
        right_pts.append(coords[i] - perp)

    left_pts = np.array(left_pts)
    right_pts = np.array(right_pts)

    # Sample terrain Z at all ribbon vertices
    all_xy = np.vstack([left_pts, right_pts])
    all_z = sample_terrain_z(terrain_mesh, all_xy[:, 0] * scale, all_xy[:, 1] * scale)

    left_z = all_z[:n] + Z_ROAD_ABOVE_TERRAIN_MM
    right_z = all_z[n:] + Z_ROAD_ABOVE_TERRAIN_MM

    # Scale XY
    left_pts_mm = left_pts * scale
    right_pts_mm = right_pts * scale

    # Build vertices: top (left+right edge) and bottom (same XY, thickness below)
    top_left = np.column_stack([left_pts_mm, left_z])
    top_right = np.column_stack([right_pts_mm, right_z])

    bot_left = np.column_stack([left_pts_mm, left_z - ROAD_THICKNESS_MM])
    bot_right = np.column_stack([right_pts_mm, right_z - ROAD_THICKNESS_MM])

    # Vertex layout: [top_left (0..n-1), top_right (n..2n-1),
    #                 bot_left (2n..3n-1), bot_right (3n..4n-1)]
    vertices = np.vstack([top_left, top_right, bot_left, bot_right])

    faces = []
    # Top surface quads (left->right, indexed as two triangles)
    for i in range(n - 1):
        tl, tr = i, n + i
        ntl, ntr = i + 1, n + i + 1
        faces.append([tl, tr, ntl])
        faces.append([tr, ntr, ntl])

    # Bottom surface quads (reversed winding)
    for i in range(n - 1):
        bl, br = 2 * n + i, 3 * n + i
        nbl, nbr = 2 * n + i + 1, 3 * n + i + 1
        faces.append([bl, nbl, br])
        faces.append([br, nbl, nbr])

    # Side walls (left edge, right edge)
    for i in range(n - 1):
        tl, bl = i, 2 * n + i
        ntl, nbl = i + 1, 2 * n + i + 1
        faces.append([tl, bl, ntl])
        faces.append([bl, nbl, ntl])

        tr, br = n + i, 3 * n + i
        ntr, nbr = n + i + 1, 3 * n + i + 1
        faces.append([tr, ntr, br])
        faces.append([ntr, nbr, br])

    # Front cap and back cap
    # Front: top_left[0], top_right[0], bot_left[0], bot_right[0]
    faces.append([0, n, 2 * n])
    faces.append([n, 3 * n, 2 * n])
    # Back: last vertices
    li = n - 1
    faces.append([li, 2 * n + li, n + li])
    faces.append([n + li, 2 * n + li, 3 * n + li])

    mesh = trimesh.Trimesh(vertices=vertices, faces=np.array(faces, dtype=np.int64))
    mesh.merge_vertices()
    mesh.update_faces(mesh.nondegenerate_faces())
    mesh.update_faces(mesh.unique_faces())

    return mesh


def build_deepseek_roads(gdf: gpd.GeoDataFrame,
                         terrain_mesh: trimesh.Trimesh,
                         area_km2: float = 0,
                         scale: float = 1.0,
                         water_gdf: gpd.GeoDataFrame = None,
                         filter_bridges_only: bool = False) -> trimesh.Trimesh:
    """Build deepseek-style road ribbons.

    Args:
        gdf: GeoDataFrame of road LineStrings in local UTM meters
        terrain_mesh: scaled terrain mesh (model mm)
        area_km2: area for LOD filtering
        water_gdf: water GeoDataFrame for bridge filtering (optional)
        filter_bridges_only: if True, only build bridges crossing water

    Returns:
        Merged trimesh of all road ribbons, or None if no roads.
    """
    if gdf is None or len(gdf) == 0:
        return None

    # Bridge filtering (only keep road segments crossing water)
    if filter_bridges_only and water_gdf is not None and len(water_gdf) > 0:
        print("\n[道路处理] 启用桥梁过滤模式...")
        gdf = filter_bridge_roads(gdf, water_gdf, extract_water_crossing_only=True)
        if len(gdf) == 0:
            print("  过滤后无桥梁道路，返回空")
            return None

    area_class = get_area_class(area_km2)
    highway_filter = ROAD_FILTER.get(area_class, None)

    ribbon_meshes = []
    for idx, row in gdf.iterrows():
        highway = row.get("highway", "residential")
        if highway_filter and highway not in highway_filter:
            continue

        geom = row.geometry
        if geom is None or geom.is_empty:
            continue

        if isinstance(geom, MultiLineString):
            lines = list(geom.geoms)
        else:
            lines = [geom]

        width = ROAD_WIDTHS.get(highway, 6.0) * ROAD_WIDTH_MULTIPLIER

        for line in lines:
            if line.length < 10.0:  # skip very short segments
                continue
            dense_line = _densify_line(line, ROAD_DENSIFY_MAX_M)
            ribbon = _build_ribbon(dense_line, width, terrain_mesh, scale)
            if ribbon is not None and len(ribbon.faces) > 0:
                ribbon_meshes.append(ribbon)

    if not ribbon_meshes:
        return None

    merged = trimesh.util.concatenate(ribbon_meshes)
    merged.merge_vertices()
    merged.update_faces(merged.nondegenerate_faces())
    merged.update_faces(merged.unique_faces())

    return merged
