"""检查为什么西湖不在Top 500内"""

import sys
import os

sys.path.insert(0, '.')
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm, project_geodataframe
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import set_pbf_file_path, fetch_water
from _TEXTURE_STYLE_OF_DEEPSEEK.config import WATERWAY_WIDTHS

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
print("检查西湖数据的真实情况")
print("=" * 70)

# 查找所有名称包含"湖"的多边形
print(f"\n[1] 查找所有名称包含'湖'的 Polygon/MultiPolygon:")
polygons_with_lake_name = water_gdf[
    (water_gdf.geometry.type.isin(['Polygon', 'MultiPolygon'])) &
    (water_gdf['name'].str.contains('湖', na=False))
]

print(f"  找到 {len(polygons_with_lake_name)} 个")

for idx, row in polygons_with_lake_name.iterrows():
    name = row.get('name', 'Unnamed')
    area = row.geometry.area
    print(f"  - {name}")
    print(f"    实际面积: {area:,.0f} m² ({area/1e6:.3f} km²)")
    print(f"    natural={row.get('natural', '')}, water={row.get('water', '')}")

# 查找最大的多边形水体
print(f"\n[2] PBF中面积最大的前20个水体多边形:")
polygons = water_gdf[water_gdf.geometry.type.isin(['Polygon', 'MultiPolygon'])].copy()
polygons['area'] = polygons.geometry.area
top20_polygons = polygons.nlargest(20, 'area')

for rank, (idx, row) in enumerate(top20_polygons.iterrows(), 1):
    name = row.get('name', 'Unnamed')
    area = row['area']
    natural = row.get('natural', '')
    water = row.get('water', '')
    print(f"  {rank:2d}. {name:30s} - {area/1e6:.3f} km² (natural={natural}, water={water})")

# 查找面积超过 1 km² 的湖泊
print(f"\n[3] 面积超过 1 km² 的湖泊:")
large_lakes = polygons[polygons['area'] > 1e6].nlargest(50, 'area')
print(f"  找到 {len(large_lakes)} 个")

for rank, (idx, row) in enumerate(large_lakes.iterrows(), 1):
    name = row.get('name', 'Unnamed')
    area = row['area']
    natural = row.get('natural', '')
    water = row.get('water', '')
    print(f"  {rank:2d}. {name:30s} - {area/1e6:.3f} km² (natural={natural}, water={water})")

# 关键问题：西湖在哪？
print(f"\n[4] 直接搜索 name='西湖' 的所有要素:")
west_lake_exact = water_gdf[water_gdf['name'] == '西湖']
print(f"  找到 {len(west_lake_exact)} 个")

if len(west_lake_exact) > 0:
    for idx, row in west_lake_exact.iterrows():
        geom_type = row.geometry.geom_type
        if geom_type in ['Polygon', 'MultiPolygon']:
            area = row.geometry.area
            print(f"  - {row['name']} ({geom_type})")
            print(f"    面积: {area:,.0f} m² ({area/1e6:.3f} km²)")
            print(f"    natural={row.get('natural', '')}, water={row.get('water', '')}")
        else:
            length = row.geometry.length
            print(f"  - {row['name']} ({geom_type})")
            print(f"    长度: {length:,.0f} m")
            print(f"    waterway={row.get('waterway', '')}")
else:
    print(f"  ❌ PBF数据中没有 name='西湖' 的要素！")

# 检查是不是用了其他名称
print(f"\n[5] 检查所有可能的西湖别名:")
possible_names = ['West Lake', '西子湖', '钱塘湖', '武林水']
for name in possible_names:
    found = water_gdf[water_gdf['name'].str.contains(name, na=False, case=False)]
    if len(found) > 0:
        print(f"  ✅ 找到 '{name}': {len(found)} 个")
        for idx, row in found.iterrows():
            print(f"    - {row['name']} ({row.geometry.geom_type}), 面积: {row.geometry.area/1e6:.3f} km²")

print(f"\n[6] 面积最大的水体（包括LineString估算）:")
def estimate_area(geom, row):
    if geom.geom_type in ['Polygon', 'MultiPolygon']:
        return geom.area
    elif geom.geom_type in ['LineString', 'MultiLineString']:
        waterway_type = row.get('waterway', 'river')
        width = WATERWAY_WIDTHS.get(waterway_type, 60)
        return geom.length * width
    return 0

water_gdf['est_area'] = water_gdf.apply(lambda r: estimate_area(r.geometry, r), axis=1)
top20_all = water_gdf.nlargest(20, 'est_area')

for rank, (idx, row) in enumerate(top20_all.iterrows(), 1):
    name = row.get('name', 'Unnamed')
    geom_type = row.geometry.geom_type
    est_area = row['est_area']
    
    if geom_type in ['Polygon', 'MultiPolygon']:
        print(f"  {rank:2d}. {name:30s} ({geom_type:15s}) - {est_area/1e6:.3f} km²")
    else:
        length = row.geometry.length
        waterway = row.get('waterway', '')
        print(f"  {rank:2d}. {name:30s} ({geom_type:15s}, {waterway:10s}) - 长度{length/1000:.1f}km, 估算{est_area/1e6:.3f} km²")

print("\n" + "=" * 70)
print("结论：")
print("=" * 70)
if len(west_lake_exact) == 0:
    print("  ❌ PBF数据中完全不存在 name='西湖' 的要素")
    print("  ❌ 这不是过滤问题，是数据源本身缺失西湖")
    print("  ❌ 浙江省的 PBF 文件中西湖湖泊多边形数据不存在")
else:
    print(f"  ✅ 找到西湖，面积: {west_lake_exact.geometry.area.max()/1e6:.3f} km²")
