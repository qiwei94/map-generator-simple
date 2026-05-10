"""搜索浙江省PBF数据中的所有大型湖泊"""

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

# 浙江省大致的边界框（覆盖主要湖泊区域）
# 包括杭州、宁波、绍兴、嘉兴、湖州等地
ZHEJIANG_LAT1, ZHEJIANG_LON1 = 29.5, 119.5  # 西南角
ZHEJIANG_LAT2, ZHEJIANG_LON2 = 30.8, 122.0  # 东北角

print("=" * 70)
print("搜索浙江省PBF数据中的所有大型湖泊")
print("=" * 70)

print(f"\n搜索范围: ({ZHEJIANG_LAT1}, {ZHEJIANG_LON1}) -> ({ZHEJIANG_LAT2}, {ZHEJIANG_LON2})")
print("覆盖: 杭州、宁波、绍兴、嘉兴、湖州等地区")

bbox = bbox_to_utm(ZHEJIANG_LAT1, ZHEJIANG_LON1, ZHEJIANG_LAT2, ZHEJIANG_LON2)
south, west, north, east = bbox["wgs84_bbox"]
utm_crs = bbox["utm_crs"]
origin = bbox["origin"]
utm_bbox = bbox["utm_bbox"]

# 获取水体
print(f"\n[1] 获取水体数据...")
water_gdf = fetch_water(south, west, north, east)
print(f"  获取到 {len(water_gdf)} 个水体要素")

water_gdf = project_geodataframe(water_gdf, utm_crs, origin, clip_bbox=utm_bbox)
print(f"  投影后: {len(water_gdf)} 个要素")

# 筛选湖泊多边形
print(f"\n[2] 筛选湖泊多边形 (natural=water)...")
polygons = water_gdf[water_gdf.geometry.type.isin(['Polygon', 'MultiPolygon'])].copy()
polygons['area'] = polygons.geometry.area

natural_water = polygons[polygons.get('natural', '') == 'water']
print(f"  natural=water 的多边形: {len(natural_water)} 个")

# 按面积排序，找出最大的湖泊
print(f"\n[3] 浙江省最大的50个湖泊/水库:")
print(f"{'排名':<5} {'名称':<30} {'面积':<15} {'类型':<15} {'坐标'}")
print("-" * 100)

top50_lakes = natural_water.nlargest(50, 'area')

for rank, (idx, row) in enumerate(top50_lakes.iterrows(), 1):
    name = row.get('name', '未命名') or '未命名'
    area_km2 = row['area'] / 1e6
    water_type = row.get('water', '') or 'unknown'
    
    centroid = row.geometry.centroid
    lat = centroid.y
    lon = centroid.x
    
    # 判断是否是湖泊还是水库
    if water_type == 'reservoir':
        type_str = '水库'
    elif water_type == 'lake':
        type_str = '湖泊'
    elif water_type == 'pond':
        type_str = '池塘'
    elif water_type == 'river':
        type_str = '河道'
    else:
        type_str = water_type
    
    # 面积格式化
    if area_km2 >= 1:
        area_str = f"{area_km2:.2f} km²"
    else:
        area_str = f"{area_km2*1000:.1f} 千m²"
    
    print(f"{rank:<5} {name:<30} {area_str:<15} {type_str:<15} ({lat:.3f}, {lon:.3f})")

# 特别关注：面积 > 1 km² 的湖泊
print(f"\n[4] 面积超过 1 km² 的湖泊/水库:")
large_lakes = natural_water[natural_water['area'] > 1e6].nlargest(30, 'area')
print(f"  找到 {len(large_lakes)} 个")

for rank, (idx, row) in enumerate(large_lakes.iterrows(), 1):
    name = row.get('name', '未命名') or '未命名'
    area_km2 = row['area'] / 1e6
    water_type = row.get('water', '') or 'unknown'
    centroid = row.geometry.centroid
    
    print(f"  {rank:2d}. {name:30s} - {area_km2:.2f} km² (water={water_type}) @ ({centroid.y:.3f}, {centroid.x:.3f})")

# 查找特定名称的湖泊
print(f"\n[5] 搜索浙江省知名湖泊:")
famous_lakes = [
    '西湖', '千岛湖', '南湖', '东钱湖', '太湖', 
    '南太湖', '青山湖', '湘湖', '鉴湖', '南明湖',
    '月湖', '日湖', '慈湖', '白马湖'
]

for lake_name in famous_lakes:
    matches = polygons[polygons['name'].str.contains(lake_name, na=False, case=False)]
    
    if len(matches) > 0:
        print(f"\n  ✅ {lake_name}: 找到 {len(matches)} 个")
        for idx, row in matches.iterrows():
            name = row.get('name', '未命名') or '未命名'
            area = row['area'] / 1e6
            centroid = row.geometry.centroid
            print(f"     - {name}: {area:.3f} km² @ ({centroid.y:.3f}, {centroid.x:.3f})")
    else:
        print(f"  ❌ {lake_name}: 未找到")

print("\n" + "=" * 70)
print("搜索完成")
print("=" * 70)
