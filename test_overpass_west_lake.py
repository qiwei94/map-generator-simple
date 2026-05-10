"""使用 Overpass API 直接查询西湖数据"""

import sys
import os
import time

sys.path.insert(0, '.')
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

import osmnx as ox
import geopandas as gpd
from shapely.geometry import box

# 西湖附近的小范围（更精确）
WEST_LAKE_LAT1, WEST_LAKE_LON1 = 30.22, 120.12  # 西南角
WEST_LAKE_LAT2, WEST_LAKE_LON2 = 30.26, 120.16  # 东北角

# 杭州大区域
HANGZHOU_LAT1, HANGZHOU_LON1 = 30.13, 120.01
HANGZHOU_LAT2, HANGZHOU_LON2 = 30.36, 120.29

print("=" * 70)
print("使用 Overpass API 查询西湖数据")
print("=" * 70)

# 1. 小范围查询 - 西湖核心区域
print(f"\n[1] 查询西湖核心区域 ({WEST_LAKE_LAT1}, {WEST_LAKE_LON1}) -> ({WEST_LAKE_LAT2}, {WEST_LAKE_LON2})...")
print("   查询 natural=water 的所有水体...")

try:
    bbox_small = [WEST_LAKE_LAT1, WEST_LAKE_LON1, WEST_LAKE_LAT2, WEST_LAKE_LON2]
    water_tags_small = {
        "natural": "water",
        "waterway": True,
        "landuse": "reservoir",
        "water": True,
    }
    
    t0 = time.time()
    water_small = ox.features_from_bbox(bbox=bbox_small, tags=water_tags_small)
    elapsed = time.time() - t0
    
    print(f"   ✅ 获取到 {len(water_small)} 个水体要素 (耗时: {elapsed:.1f}s)")
    
    # 查找西湖
    west_lake_features = water_small[water_small['name'].str.contains('西湖', na=False, case=False)]
    print(f"   名称包含'西湖': {len(west_lake_features)} 个")
    
    if len(west_lake_features) > 0:
        print(f"\n   西湖要素详情:")
        for idx, row in west_lake_features.iterrows():
            name = row.get('name', 'Unnamed')
            geom_type = row.geometry.geom_type
            natural = row.get('natural', '')
            water = row.get('water', '')
            waterway = row.get('waterway', '')
            
            if geom_type in ['Polygon', 'MultiPolygon']:
                area = row.geometry.area
                print(f"     - {name} ({geom_type})")
                print(f"       面积: {area:.0f} m² ({area/1e6:.3f} km²)")
                print(f"       标签: natural={natural}, water={water}")
            else:
                length = row.geometry.length
                print(f"     - {name} ({geom_type})")
                print(f"       长度: {length:.0f} m")
                print(f"       标签: natural={natural}, waterway={waterway}")
    
    # 查找所有多边形水体
    polygons = water_small[water_small.geometry.type.isin(['Polygon', 'MultiPolygon'])].copy()
    if len(polygons) > 0:
        polygons['area'] = polygons.geometry.area
        top10 = polygons.nlargest(10, 'area')
        
        print(f"\n   Top 10 最大水体多边形:")
        for idx, row in top10.iterrows():
            name = row.get('name', 'Unnamed')
            area = row['area']
            natural = row.get('natural', '')
            water = row.get('water', '')
            print(f"     {name:30s} - {area/1e6:.3f} km² (natural={natural}, water={water})")
            
except Exception as e:
    print(f"   ❌ 查询失败: {e}")
    import traceback
    traceback.print_exc()

# 2. 大范围查询 - 杭州区域
print(f"\n[2] 查询杭州大区域 (25km × 25km)...")
print("   这个查询可能需要较长时间...")

