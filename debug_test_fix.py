import sys, os
sys.path.insert(0, '.')
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm, project_geodataframe
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import fetch_water
from _TEXTURE_STYLE_OF_DEEPSEEK.water_column import create_water_columns_union_manifold

LAT1, LON1 = 30.1375, 120.020
LAT2, LON2 = 30.3625, 120.280
bbox = bbox_to_utm(LAT1, LON1, LAT2, LON2)
south, west, north, east = bbox['wgs84_bbox']
utm_crs = bbox['utm_crs']
origin = bbox['origin']
utm_bbox = bbox['utm_bbox']
scale = 196.0 / max(bbox['width_m'], bbox['height_m'])

water_gdf = fetch_water(south, west, north, east)
print('Raw water features:', len(water_gdf))

water_proj = project_geodataframe(water_gdf, utm_crs, origin, clip_bbox=utm_bbox)
print('Projected water features:', len(water_proj))

# Top 10 by area
water_proj = water_proj.copy()
water_proj['area_m2'] = water_proj.geometry.area
top10 = water_proj.nlargest(10, 'area_m2')
print('\nTop 10 water features:')
for i, (_, row) in enumerate(top10.iterrows()):
    gt = row.geometry.geom_type
    w = row.get('water', '')
    wa = row.get('waterway', '')
    n = row.get('name', '')
    print('  %d. area=%d m2, type=%s, water=%s, waterway=%s, name=%s' % (i+1, row['area_m2'], gt, w, wa, n))

# Now test the buffered water column creation
print('\n=== Testing buffered water column union ===')
result = create_water_columns_union_manifold(water_proj, -1.17, 4.83, scale)
print('Result empty:', result.is_empty())
if not result.is_empty():
    print('Result volume: %.2f mm3' % result.volume())
    print('Result edges: %d' % result.num_edge())
else:
    print('WARNING: Result is empty!')
