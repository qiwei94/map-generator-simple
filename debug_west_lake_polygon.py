"""查找西湖主体湖泊"""

import sys
import os

sys.path.insert(0, '.')
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm, project_geodataframe
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.pbf_reader import fetch_from_pbf

LAT1, LON1 = 30.13, 120.01
LAT2, LON2 = 30.36, 120.29

# PBF 文件路径
pbf_file = os.path.join(os.path.dirname(__file__), 'pbf_cache', 'zhejiang-latest.osm.pbf')
print(f"使用 PBF 文件: {pbf_file}")
print(f"文件存在: {os.path.exists(pbf_file)}")

bbox = bbox_to_utm(LAT1, LON1, LAT2, LON2)
south, west, north, east = bbox["wgs84_bbox"]
utm_crs = bbox["utm_crs"]
origin = bbox["origin"]
utm_bbox = bbox["utm_bbox"]

# 使用 fetch_from_pbf 从本地 PBF 读取
water_gdf = fetch_from_pbf(pbf_file, "water", south, west, north, east)
print(f"\n从 PBF 读取到 {len(water_gdf)} 个水体要素")

water_gdf = project_geodataframe(water_gdf, utm_crs, origin, clip_bbox=utm_bbox)

print("=" * 70)
print("查找西湖主体湖泊")
print("=" * 70)

# 1. 查找所有 natural=water 且 water=lake 的多边形
print("\n[1] natural=water AND water=lake 的多边形:")
lake_polygons = water_gdf[
    (water_gdf.geometry.type.isin(['Polygon', 'MultiPolygon'])) &
    (water_gdf.get('natural', '') == 'water') &
    (water_gdf.get('water', '') == 'lake')
]
print(f"  找到 {len(lake_polygons)} 个")

lake_polygons = lake_polygons.copy()
lake_polygons['area'] = lake_polygons.geometry.area

for idx, row in lake_polygons.nlargest(10, 'area').iterrows():
    name = row.get('name', 'Unnamed')
    area = row['area']
    print(f"  - {name}: {area:,.0f} m² ({area/1e6:.3f} km²)")

# 2. 查找所有名称包含"西湖"的多边形
print("\n[2] 名称包含'西湖'的多边形:")
west_lake_poly = water_gdf[
    (water_gdf.geometry.type.isin(['Polygon', 'MultiPolygon'])) &
    (water_gdf['name'].str.contains('西湖', na=False))
]
print(f"  找到 {len(west_lake_poly)} 个")

for idx, row in west_lake_poly.iterrows():
    name = row.get('name', 'Unnamed')
    area = row.geometry.area if row.geometry.geom_type in ['Polygon', 'MultiPolygon'] else 0
    print(f"  - {name}: {area:,.0f} m²")
    print(f"    natural={row.get('natural', '')}, water={row.get('water', '')}, waterway={row.get('waterway', '')}")

# 3. 查找面积最大的前20个湖泊多边形
print("\n[3] 面积最大的前20个湖泊多边形:")
all_lakes = water_gdf[
    (water_gdf.geometry.type.isin(['Polygon', 'MultiPolygon'])) &
    (water_gdf.get('natural', '') == 'water')
].copy()
all_lakes['area'] = all_lakes.geometry.area
all_lakes = all_lakes.nlargest(20, 'area')

for idx, row in all_lakes.iterrows():
    name = row.get('name', 'Unnamed')
    area = row.geometry.area
    water_type = row.get('water', '')
    print(f"  - {name} (water={water_type}): {area:,.0f} m² ({area/1e6:.3f} km²)")

print("\n" + "=" * 70)
