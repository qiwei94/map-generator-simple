import sys, os
sys.path.insert(0, '.')
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm, project_geodataframe
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import fetch_water
from shapely.geometry import LineString, MultiLineString, Polygon, MultiPolygon

LAT1, LON1 = 30.1375, 120.020
LAT2, LON2 = 30.3625, 120.280
bbox = bbox_to_utm(LAT1, LON1, LAT2, LON2)
south, west, north, east = bbox['wgs84_bbox']
utm_crs = bbox['utm_crs']
origin = bbox['origin']
utm_bbox = bbox['utm_bbox']
scale = 196.0 / max(bbox['width_m'], bbox['height_m'])

from _TEXTURE_STYLE_OF_DEEPSEEK.config import WATERWAY_WIDTHS

water_gdf = fetch_water(south, west, north, east)
water_proj = project_geodataframe(water_gdf, utm_crs, origin, clip_bbox=utm_bbox)

# Check Qiantang River specifically after buffering
qt_raw = water_gdf[water_gdf['name'] == '钱塘江'].copy()
print('=== Qiantang River raw features ===')
print('Total:', len(qt_raw))
print('Types:', qt_raw.geometry.type.value_counts().to_dict())

# Project and buffer
qt_proj = project_geodataframe(qt_raw, utm_crs, origin, clip_bbox=utm_bbox)
print('\nAfter projection:')
print('Total:', len(qt_proj))

# Calculate buffered area
total_buffered_area = 0
for _, row in qt_proj.iterrows():
    geom = row.geometry
    waterway_type = row.get('waterway', 'river')
    buffer_width = WATERWAY_WIDTHS.get(waterway_type, 60)  # 60m for river
    half_width = buffer_width / 2.0
    
    if isinstance(geom, (LineString, MultiLineString)):
        buffered = geom.buffer(half_width)
        if isinstance(buffered, MultiPolygon):
            for p in buffered.geoms:
                total_buffered_area += p.area
        elif isinstance(buffered, Polygon):
            total_buffered_area += buffered.area

print('Buffered Qiantang River area: %d m2 (%.2f km2)' % (total_buffered_area, total_buffered_area/1e6))
print('Buffer width used: 60m (river)')

# Check total LineString water features
lines = water_proj[water_proj.geometry.type.isin(['LineString', 'MultiLineString'])].copy()
print('\n=== All water LineString features ===')
print('Total LineString features:', len(lines))
lines['area_m2'] = lines.geometry.length * 60  # rough estimate at 60m width
total_line_area = lines['area_m2'].sum()
print('Estimated total river/canal area (60m width): %d m2 (%.2f km2)' % (total_line_area, total_line_area/1e6))