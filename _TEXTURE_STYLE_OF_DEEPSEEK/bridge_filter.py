"""桥梁过滤器 — 只保留水体上方跨越的桥梁道路段。

根据 manifold_boolean_spec.md 第192-215行的桥梁处理策略：
- 桥梁：道路跨越水体，应保留（bridge=yes标签）
- 普通道路：应过滤掉，只保留与水体交集部分

实现策略：
1. 筛选 bridge=yes 标签的道路
2. 计算道路与水体的交集（只保留实际跨越水体的段）
3. 可选：提取纯桥梁段（道路在水体内的片段）
"""

import numpy as np
import geopandas as gpd
from shapely.geometry import LineString, MultiLineString, Polygon, MultiPolygon
from shapely.ops import split, unary_union, linemerge
from typing import Optional


def filter_bridges_only(roads_gdf: gpd.GeoDataFrame,
                        water_gdf: gpd.GeoDataFrame,
                        extract_water_crossing_only: bool = True) -> gpd.GeoDataFrame:
    """只保留桥梁道路（跨越水体的部分）。

    Args:
        roads_gdf: 道路GeoDataFrame（包含 highway, bridge 等标签）
        water_gdf: 水体GeoDataFrame
        extract_water_crossing_only: 是否只提取道路在水体内的片段

    Returns:
        过滤后的道路GeoDataFrame，只包含桥梁段
    """
    if roads_gdf is None or len(roads_gdf) == 0:
        return roads_gdf

    if water_gdf is None or len(water_gdf) == 0:
        print("[桥梁过滤] 无水体数据，返回空道路")
        return gpd.GeoDataFrame(columns=roads_gdf.columns, crs=roads_gdf.crs)

    print(f"\n[桥梁过滤] 输入道路: {len(roads_gdf)} 条")
    print(f"[桥梁过滤] 输入水体: {len(water_gdf)} 个")

    # Step 1: 创建水体union（用于快速判断交集）
    water_union = unary_union(water_gdf.geometry)
    print(f"  水体Union面积: {water_union.area:.1f} m²")

    # Step 2: 筛选 bridge=yes 标签的道路
    bridge_roads = roads_gdf[roads_gdf.get('bridge', '') == 'yes'].copy()
    n_tagged_bridges = len(bridge_roads)

    print(f"  Step 1: 标记为 bridge=yes 的道路: {n_tagged_bridges} 条")

    # Step 3: 如果没有显式标记的桥梁，则从所有道路中筛选与水体相交的
    if n_tagged_bridges == 0:
        print("  未找到显式标记的桥梁，筛选所有与水体相交的道路...")
        intersecting_roads = roads_gdf[roads_gdf.geometry.intersects(water_union)].copy()
        print(f"  与水体相交的道路: {len(intersecting_roads)} 条")
        bridge_roads = intersecting_roads
    else:
        # 进一步筛选：确保标记的桥梁确实与水体相交
        bridge_roads = bridge_roads[bridge_roads.geometry.intersects(water_union)].copy()
        print(f"  实际跨越水体的标记桥梁: {len(bridge_roads)} 条")

    if len(bridge_roads) == 0:
        print("  无桥梁道路，返回空")
        return gpd.GeoDataFrame(columns=roads_gdf.columns, crs=roads_gdf.crs)

    # Step 4: 提取纯桥梁段（道路在水体内的片段）
    if extract_water_crossing_only:
        print("  Step 2: 提取道路在水体内的片段...")
        bridge_segments = []

        for idx, row in bridge_roads.iterrows():
            road_geom = row.geometry

            # 提取道路与水体的交集（纯桥梁段）
            intersection = road_geom.intersection(water_union)

            if intersection.is_empty:
                continue

            # 处理交集结果（可能是 LineString 或 MultiLineString）
            if isinstance(intersection, (LineString, MultiLineString)):
                # 创建新的行，保留原有属性
                new_row = row.copy()
                new_row.geometry = intersection
                bridge_segments.append(new_row)

        if bridge_segments:
            result_gdf = gpd.GeoDataFrame(bridge_segments, crs=roads_gdf.crs)
            print(f"  提取的纯桥梁段: {len(result_gdf)} 条")
            print(f"  桥梁段总长度: {result_gdf.geometry.length.sum():.1f} m")
        else:
            print("  无桥梁段，返回空")
            return gpd.GeoDataFrame(columns=roads_gdf.columns, crs=roads_gdf.crs)

        return result_gdf
    else:
        # 只返回完整的桥梁道路（不提取片段）
        print(f"  返回完整桥梁道路: {len(bridge_roads)} 条")
        return bridge_roads


