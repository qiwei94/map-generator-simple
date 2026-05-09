#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""桥梁过滤测试 - 详细性能分析"""

import os
import sys
import time

_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

print("=" * 60)
print("  桥梁过滤测试 - 详细性能分析")
print("=" * 60)

# 测试区域：杭州西湖（缩小范围，快速测试）
LAT1, LON1 = 30.22, 120.12  # 西湖核心区域
LAT2, LON2 = 30.26, 120.16

print(f"\n测试区域: ({LAT1}, {LON1}) -> ({LAT2}, {LON2})")
area_km2 = (LAT2 - LAT1) * 111 * (LON2 - LON1) * 111 * 0.866
print(f"区域面积: {area_km2:.1f} km²")

# ========================================
# Step 1: 获取道路数据
# ========================================
print("\n" + "=" * 60)
print("[Step 1] 获取道路数据")
print("=" * 60)

t_start = time.time()
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import fetch_roads

roads_gdf = fetch_roads(LAT1, LON1, LAT2, LON2)
t_fetch_roads = time.time() - t_start

print(f"  ⏱ 获取道路数据耗时: {t_fetch_roads:.1f}s")
if roads_gdf is not None:
    print(f"  ✓ 道路总数: {len(roads_gdf)} 条")
    if 'highway' in roads_gdf.columns:
        highway_counts = roads_gdf['highway'].value_counts()
        print(f"  道路类型分布:")
        for highway, count in highway_counts.head(5).items():
            print(f"    - {highway}: {count} 条")
else:
    print("  ✗ 道路数据获取失败")
    sys.exit(1)

# ========================================
# Step 2: 获取水体数据
# ========================================
print("\n" + "=" * 60)
print("[Step 2] 获取水体数据")
print("=" * 60)

t_start = time.time()
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import fetch_water

water_gdf = fetch_water(LAT1, LON1, LAT2, LON2)
t_fetch_water = time.time() - t_start

print(f"  ⏱ 获取水体数据耗时: {t_fetch_water:.1f}s")
if water_gdf is not None and len(water_gdf) > 0:
    print(f"  ✓ 水体总数: {len(water_gdf)} 个")
    water_area = water_gdf.geometry.area.sum()
    print(f"  水体总面积: {water_area:.1f} m²")
else:
    print("  ✗ 无水体数据")

# ========================================
# Step 3: 桥梁统计（原始数据）
# ========================================
print("\n" + "=" * 60)
print("[Step 3] 桥梁统计（原始数据）")
print("=" * 60)

t_start = time.time()
from _TEXTURE_STYLE_OF_DEEPSEEK.bridge_filter import get_bridge_statistics

stats_original = get_bridge_statistics(roads_gdf, water_gdf)
t_stats = time.time() - t_start

print(f"  ⏱ 统计耗时: {t_stats:.2f}s")
print(f"  总道路数: {stats_original['total_roads']}")
print(f"  标记为 bridge=yes: {stats_original['tagged_bridges']}")
print(f"  与水体相交的道路: {stats_original['water_intersecting']}")
print(f"  桥梁总长度: {stats_original['bridge_length_m']:.1f} m")

# ========================================
# Step 4: 执行桥梁过滤
# ========================================
print("\n" + "=" * 60)
print("[Step 4] 执行桥梁过滤")
print("=" * 60)

t_start = time.time()
from _TEXTURE_STYLE_OF_DEEPSEEK.bridge_filter import filter_bridges_only

bridge_roads = filter_bridges_only(
    roads_gdf,
    water_gdf,
    extract_water_crossing_only=True
)
t_filter = time.time() - t_start

print(f"  ⏱ 过滤耗时: {t_filter:.2f}s")
print(f"  过滤前道路: {len(roads_gdf)} 条")
print(f"  过滤后桥梁: {len(bridge_roads)} 条")
print(f"  过滤率: {(1 - len(bridge_roads)/len(roads_gdf))*100:.1f}%")

if len(bridge_roads) > 0:
    bridge_length = bridge_roads.geometry.length.sum()
    print(f"  桥梁总长度: {bridge_length:.1f} m")
    
    # 显示桥梁类型
    if 'highway' in bridge_roads.columns:
        highway_counts = bridge_roads['highway'].value_counts()
        print(f"\n  桥梁类型分布:")
        for highway, count in highway_counts.items():
            print(f"    - {highway}: {count} 条")
    
    # 显示前5条桥梁
    print(f"\n  前5条桥梁示例:")
    for idx, row in bridge_roads.head(5).iterrows():
        highway = row.get('highway', 'unknown')
        length = row.geometry.length
        bridge_tag = row.get('bridge', 'no')
        print(f"    [{idx}] highway={highway}, length={length:.1f}m, bridge={bridge_tag}")

# ========================================
# 性能分析总结
# ========================================
print("\n" + "=" * 60)
print("性能分析总结")
print("=" * 60)

total_time = t_fetch_roads + t_fetch_water + t_stats + t_filter

print(f"\n各阶段耗时:")
print(f"  [Step 1] 道路数据获取: {t_fetch_roads:.1f}s ({t_fetch_roads/total_time*100:.1f}%)")
print(f"  [Step 2] 水体数据获取: {t_fetch_water:.1f}s ({t_fetch_water/total_time*100:.1f}%)")
print(f"  [Step 3] 桥梁统计: {t_stats:.2f}s ({t_stats/total_time*100:.1f}%)")
print(f"  [Step 4] 桥梁过滤: {t_filter:.2f}s ({t_filter/total_time*100:.1f}%)")
print(f"\n总耗时: {total_time:.1f}s")

print(f"\n过滤效果:")
print(f"  输入: {len(roads_gdf)} 条道路")
print(f"  输出: {len(bridge_roads)} 条桥梁")
print(f"  过滤掉: {len(roads_gdf) - len(bridge_roads)} 条道路")

print(f"\n关键指标:")
print(f"  - 桥梁占比: {len(bridge_roads)/len(roads_gdf)*100:.2f}%")
print(f"  - 平均桥梁长度: {bridge_length/len(bridge_roads):.1f}m" if len(bridge_roads) > 0 else "  - 无桥梁")

print("\n" + "=" * 60)
print("测试完成")
print("=" * 60)