try:
    bbox_large = [HANGZHOU_LAT1, HANGZHOU_LON1, HANGZHOU_LAT2, HANGZHOU_LON2]
    
    t0 = time.time()
    water_large = ox.features_from_bbox(bbox=bbox_large, tags=water_tags_small)
    elapsed = time.time() - t0
    
    print(f"   ✅ 获取到 {len(water_large)} 个水体要素 (耗时: {elapsed:.1f}s)")
    
    # 查找西湖
    west_lake_large = water_large[water_large['name'].str.contains('西湖', na=False, case=False)]
    print(f"   名称包含'西湖': {len(west_lake_large)} 个")
    
    if len(west_lake_large) > 0:
        print(f"\n   西湖要素详情:")
        for idx, row in west_lake_large.iterrows():
            name = row.get('name', 'Unnamed')
            geom_type = row.geometry.geom_type
            natural = row.get('natural', '')
            water = row.get('water', '')
            waterway = row.get('waterway', '')
            
            if geom_type in ['Polygon', 'MultiPolygon']:
                area = row.geometry.area
                print(f"     - {name} ({geom_type})")
                print(f"       面积: {area:.0f} m² ({area/1e6:.3f} km²)")
                print(f"       标签: natural={natural}, water={water}")
            else:
                length = row.geometry.length
                print(f"     - {name} ({geom_type})")
                print(f"       长度: {length:.0f} m")
                print(f"       标签: natural={natural}, waterway={waterway}")
    
    # 查找所有名为"西湖"的多边形
    west_lake_polygon = water_large[
        (water_large.geometry.type.isin(['Polygon', 'MultiPolygon'])) &
        (water_large['name'] == '西湖')
    ]
    
    print(f"\n   名称为'西湖'的多边形: {len(west_lake_polygon)} 个")
    if len(west_lake_polygon) > 0:
        for idx, row in west_lake_polygon.iterrows():
            area = row.geometry.area
            print(f"     面积: {area:.0f} m² ({area/1e6:.3f} km²)")
            print(f"     标签: natural={row.get('natural', '')}, water={row.get('water', '')}")
    
    # Top 10 最大水体
    polygons_large = water_large[water_large.geometry.type.isin(['Polygon', 'MultiPolygon'])].copy()
    if len(polygons_large) > 0:
        polygons_large['area'] = polygons_large.geometry.area
        top10_large = polygons_large.nlargest(10, 'area')
        
        print(f"\n   Top 10 最大水体多边形:")
        for idx, row in top10_large.iterrows():
            name = row.get('name', 'Unnamed')
            area = row['area']
            natural = row.get('natural', '')
            water = row.get('water', '')
            print(f"     {name:30s} - {area/1e6:.3f} km² (natural={natural}, water={water})")
            
except Exception as e:
    print(f"   ❌ 查询失败: {e}")
    import traceback
    traceback.print_exc()

# 3. 精确查询 - 使用 Overpass QL 直接查询西湖
print(f"\n[3] 使用自定义 Overpass QL 查询西湖...")

try:
    # 构建自定义查询
    overpass_query = f"""
    [out:json][timeout:60];
    (
      way["name"="西湖"]["natural"="water"]({WEST_LAKE_LAT1},{WEST_LAKE_LON1},{WEST_LAKE_LAT2},{WEST_LAKE_LON2});
      relation["name"="西湖"]["natural"="water"]({WEST_LAKE_LAT1},{WEST_LAKE_LON1},{WEST_LAKE_LAT2},{WEST_LAKE_LON2});
      way["name"="西湖"]({WEST_LAKE_LAT1},{WEST_LAKE_LON1},{WEST_LAKE_LAT2},{WEST_LAKE_LON2});
      relation["name"="西湖"]({WEST_LAKE_LAT1},{WEST_LAKE_LON1},{WEST_LAKE_LAT2},{WEST_LAKE_LON2});
    );
    out geom;
    """
    
    print(f"   查询西湖核心区域的 '西湖' 要素...")
    west_lake_direct = ox.features_from_polygon(
        box(WEST_LAKE_LON1, WEST_LAKE_LAT1, WEST_LAKE_LON2, WEST_LAKE_LAT2),
        custom_filter=overpass_query
    )
    
    print(f"   ✅ 获取到 {len(west_lake_direct)} 个要素")
    
    if len(west_lake_direct) > 0:
        print(f"\n   西湖要素详情:")
        for idx, row in west_lake_direct.iterrows():
            name = row.get('name', 'Unnamed')
            geom_type = row.geometry.geom_type
            
            if geom_type in ['Polygon', 'MultiPolygon']:
                area = row.geometry.area
                print(f"     - {name} ({geom_type})")
                print(f"       面积: {area:.0f} m² ({area/1e6:.3f} km²)")
                for key in ['natural', 'water', 'waterway', 'type']:
                    if key in row and row[key]:
                        print(f"       {key}={row[key]}")
            else:
                length = row.geometry.length
                print(f"     - {name} ({geom_type})")
                print(f"       长度: {length:.0f} m")
    
except Exception as e:
    print(f"   ⚠️  查询失败: {e}")
    print(f"   (可能是自定义查询语法问题，不影响前面的结果)")

print("\n" + "=" * 70)
print("查询完成")
print("=" * 70)
