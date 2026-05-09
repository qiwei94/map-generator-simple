import sys, os
sys.path.insert(0, '.')
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm, project_geodataframe
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import fetch_water
import geopandas as gpd

LAT1, LON1 = 30.1375, 120.020
LAT2, LON2 = 30.3625, 120.280
bbox = bbox_to_utm(LAT1, LON1, LAT2, LON2)
south, west, north, east = bbox['wgs84_bbox']
utm_crs = bbox['utm_crs']
origin = bbox['origin']
utm_bbox = bbox['utm_bbox']
scale = 196.0 / max(bbox['width_m'], bbox['height_m'])

print('=== Raw water fetch ===')
water_gdf = fetch_water(south, west, north, east)
print('Raw water features:', len(water_gdf))

geom_types = water_gdf.geometry.type.value_counts()
print('Geometry types:')
print(geom_types)

for col in ['water','natural','waterway','landuse']:
    if col in water_gdf.columns:
        vc = water_gdf[col].value_counts().head(10).to_dict()
        print(col, 'tags top 10:', vc)

print()
print('=== After projection ===')
water_proj = project_geodataframe(water_gdf, utm_crs, origin, clip_bbox=utm_bbox)
print('Projected water features:', len(water_proj))

water_proj = water_proj.copy()
water_proj['area_m2'] = water_proj.geometry.area
top10 = water_proj.nlargest(10, 'area_m2')
print('Top 10 largest water features after projection:')
for i, (_, row) in enumerate(top10.iterrows()):
    geom = row.geometry
    props = {}
    for col in ['water','natural','waterway','name','landuse']:
        if col in row and row[col] is not None and not (isinstance(row[col], float) and str(row[col])=='nan'):
            props[col] = str(row[col])[:30]
    print('  %d. area=%.0f m2, type=%s, props=%s' % (i+1, row['area_m2'], geom.geom_type, props))

# Also check the water column creation
print()
print('=== Testing water column creation ===')
from _TEXTURE_STYLE_OF_DEEPSEEK.water_column import extrude_water_column_manifold, create_water_columns_union_manifold

# Get West Lake and Qiantang River candidates
river_candidates = water_proj[water_proj['area_m2'] > 100000].nlargest(5, 'area_m2')
for i, (_, row) in enumerate(river_candidates.iterrows()):
    geom = row.geometry
    col = extrude_water_column_manifold(geom, -1.17, 4.83, scale)
    print('  %d. area=%.0f m2 -> column empty=%s' % (i+1, row['area_m2'], col.is_empty()))
    if not col.is_empty():
        print('      volume=%.2f mm3, edges=%d' % (col.volume(), col.num_edge()))

# Full union test
print()
print('=== Full water column union ===')
result = create_water_columns_union_manifold(water_proj, -1.17, 4.83, scale)
print('Result empty:', result.is_empty())
if not result.is_empty():
    print('Result volume:', result.volume(), 'mm3')
    print('Result edges:', result.num_edge())
