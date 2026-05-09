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

print('=== Raw water features (before clipping) ===')
water_gdf = fetch_water(south, west, north, east)
print('Total raw features:', len(water_gdf))

# Search for West Lake by name
if 'name' in water_gdf.columns:
    west_lake = water_gdf[water_gdf['name'].str.contains('西湖|West Lake|西子湖', na=False, case=False)]
    print('\nWest Lake entries:', len(west_lake))
    if len(west_lake) > 0:
        for i, (_, row) in enumerate(west_lake.iterrows()):
            geom = row.geometry
            print(f'  {i+1}. name={row.get("name")}, type={geom.geom_type}, water={row.get("water")}, natural={row.get("natural")}')
            if hasattr(geom, 'area'):
                print(f'      area={geom.area:.0f} m2')

# Search for Qiantang River
if 'name' in water_gdf.columns:
    qiantang = water_gdf[water_gdf['name'].str.contains('钱塘|Qiantang|之江', na=False, case=False)]
    print('\nQiantang River entries:', len(qiantang))
    if len(qiantang) > 0:
        for i, (_, row) in enumerate(qiantang.iterrows()):
            geom = row.geometry
            name = row.get('name', '')
            print(f'  {i+1}. name={name}, type={geom.geom_type}, water={row.get("water")}')
            if hasattr(geom, 'area'):
                print(f'      area={geom.area:.0f} m2')

# Check for river-type polygons
river_features = water_gdf[water_gdf.get('water', '') == 'river']
print('\nRiver features (water=river):', len(river_features))
river_polys = river_features[river_features.geometry.type.isin(['Polygon','MultiPolygon'])]
print('  Polygon river features:', len(river_polys))
if len(river_polys) > 0:
    total_river_area = river_polys.geometry.area.sum()
    print('  Total river polygon area: %.0f m2' % total_river_area)

# Look for large water features (> 0.5 km2)
large = water_gdf[water_gdf.geometry.type.isin(['Polygon','MultiPolygon'])].copy()
large['area'] = large.geometry.area
large_features = large[large['area'] > 500000].nlargest(20, 'area')
print('\nLarge water features (> 0.5 km2):')
for i, (_, row) in enumerate(large_features.iterrows()):
    names = {}
    for col in ['name','water','natural']:
        if col in row and row[col] is not None:
            names[col] = str(row[col])[:40]
    print('  %d. area=%.0f m2 (%.2f km2), type=%s, %s' % (i+1, row['area'], row['area']/1e6, row.geometry.geom_type, names))

# After projection check
print('\n=== After projection ===')
water_proj = project_geodataframe(water_gdf, utm_crs, origin, clip_bbox=utm_bbox)
water_proj = water_proj.copy()
water_proj['area_m2'] = water_proj.geometry.area
large_proj = water_proj[water_proj['area_m2'] > 500000]
print('Large features after projection (> 0.5 km2):', len(large_proj))
if len(large_proj) > 0:
    for i, (_, row) in enumerate(large_proj.nlargest(20, 'area_m2').iterrows()):
        names = {}
        for col in ['name','water','natural']:
            if col in row and row[col] is not None:
                names[col] = str(row[col])[:40]
        print('  %d. area=%.0f m2 (%.2f km2), type=%s, %s' % (i+1, row['area_m2'], row['area_m2']/1e6, row.geometry.geom_type, names))
