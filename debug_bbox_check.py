"""验证边界框是否包含西湖，并检查OSM数据质量"""

import sys
import os

sys.path.insert(0, '.')
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm, project_geodataframe
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import set_pbf_file_path, fetch_water

# 西湖的实际坐标（约 30.24°N, 120.14°E）
print("=" * 70)
print("验证边界框和西湖位置")
print("=" * 70)

LAT1, LON1 = 30.13, 120.01  # 西南角
LAT2, LON2 = 30.36, 120.29  # 东北角

print(f"\n当前边界框:")
print(f"  西南角: ({LAT1}, {LON1})")
print(f"  东北角: ({LAT2}, {LON2})")
print(f"  中心点: ({(LAT1+LAT2)/2:.2f}, {(LON1+LON2)/2:.2f})")

print(f"\n西湖实际位置:")
print(f"  约 (30.24°N, 120.14°E)")
print(f"  是否在边界框内: {LAT1 <= 30.24 <= LAT2 and LON1 <= 120.14 <= LON2}")

# 设置 PBF
pbf_file = os.path.join(os.path.dirname(__file__), 'pbf_cache', 'zhejiang-latest.osm.pbf')
if os.path.exists(pbf_file):
    set_pbf_file_path(pbf_file)

bbox = bbox_to_utm(LAT1, LON1, LAT2, LON2)
south, west, north, east = bbox["wgs84_bbox"]
utm_crs = bbox["utm_crs"]
origin = bbox["origin"]
utm_bbox = bbox["utm_bbox"]

# 获取水体
water_gdf = fetch_water(south, west, north, east)
water_gdf = project_geodataframe(water_gdf, utm_crs, origin, clip_bbox=utm_bbox)

print(f"\n获取到 {len(water_gdf)} 个水体要素")

# 查找所有可能的西湖相关名称
print(f"\n查找所有可能的湖泊名称...")
polygons = water_gdf[water_gdf.geometry.type.isin(['Polygon', 'MultiPolygon'])].copy()
polygons['area'] = polygons.geometry.area

# 查找所有有名称的湖泊
named_lakes = polygons[polygons['name'].notna()].nlargest(30, 'area')

print(f"\n有名称的湖泊/水体 Top 30:")
for idx, row in named_lakes.iterrows():
    name = row.get('name', 'Unnamed')
    area = row['area']
    natural = row.get('natural', '')
    water = row.get('water', '')
    waterway = row.get('waterway', '')
    print(f"  {name:30s} - {area/1e6:7.3f} km² (natural={natural}, water={water}, waterway={waterway})")

# 特别检查：搜索包含"湖"字的
print(f"\n名称包含'湖'的水体:")
has_lake = polygons[polygons['name'].str.contains('湖', na=False)]
print(f"  找到 {len(has_lake)} 个")
for idx, row in has_lake.nlargest(20, 'area').iterrows():
    name = row.get('name', 'Unnamed')
    area = row['area']
    print(f"  {name:30s} - {area/1e6:.3f} km²")

print("\n" + "=" * 70)
