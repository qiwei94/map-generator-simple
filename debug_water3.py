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
scale = 196.0 / max(bbox['width_m'], bbox['height_m'])

print('=== West Lake specific check ===')
water_gdf = fetch_water(south, west, north, east)

# West Lake polygons in raw data
wl_polys = water_gdf[(water_gdf['name'] == '西湖') & (water_gdf.geometry.type.isin(['Polygon','MultiPolygon']))].copy()
print('West Lake polygon features in raw data:', len(wl_polys))

# Project them and check areas
wl_proj = project_geodataframe(wl_polys, utm_crs, origin, clip_bbox=utm_bbox)
wl_proj = wl_proj.copy()
wl_proj['area_m2'] = wl_proj.geometry.area
print('After projection:')
for i, (_, row) in enumerate(wl_proj.iterrows()):
    print('  %d. area=%.0f m2 (%.2f km2), type=%s' % (i+1, row['area_m2'], row['area_m2']/1e6, row.geometry.geom_type))
print('  Total West Lake area: %.0f m2 (%.2f km2)' % (wl_proj['area_m2'].sum(), wl_proj['area_m2'].sum()/1e6))

print()
print('=== Qiantang River specific check ===')
qt_line = water_gdf[(water_gdf['name'] == '钱塘江')].copy()
print('Qiantang River features:', len(qt_line))
qt_types = qt_line.geometry.type.value_counts()
print('  Types:', qt_types.to_dict())

# Check if there are any river-area polygons in the bbox
river_poly = water_gdf[(water_gdf.get('water','') == 'river') & (water_gdf.geometry.type.isin(['Polygon','MultiPolygon']))].copy()
print('\nRiver polygon features (water=river, Polygon/MultiPolygon):', len(river_poly))
if len(river_poly) > 0:
    river_poly_proj = project_geodataframe(river_poly, utm_crs, origin, clip_bbox=utm_bbox)
    river_poly_proj = river_poly_proj.copy()
    river_poly_proj['area_m2'] = river_poly_proj.geometry.area
    top5 = river_poly_proj.nlargest(5, 'area_m2')
    print('  Top 5 by area:')
    for i, (_, row) in enumerate(top5.iterrows()):
        name = row.get('name', '')
        print('    %d. area=%.0f m2 (%.2f km2), name=%s' % (i+1, row['area_m2'], row['area_m2']/1e6, name))

    # Check if Qiantang River polygon is present
    qt_poly = river_poly_proj[river_poly_proj.get('name','').str.contains('钱塘|Qiantang', na=False)]
    print('  Qiantang River polygons:', len(qt_poly))
