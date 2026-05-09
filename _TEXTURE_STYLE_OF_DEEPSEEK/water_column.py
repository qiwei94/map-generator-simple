"""水体挤出柱函数 - 用于地形和植被的布尔差集镂空。

提供两种创建方式：
  1. 传统方式: earcut + 手动建墙（旧，保留兼容）
  2. Manifold 方式: CrossSection.extrude()（新，保证流型）
"""

import time
import numpy as np
import trimesh
import manifold3d
from shapely.geometry import Polygon, MultiPolygon, LineString, MultiLineString
from shapely.ops import unary_union

try:
    from mapbox_earcut import triangulate_float64 as earcut
except ImportError:
    earcut = None

from _TEXTURE_STYLE_OF_DEEPSEEK.config import (
    WATER_MIN_AREA_M2,
    WATERWAY_WIDTHS,
)


# =====================================================================
#  Winding helpers (CrossSection(FillRule.Positive) 需要 CCW exterior / CW holes)
# =====================================================================


def _signed_area_2d(contour: np.ndarray) -> float:
    """Signed area of a 2D closed contour (positive = CCW)."""
    x = contour[:, 0]
    y = contour[:, 1]
    return 0.5 * float(np.sum(x[:-1] * y[1:] - x[1:] * y[:-1]))


def _ensure_ccw(contour: np.ndarray) -> np.ndarray:
    if _signed_area_2d(contour) < 0:
        return contour[::-1]
    return contour


def _ensure_cw(contour: np.ndarray) -> np.ndarray:
    if _signed_area_2d(contour) > 0:
        return contour[::-1]
    return contour


def _shapely_poly_to_crosssection(poly: Polygon) -> manifold3d.CrossSection:
    """Convert a Shapely Polygon to a Manifold CrossSection (winding-normalised).

    Returns empty CrossSection on failure.
    """
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


# =====================================================================
#  Manifold-based water column (preferred — guaranteed watertight)
# =====================================================================


def extrude_water_column_manifold(
    water_polygon: Polygon,
    z_bottom: float,
    z_top: float,
    scale: float,
) -> manifold3d.Manifold:
    """Create a single water column via Manifold CrossSection.extrude().

    The polygon is in *model meters*; ``scale`` converts XY to model mm
    to match the terrain coordinate space.

    Returns:
        Manifold solid (guaranteed watertight) or empty Manifold on failure.
    """
    cs = _shapely_poly_to_crosssection(water_polygon)
    if cs.is_empty():
        return manifold3d.Manifold()

    try:
        # Scale XY from model meters to model mm (matching terrain XY space)
        cs = cs.scale((scale, scale))

        height = z_top - z_bottom
        column = cs.extrude(height=height)
        column = column.translate((0, 0, z_bottom))
        return column
    except Exception:
        return manifold3d.Manifold()


