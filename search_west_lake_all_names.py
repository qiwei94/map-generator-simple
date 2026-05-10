"""搜索西湖的所有可能别名"""

import sys
import os

sys.path.insert(0, '.')
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm, project_geodataframe
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import set_pbf_file_path, fetch_water

# 设置 PBF
pbf_file = os.path.join(os.path.dirname(__file__), 'pbf_cache', 'zhejiang-latest.osm.pbf')
if os.path.exists(pbf_file):
    set_pbf_file_path(pbf_file)

# 杭州区域
LAT1, LON1 = 30.13, 120.01
LAT2, LON2 = 30.36, 120.29

bbox = bbox_to_utm(LAT1, LON1, LAT2, LON2)
south, west, north, east = bbox["wgs84_bbox"]
utm_crs = bbox["utm_crs"]
origin = bbox["origin"]
utm_bbox = bbox["utm_bbox"]

# 获取水体
water_gdf = fetch_water(south, west, north, east)
water_gdf = project_geodataframe(water_gdf, utm_crs, origin, clip_bbox=utm_bbox)

print("=" * 70)
print("搜索西湖的所有可能别名")
print("=" * 70)

# 西湖的所有可能名称
search_terms = {
    '中文': ['西湖', '西子湖', '钱塘湖', '武林水', '明圣湖', '金牛湖'],
    '英文': ['West Lake', 'westlake'],
    '拼音': ['Xihu', 'xihu', 'Xi Hu'],
    '其他': ['三潭印月', '苏堤', '白堤', '断桥', '雷峰塔', '湖心亭'],
}

for category, terms in search_terms.items():
    print(f"\n[{category}] 搜索:")
    
    for term in terms:
        if 'name' in water_gdf.columns:
            matches = water_gdf[water_gdf['name'].str.contains(term, na=False, case=False)]
            
            if len(matches) > 0:
                print(f"  ✅ '{term}': 找到 {len(matches)} 个")
                
                # 显示详情
                for idx, row in matches.iterrows():
                    name = row.get('name', 'Unnamed')
                    geom_type = row.geometry.geom_type
                    
                    if geom_type in ['Polygon', 'MultiPolygon']:
                        area = row.geometry.area
                        print(f"     - {name} ({geom_type})")
                        print(f"       面积: {area:,.0f} m² ({area/1e6:.3f} km²)")
                    else:
                        length = row.geometry.length
                        print(f"     - {name} ({geom_type})")
                        print(f"       长度: {length:,.0f} m")
            else:
                print(f"  ❌ '{term}': 未找到")

# 另外检查西湖中心坐标附近的所有水体
print(f"\n{'='*70}")
print(f"西湖中心 (30.24, 120.14) 附近的大型水体:")
print(f"{'='*70}")

from shapely.geometry import Point

# 创建西湖中心点
west_lake_center = Point(120.14, 30.24)

# 查找中心点附近的水体多边形
polygons = water_gdf[water_gdf.geometry.type.isin(['Polygon', 'MultiPolygon'])].copy()

if len(polygons) > 0:
    # 计算每个多边形到西湖中心的距离
    polygons['distance_to_center'] = polygons.geometry.apply(
        lambda g: g.centroid.distance(west_lake_center)
    )
    
    # 找出距离中心最近的大型水体（> 0.1 km²）
    large_nearby = polygons[
        (polygons.geometry.area > 100000) &  # > 0.1 km²
        (polygons['distance_to_center'] < 0.05)  # 中心点0.05度范围内
    ].nsmallest(10, 'distance_to_center')
    
    print(f"\n找到 {len(large_nearby)} 个大型水体在西湖中心附近:")
    
    for idx, row in large_nearby.iterrows():
        name = row.get('name', 'Unnamed') or 'Unnamed'
        area = row.geometry.area
        distance = row['distance_to_center']
        natural = row.get('natural', '') or ''
        water = row.get('water', '') or ''
        
        print(f"  - {name}")
        print(f"    面积: {area/1e6:.3f} km²")
        print(f"    距离西湖中心: {distance:.4f} 度")
        print(f"    natural={natural}, water={water}")
        
        # 显示部分坐标
        centroid = row.geometry.centroid
        print(f"    中心坐标: ({centroid.y:.4f}, {centroid.x:.4f})")

# 查找所有面积 > 1 km² 的未命名水体
print(f"\n{'='*70}")
print(f"面积 > 1 km² 的未命名水体:")
print(f"{'='*70}")

unnamed_large = polygons[
    (polygons['name'].isna()) & 
    (polygons.geometry.area > 1e6)
].nlargest(20, 'geometry.area')

print(f"找到 {len(unnamed_large)} 个:")

for idx, row in unnamed_large.iterrows():
    area = row.geometry.area
    centroid = row.geometry.centroid
    natural = row.get('natural', '') or ''
    water = row.get('water', '') or ''
    
    # 计算到西湖中心的距离
    distance = row.geometry.centroid.distance(west_lake_center)
    
    print(f"  - 未命名")
    print(f"    面积: {area/1e6:.3f} km²")
    print(f"    中心坐标: ({centroid.y:.4f}, {centroid.x:.4f})")
    print(f"    距离西湖中心: {distance:.4f} 度")
    print(f"    natural={natural}, water={water}")

print("\n" + "=" * 70)
print("搜索完成")
print("=" * 70)
