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

print('=== OSM Water Tags Analysis ===')
water_gdf = fetch_water(south, west, north, east)

# Check all columns for width-related tags
print('\nAll columns in water data:')
for col in water_gdf.columns:
    if 'width' in col.lower() or 'depth' in col.lower() or col in ['waterway', 'water', 'natural', 'name']:
        non_null = water_gdf[col].notna().sum()
        print('  %s: %d non-null values' % (col, non_null))

# Check width tag specifically
if 'width' in water_gdf.columns:
    width_values = water_gdf[water_gdf['width'].notna()]['width'].value_counts()
    print('\nWidth values found:')
    print(width_values.head(20))
else:
    print('\nNo "width" column found')

# Check for other size-related tags
size_cols = ['width', 'depth', 'maxwidth', 'minwidth', 'length', 'area', 'maxdepth', 'mindepth']
found_cols = []
for col in size_cols:
    if col in water_gdf.columns:
        non_null = water_gdf[col].notna().sum()
        if non_null > 0:
            found_cols.append(col)
            print('\n%s values:' % col)
            vc = water_gdf[col].value_counts().head(10)
            print(vc)

if not found_cols:
    print('\nNo size-related tags found in water data')

# Check Qiantang River specific tags
print('\n=== Qiantang River Tags ===')
qt_proj = water_gdf[water_gdf.get('name','').str.contains('钱塘|Qiantang', na=False)]
if len(qt_proj) > 0:
    print('Qiantang features:', len(qt_proj))
    # Show all non-null tags for first feature
    first = qt_proj.iloc[0]
    non_null_tags = {}
    for col in qt_proj.columns:
        val = first.get(col)
        if val is not None and str(val) != 'nan' and col != 'geometry':
            non_null_tags[col] = str(val)[:50]
    print('Non-null tags for first Qiantang feature:')
    for k, v in sorted(non_null_tags.items()):
        print('  %s: %s' % (k, v))

# Check waterway=river features for width
print('\n=== River Features with Width Info ===')
rivers = water_gdf[water_gdf.get('waterway') == 'river']
print('Total waterway=river features:', len(rivers))

# Check if any have width
if 'width' in rivers.columns:
    rivers_with_width = rivers[rivers['width'].notna()]
    print('Rivers with width tag:', len(rivers_with_width))
    if len(rivers_with_width) > 0:
        print('Width values:')
        for _, row in rivers_with_width.head(5).iterrows():
            name = row.get('name', '')
            width = row.get('width', '')
            print('  name=%s, width=%s' % (name, width))

# Check river polygons (water=river) for any size info
print('\n=== River Polygon Tags ===')
river_polys = water_gdf[
    (water_gdf.get('water') == 'river') &
    (water_gdf.geometry.type.isin(['Polygon', 'MultiPolygon']))
]
print('River polygon features:', len(river_polys))

# Check their tags
if len(river_polys) > 0:
    first_poly = river_polys.iloc[0]
    non_null_tags = {}
    for col in river_polys.columns:
        val = first_poly.get(col)
        if val is not None and str(val) != 'nan' and col != 'geometry':
            non_null_tags[col] = str(val)[:50]
    print('Non-null tags for first river polygon:')
    for k, v in sorted(non_null_tags.items()):
        print('  %s: %s' % (k, v))