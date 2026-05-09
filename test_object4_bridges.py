#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""测试对象4：地形+桥梁融合+水体镂空。

测试内容：
1. 地形重建
2. 水体镂空
3. 桥梁过滤和融合（只保留跨越水体的道路段）

杭州案例（西湖区域）
"""

import os
import sys
import time
import numpy as np

# 确保项目根目录在path中
_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm, project_geodataframe
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import fetch_water, fetch_roads
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.elevation import fetch_elevation_grid
from _TEXTURE_STYLE_OF_DEEPSEEK.object4_terrain_with_holes import build_terrain_with_water_holes_manifold
from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf
from _TEXTURE_STYLE_OF_DEEPSEEK.config import compute_scale
from _TEXTURE_STYLE_OF_DEEPSEEK.bridge_filter import get_bridge_statistics, visualize_bridge_filtering

# 杭州西湖区域（25km x 25km）
LAT1, LON1 = 30.1375, 120.020   # south-west
LAT2, LON2 = 30.3625, 120.280   # north-east


def test_object4_with_bridges():
    """测试对象4（地形+桥梁+水体镂空）"""
    print("="*60)
    print("  测试对象4：地形+桥梁融合+水体镂空")
    print("="*60)

    output_dir = "output/test_object4_bridges"
    os.makedirs(output_dir, exist_ok=True)

    # ========================================
    # Step 0: 坐标系统
    # ========================================
    print("\n[Step 0] 坐标系统...")
    bbox = bbox_to_utm(LAT1, LON1, LAT2, LON2)
    utm_crs = bbox["utm_crs"]
    origin = bbox["origin"]
    utm_bbox = bbox["utm_bbox"]
    width_m = bbox["width_m"]
    height_m = bbox["height_m"]
    area_km2 = bbox["area_km2"]

    print(f"  Area: {area_km2:.1f} km2")
    print(f"  UTM zone: {utm_crs.utm_zone}")

    scale = compute_scale(width_m, height_m)
    print(f"  Scale: {scale:.6f} mm/m")

    south, west, north, east = bbox["wgs84_bbox"]

    # ========================================
    # Step 1: 获取高程数据
    # ========================================
    print("\n[Step 1] 获取高程数据...")
    resolution = 512  # 测试用较低分辨率
    elevation_grid = fetch_elevation_grid(south, west, north, east, resolution)
    print(f"  Grid shape: {elevation_grid.shape}")
    print(f"  Elevation range: {elevation_grid.min():.1f}m - {elevation_grid.max():.1f}m")

    # ========================================
    # Step 2: 获取OSM数据
    # ========================================
    print("\n[Step 2] 获取OSM数据...")

    # 水体
    water_gdf = fetch_water(south, west, north, east)
    print(f"  Water features: {len(water_gdf) if water_gdf is not None else 0}")

    # 道路
    roads_gdf = fetch_roads(south, west, north, east)
    print(f"  Roads: {len(roads_gdf) if roads_gdf is not None else 0}")

    # ========================================
    # Step 3: 投影到本地坐标
    # ========================================
    print("\n[Step 3] 投影到本地坐标...")

    if water_gdf is not None and len(water_gdf) > 0:
        water_gdf = project_geodataframe(water_gdf, utm_crs, origin, clip_bbox=utm_bbox)

    if roads_gdf is not None and len(roads_gdf) > 0:
        roads_gdf = project_geodataframe(roads_gdf, utm_crs, origin, clip_bbox=utm_bbox)

    # ========================================
    # Step 4: 桥梁统计（调试）
    # ========================================
    print("\n[Step 4] 桥梁统计...")
    stats = get_bridge_statistics(roads_gdf, water_gdf)
    print(f"  总道路数: {stats['total_roads']}")
    print(f"  标记的桥梁: {stats['tagged_bridges']}")
    print(f"  与水体相交的道路: {stats['water_intersecting']}")
    print(f"  桥梁长度: {stats['bridge_length_m']:.1f} m")

    # ========================================
    # Step 5: 构建对象4（地形+桥梁+水体镂空）
    # ========================================
    print("\n[Step 5] 构建对象4...")

    # 测试1: 只地形+水体镂空（无道路）
    print("\n--- 测试1: 地形+水体镂空（无道路）---")
    result1 = build_terrain_with_water_holes_manifold(
        elevation_grid, width_m, height_m, area_km2, scale,
        water_gdf=water_gdf,
        roads_gdf=None,
        enable_roads_fusion=False,
    )

    terrain_mesh1 = result1["mesh"]
    print(f"  网格: {len(terrain_mesh1.vertices)} vertices, {len(terrain_mesh1.faces)} faces")
    print(f"  Watertight: {terrain_mesh1.is_watertight}")

    # 导出测试1
    output_path1 = os.path.join(output_dir, "terrain_water_holes_no_roads.3mf")
    export_deepseek_3mf(
        {"terrain_surface": terrain_mesh1},
        output_path1,
        extruders=4
    )
    print(f"  导出: {output_path1}")

    # 测试2: 地形+桥梁融合+水体镂空
    print("\n--- 测试2: 地形+桥梁融合+水体镂空 ---")
    result2 = build_terrain_with_water_holes_manifold(
        elevation_grid, width_m, height_m, area_km2, scale,
        water_gdf=water_gdf,
        roads_gdf=roads_gdf,
        enable_roads_fusion=True,
        bridges_only=True,  # 只处理桥梁
    )

    terrain_mesh2 = result2["mesh"]
    print(f"  网格: {len(terrain_mesh2.vertices)} vertices, {len(terrain_mesh2.faces)} faces")
    print(f"  Watertight: {terrain_mesh2.is_watertight}")

    # 导出测试2
    output_path2 = os.path.join(output_dir, "terrain_water_holes_with_bridges.3mf")
    export_deepseek_3mf(
        {"terrain_surface": terrain_mesh2},
        output_path2,
        extruders=4
    )
    print(f"  导出: {output_path2}")

    # ========================================
    # 总结
    # ========================================
    print("\n" + "="*60)
    print("  测试完成")
    print("="*60)
    print(f"  输出目录: {output_dir}")
    print(f"  测试1（无道路）: {output_path1}")
    print(f"  测试2（有桥梁）: {output_path2}")
    print("\n下一步:")
    print("  1. 在Bambu Studio中打开两个3MF文件")
    print("  2. 检查水体镂空是否正确")
    print("  3. 检查桥梁是否只在水体上方")
    print("  4. 检查网格是否watertight")


if __name__ == "__main__":
    test_object4_with_bridges()