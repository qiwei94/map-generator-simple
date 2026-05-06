"""对象4实现：地形+道路融合+水体镂空（Manifold布尔运算）。

完整实现流程：
1. 地形重建
2. 道路布尔并集融合（可选）
3. 水体布尔差集镂空 — 批量 Union + 单次 Subtract
4. 网格修复和验证
"""

import time
import numpy as np
import trimesh
import geopandas as gpd
from typing import Dict, Any
from shapely.geometry import Polygon, MultiPolygon

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import build_deepseek_terrain
from _TEXTURE_STYLE_OF_DEEPSEEK.roads import build_deepseek_roads
from _TEXTURE_STYLE_OF_DEEPSEEK.water_column import (
    create_water_columns_union_manifold,
    extrude_water_column_manifold,
)
from _TEXTURE_STYLE_OF_DEEPSEEK._bridge import (
    trimesh_to_manifold,
    manifold_to_trimesh,
    is_manifold_available,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.config import (
    Z_TERRAIN_BASE,
    TERRAIN_THICKNESS_MM,
)


def build_terrain_with_water_holes_manifold(elevation_grid: np.ndarray,
                                             width_m: float,
                                             height_m: float,
                                             area_km2: float,
                                             scale: float,
                                             water_gdf: gpd.GeoDataFrame,
                                             roads_gdf: gpd.GeoDataFrame = None,
                                             enable_roads_fusion: bool = False,
                                             bridges_only: bool = True) -> Dict[str, Any]:
    """实现对象4：地形+道路融合+水体镂空（Manifold布尔运算）。

    Args:
        elevation_grid: 高程网格（2D numpy array）
        width_m: 地形宽度（米）
        height_m: 地形高度（米）
        area_km2: 区域面积（km²）
        scale: 比例尺（mm/m）
        water_gdf: 水体GeoDataFrame（已投影到local坐标）
        roads_gdf: 道路GeoDataFrame（可选）
        enable_roads_fusion: 是否启用道路融合
        bridges_only: 是否只处理桥梁（道路跨越水体的部分）

    Returns:
        {
            "mesh": trimesh.Trimesh,
            "stats": dict,
            "validation": dict,
        }
    """
    if not is_manifold_available():
        raise ImportError("Manifold库不可用，请先安装: pip install manifold3d>=3.4.0")

    print("\n" + "=" * 60)
    print("  对象4：地形+道路融合+水体镂空")
    print("=" * 60)

    stats = {
        "terrain_faces": 0,
        "roads_faces": 0,
        "water_columns": 0,
        "final_faces": 0,
        "boolean_ops": [],
    }

    # ========================================
    # Step 1: 地形重建
    # ========================================
    t1 = time.time()
    print("\n[Step 1] 地形重建...")
    terrain_mesh = build_deepseek_terrain(
        elevation_grid, width_m, height_m, area_km2, scale
    )

    stats["terrain_faces"] = len(terrain_mesh.faces)
    print(f"  地形网格: {len(terrain_mesh.vertices)} vertices, {stats['terrain_faces']} faces")
    print(f"  Terrain Z range: {terrain_mesh.bounds[0][2]:.2f} → {terrain_mesh.bounds[1][2]:.2f} mm")
    print(f"  Watertight: {terrain_mesh.is_watertight}")
    print(f"  ⏱ Step 1 耗时: {time.time() - t1:.1f}s")

    # 转换为Manifold
    t1b = time.time()
    terrain_m = trimesh_to_manifold(terrain_mesh)
    stats["boolean_ops"].append("terrain → Manifold")
    print(f"  ⏱ trimesh→Manifold 转换: {time.time() - t1b:.1f}s")

    # ========================================
    # Step 2: 道路布尔并集融合（可选）
    # ========================================
    if enable_roads_fusion and roads_gdf is not None and len(roads_gdf) > 0:
        print("\n[Step 2] 道路布尔并集融合...")
        print(f"  桥梁过滤模式: {bridges_only}")

        roads_mesh = build_deepseek_roads(
            roads_gdf, terrain_mesh, area_km2, scale,
            water_gdf=water_gdf,  # 传入水体数据用于桥梁过滤
            filter_bridges_only=bridges_only  # 只处理桥梁
        )

        if roads_mesh is not None and len(roads_mesh.faces) > 0:
            stats["roads_faces"] = len(roads_mesh.faces)
            print(f"  道路网格: {len(roads_mesh.vertices)} vertices, {stats['roads_faces']} faces")

            roads_m = trimesh_to_manifold(roads_mesh)
            terrain_m = terrain_m.union(roads_m)
            stats["boolean_ops"].append("terrain ∪ roads (bridges only)")
            print("  道路融合完成")
        else:
            print("  过滤后道路数据为空，跳过融合")
    else:
        print("\n[Step 2] 道路融合: 跳过")

    # ========================================
    # Step 3: 水体布尔差集镂空 — 批量 Union + 单次 Subtract
    # ========================================
    t3 = time.time()
    print("\n[Step 3] 水体布尔差集镂空...")

    if water_gdf is not None and len(water_gdf) > 0:
        # 水体挤出柱的Z范围（确保穿透地形）
        terrain_z_min = terrain_mesh.bounds[0][2]
        terrain_z_max = terrain_mesh.bounds[1][2]

        z_bottom = terrain_z_min - 1.0
        z_top = terrain_z_max + 1.0

        print(f"  地形Z范围: {terrain_z_min:.2f} → {terrain_z_max:.2f} mm")
        print(f"  水体挤出柱Z: {z_bottom:.2f} → {z_top:.2f} mm")

        # 批量创建水体挤出柱并Union（坐标自动从model meters缩放到model mm）
        t3a = time.time()
        water_union = create_water_columns_union_manifold(
            water_gdf, z_bottom, z_top, scale,
        )
        print(f"  ⏱ 水体 Union 创建: {time.time() - t3a:.1f}s")

        if not water_union.is_empty():
            n_water_edges = int(water_union.num_edge())
            print(f"  水体Union网格: {n_water_edges} edges, "
                  f"volume={water_union.volume():.2f} mm³")

            # 单次布尔差集: terrain - water
            t3b = time.time()
            terrain_m = terrain_m - water_union
            print(f"  ⏱ 布尔差集 (terrain − water): {time.time() - t3b:.1f}s")
            stats["boolean_ops"].append(f"terrain - water (union of {n_water_edges} cols)")

            # 获取水体数量（从water_union统计）
            n_water_features = n_water_edges
            print(f"  镂空完成 (terrain − water_union)")
        else:
            print("  无符合条件的水体，跳过镂空")
    else:
        print("  无水体数据，跳过镂空")
    print(f"  ⏱ Step 3 总耗时: {time.time() - t3:.1f}s")

    # ========================================
    # Step 4: 转回trimesh并验证
    # ========================================
    t4 = time.time()
    print("\n[Step 4] 转回trimesh并验证...")
    terrain_final = manifold_to_trimesh(terrain_m)
    print(f"  ⏱ manifold→trimesh 转换: {time.time() - t4:.1f}s")

    stats["final_faces"] = len(terrain_final.faces)
    print(f"  最终网格: {len(terrain_final.vertices)} vertices, {stats['final_faces']} faces")
    print(f"  Watertight: {terrain_final.is_watertight}")
    print(f"  Volume: {terrain_final.volume if terrain_final.is_watertight else 0:.2f} mm³")
    print(f"  Z range: {terrain_final.bounds[0][2]:.2f} → {terrain_final.bounds[1][2]:.2f} mm")

    validation = {
        "watertight": terrain_final.is_watertight,
        "volume": terrain_final.volume if terrain_final.is_watertight else 0,
        "bounds": terrain_final.bounds,
        "n_vertices": len(terrain_final.vertices),
        "n_faces": len(terrain_final.faces),
    }

    print("\n" + "=" * 60)
    print("  对象4实现完成")
    print("=" * 60)

    return {
        "mesh": terrain_final,
        "stats": stats,
        "validation": validation,
    }
