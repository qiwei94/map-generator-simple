#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""快速测试桥梁过滤效果"""

import os
import sys

_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import fetch_roads, fetch_water
from _TEXTURE_STYLE_OF_DEEPSEEK.bridge_filter import (
    filter_bridges_only,
    get_bridge_statistics,
    visualize_bridge_filtering
)

# 杭州西湖区域
LAT1, LON1 = 30.1375, 120.020
LAT2, LON2 = 30.3625, 120.280

print("=" * 60)
print("  桥梁过滤效果测试")
print("=" * 60)

# Step 1: 获取数据
print("\n[Step 1] 获取OSM数据...")
roads_gdf = fetch_roads(LAT1, LON1, LAT2, LON2)
water_gdf = fetch_water(LAT1, LON1, LAT2, LON2)

print(f"  原始道路数: {len(roads_gdf) if roads_gdf is not None else 0}")
print(f"  原始水体数: {len(water_gdf) if water_gdf is not None else 0}")

# Step 2: 统计原始数据
print("\n[Step 2] 统计原始数据...")
stats_original = get_bridge_statistics(roads_gdf, water_gdf)
print(f"  总道路数: {stats_original['total_roads']}")
print(f"  标记为bridge=yes: {stats_original['tagged_bridges']}")
print(f"  与水体相交的道路: {stats_original['water_intersecting']}")
print(f"  桥梁总长度: {stats_original['bridge_length_m']:.1f} m")

# Step 3: 应用桥梁过滤
print("\n[Step 3] 应用桥梁过滤...")
if roads_gdf is not None and water_gdf is not None:
    bridge_roads = filter_bridges_only(
        roads_gdf, 
        water_gdf,
        extract_water_crossing_only=True
    )
    
    print(f"\n过滤结果:")
    print(f"  过滤后道路数: {len(bridge_roads)}")
    if len(bridge_roads) > 0:
        print(f"  过滤后总长度: {bridge_roads.geometry.length.sum():.1f} m")
        
        # 显示道路类型分布
        if 'highway' in bridge_roads.columns:
            print(f"\n道路类型分布:")
            highway_counts = bridge_roads['highway'].value_counts()
            for highway, count in highway_counts.items():
                print(f"    {highway}: {count} 条")
        
        # 显示部分道路示例
        print(f"\n前5条桥梁道路示例:")
        for idx, row in bridge_roads.head(5).iterrows():
            highway = row.get('highway', 'unknown')
            length = row.geometry.length
            print(f"    [{idx}] highway={highway}, length={length:.1f}m")
    else:
        print("  无桥梁道路（过滤后结果为空）")
    
    # Step 4: 可视化对比
    visualize_bridge_filtering(roads_gdf, bridge_roads, water_gdf)
    
else:
    print("  数据获取失败")

print("\n" + "=" * 60)
print("  测试完成")
print("=" * 60)