def create_water_columns_union_manifold(
    water_gdf,
    z_bottom: float,
    z_top: float,
    scale: float,
    min_area_m2: float = WATER_MIN_AREA_M2,
) -> manifold3d.Manifold:
    """Union all qualifying water features into a single Manifold solid.

    Filters by area (min_area_m2), handles Polygon / MultiPolygon /
    LineString geometry, and returns one watertight Manifold suitable
    for a single boolean subtract from terrain.

    Returns:
        Unioned Manifold solid, or empty Manifold if no features qualify.
    """
    if water_gdf is None or len(water_gdf) == 0:
        return manifold3d.Manifold()

    columns = []
    n_kept = 0
    n_skipped = 0
    n_total = 0

    print(f"  Processing {len(water_gdf)} water features...")
    n_skipped_overlap = 0

    # Step 1: Process LineStrings first, collect their buffered coverage
    linestring_coverage = None  # Union of all buffered LineStrings
    linestring_columns = []

    for idx, (_, row) in enumerate(water_gdf.iterrows()):
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue

        if isinstance(geom, (LineString, MultiLineString)):
            waterway_type = row.get('waterway', 'river')

            # Priority: OSM width tag > WATERWAY_WIDTHS config
            osm_width = row.get('width', None)
            buffer_width = WATERWAY_WIDTHS.get(waterway_type, 60)  # default 60m
            if osm_width is not None:
                try:
                    import math
                    if isinstance(osm_width, float) and math.isnan(osm_width):
                        pass
                    else:
                        parsed = float(osm_width)
                        if parsed > 0 and parsed < 10000:
                            buffer_width = parsed
                except (ValueError, TypeError):
                    pass

            half_width = buffer_width / 2.0

            buffered_polys = []
            if isinstance(geom, MultiLineString):
                for line in geom.geoms:
                    buf = line.buffer(half_width)
                    if not buf.is_empty and isinstance(buf, (Polygon, MultiPolygon)):
                        if isinstance(buf, MultiPolygon):
                            buffered_polys.extend(list(buf.geoms))
                        else:
                            buffered_polys.append(buf)
            else:
                buf = geom.buffer(half_width)
                if not buf.is_empty and isinstance(buf, (Polygon, MultiPolygon)):
                    if isinstance(buf, MultiPolygon):
                        buffered_polys.extend(list(buf.geoms))
                    else:
                        buffered_polys.append(buf)

            for poly in buffered_polys:
                n_total += 1
                if poly.area < min_area_m2:
                    n_skipped += 1
                    continue

                col = extrude_water_column_manifold(poly, z_bottom, z_top, scale)
                if not col.is_empty():
                    linestring_columns.append(col)
                    n_kept += 1
                    # Track coverage for overlap detection
                    if linestring_coverage is None:
                        linestring_coverage = poly
                    else:
                        linestring_coverage = linestring_coverage.union(poly)
                else:
                    n_skipped += 1

    # Step 2: Process Polygons, skip those overlapping with LineString coverage
    for idx, (_, row) in enumerate(water_gdf.iterrows()):
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue

        if isinstance(geom, (Polygon, MultiPolygon)):
            polys_to_process = []
            if isinstance(geom, MultiPolygon):
                polys_to_process = list(geom.geoms)
            else:
                polys_to_process = [geom]

            for poly in polys_to_process:
                n_total += 1
                if poly.area < min_area_m2:
                    n_skipped += 1
                    continue

                # Skip if overlapping with LineString coverage (优先使用LineString)
                if linestring_coverage is not None and poly.intersects(linestring_coverage):
                    overlap_ratio = poly.intersection(linestring_coverage).area / poly.area
                    if overlap_ratio > 0.3:  # >30% overlap, skip Polygon
                        n_skipped_overlap += 1
                        continue

                col = extrude_water_column_manifold(poly, z_bottom, z_top, scale)
                if not col.is_empty():
                    columns.append(col)
                    n_kept += 1
                else:
                    n_skipped += 1

    # Combine LineString columns with Polygon columns
    columns = linestring_columns + columns

    print(f"  Water columns: {n_kept} created, {n_skipped} skipped (small), {n_skipped_overlap} skipped (overlap)")
    t_union = time.time()

    if not columns:
        return manifold3d.Manifold()

    if len(columns) == 1:
        print(f"  ⏱ 水体 Union: 仅1个柱体，跳过 batch_boolean")
        return columns[0]

    t_batch = time.time()
    result = manifold3d.Manifold.batch_boolean(columns, manifold3d.OpType.Add)
    print(f"  ⏱ 水体 batch_boolean Union: {time.time() - t_batch:.1f}s ({len(columns)} columns)")
    return result


# =====================================================================
#  Legacy functions (earcut + manual walls — kept for reference)
# =====================================================================


