"""Check West Lake cache data"""
import sys, os
sys.path.insert(0, '.')
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm, project_geodataframe
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import fetch_water
import math

LAT1, LON1 = 30.1375, 120.020
LAT2, LON2 = 30.3625, 120.280
bbox = bbox_to_utm(LAT1, LON1, LAT2, LON2)
south, west, north, east = bbox['wgs84_bbox']
utm_crs = bbox['utm_crs']
origin = bbox['origin']
utm_bbox = bbox['utm_bbox']

print('=== West Lake (西湖) Cache Data Analysis ===')
print('Query bbox: (%.4f, %.4f) to (%.4f, %.4f)' % (south, west, north, east))
print('West Lake approximate location: ~30.25N, 120.15E')

water_gdf = fetch_water(south, west, north, east)

# Filter West Lake features
def safe_str(val):
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return ''
    return str(val)

west_lake_raw = water_gdf[water_gdf.get('name', '').str.contains('西湖', na=False)]
print('\nRaw features with name="西湖": %d' % len(west_lake_raw))

# Separate by geometry type
wl_polys = west_lake_raw[west_lake_raw.geometry.type.isin(['Polygon', 'MultiPolygon'])]
wl_lines = west_lake_raw[west_lake_raw.geometry.type.isin(['LineString', 'MultiLineString'])]
print('  Polygon: %d' % len(wl_polys))
print('  LineString: %d' % len(wl_lines))

# Project and check areas
water_proj = project_geodataframe(water_gdf, utm_crs, origin, clip_bbox=utm_bbox)
west_lake_proj = water_proj[water_proj.get('name', '').str.contains('西湖', na=False)].copy()

print('\n=== After projection ===')
print('Projected features: %d' % len(west_lake_proj))

west_lake_proj['area_m2'] = west_lake_proj.geometry.area
wl_polys_proj = west_lake_proj[west_lake_proj.geometry.type.isin(['Polygon', 'MultiPolygon'])]

total_area = wl_polys_proj['area_m2'].sum()
print('Total West Lake polygon area: %.0f m2 (%.2f km2)' % (total_area, total_area/1e6))
print('Expected West Lake area: ~6.5 km2')
print('Coverage: %.1f%% of expected' % (total_area/6.5e6 * 100))

print('\n=== Individual West Lake Polygon Features ===')
for i, (_, row) in enumerate(wl_polys_proj.iterrows()):
    geom = row.geometry
    area = row['area_m2']
    bounds = geom.bounds
    center_x = (bounds[0] + bounds[2]) / 2
    center_y = (bounds[1] + bounds[3]) / 2
    
    # Other tags
    water = safe_str(row.get('water', ''))
    natural = safe_str(row.get('natural', ''))
    name = safe_str(row.get('name', ''))
    
    print('  %d. area=%.0f m2 (%.4f km2), type=%s' % (i+1, area, area/1e6, geom.geom_type))
    print('      center: (%.0f, %.0f) m local')
    print('      tags: water=%s, natural=%s, name=%s' % (water, natural, name))

# Check if there are larger water=lake features nearby
print('\n=== Other lake features (water=lake) ===')
lakes = water_proj[water_proj.get('water', '') == 'lake'].copy()
lakes['area_m2'] = lakes.geometry.area
large_lakes = lakes[lakes['area_m2'] > 100000].nlargest(10, 'area_m2')
print('Large lake features (>0.1 km2): %d' % len(large_lakes))
for i, (_, row) in enumerate(large_lakes.iterrows()):
    name = safe_str(row.get('name', ''))
    area = row['area_m2']
    print('  %d. area=%.0f m2 (%.2f km2), name=%s' % (i+1, area, area/1e6, name))

# Check cache source
print('\n=== Cache Source Info ===')
print('Data loaded from city cache via city_cache_loader.py')
print('Check F:/map_gen_cache/city/cache/osm/ for Hangzhou files')