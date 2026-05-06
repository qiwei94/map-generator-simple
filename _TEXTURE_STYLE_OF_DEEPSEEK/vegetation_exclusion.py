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

import time
import numpy as np
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
from _TEXTURE_STYLE_OF_DEEPSEEK.water_column import (
    _shapely_poly_to_crosssection,
    create_water_columns_union_manifold,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.terrain import sample_terrain_z
from _TEXTURE_STYLE_OF_DEEPSEEK.config import (
    Z_TERRAIN_BASE,
    VEGETATION_Z_OFFSET_MM,
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

    cs = _shapely_poly_to_crosssection(polygon)
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

    print(f"\n[建筑排除柱] 创建 {len(buildings_gdf)} 个建筑的排除柱...")
    columns = []
    n_created = 0

    for idx, row in buildings_gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue

        # 处理Polygon和MultiPolygon
        polygons = []
        if isinstance(geom, MultiPolygon):
            polygons = list(geom.geoms)
        elif isinstance(geom, Polygon):
            polygons = [geom]
        else:
            continue

        for poly in polygons:
            if poly.area < 10:  # 过滤太小的建筑
                continue

            # 采样地形高度（建筑中心点）
            centroid = poly.centroid
            terrain_z = sample_terrain_z(terrain_mesh,
                                          np.array([centroid.x]) * scale,
                                          np.array([centroid.y]) * scale)
            if len(terrain_z) == 0 or np.isnan(terrain_z[0]):
                continue

            z_base = float(terrain_z[0])

            # 建筑排除柱的Z范围（从植被下方到建筑上方）
            # 植被层高度: terrain_z + VEGETATION_Z_OFFSET_MM (约0.1mm)
            # 建筑层高度: terrain_z - 0.04mm (嵌入) + building_height (约+5mm)
            # 排除柱需要穿透整个植被层到建筑层
            z_bottom = z_base - z_buffer
            z_top = z_base + 5.0 + z_buffer  # 建筑高度约5mm，加上缓冲

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

    print(f"\n[道路排除柱] 创建 {len(roads_gdf)} 条道路的排除柱...")
    columns = []
    n_created = 0

    for idx, row in roads_gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue

        # 处理LineString和MultiLineString
        lines = []
        if isinstance(geom, MultiLineString):
            lines = list(geom.geoms)
        elif isinstance(geom, LineString):
            lines = [geom]
        else:
            continue

        for line in lines:
            if line.length < 10:  # 过滤太短的道路
                continue

            # 道路缓冲为Polygon（用于创建排除柱）
            road_poly = line.buffer(road_width_m / 2)

            # 采样地形高度（道路中心）
            centroid = line.centroid
            terrain_z = sample_terrain_z(terrain_mesh,
                                          np.array([centroid.x]) * scale,
                                          np.array([centroid.y]) * scale)
            if len(terrain_z) == 0 or np.isnan(terrain_z[0]):
                continue

            z_base = float(terrain_z[0])

            # 道路排除柱的Z范围
            # 道路层高度: terrain_z + 0.51mm
            z_bottom = z_base - z_buffer
            z_top = z_base + 0.51 + 0.4 + z_buffer  # 道路厚度约0.4mm

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


def build_deepseek_vegetation_with_exclusions(gdf: gpd.GeoDataFrame,
                                               terrain_mesh: trimesh.Trimesh,
                                               scale: float,
                                               water_gdf: gpd.GeoDataFrame = None,
                                               buildings_gdf: gpd.GeoDataFrame = None,
                                               roads_gdf: gpd.GeoDataFrame = None,
                                               exclude_water: bool = True,
                                               exclude_buildings: bool = True,
                                               exclude_roads: bool = False) -> trimesh.Trimesh:
    """构建植被并处理遮挡关系（完整流程）。

    Args:
        gdf: 植被GeoDataFrame
        terrain_mesh: 地形网格
        scale: 比例尺
        water_gdf: 水体数据（可选）
        buildings_gdf: 建筑数据（可选）
        roads_gdf: 道路数据（可选）
        exclude_water: 是否镂空水体（默认True，P0）
        exclude_buildings: 是否镂空建筑（默认True，P0）
        exclude_roads: 是否镂空道路（默认False，P1）

    Returns:
        镂空后的植被网格（watertight）
    """
    if gdf is None or len(gdf) == 0:
        return None

    # Step 1: 构建基础植被网格
    from _TEXTURE_STYLE_OF_DEEPSEEK.vegetation import build_deepseek_vegetation

    print("\n[Step 1] 构建基础植被网格...")
    vegetation_mesh = build_deepseek_vegetation(gdf, terrain_mesh, scale)

    if vegetation_mesh is None or len(vegetation_mesh.faces) == 0:
        print("  植被数据为空，返回None")
        return None

    print(f"  基础植被网格: {len(vegetation_mesh.vertices)} vertices, {len(vegetation_mesh.faces)} faces")

    # Step 2: 检查是否需要遮挡处理
    need_exclusion = (
        (exclude_water and water_gdf is not None and len(water_gdf) > 0) or
        (exclude_buildings and buildings_gdf is not None and len(buildings_gdf) > 0) or
        (exclude_roads and roads_gdf is not None and len(roads_gdf) > 0)
    )

    if not need_exclusion:
        print("  无需遮挡处理，返回基础植被")
        return vegetation_mesh

    # Step 3: 执行遮挡处理
    vegetation_final = build_vegetation_with_exclusions_manifold(
        vegetation_mesh,
        water_gdf,
        buildings_gdf,
        roads_gdf,
        terrain_mesh,
        scale,
        exclude_water,
        exclude_buildings,
        exclude_roads
    )

    return vegetation_final