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
width_m = bbox['width_m']
height_m = bbox['height_m']

water_gdf = fetch_water(south, west, north, east)
water_proj = project_geodataframe(water_gdf, utm_crs, origin, clip_bbox=utm_bbox)

print('=== Finding Wide Water Features ===')
print('Model extent: %.0f x %.0f m' % (width_m, height_m))

polygons = water_proj[water_proj.geometry.type.isin(['Polygon', 'MultiPolygon'])].copy()
polygons['area_m2'] = polygons.geometry.area

def estimate_width(geom):
    bounds = geom.bounds
    return max(bounds[2] - bounds[0], bounds[3] - bounds[1])

def safe_str(val):
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return ''
    return str(val)[:40]

polygons['est_width_m'] = polygons.geometry.apply(estimate_width)

wide_features = polygons[polygons['est_width_m'] > 200].nlargest(20, 'est_width_m')

print('\n=== Wide Polygon Features (>200m width) ===')
for i, (_, row) in enumerate(wide_features.iterrows()):
    bounds = row.geometry.bounds
    center_x = (bounds[0] + bounds[2]) / 2
    center_y = (bounds[1] + bounds[3]) / 2
    rel_x = center_x / width_m * 100
    rel_y = center_y / height_m * 100
    
    water = safe_str(row.get('water', ''))
    natural = safe_str(row.get('natural', ''))
    name = safe_str(row.get('name', ''))
    
    print('  %d. width=%.0f m, area=%.0f m2 (%.2f km2)' % (i+1, row['est_width_m'], row['area_m2'], row['area_m2']/1e6))
    print('      position: (%.1f%%, %.1f%%) of model' % (rel_x, rel_y))
    print('      water=%s, natural=%s, name=%s' % (water, natural, name))

print('\n=== River Polygon (water=river) Analysis ===')
river_polys = polygons[polygons.get('water', '') == 'river'].copy()
river_polys['est_width_m'] = river_polys.geometry.apply(estimate_width)
wide_river_polys = river_polys[river_polys['est_width_m'] > 200].nlargest(10, 'est_width_m')

print('River polygons wider than 200m:', len(wide_river_polys))
for i, (_, row) in enumerate(wide_river_polys.iterrows()):
    bounds = row.geometry.bounds
    rel_y = ((bounds[1] + bounds[3]) / 2) / height_m * 100
    name = safe_str(row.get('name', ''))
    print('  %d. width=%.0f m, y=%.1f%%, name=%s' % (i+1, row['est_width_m'], rel_y, name))

print('\n=== Qiantang River Location ===')
qt_proj = water_proj[water_proj.get('name','').str.contains('钱塘|Qiantang', na=False)]
for _, row in qt_proj.head(5).iterrows():
    geom = row.geometry
    bounds = geom.bounds
    rel_y = ((bounds[1] + bounds[3]) / 2) / height_m * 100
    rel_x = ((bounds[0] + bounds[2]) / 2) / width_m * 100
    geom_type = geom.geom_type
    water = safe_str(row.get('water', ''))
    waterway = safe_str(row.get('waterway', ''))
    print('  Qiantang: pos=(%.1f%%, %.1f%%), type=%s, water=%s, waterway=%s' % (rel_x, rel_y, geom_type, water, waterway))