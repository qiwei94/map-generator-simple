from __future__ import annotations

"""植被遮挡处理 — 使用 Manifold 布尔差集镂空水体、建筑、道路。

根据 manifold_boolean_spec.md 第47-75行的遮挡关系矩阵：
- 植被布尔差集水体区域（P0 - 高优先级）
- 植被布尔差集建筑区域（P0 - 高优先级）
- 植被布尔差集道路区域（P1 - 中等优先级）

实现策略：
1. 创建排除柱（水体/建筑/道路的挤出柱）
2. 合并所有排除柱（Manifold布尔并集）
3. 植被布尔差集（镂空）
"""

from typing import List

import time
import numpy as np
import shapely
import trimesh
import manifold3d
from shapely.geometry import Polygon, MultiPolygon, LineString, MultiLineString
from shapely.ops import unary_union
import geopandas as gpd

from _TEXTURE_STYLE_OF_DEEPSEEK._bridge import (
    trimesh_to_manifold,
    manifold_to_trimesh,
    is_manifold_available,
)
from _TEXTURE_STYLE_OF_DEEPSEEK._geom_utils import shapely_poly_to_crosssection
from _TEXTURE_STYLE_OF_DEEPSEEK.water_column import (
    create_water_columns_union_manifold,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.terrain import sample_terrain_z
from _TEXTURE_STYLE_OF_DEEPSEEK.config import (
    Z_TERRAIN_BASE,
    Z_ROAD_ABOVE_TERRAIN_MM,
    ROAD_THICKNESS_MM,
    VEGETATION_Z_OFFSET_MM,
    VEGETATION_THICKNESS_MM,
    VL_Z_OFFSET_MM,
    VO_Z_OFFSET_MM,
    BUILDING_EXCLUSION_TOP_MM,
    ROAD_MIN_LINE_LENGTH_M,
)


def create_exclusion_column_manifold(polygon: Polygon,
                                      z_bottom: float,
                                      z_top: float,
                                      scale: float) -> manifold3d.Manifold:
    """创建单个排除柱（用于布尔差集镂空）。

    Args:
        polygon: Shapely Polygon（模型米单位）
        z_bottom: 底部Z坐标（模型毫米）
        z_top: 顶部Z坐标（模型毫米）
        scale: 比例尺（mm/m）

    Returns:
        Manifold挤出柱（watertight）
    """
    if polygon.is_empty or len(polygon.exterior.coords) < 4:
        return manifold3d.Manifold()

    cs = shapely_poly_to_crosssection(polygon)
    if cs.is_empty():
        return manifold3d.Manifold()

    try:
        # 缩放XY（从模型米到模型毫米）
        cs = cs.scale((scale, scale))

        height = z_top - z_bottom
        column = cs.extrude(height=height)
        column = column.translate((0, 0, z_bottom))
        return column
    except Exception:
        return manifold3d.Manifold()


def create_building_exclusion_columns_manifold(buildings_gdf: gpd.GeoDataFrame,
                                                terrain_mesh: trimesh.Trimesh,
                                                scale: float,
                                                z_buffer: float = 0.5) -> manifold3d.Manifold:
    """创建建筑排除柱（用于植被镂空）。

    Optimized: vectorized geometry extraction + batch terrain Z sampling.
    Only the per-building Manifold extrusion remains as a loop (3D geometry
    construction that cannot be vectorized).

    Args:
        buildings_gdf: 建筑GeoDataFrame
        terrain_mesh: 地形网格（用于采样地形高度）
        scale: 比例尺
        z_buffer: Z轴缓冲距离（确保完全穿透植被层）

    Returns:
        合并的建筑排除柱（Manifold）
    """
    if buildings_gdf is None or len(buildings_gdf) == 0:
        return manifold3d.Manifold()

    print(f"\n[建筑排除柱] 处理 {len(buildings_gdf)} 个建筑...")

    # ── 1. Vectorized geometry extraction (no iterrows) ──
    geoms = buildings_gdf.geometry.values

    # Explode MultiPolygon → individual Polygon + collect single Polygons
    all_polys: list = []
    for geom in geoms:
        if geom is None or geom.is_empty:
            continue
        if isinstance(geom, MultiPolygon):
            all_polys.extend(geom.geoms)
        elif isinstance(geom, Polygon):
            all_polys.append(geom)

    if not all_polys:
        print("  无有效建筑多边形")
        return manifold3d.Manifold()

    # ── 2. Vectorized area filter ──
    areas = np.array([p.area for p in all_polys])
    keep = areas >= 10.0
    filtered_polys = [p for p, k in zip(all_polys, keep) if k]

    if not filtered_polys:
        print("  所有建筑面积 < 10 m², 全部过滤")
        return manifold3d.Manifold()

    # ── 3. Batch centroid extraction + single terrain Z sampling call ──
    centroids = shapely.centroid(filtered_polys)
    cx = shapely.get_x(centroids) * scale
    cy = shapely.get_y(centroids) * scale

    terrain_z_all = sample_terrain_z(terrain_mesh, cx, cy)
    valid_z = ~(np.isnan(terrain_z_all) | (terrain_z_all == 0))

    # ── 4. 3D geometry construction loop (Manifold — cannot vectorize) ──
    columns = []
    n_created = 0

    for i, poly in enumerate(filtered_polys):
        if not valid_z[i]:
            continue

        z_base = float(terrain_z_all[i])
        z_bottom = z_base - z_buffer
        z_top = z_base + BUILDING_EXCLUSION_TOP_MM + z_buffer

        col = create_exclusion_column_manifold(poly, z_bottom, z_top, scale)
        if not col.is_empty():
            columns.append(col)
            n_created += 1

    print(f"  创建成功: {n_created} 个建筑排除柱")

    if n_created == 0:
        return manifold3d.Manifold()

    if n_created == 1:
        return columns[0]

    # 合并所有建筑排除柱
    t_union = time.time()
    result = manifold3d.Manifold.batch_boolean(columns, manifold3d.OpType.Add)
    print(f"  Union耗时: {time.time() - t_union:.2f}s")
    return result


def create_road_exclusion_columns_manifold(roads_gdf: gpd.GeoDataFrame,
                                            terrain_mesh: trimesh.Trimesh,
                                            scale: float,
                                            road_width_m: float = 8.0,
                                            z_buffer: float = 0.5) -> manifold3d.Manifold:
    """创建道路排除柱（用于植被镂空）。

    Optimized: vectorized geometry extraction + batch terrain Z sampling.
    Only the per-road Manifold extrusion remains as a loop (3D geometry
    construction that cannot be vectorized).

    Args:
        roads_gdf: 道路GeoDataFrame
        terrain_mesh: 地形网格
        scale: 比例尺
        road_width_m: 道路宽度（米）
        z_buffer: Z轴缓冲距离

    Returns:
        合并的道路排除柱（Manifold）
    """
    if roads_gdf is None or len(roads_gdf) == 0:
        return manifold3d.Manifold()

    print(f"\n[道路排除柱] 处理 {len(roads_gdf)} 条道路...")

    # ── 1. Vectorized geometry extraction (no iterrows) ──
    geoms = roads_gdf.geometry.values

    all_lines: list = []
    for geom in geoms:
        if geom is None or geom.is_empty:
            continue
        if isinstance(geom, MultiLineString):
            all_lines.extend(geom.geoms)
        elif isinstance(geom, LineString):
            all_lines.append(geom)

    if not all_lines:
        print("  无有效道路线")
        return manifold3d.Manifold()

    # ── 2. Vectorized length filter ──
    lengths = np.array([line.length for line in all_lines])
    keep = lengths >= ROAD_MIN_LINE_LENGTH_M
    filtered_lines = [ln for ln, k in zip(all_lines, keep) if k]

    if not filtered_lines:
        print(f"  所有道路长度 < {ROAD_MIN_LINE_LENGTH_M} m, 全部过滤")
        return manifold3d.Manifold()

    # ── 3. Batch centroid extraction + single terrain Z sampling call ──
    centroids = shapely.centroid(filtered_lines)
    cx = shapely.get_x(centroids) * scale
    cy = shapely.get_y(centroids) * scale

    terrain_z_all = sample_terrain_z(terrain_mesh, cx, cy)
    valid_z = ~(np.isnan(terrain_z_all) | (terrain_z_all == 0))

    # ── 4. 3D geometry construction loop (Manifold — cannot vectorize) ──
    columns = []
    n_created = 0

    for i, line in enumerate(filtered_lines):
        if not valid_z[i]:
            continue

        z_base = float(terrain_z_all[i])
        z_bottom = z_base - z_buffer
        z_top = z_base + Z_ROAD_ABOVE_TERRAIN_MM + ROAD_THICKNESS_MM + z_buffer

        road_poly = line.buffer(road_width_m / 2)

        col = create_exclusion_column_manifold(road_poly, z_bottom, z_top, scale)
        if not col.is_empty():
            columns.append(col)
            n_created += 1

    print(f"  创建成功: {n_created} 条道路排除柱")

    if n_created == 0:
        return manifold3d.Manifold()

    if n_created == 1:
        return columns[0]

    # 合并所有道路排除柱
    t_union = time.time()
    result = manifold3d.Manifold.batch_boolean(columns, manifold3d.OpType.Add)
    print(f"  Union耗时: {time.time() - t_union:.2f}s")
    return result


def build_vegetation_with_exclusions_manifold(vegetation_mesh: trimesh.Trimesh,
                                               water_gdf: gpd.GeoDataFrame,
                                               buildings_gdf: gpd.GeoDataFrame,
                                               roads_gdf: gpd.GeoDataFrame,
                                               terrain_mesh: trimesh.Trimesh,
                                               scale: float,
                                               exclude_water: bool = True,
                                               exclude_buildings: bool = True,
                                               exclude_roads: bool = False) -> trimesh.Trimesh:
    """植被遮挡处理 — 使用Manifold布尔差集镂空。

    根据 manifold_boolean_spec.md 的遮挡关系：
    - 植vegetation - water（P0，必须）
    - vegetation - buildings（P0，必须）
    - vegetation - roads（P1，可选）

    Args:
        vegetation_mesh: 基础植被网格
        water_gdf: 水体数据
        buildings_gdf: 建筑数据
        roads_gdf: 道路数据
        terrain_mesh: 地形网格
        scale: 比例尺
        exclude_water: 是否镂空水体（默认True）
        exclude_buildings: 是否镂空建筑（默认True）
        exclude_roads: 是否镂空道路（默认False，P1优先级）

    Returns:
        镂空后的植被网格（watertight）
    """
    if not is_manifold_available():
        raise ImportError("Manifold库不可用，请先安装: pip install manifold3d>=3.4.0")

    print("\n" + "="*60)
    print("  植被遮挡处理 — Manifold布尔差集")
    print("="*60)

    # Step 1: 转换为Manifold
    t1 = time.time()
    print("\n[Step 1] 植被转换为Manifold...")
    vegetation_m = trimesh_to_manifold(vegetation_mesh)
    print(f"  植被网格: {int(vegetation_m.num_edge())} edges")
    print(f"  转换耗时: {time.time() - t1:.2f}s")

    # Step 2: 创建水体排除柱（P0优先级）
    exclusion_columns = []

    if exclude_water and water_gdf is not None and len(water_gdf) > 0:
        print("\n[Step 2] 创建水体排除柱...")
        terrain_z_min = terrain_mesh.bounds[0][2]
        terrain_z_max = terrain_mesh.bounds[1][2]

        # 水体排除柱的Z范围（穿透植被层）
        z_bottom = terrain_z_min - 1.0
        z_top = terrain_z_max + VEGETATION_Z_OFFSET_MM + 1.0

        water_exclusion_m = create_water_columns_union_manifold(
            water_gdf, z_bottom, z_top, scale
        )

        if not water_exclusion_m.is_empty():
            exclusion_columns.append(water_exclusion_m)
            print(f"  水体排除柱体积: {water_exclusion_m.volume():.2f} mm3")

    # Step 3: 创建建筑排除柱（P0优先级）
    if exclude_buildings and buildings_gdf is not None and len(buildings_gdf) > 0:
        print("\n[Step 3] 创建建筑排除柱...")
        buildings_exclusion_m = create_building_exclusion_columns_manifold(
            buildings_gdf, terrain_mesh, scale
        )
        if not buildings_exclusion_m.is_empty():
            exclusion_columns.append(buildings_exclusion_m)
            print(f"  建筑排除柱体积: {buildings_exclusion_m.volume():.2f} mm3")

    # Step 4: 创建道路排除柱（P1优先级，可选）
    if exclude_roads and roads_gdf is not None and len(roads_gdf) > 0:
        print("\n[Step 4] 创建道路排除柱...")
        roads_exclusion_m = create_road_exclusion_columns_manifold(
            roads_gdf, terrain_mesh, scale
        )
        if not roads_exclusion_m.is_empty():
            exclusion_columns.append(roads_exclusion_m)
            print(f"  道路排除柱体积: {roads_exclusion_m.volume():.2f} mm3")

    # Step 5: 合并所有排除柱
    if len(exclusion_columns) == 0:
        print("\n[Step 5] 无排除柱，返回原始植被")
        return vegetation_mesh

    print("\n[Step 5] 合并所有排除柱...")
    t5 = time.time()

    if len(exclusion_columns) == 1:
        exclusion_union_m = exclusion_columns[0]
    else:
        exclusion_union_m = manifold3d.Manifold.batch_boolean(
            exclusion_columns, manifold3d.OpType.Add
        )

    print(f"  排除柱合并耗时: {time.time() - t5:.2f}s")
    print(f"  排除柱Union体积: {exclusion_union_m.volume():.2f} mm3")

    # Step 6: 植被布尔差集（镂空）
    print("\n[Step 6] 植被布尔差集...")
    t6 = time.time()
    vegetation_final_m = vegetation_m - exclusion_union_m
    print(f"  布尔差集耗时: {time.time() - t6:.2f}s")

    # Step 7: 转回trimesh并验证
    print("\n[Step 7] 转回trimesh并验证...")
    t7 = time.time()
    vegetation_final = manifold_to_trimesh(vegetation_final_m)
    print(f"  转换耗时: {time.time() - t7:.2f}s")

    print(f"\n  最终植被网格:")
    print(f"    Vertices: {len(vegetation_final.vertices)}")
    print(f"    Faces: {len(vegetation_final.faces)}")
    print(f"    Watertight: {vegetation_final.is_watertight}")

    if vegetation_final.is_watertight:
        print(f"    Volume: {vegetation_final.volume:.2f} mm3")

    print("\n" + "="*60)
    print("  植被遮挡处理完成")
    print("="*60)

    return vegetation_final


# ---------------------------------------------------------------------------
# V3 builder: terrain-draped vegetation (per-vertex Z sampling)
# ---------------------------------------------------------------------------

from scipy.spatial import Delaunay
from _TEXTURE_STYLE_OF_DEEPSEEK.block_base import _densify_ring

_MAX_VEG_GRID_POINTS = 6000


def _split_point_touching_topology(
    points: np.ndarray,
    faces: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Duplicate vertices between triangle islands that only touch at a point.

    A top surface can contain two edge-disconnected islands which reuse one
    vertex.  Adding side walls then makes the vertical edge at that vertex
    incident to four faces.  Splitting by shared-edge connectivity preserves
    the exact geometry while giving every shell independent vertex indices.
    """
    faces = np.asarray(faces, dtype=np.int32)
    points = np.asarray(points, dtype=np.float64)
    if len(faces) < 2:
        return points, faces

    parents = np.arange(len(faces), dtype=np.int32)

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = int(parents[index])
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    edge_owner: dict = {}
    for face_index, (a, b, c) in enumerate(faces):
        for start, end in ((a, b), (b, c), (c, a)):
            edge = (min(int(start), int(end)), max(int(start), int(end)))
            owner = edge_owner.setdefault(edge, face_index)
            if owner != face_index:
                union(face_index, owner)

    groups: dict = {}
    for face_index in range(len(faces)):
        groups.setdefault(find(face_index), []).append(face_index)
    if len(groups) > 1:
        split_points = []
        split_faces = []
        offset = 0
        for indices in groups.values():
            component_faces = faces[np.asarray(indices, dtype=np.int32)]
            used = np.unique(component_faces)
            remap = np.full(len(points), -1, dtype=np.int32)
            remap[used] = np.arange(len(used), dtype=np.int32) + offset
            split_points.append(points[used])
            split_faces.append(remap[component_faces])
            offset += len(used)
        points = np.vstack(split_points)
        faces = np.vstack(split_faces)

    # A single globally connected island can still pinch at a boundary vertex
    # (a figure-eight topology).  Split independent local face fans even when
    # those fans reconnect somewhere else in the polygon.
    mutable_points = points.tolist()
    mutable_faces = faces.copy()
    for vertex_index in range(len(points)):
        incident = np.flatnonzero(np.any(mutable_faces == vertex_index, axis=1))
        if len(incident) < 2:
            continue
        local_parents = {int(face_index): int(face_index)
                         for face_index in incident}

        def local_find(face_index: int) -> int:
            while local_parents[face_index] != face_index:
                local_parents[face_index] = local_parents[
                    local_parents[face_index]]
                face_index = local_parents[face_index]
            return face_index

        neighbor_owner = {}
        for face_index in incident:
            face_index = int(face_index)
            neighbors = mutable_faces[face_index][
                mutable_faces[face_index] != vertex_index]
            for neighbor in neighbors:
                neighbor = int(neighbor)
                owner = neighbor_owner.setdefault(neighbor, face_index)
                left, right = local_find(face_index), local_find(owner)
                if left != right:
                    local_parents[right] = left

        fans = {}
        for face_index in incident:
            fans.setdefault(local_find(int(face_index)), []).append(
                int(face_index))
        for fan_faces in list(fans.values())[1:]:
            duplicate = len(mutable_points)
            mutable_points.append(points[vertex_index].tolist())
            for face_index in fan_faces:
                mutable_faces[face_index][
                    mutable_faces[face_index] == vertex_index] = duplicate

    return np.asarray(mutable_points, dtype=np.float64), mutable_faces


def _polygon_to_draped_mesh(
    poly: Polygon,
    terrain_mesh: trimesh.Trimesh,
    scale: float,
    z_offset: float,
    thickness: float = VEGETATION_THICKNESS_MM,
    grid_step_m: float = 80.0,
) -> "trimesh.Trimesh | None":
    """Build a terrain-conforming vegetation plate for one polygon.

    Uses Delaunay triangulation + per-vertex terrain Z sampling so the plate
    follows mountain contours instead of being flat.
    """
    from matplotlib.path import Path as MplPath

    has_holes = len(poly.interiors) > 0

    # Scale polygon exterior to mm
    exterior_coords = np.array(poly.exterior.coords)[:, :2] * scale
    poly_mm = Polygon(exterior_coords)
    if poly_mm.is_empty or poly_mm.area < 0.01:
        return None

    # For polygons with holes, use earcut (handles holes natively)
    if has_holes:
        try:
            from trimesh.creation import triangulate_polygon
            hole_coords_mm = [np.array(h.coords)[:, :2] * scale for h in poly.interiors]
            poly_with_holes_mm = Polygon(exterior_coords, hole_coords_mm)
            verts_2d, faces_tri = triangulate_polygon(poly_with_holes_mm, engine="earcut")
            if len(verts_2d) == 0 or len(faces_tri) == 0:
                return None
            all_pts = np.array(verts_2d, dtype=np.float64)
            faces_top = np.array(faces_tri, dtype=np.int32)
        except Exception:
            return None
    else:
        # Adaptive grid step
        grid_step_mm = grid_step_m * scale
        minx, miny, maxx, maxy = poly_mm.bounds
        effective_step = grid_step_mm
        estimated_pts = poly_mm.area / (grid_step_mm ** 2)
        if estimated_pts > _MAX_VEG_GRID_POINTS:
            effective_step = np.sqrt(poly_mm.area / _MAX_VEG_GRID_POINTS)

        # Interior grid points
        xs = np.arange(minx + effective_step * 0.5, maxx, effective_step)
        ys = np.arange(miny + effective_step * 0.5, maxy, effective_step)
        if len(xs) == 0 or len(ys) == 0:
            # Very small polygon — fall back to earcut
            try:
                from trimesh.creation import triangulate_polygon
                verts_2d, faces_tri = triangulate_polygon(poly_mm, engine="earcut")
                if len(verts_2d) == 0 or len(faces_tri) == 0:
                    return None
                all_pts = np.array(verts_2d, dtype=np.float64)
                faces_top = np.array(faces_tri, dtype=np.int32)
                all_pts, faces_top = _split_point_touching_topology(
                    all_pts, faces_top)
                n_total = len(all_pts)
                # Sample terrain Z
                tz = sample_terrain_z(terrain_mesh, all_pts[:, 0], all_pts[:, 1])
                top_z = tz + z_offset
                bot_z = tz + z_offset - thickness
                top_verts = np.column_stack([all_pts[:, 0], all_pts[:, 1], top_z])
                bot_verts = np.column_stack([all_pts[:, 0], all_pts[:, 1], bot_z])
                vertices = np.vstack([top_verts, bot_verts])
                faces_top_arr = faces_top
                faces_bot_arr = np.column_stack([
                    n_total + faces_top_arr[:, 0],
                    n_total + faces_top_arr[:, 2],
                    n_total + faces_top_arr[:, 1],
                ])
                # Boundary edges for side walls
                edge_count: dict = {}
                for fi in range(len(faces_top_arr)):
                    a, b, c = int(faces_top_arr[fi, 0]), int(faces_top_arr[fi, 1]), int(faces_top_arr[fi, 2])
                    for e in [(a, b), (b, c), (c, a)]:
                        canon = (min(e), max(e))
                        edge_count[canon] = edge_count.get(canon, 0) + 1
                boundary_edges = []
                for fi in range(len(faces_top_arr)):
                    a, b, c = int(faces_top_arr[fi, 0]), int(faces_top_arr[fi, 1]), int(faces_top_arr[fi, 2])
                    for e in [(a, b), (b, c), (c, a)]:
                        canon = (min(e), max(e))
                        if edge_count[canon] == 1:
                            boundary_edges.append(e)
                side_faces = []
                for a, b in boundary_edges:
                    ba, bb = n_total + a, n_total + b
                    side_faces.append([a, ba, bb])
                    side_faces.append([a, bb, b])
                faces_arr = np.vstack([
                    faces_top_arr,
                    faces_bot_arr,
                    np.array(side_faces, dtype=np.int32),
                ])
                return trimesh.Trimesh(vertices=vertices, faces=faces_arr, process=False)
            except Exception:
                return None

        gx, gy = np.meshgrid(xs, ys)
        grid_pts = np.column_stack([gx.ravel(), gy.ravel()])

        mpl_path = MplPath(np.array(poly_mm.exterior.coords))
        mask = mpl_path.contains_points(grid_pts)
        interior_pts = grid_pts[mask]

        # Densify boundary
        boundary_coords = _densify_ring(exterior_coords, effective_step)
        if len(boundary_coords) >= 2 and np.linalg.norm(boundary_coords[-1] - boundary_coords[0]) < 1e-6:
            boundary_coords = boundary_coords[:-1]
        if len(boundary_coords) < 3:
            return None

        # Combine boundary + interior
        if len(interior_pts) > 0:
            all_pts = np.vstack([boundary_coords, interior_pts])
        else:
            all_pts = boundary_coords.copy()
        if len(all_pts) < 3:
            return None

        # Delaunay triangulation + centroid filter
        try:
            tri = Delaunay(all_pts)
        except Exception:
            return None

        simplices = tri.simplices
        centroids = all_pts[simplices].mean(axis=1)
        centroid_mask = mpl_path.contains_points(centroids)
        faces_top = simplices[centroid_mask].astype(np.int32)
        if len(faces_top) == 0:
            return None

    # Point-touching islands need independent indices before side walls are
    # added; otherwise their shared vertical edge becomes non-manifold.
    all_pts, faces_top = _split_point_touching_topology(all_pts, faces_top)

    # Per-vertex terrain Z sampling
    n_total = len(all_pts)
    tz = sample_terrain_z(terrain_mesh, all_pts[:, 0], all_pts[:, 1])

    top_z = tz + z_offset
    bot_z = tz + z_offset - thickness
    top_verts = np.column_stack([all_pts[:, 0], all_pts[:, 1], top_z])
    bot_verts = np.column_stack([all_pts[:, 0], all_pts[:, 1], bot_z])
    vertices = np.vstack([top_verts, bot_verts])

    # Top + bottom faces
    faces_top_arr = faces_top
    faces_bot_arr = np.column_stack([
        n_total + faces_top_arr[:, 0],
        n_total + faces_top_arr[:, 2],
        n_total + faces_top_arr[:, 1],
    ])

    # Boundary edges (edges shared by exactly 1 face)
    edge_count: dict = {}
    for fi in range(len(faces_top_arr)):
        a, b, c = int(faces_top_arr[fi, 0]), int(faces_top_arr[fi, 1]), int(faces_top_arr[fi, 2])
        for e in [(a, b), (b, c), (c, a)]:
            canon = (min(e), max(e))
            edge_count[canon] = edge_count.get(canon, 0) + 1

    boundary_edges = []
    for fi in range(len(faces_top_arr)):
        a, b, c = int(faces_top_arr[fi, 0]), int(faces_top_arr[fi, 1]), int(faces_top_arr[fi, 2])
        for e in [(a, b), (b, c), (c, a)]:
            canon = (min(e), max(e))
            if edge_count[canon] == 1:
                boundary_edges.append(e)

    # Side wall faces
    side_faces = []
    for a, b in boundary_edges:
        ba, bb = n_total + a, n_total + b
        side_faces.append([a, ba, bb])
        side_faces.append([a, bb, b])

    faces_arr = np.vstack([
        faces_top_arr,
        faces_bot_arr,
        np.array(side_faces, dtype=np.int32) if side_faces else np.empty((0, 3), dtype=np.int32),
    ])
    return trimesh.Trimesh(vertices=vertices, faces=faces_arr, process=False)


def build_deepseek_vegetation_v3(
    VL_polys: List[Polygon],
    VO_polys: List[Polygon],
    terrain_mesh: trimesh.Trimesh,
    scale: float,
) -> trimesh.Trimesh:
    """V3 vegetation builder — terrain-draped plates (per-vertex Z sampling).

    Args:
        VL_polys: 植被地标（公园 / 保护区）
        VO_polys: 普通植被
        terrain_mesh: 地形网格（用于 Z 采样）
        scale: mm/m

    Returns:
        Trimesh of all vegetation plates conforming to terrain contour.
    """
    all_groups = [("VL", VL_polys, VL_Z_OFFSET_MM),
                  ("VO", VO_polys, VO_Z_OFFSET_MM)]

    parts: list = []
    n_total = 0
    n_skipped = 0

    for label, polys, z_offset in all_groups:
        for poly in polys:
            if poly.is_empty or poly.area < 10.0:
                n_skipped += 1
                continue

            mesh = _polygon_to_draped_mesh(poly, terrain_mesh, scale, z_offset)
            if mesh is not None:
                parts.append(mesh)
                n_total += 1
            else:
                n_skipped += 1

    print(f"  Vegetation(v3): {n_total} draped ({len(VL_polys)} VL, {len(VO_polys)} VO), "
          f"{n_skipped} skipped")

    if not parts:
        return None

    return trimesh.util.concatenate(parts)
