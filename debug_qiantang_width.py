import sys, os
sys.path.insert(0, '.')
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm, project_geodataframe
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import fetch_water
from _TEXTURE_STYLE_OF_DEEPSEEK.config import WATERWAY_WIDTHS
from shapely.geometry import Polygon
import geopandas as gpd

LAT1, LON1 = 30.1375, 120.020
LAT2, LON2 = 30.3625, 120.280
bbox = bbox_to_utm(LAT1, LON1, LAT2, LON2)
south, west, north, east = bbox['wgs84_bbox']
utm_crs = bbox['utm_crs']
origin = bbox['origin']
utm_bbox = bbox['utm_bbox']

print('=== Qiantang River Analysis ===')
water_gdf = fetch_water(south, west, north, east)
water_proj = project_geodataframe(water_gdf, utm_crs, origin, clip_bbox=utm_bbox)

# Filter Qiantang River
qt_proj = water_proj[water_proj.get('name','').str.contains('钱塘|Qiantang', na=False)].copy()
print('Qiantang River features:', len(qt_proj))

# Check lengths and buffered areas
total_length = 0
total_buffered_area = 0
for _, row in qt_proj.iterrows():
    geom = row.geometry
    waterway = row.get('waterway', 'river')
    width = WATERWAY_WIDTHS.get(waterway, 30)
    
    length = geom.length
    buffered = geom.buffer(width / 2)
    area = buffered.area
    
    total_length += length
    total_buffered_area += area
    
    # Check geom bounds
    bounds = geom.bounds
    print('  length=%.0f m, width_used=%d m, buffered_area=%.0f m2' % (length, width, area))
    print('    bounds: (%.0f, %.0f) to (%.0f, %.0f) m' % bounds)

print('\nTotal:')
print('  Length: %.0f m' % total_length)
print('  Buffered area (60m width): %.0f m2 (%.2f km2)' % (total_buffered_area, total_buffered_area/1e6))

# Expected Qiantang River width in this area
print('\n=== Expected width ===')
print('Qiantang River actual width in Hangzhou: ~500-1000m')
print('Current buffer width setting: 60m (for waterway=river)')
print('This is too narrow!')

# Check if there are polygon features for Qiantang
qt_poly = water_proj[
    (water_proj.get('name','').str.contains('钱塘|Qiantang', na=False)) &
    (water_proj.geometry.type.isin(['Polygon', 'MultiPolygon']))
]
print('\nQiantang River polygon features:', len(qt_poly))

# Check water=river polygons (might include Qiantang sections)
river_polys = water_proj[
    (water_proj.get('water','') == 'river') &
    (water_proj.geometry.type.isin(['Polygon', 'MultiPolygon']))
].copy()
print('River (water=river) polygon features:', len(river_polys))

if len(river_polys) > 0:
    river_polys['area'] = river_polys.geometry.area
    total_river_poly_area = river_polys['area'].sum()
    print('Total river polygon area: %.0f m2 (%.2f km2)' % (total_river_poly_area, total_river_poly_area/1e6))
    
    # Top 10 by area
    print('\nTop 10 river polygons:')
    top10 = river_polys.nlargest(10, 'area')
    for i, (_, row) in enumerate(top10.iterrows()):
        name = row.get('name', '')
        print('  %d. area=%.0f m2 (%.2f km2), name=%s' % (i+1, row['area'], row['area']/1e6, name))

# Check all large water features in bbox
print('\n=== All large water polygons > 1km2 ===')
large_polys = water_proj[
    water_proj.geometry.type.isin(['Polygon', 'MultiPolygon'])
].copy()
large_polys['area'] = large_polys.geometry.area
large_features = large_polys[large_polys['area'] > 1000000]
print('Features > 1km2:', len(large_features))

for _, row in large_features.iterrows():
    area = row['area']
    name = row.get('name', '')
    water = row.get('water', '')
    print('  area=%.0f m2 (%.2f km2), name=%s, water=%s' % (area, area/1e6, name, water))