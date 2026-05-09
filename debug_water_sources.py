import sys, os
sys.path.insert(0, '.')
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm, project_geodataframe
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import fetch_water

LAT1, LON1 = 30.1375, 120.020
LAT2, LON2 = 30.3625, 120.280
bbox = bbox_to_utm(LAT1, LON1, LAT2, LON2)
south, west, north, east = bbox['wgs84_bbox']
utm_crs = bbox['utm_crs']
origin = bbox['origin']
utm_bbox = bbox['utm_bbox']

water_gdf = fetch_water(south, west, north, east)
water_proj = project_geodataframe(water_gdf, utm_crs, origin, clip_bbox=utm_bbox)

print('=== Water Data Source Analysis ===')
print('Total features:', len(water_proj))

# Separate by geometry type and tags
polygons = water_proj[water_proj.geometry.type.isin(['Polygon', 'MultiPolygon'])].copy()
linestrings = water_proj[water_proj.geometry.type.isin(['LineString', 'MultiLineString'])].copy()

print('\nPolygon features:', len(polygons))
print('LineString features:', len(linestrings))

# Polygon sources
print('\n=== Polygon Sources ===')
polygons['area_m2'] = polygons.geometry.area
for water_type in ['river', 'lake', 'pond', 'basin', 'reservoir', 'canal']:
    subset = polygons[polygons.get('water', '') == water_type]
    if len(subset) > 0:
        total_area = subset['area_m2'].sum()
        print('  water=%s: %d features, %.2f km2 total' % (water_type, len(subset), total_area/1e6))

# Natural water polygons
natural_water = polygons[polygons.get('natural', '') == 'water']
if len(natural_water) > 0:
    total_area = natural_water['area_m2'].sum()
    print('  natural=water: %d features, %.2f km2 total' % (len(natural_water), total_area/1e6))

# LineString sources
print('\n=== LineString Sources ===')
for waterway_type in ['river', 'canal', 'stream', 'ditch']:
    subset = linestrings[linestrings.get('waterway', '') == waterway_type]
    if len(subset) > 0:
        total_length = subset.geometry.length.sum()
        print('  waterway=%s: %d features, %.1f km total length' % (waterway_type, len(subset), total_length/1000))

# Check width tags on LineStrings
print('\n=== LineString Width Tags ===')
for waterway_type in ['river', 'canal']:
    subset = linestrings[linestrings.get('waterway', '') == waterway_type]
    if len(subset) > 0 and 'width' in subset.columns:
        with_width = subset[subset['width'].notna()]
        if len(with_width) > 0:
            print('  waterway=%s with width tag: %d features' % (waterway_type, len(with_width)))
            for _, row in with_width.head(3).iterrows():
                name = row.get('name', '')
                width = row.get('width', '')
                print('    name=%s, width=%s' % (name, width))

print('\n=== Key Insight ===')
print('Polygon features (water=river, natural=water) are REAL river shapes from OSM')
print('LineString features (waterway=river) are buffered with config width')
print('The wide areas you see are likely from Polygon data, not buffered LineStrings')