def extrude_water_column_for_cutting(water_polygon: Polygon,
                                      z_bottom: float,
                                      z_top: float,
                                      max_edge_m: float = 100.0) -> trimesh.Trimesh:
    """创建水体挤出柱，用于布尔差集切割地形/植被。

    旧方法: earcut + 手动建墙。新代码请使用 extrude_water_column_manifold()。
    """
    if water_polygon.is_empty:
        return None

    exterior_coords = np.array(water_polygon.exterior.coords[:-1], dtype=np.float64)
    if len(exterior_coords) < 3:
        return None

    exterior_coords = _densify_ring(exterior_coords, max_edge_m)

    rings_2d = [exterior_coords[:, :2]]
    ring_ends = [len(exterior_coords)]

    for interior in water_polygon.interiors:
        hole_coords = np.array(interior.coords[:-1], dtype=np.float64)
        if len(hole_coords) < 3:
            continue
        hole_coords = _densify_ring(hole_coords, max_edge_m)
        rings_2d.append(hole_coords[:, :2])
        ring_ends.append(ring_ends[-1] + len(hole_coords))

    all_pts_2d = np.vstack(rings_2d).astype(np.float64)
    n_pts = len(all_pts_2d)

    if earcut is not None:
        tri_faces = earcut(all_pts_2d, np.array(ring_ends, dtype=np.int32))
        if tri_faces is not None and len(tri_faces) >= 3:
            tri_faces = tri_faces.reshape(-1, 3)
        else:
            tri_faces = _fan_triangulate(exterior_coords)
    else:
        tri_faces = _fan_triangulate(exterior_coords)

    if tri_faces is None or len(tri_faces) == 0:
        return None

    top_verts = np.column_stack([all_pts_2d, np.full(n_pts, z_top)])
    bot_verts = np.column_stack([all_pts_2d, np.full(n_pts, z_bottom)])

    top_faces = tri_faces + n_pts
    bot_faces = tri_faces[:, ::-1]

    wall_faces = []
    for ri in range(len(ring_ends)):
        start = ring_ends[ri - 1] if ri > 0 else 0
        end = ring_ends[ri]
        for i in range(start, end):
            j = start + (i + 1 - start) % (end - start)
            wall_faces.append([i, j, j + n_pts])
            wall_faces.append([i, j + n_pts, i + n_pts])

    all_verts = np.vstack([bot_verts, top_verts])
    all_faces = np.vstack([
        bot_faces, top_faces,
        np.array(wall_faces, dtype=np.int64),
    ])

    mesh = trimesh.Trimesh(vertices=all_verts, faces=all_faces, process=True)
    mesh.merge_vertices()
    mesh.update_faces(mesh.nondegenerate_faces())
    mesh.update_faces(mesh.unique_faces())
    mesh.fix_normals()

    if not mesh.is_watertight:
        try:
            mesh.fill_holes()
        except Exception:
            pass

    return mesh


def create_water_columns_union(water_gdf,
                                z_bottom: float,
                                z_top: float,
                                max_edge_m: float = 100.0) -> trimesh.Trimesh:
    """创建所有水体挤出柱的合并网格（旧方法）。

    新代码请使用 create_water_columns_union_manifold()。
    """
    if water_gdf is None or len(water_gdf) == 0:
        return None

    water_columns = []
    n_created = 0

    for _, row in water_gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue

        polygons = []
        if isinstance(geom, MultiPolygon):
            polygons = list(geom.geoms)
        elif isinstance(geom, Polygon):
            polygons = [geom]
        else:
            continue

        for poly in polygons:
            if poly.is_empty:
                continue
            column = extrude_water_column_for_cutting(poly, z_bottom, z_top, max_edge_m)
            if column is not None and len(column.faces) > 0:
                water_columns.append(column)
                n_created += 1

    if not water_columns:
        return None

    merged = trimesh.util.concatenate(water_columns)
    merged.merge_vertices()
    merged.update_faces(merged.nondegenerate_faces())
    merged.update_faces(merged.unique_faces())

    return merged


def _densify_ring(coords: np.ndarray, max_edge_m: float) -> np.ndarray:
    """加密边界，确保边长不超过max_edge_m。"""
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


def _fan_triangulate(exterior_coords: np.ndarray):
    """扇形三角化（fallback）。"""
    n = len(exterior_coords)
    if n < 3:
        return None
    faces = []
    for i in range(n):
        j = (i + 1) % n
        faces.append([i, j, n])
    return np.array(faces, dtype=np.int64)
