"""Check if same river has both Polygon and LineString"""
import sys, os
sys.path.insert(0, '.')
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm, project_geodataframe
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import fetch_water
from shapely.ops import unary_union
from shapely.geometry import box
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

print('=== Overlap Analysis: Polygon vs LineString ===')

# Get river polygons
river_polys = water_proj[
    (water_proj.get('water', '') == 'river') &
    (water_proj.geometry.type.isin(['Polygon', 'MultiPolygon']))
].copy()
print('River Polygons (water=river):', len(river_polys))
river_poly_union = unary_union(river_polys.geometry)
print('River Polygon total area: %.2f km2' % (river_poly_union.area / 1e6))

# Get river linestrings
river_lines = water_proj[
    (water_proj.get('waterway', '') == 'river') &
    (water_proj.geometry.type == 'LineString')
].copy()
print('River LineStrings (waterway=river):', len(river_lines))
river_line_total_length = river_lines.geometry.length.sum()
print('River LineString total length: %.1f km' % (river_line_total_length / 1000))

# Buffer linestrings at 60m
river_line_buffered = unary_union(river_lines.geometry.buffer(30))  # 30m half-width = 60m total
print('River LineString buffered (60m) area: %.2f km2' % (river_line_buffered.area / 1e6))

# Check overlap
overlap = river_poly_union.intersection(river_line_buffered)
print('\n=== Overlap Check ===')
print('Overlap area: %.2f km2' % (overlap.area / 1e6))

overlap_ratio = overlap.area / river_poly_union.area * 100
print('Overlap ratio (buffer covers polygon): %.1f%%' % overlap_ratio)

# Check if linestrings are inside polygons
lines_inside = river_lines[river_lines.geometry.intersects(river_poly_union)]
print('LineStrings intersecting Polygon: %d / %d' % (len(lines_inside), len(river_lines)))

# Visualize: find areas with ONLY polygon (no linestring coverage)
poly_only = river_poly_union.difference(river_line_buffered)
print('\nAreas covered ONLY by Polygon (not by buffered LineString): %.2f km2' % (poly_only.area / 1e6))

# Areas covered ONLY by buffered linestring (no polygon)
line_only = river_line_buffered.difference(river_poly_union)
print('Areas covered ONLY by buffered LineString: %.2f km2' % (line_only.area / 1e6))

print('\n=== Conclusion ===')
if overlap.area > river_poly_union.area * 0.5:
    print('Major overlap: LineStrings mostly cover same area as Polygons')
    print('This means same rivers are represented TWICE (both as Polygon and LineString)')
else:
    print('Limited overlap: LineStrings and Polygons cover different areas')
    print('Polygon data may represent different water bodies than LineString data')

# Check specific named rivers
print('\n=== Named Rivers Analysis ===')
named_rivers = water_proj[
    water_proj.get('name', '').notna() &
    water_proj.get('name', '').str.contains('钱塘|Qiantang|运河|Canal', na=False)
].copy()

for name_pattern in ['钱塘|Qiantang', '运河']:
    subset = named_rivers[named_rivers.get('name', '').str.contains(name_pattern, na=False)]
    if len(subset) > 0:
        polys = subset[subset.geometry.type.isin(['Polygon', 'MultiPolygon'])]
        lines = subset[subset.geometry.type.isin(['LineString', 'MultiLineString'])]
        print('  "%s":' % name_pattern)
        print('    Polygon: %d, LineString: %d' % (len(polys), len(lines)))
        if len(polys) > 0 and len(lines) > 0:
            print('    --> Has BOTH Polygon and LineString!')