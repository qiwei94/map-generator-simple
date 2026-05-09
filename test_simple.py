#!/usr/bin/env python
# -*- coding: utf-8 -*-
import os
import sys

# 设置输出编码
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

print("=" * 60)
print("桥梁过滤效果测试")
print("=" * 60)

# 杭州西湖区域（缩小范围）
LAT1, LON1 = 30.20, 120.10
LAT2, LON2 = 30.25, 120.15

print(f"\n测试区域: ({LAT1}, {LON1}) - ({LAT2}, {LON2})")
print(f"区域大小: {(LAT2-LAT1)*111:.1f} km × {(LON2-LON1)*111:.1f} km")

print("\n[Step 1] 获取OSM数据...")
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import fetch_roads, fetch_water

roads_gdf = fetch_roads(LAT1, LON1, LAT2, LON2)
print(f"  道路数据: {len(roads_gdf) if roads_gdf is not None else 0} 条")

water_gdf = fetch_water(LAT1, LON1, LAT2, LON2)
print(f"  水体数据: {len(water_gdf) if water_gdf is not None else 0} 个")

if roads_gdf is None or water_gdf is None:
    print("\n数据获取失败，测试结束")
    sys.exit(1)

print("\n[Step 2] 统计原始数据...")
from _TEXTURE_STYLE_OF_DEEPSEEK.bridge_filter import get_bridge_statistics

stats = get_bridge_statistics(roads_gdf, water_gdf)
print(f"  总道路数: {stats['total_roads']}")
print(f"  bridge=yes标记: {stats['tagged_bridges']}")
print(f"  与水体相交: {stats['water_intersecting']}")
print(f"  桥梁长度: {stats['bridge_length_m']:.1f} m")

print("\n[Step 3] 执行桥梁过滤...")
from _TEXTURE_STYLE_OF_DEEPSEEK.bridge_filter import filter_bridges_only

bridge_roads = filter_bridges_only(
    roads_gdf,
    water_gdf,
    extract_water_crossing_only=True
)

print(f"\n过滤结果:")
print(f"  过滤前: {len(roads_gdf)} 条道路")
print(f"  过滤后: {len(bridge_roads)} 条桥梁")

if len(bridge_roads) > 0:
    total_length = bridge_roads.geometry.length.sum()
    print(f"  桥梁总长度: {total_length:.1f} m")
    
    # 道路类型分布
    if 'highway' in bridge_roads.columns:
        print(f"\n桥梁道路类型:")
        for highway, count in bridge_roads['highway'].value_counts().items():
            print(f"    {highway}: {count} 条")

print("\n[完成] 桥梁过滤测试完成")
print("=" * 60)