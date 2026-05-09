import sys, os
sys.path.insert(0, '.')
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm, project_geodataframe
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import fetch_water
from shapely.geometry import box, Point
import geopandas as gpd

LAT1, LON1 = 30.1375, 120.020
LAT2, LON2 = 30.3625, 120.280
bbox = bbox_to_utm(LAT1, LON1, LAT2, LON2)
south, west, north, east = bbox['wgs84_bbox']
utm_crs = bbox['utm_crs']
origin = bbox['origin']
utm_bbox = bbox['utm_bbox']
width_m = bbox['width_m']
height_m = bbox['height_m']

water_gdf = fetch_water(south, west, north, east)
water_proj = project_geodataframe(water_gdf, utm_crs, origin, clip_bbox=utm_bbox)

# The circled area appears to be in the lower part of the model
# Based on the screenshot orientation, estimate the location
# Let's sample different regions to find wide water features

print('=== Finding Wide Water Features ===')
print('Model extent: %.0f x %.0f m (local coords 0 to %.0f, 0 to %.0f)' % (width_m, height_m, width_m, height_m))

# Calculate feature widths (for Polygons: width = bounds difference)
water_proj['area_m2'] = water_proj.geometry.area
polygons = water_proj[water_proj.geometry.type.isin(['Polygon', 'MultiPolygon'])].copy()

# Estimate width from bounds
def estimate_width(geom):
    bounds = geom.bounds  # (minx, miny, maxx, maxy)
    return max(bounds[2] - bounds[0], bounds[3] - bounds[1])

polygons['est_width_m'] = polygons.geometry.apply(estimate_width)

# Find features wider than 200m
wide_features = polygons[polygons['est_width_m'] > 200].nlargest(20, 'est_width_m')

print('\n=== Wide Polygon Features (>200m width) ===')
for i, (_, row) in enumerate(wide_features.iterrows()):
    bounds = row.geometry.bounds
    center_x = (bounds[0] + bounds[2]) / 2
    center_y = (bounds[1] + bounds[3]) / 2
    
    # Convert to relative position in model
    rel_x = center_x / width_m  # 0~1
    rel_y = center_y / height_m  # 0~1
    
    water = row.get('water', '')
    natural = row.get('natural', '')
    name = row.get('name', '')
    waterway = row.get('waterway', '')
    
    print('  %d. width=%.0f m, area=%.0f m2' % (i+1, row['est_width_m'], row['area_m2']))
    print('      position: (%.1f%%, %.1f%%) of model' % (rel_x*100, rel_y*100))
    print('      tags: water=%s, natural=%s, name=%s' % (water, natural, name[:30] if name else ''))
    print('      geom_type: %s' % row.geometry.geom_type)

# Also check LineString buffered features in the same area
print('\n=== LineString (waterway=river) at Lower Part of Model ===')
linestrings = water_proj[water_proj.geometry.type.isin(['LineString', 'MultiLineString'])].copy()
river_lines = linestrings[linestrings.get('waterway', '') == 'river']

# Filter to lower part (y < 30% of model height)
lower_rivers = river_lines[river_lines.geometry.bounds[1] < height_m * 0.3]
print('River LineStrings in lower 30% of model:', len(lower_rivers))

if len(lower_rivers) > 0:
    for _, row in lower_rivers.head(5).iterrows():
        bounds = row.geometry.bounds
        center_y = (bounds[1] + bounds[3]) / 2
        rel_y = center_y / height_m
        name = row.get('name', '')
        width = row.get('width', '')
        length = row.geometry.length
        print('  y=%.1f%%, length=%.0f m, name=%s, OSM_width=%s' % (rel_y*100, length, name[:30] if name else '', width))

print('\n=== Checking Qiantang River Position ===')
qt_proj = water_proj[water_proj.get('name','').str.contains('钱塘|Qiantang', na=False)]
for _, row in qt_proj.head(3).iterrows():
    geom = row.geometry
    bounds = geom.bounds
    center_y = (bounds[1] + bounds[3]) / 2
    rel_y = center_y / height_m
    geom_type = geom.geom_type
    print('  Qiantang: y=%.1f%%, type=%s' % (rel_y*100, geom_type))