import sys, os
sys.path.insert(0, '.')
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm, project_geodataframe
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import fetch_water
from _TEXTURE_STYLE_OF_DEEPSEEK.config import WATERWAY_WIDTHS

LAT1, LON1 = 30.1375, 120.020
LAT2, LON2 = 30.3625, 120.280
bbox = bbox_to_utm(LAT1, LON1, LAT2, LON2)
south, west, north, east = bbox['wgs84_bbox']
utm_crs = bbox['utm_crs']
origin = bbox['origin']
utm_bbox = bbox['utm_bbox']

print('=== Qiantang River Buffer Check ===')
print('River buffer width: %d m (was 60m)' % WATERWAY_WIDTHS.get('river', 60))

water_gdf = fetch_water(south, west, north, east)
water_proj = project_geodataframe(water_gdf, utm_crs, origin, clip_bbox=utm_bbox)

qt_proj = water_proj[water_proj.get('name','').str.contains('钱塘|Qiantang', na=False)].copy()
print('Qiantang LineString features:', len(qt_proj))

# Calculate buffered area with new width
total_length = 0
total_buffered_area = 0
width = WATERWAY_WIDTHS.get('river', 500)

for _, row in qt_proj.iterrows():
    geom = row.geometry
    length = geom.length
    buffered = geom.buffer(width / 2)
    area = buffered.area
    total_length += length
    total_buffered_area += area

print('Total LineString length: %d m' % total_length)
print('Buffered area (500m width): %d m2 (%.2f km2)' % (total_buffered_area, total_buffered_area/1e6))
print('Expected Qiantang area: ~30-40 km2 (for full river)')
print('Coverage in 25km bbox: ~6-10 km2 expected')

# Also check polygon coverage
river_polys = water_proj[
    (water_proj.get('water','') == 'river') &
    (water_proj.geometry.type.isin(['Polygon', 'MultiPolygon']))
].copy()
river_polys['area'] = river_polys.geometry.area
poly_total = river_polys['area'].sum()
print('\nRiver polygon coverage: %d m2 (%.2f km2)' % (poly_total, poly_total/1e6))

print('\nTotal water coverage:')
print('  LineString buffer: %.2f km2' % (total_buffered_area/1e6))
print('  River polygons: %.2f km2' % (poly_total/1e6))
print('  Combined: %.2f km2' % ((total_buffered_area + poly_total)/1e6))