def split_road_at_water_boundary(road_geom: LineString,
                                  water_geom: Polygon) -> tuple:
    """将道路在水体边界处切分，返回桥梁段和陆地段。

    Args:
        road_geom: 道路几何（LineString）
        water_geom: 水体几何（Polygon）

    Returns:
        (bridge_segments, land_segments): 桥梁段和陆地段的列表
    """
    # 检查是否相交
    if not road_geom.intersects(water_geom):
        return ([], [road_geom])

    # 切分道路
    try:
        # 使用 split 函数在水体边界处切分道路
        boundary = water_geom.boundary
        split_roads = split(road_geom, boundary)

        # 分类：桥梁段（在水体内） vs 陆地段（在水体外）
        bridge_segments = []
        land_segments = []

        for segment in split_roads.geoms:
            # 检查片段中心点是否在水体内
            centroid = segment.centroid
            if water_geom.contains(centroid):
                bridge_segments.append(segment)
            else:
                land_segments.append(segment)

        return (bridge_segments, land_segments)

    except Exception as e:
        # split 操作失败时，返回简单交集
        intersection = road_geom.intersection(water_geom)
        if isinstance(intersection, LineString):
            return ([intersection], [])
        elif isinstance(intersection, MultiLineString):
            return (list(intersection.geoms), [])
        else:
            return ([], [road_geom])


def get_bridge_statistics(roads_gdf: gpd.GeoDataFrame,
                          water_gdf: gpd.GeoDataFrame) -> dict:
    """统计桥梁道路信息（用于调试和验证）。

    Returns:
        {
            "total_roads": 总道路数,
            "tagged_bridges": bridge=yes 标记数,
            "water_intersecting": 与水体相交的道路数,
            "bridge_length_m": 桥梁总长度（米）,
        }
    """
    if roads_gdf is None or len(roads_gdf) == 0:
        return {"total_roads": 0}

    stats = {
        "total_roads": len(roads_gdf),
        "tagged_bridges": 0,
        "water_intersecting": 0,
        "bridge_length_m": 0.0,
    }

    # 统计显式标记的桥梁
    tagged = roads_gdf[roads_gdf.get('bridge', '') == 'yes']
    stats["tagged_bridges"] = len(tagged)

    if water_gdf is not None and len(water_gdf) > 0:
        water_union = unary_union(water_gdf.geometry)

        # 统计与水体相交的道路
        intersecting = roads_gdf[roads_gdf.geometry.intersects(water_union)]
        stats["water_intersecting"] = len(intersecting)

        # 计算桥梁长度（交集长度）
        if len(intersecting) > 0:
            for idx, row in intersecting.iterrows():
                intersection = row.geometry.intersection(water_union)
                if not intersection.is_empty:
                    stats["bridge_length_m"] += intersection.length

    return stats


def visualize_bridge_filtering(original_gdf: gpd.GeoDataFrame,
                                filtered_gdf: gpd.GeoDataFrame,
                                water_gdf: gpd.GeoDataFrame):
    """可视化桥梁过滤结果（用于调试）。

    输出：
    - 原始道路网络概览
    - 水体范围概览
    - 过滤后的桥梁段概览
    """
    print("\n" + "="*60)
    print("  桥梁过滤可视化")
    print("="*60)

    # 原始道路统计
    print(f"\n[原始道路]")
    print(f"  总数: {len(original_gdf)} 条")
    if len(original_gdf) > 0:
        highway_types = original_gdf['highway'].value_counts()
        print(f"  类型分布:")
        for highway, count in highway_types.items():
            print(f"    - {highway}: {count} 条")

    # 水体统计
    print(f"\n[水体]")
    print(f"  总数: {len(water_gdf)} 个")
    if len(water_gdf) > 0:
        print(f"  总面积: {water_gdf.geometry.area.sum():.1f} m²")

    # 过滤后的桥梁统计
    print(f"\n[过滤后桥梁]")
    print(f"  总数: {len(filtered_gdf)} 条")
    if len(filtered_gdf) > 0:
        print(f"  总长度: {filtered_gdf.geometry.length.sum():.1f} m")
        if 'highway' in filtered_gdf.columns:
            highway_types = filtered_gdf['highway'].value_counts()
            print(f"  类型分布:")
            for highway, count in highway_types.items():
                print(f"    - {highway}: {count} 条")

    print("\n" + "="*60)


# 使用示例
if __name__ == "__main__":
    print("桥梁过滤器模块")
    print("\n使用方法:")
    print("  from _TEXTURE_STYLE_OF_DEEPSEEK.bridge_filter import filter_bridges_only")
    print("  bridge_roads = filter_bridges_only(roads_gdf, water_gdf)")
    print("\n策略:")
    print("  1. 筛选 bridge=yes 标签的道路")
    print("  2. 计算道路与水体的交集")
    print("  3. 只提取道路在水体内的片段（纯桥梁段）")