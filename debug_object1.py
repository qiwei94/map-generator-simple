import sys, os
sys.path.insert(0, '.')
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm, project_geodataframe
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import fetch_water
from _TEXTURE_STYLE_OF_DEEPSEEK.water import build_deepseek_water
from _TEXTURE_STYLE_OF_DEEPSEEK.config import WATERWAY_WIDTHS
from shapely.geometry import Polygon, MultiPolygon, LineString, MultiLineString

LAT1, LON1 = 30.1375, 120.020
LAT2, LON2 = 30.3625, 120.280
bbox = bbox_to_utm(LAT1, LON1, LAT2, LON2)
south, west, north, east = bbox['wgs84_bbox']
utm_crs = bbox['utm_crs']
origin = bbox['origin']
utm_bbox = bbox['utm_bbox']
scale = 196.0 / max(bbox['width_m'], bbox['height_m'])

# Local bbox for water plate
bbox_x_min = utm_bbox[0] - origin[0]
bbox_y_min = utm_bbox[1] - origin[1]
bbox_x_max = utm_bbox[2] - origin[0]
bbox_y_max = utm_bbox[3] - origin[1]

print('=== Object 1: Base Plate + Water Relief ===')
water_gdf = fetch_water(south, west, north, east)
water_proj = project_geodataframe(water_gdf, utm_crs, origin, clip_bbox=utm_bbox)
print('Projected water features:', len(water_proj))

# Build Object 1
water_mesh = build_deepseek_water(
    water_proj, bbox_x_min, bbox_y_min, bbox_x_max, bbox_y_max, scale
)

if water_mesh is not None:
    print('Water mesh vertices:', len(water_mesh.vertices))
    print('Water mesh faces:', len(water_mesh.faces))
    print('Watertight:', water_mesh.is_watertight)
    print('Z range:', water_mesh.bounds[0][2], '->', water_mesh.bounds[1][2], 'mm')
    print('Volume:', water_mesh.volume, 'mm3')
else:
    print('Water mesh: None')

# Check Qiantang River coverage in water.py
print('\n=== Qiantang River in Object 1 ===')
qt_proj = water_proj[water_proj.get('name','').str.contains('钱塘|Qiantang', na=False)]
print('Qiantang features in projected data:', len(qt_proj))

# Show waterway types
for i, (_, row) in enumerate(qt_proj.head(5).iterrows()):
    geom_type = row.geometry.geom_type
    waterway = row.get('waterway', '')
    width = WATERWAY_WIDTHS.get(waterway, 30)
    print('  %d. type=%s, waterway=%s, buffer_width=%dm' % (i+1, geom_type, waterway, width))