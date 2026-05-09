import sys, os
sys.path.insert(0, '.')
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm, project_geodataframe
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import fetch_water
from shapely.ops import unary_union

LAT1, LON1 = 30.1375, 120.020
LAT2, LON2 = 30.3625, 120.280
bbox = bbox_to_utm(LAT1, LON1, LAT2, LON2)
south, west, north, east = bbox['wgs84_bbox']
utm_crs = bbox['utm_crs']
origin = bbox['origin']
utm_bbox = bbox['utm_bbox']

water_gdf = fetch_water(south, west, north, east)
water_proj = project_geodataframe(water_gdf, utm_crs, origin, clip_bbox=utm_bbox)

qt_proj = water_proj[water_proj.get('name','').str.contains('钱塘|Qiantang', na=False)].copy()
print('=== Qiantang LineString Deduplication Check ===')
print('Raw features:', len(qt_proj))

# Check unique geometries
unique_geoms = set()
for _, row in qt_proj.iterrows():
    coords = tuple(row.geometry.coords)
    unique_geoms.add(coords)
print('Unique geometries:', len(unique_geoms))

# Merge all LineStrings into one
merged_line = unary_union(qt_proj.geometry)
print('Merged line type:', merged_line.geom_type)
print('Merged line length:', merged_line.length, 'm')

# Buffer merged line at different widths
for width in [60, 200, 300, 500]:
    buffered = merged_line.buffer(width / 2)
    print('  Buffer %dm: %.0f m2 (%.2f km2)' % (width, buffered.area, buffered.area/1e6))

# Check river polygons
print('\n=== River Polygon Coverage ===')
river_polys = water_proj[
    (water_proj.get('water','') == 'river') &
    (water_proj.geometry.type.isin(['Polygon', 'MultiPolygon']))
].copy()
river_polys['area'] = river_polys.geometry.area
print('River polygon features:', len(river_polys))
print('Total river polygon area: %.0f m2 (%.2f km2)' % (river_polys['area'].sum(), river_polys['area'].sum()/1e6))

# Merge river polygons
merged_poly = unary_union(river_polys.geometry)
print('Merged polygon area: %.0f m2 (%.2f km2)' % (merged_poly.area, merged_poly.area/1e6))