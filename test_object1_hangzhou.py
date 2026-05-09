import sys, os
sys.path.insert(0, '.')
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

import time
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm, project_geodataframe
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import fetch_water
from _TEXTURE_STYLE_OF_DEEPSEEK.water import build_deepseek_water
from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf

LAT1, LON1 = 30.1375, 120.020
LAT2, LON2 = 30.3625, 120.280
CITY_NAME = "hangzhou_west_lake"
OUTPUT_DIR = "output/object1_validation"

bbox = bbox_to_utm(LAT1, LON1, LAT2, LON2)
south, west, north, east = bbox['wgs84_bbox']
utm_crs = bbox['utm_crs']
origin = bbox['origin']
utm_bbox = bbox['utm_bbox']
scale = 196.0 / max(bbox['width_m'], bbox['height_m'])

bbox_x_min = utm_bbox[0] - origin[0]
bbox_y_min = utm_bbox[1] - origin[1]
bbox_x_max = utm_bbox[2] - origin[0]
bbox_y_max = utm_bbox[3] - origin[1]

print('=== Object 1: Base Plate + Water Relief ===')

water_gdf = fetch_water(south, west, north, east)
water_proj = project_geodataframe(water_gdf, utm_crs, origin, clip_bbox=utm_bbox)

water_mesh = build_deepseek_water(
    water_proj, bbox_x_min, bbox_y_min, bbox_x_max, bbox_y_max, scale
)

if water_mesh is not None:
    print('\nMesh stats:')
    print('  Vertices:', len(water_mesh.vertices))
    print('  Faces:', len(water_mesh.faces))
    print('  Watertight:', water_mesh.is_watertight)
    print('  Volume:', water_mesh.volume, 'mm3')

    # Export 3MF
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    output_path = os.path.join(OUTPUT_DIR, 'water_plate_%s.3mf' % CITY_NAME)

    meshes = {
        'terrain_surface': None,
        'terrain_walls': None,
        'buildings': None,
        'roads': None,
        'water': water_mesh,
        'vegetation': None,
    }

    export_deepseek_3mf(meshes, output_path, extruders=4)
    file_size_mb = os.path.getsize(output_path) / (1024 * 1024)
    print('\nOutput:', output_path)
    print('File size: %.2f MB' % file_size_mb)
else:
    print('ERROR: Water mesh generation failed')