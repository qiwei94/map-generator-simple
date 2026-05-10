"""对比5月5日完美版本和当前版本的处理差异"""

import sys
import os

sys.path.insert(0, '.')
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm, project_geodataframe
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import set_pbf_file_path, fetch_water
from _TEXTURE_STYLE_OF_DEEPSEEK.config import WATERWAY_WIDTHS, WATER_MIN_AREA_M2

print("=" * 70)
print("对比两个版本的处理差异")
print("=" * 70)

# 设置 PBF
pbf_file = os.path.join(os.path.dirname(__file__), 'pbf_cache', 'zhejiang-latest.osm.pbf')
if os.path.exists(pbf_file):
    set_pbf_file_path(pbf_file)

# 5月5日版本的边界框
LAT1, LON1 = 30.1375, 120.020
LAT2, LON2 = 30.3625, 120.280

bbox = bbox_to_utm(LAT1, LON1, LAT2, LON2)
south, west, north, east = bbox["wgs84_bbox"]
utm_crs = bbox["utm_crs"]
origin = bbox["origin"]
utm_bbox = bbox["utm_bbox"]

# 获取水体
print(f'\n[Step 1] 获取PBF水体数据...')
water_gdf = fetch_water(south, west, north, east)
print(f'  原始数据: {len(water_gdf)} 个要素')

# 投影
water_gdf = project_geodataframe(water_gdf, utm_crs, origin, clip_bbox=utm_bbox)
print(f'  投影后: {len(water_gdf)} 个要素')

# 5月5日版本：无过滤
print(f'\n{"="*70}')
print(f'[5月5日完美版本] 处理方式：无过滤，直接使用所有数据')
print(f'{"="*70}')
print(f'  传递给 build_deepseek_water 的要素数: {len(water_gdf)}')
print(f'  build_deepseek_water 内部会过滤掉 < {WATER_MIN_AREA_M2/1000:.0f} 平方公里的要素')

# 当前版本：Top 500 过滤
print(f'\n{"="*70}')
print(f'[当前PBF版本] 处理方式：Top 500 过滤')
print(f'{"="*70}')

# 估算面积
def estimate_water_area(geom, row):
    if geom.geom_type in ['Polygon', 'MultiPolygon']:
        return geom.area
    elif geom.geom_type in ['LineString', 'MultiLineString']:
        waterway_type = row.get('waterway', 'river')
        width = WATERWAY_WIDTHS.get(waterway_type, 60)
        return geom.length * width
    return 0

water_gdf['est_area'] = water_gdf.apply(lambda r: estimate_water_area(r.geometry, r), axis=1)

print(f'  估算面积后排序...')
print(f'  Top 1 面积: {water_gdf["est_area"].max():,.0f} m²')
print(f'  Top 100 最小面积: {water_gdf.nlargest(100, "est_area")["est_area"].min():,.0f} m²')
print(f'  Top 500 最小面积: {water_gdf.nlargest(500, "est_area")["est_area"].min():,.0f} m²')
print(f'  Top 1000 最小面积: {water_gdf.nlargest(1000, "est_area")["est_area"].min():,.0f} m²')

water_gdf_top500 = water_gdf.nlargest(500, 'est_area')
print(f'\n  过滤后传递给 build_deepseek_water 的要素数: {len(water_gdf_top500)}')

# 关键问题：西湖是否在Top 500中？
print(f'\n{"="*70}')
print(f'关键问题：西湖是否在数据中？')
print(f'{"="*70}')

# 查找西湖
west_lake = water_gdf[water_gdf['name'].str.contains('西湖', na=False, case=False)]
print(f'  名称包含"西湖"的要素: {len(west_lake)} 个')

if len(west_lake) > 0:
    for idx, row in west_lake.iterrows():
        name = row.get('name', 'Unknown')
        geom_type = row.geometry.geom_type
        est_area = row['est_area']
        rank = (water_gdf['est_area'] >= est_area).sum()
        
        print(f'    - {name} ({geom_type})')
        print(f'      估算面积: {est_area:,.0f} m²')
        print(f'      排名: Top {rank}')
        print(f'      在Top 500中: {"✅ 是" if rank <= 500 else "❌ 否"}')

# 查找最大的湖泊多边形
print(f'\n{"="*70}')
print(f'PBF中最大的湖泊多边形（natural=water）:')
print(f'{"="*70}')

polygons = water_gdf[water_gdf.geometry.type.isin(['Polygon', 'MultiPolygon'])].copy()
if 'natural' in polygons.columns:
    natural_water = polygons[polygons['natural'] == 'water'].nlargest(10, 'est_area')
    for idx, row in natural_water.iterrows():
        name = row.get('name', 'Unnamed')
        area = row['est_area']
        rank = (water_gdf['est_area'] >= area).sum()
        water_type = row.get('water', '')
        print(f'  {name:30s} - {area/1e6:.3f} km² (water={water_type}), 排名: Top {rank}')

print(f'\n{"="*70}')
print(f'结论')
print(f'{"="*70}')
print(f'  1. 两个版本的处理逻辑差异: Top 500 过滤')
print(f'  2. 5月5日版本使用全部 {len(water_gdf)} 个要素')
print(f'  3. 当前版本只使用前 500 个要素')
print(f'  4. 但核心问题: PBF数据中{"有" if len(west_lake) > 0 else "没有"}西湖湖泊多边形')
print(f'{"="*70}')
