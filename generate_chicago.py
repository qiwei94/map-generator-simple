"""Generate 3D map models for Chicago - simplified version."""

import os
import sys
import time
import numpy as np
import trimesh

_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm, project_geodataframe
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import fetch_water, fetch_roads, fetch_buildings
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.elevation import fetch_elevation_grid
from _TEXTURE_STYLE_OF_DEEPSEEK.object4_terrain_with_holes import build_terrain_with_water_holes_manifold
from _TEXTURE_STYLE_OF_DEEPSEEK.water import build_deepseek_water
from _TEXTURE_STYLE_OF_DEEPSEEK.buildings import build_deepseek_buildings
from _TEXTURE_STYLE_OF_DEEPSEEK.roads import build_deepseek_roads
from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf
from _TEXTURE_STYLE_OF_DEEPSEEK.config import compute_scale, WATERWAY_WIDTHS

# Chicago downtown area (~8km x 6km)
LAT1, LON1 = 41.85, -87.68   # south-west
LAT2, LON2 = 41.91, -87.61   # north-east
CITY_NAME = "chicago"
OUTPUT_DIR = "output/chicago"

print("=" * 70)
print("  Chicago 3D Map Generator (Simplified)")
print("=" * 70)

os.makedirs(OUTPUT_DIR, exist_ok=True)

# =====================================================================
# Stage 0: Bounding box and coordinates
# =====================================================================
print(f"\n[Stage 0] Bounding box setup...")
t0 = time.time()

bbox = bbox_to_utm(LAT1, LON1, LAT2, LON2)
width_m = bbox["width_m"]
height_m = bbox["height_m"]
area_km2 = bbox["area_km2"]
scale = compute_scale(width_m, height_m)
south, west, north, east = bbox["wgs84_bbox"]
utm_crs = bbox["utm_crs"]
origin = bbox["origin"]
utm_bbox = bbox["utm_bbox"]

bbox_x_min = utm_bbox[0] - origin[0]
bbox_y_min = utm_bbox[1] - origin[1]
bbox_x_max = utm_bbox[2] - origin[0]
bbox_y_max = utm_bbox[3] - origin[1]

print(f"  Bounding box: ({LAT1}, {LON1}) -> ({LAT2}, {LON2})")
print(f"  Width: {width_m:.0f}m, Height: {height_m:.0f}m")
print(f"  Area: {area_km2:.1f} km2")
print(f"  Scale: {scale:.6f} mm/m")
print(f"  Time: {time.time() - t0:.1f}s")

# =====================================================================
# Stage 1: Elevation data
# =====================================================================
print(f"\n[Stage 1] Fetching elevation data...")
t1 = time.time()

elevation_grid = fetch_elevation_grid(south, west, north, east, resolution=512)

if elevation_grid is None:
    print("  ERROR: Elevation data fetch failed!")
    sys.exit(1)

print(f"  Grid shape: {elevation_grid.shape}")
print(f"  Elevation range: {elevation_grid.min():.1f}m -> {elevation_grid.max():.1f}m")
print(f"  Time: {time.time() - t1:.1f}s")

# =====================================================================
# Stage 2: Water features
# =====================================================================
print(f"\n[Stage 2] Fetching water data...")
t2 = time.time()

water_gdf = fetch_water(south, west, north, east)

if water_gdf is None or len(water_gdf) == 0:
    print("  WARNING: No water features found")
    water_gdf = None
else:
    water_gdf = project_geodataframe(water_gdf, utm_crs, origin, clip_bbox=utm_bbox)
    
    def estimate_water_area(geom, row):
        if geom.geom_type in ['Polygon', 'MultiPolygon']:
            return geom.area
        elif geom.geom_type in ['LineString', 'MultiLineString']:
            waterway_type = row.get('waterway', 'river')
            width = WATERWAY_WIDTHS.get(waterway_type, 60)
            return geom.length * width
        return 0
    
    water_gdf['est_area'] = water_gdf.apply(lambda r: estimate_water_area(r.geometry, r), axis=1)
    water_gdf = water_gdf.nlargest(100, 'est_area')  # Keep top 100
    
    print(f"  Water features (top 100): {len(water_gdf)}")
    print(f"  Geometry types: {water_gdf.geometry.type.value_counts().to_dict()}")
    
    if 'name' in water_gdf.columns:
        named = water_gdf['name'].dropna().unique()
        print(f"  Named features: {list(named[:10])}")

print(f"  Time: {time.time() - t2:.1f}s")

# =====================================================================
# Stage 3: Road data (limit to main roads)
# =====================================================================
print(f"\n[Stage 3] Fetching road data...")
t3 = time.time()

roads_gdf = fetch_roads(south, west, north, east)

if roads_gdf is not None and len(roads_gdf) > 0:
    roads_gdf = project_geodataframe(roads_gdf, utm_crs, origin, clip_bbox=utm_bbox)
    
    # Keep only main road types
    main_roads = ['motorway', 'trunk', 'primary', 'secondary', 'tertiary', 'motorway_link', 'trunk_link', 'primary_link']
    if 'highway' in roads_gdf.columns:
        roads_gdf = roads_gdf[roads_gdf['highway'].isin(main_roads)]
    
    # Limit to 5000 features
    if len(roads_gdf) > 5000:
        roads_gdf = roads_gdf.sample(5000, random_state=42)
    
    print(f"  Road features (filtered): {len(roads_gdf)}")
    if 'highway' in roads_gdf.columns:
        print(f"  Highway types: {roads_gdf['highway'].value_counts().head(5).to_dict()}")
else:
    print(f"  No roads found")
    roads_gdf = None

print(f"  Time: {time.time() - t3:.1f}s")

# =====================================================================
# Stage 4: Building data (limit to 5000)
# =====================================================================
print(f"\n[Stage 4] Fetching building data...")
t4 = time.time()

buildings_gdf = fetch_buildings(south, west, north, east)

if buildings_gdf is not None and len(buildings_gdf) > 0:
    buildings_gdf = project_geodataframe(buildings_gdf, utm_crs, origin, clip_bbox=utm_bbox)
    
    if len(buildings_gdf) > 5000:
        buildings_gdf = buildings_gdf.sample(5000, random_state=42)
    
    print(f"  Building features (sampled): {len(buildings_gdf)}")
else:
    print(f"  No buildings found")
    buildings_gdf = None

print(f"  Time: {time.time() - t4:.1f}s")

# =====================================================================
# Object 1: Water Plate
# =====================================================================
print(f"\n{'=' * 70}")
print("  Object 1: Water Plate")
print("=" * 70)
t_obj1 = time.time()

if water_gdf is not None and len(water_gdf) > 0:
    water_mesh = build_deepseek_water(
        water_gdf, bbox_x_min, bbox_y_min, bbox_x_max, bbox_y_max, scale
    )
    
    if water_mesh is not None:
        print(f"  Vertices: {len(water_mesh.vertices)}")
        print(f"  Faces: {len(water_mesh.faces)}")
        print(f"  Watertight: {water_mesh.is_watertight}")
        
        obj1_path = os.path.join(OUTPUT_DIR, f"obj1_water_plate_{CITY_NAME}.3mf")
        meshes = {
            'terrain_surface': None,
            'terrain_walls': None,
            'buildings': None,
            'roads': None,
            'water': water_mesh,
            'vegetation': None,
        }
        export_deepseek_3mf(meshes, obj1_path, extruders=4)
        print(f"  Exported: {obj1_path} ({os.path.getsize(obj1_path)/1024/1024:.2f} MB)")
    else:
        print("  ERROR: Water mesh generation failed")
else:
    print("  SKIPPED: No water data")

print(f"  Time: {time.time() - t_obj1:.1f}s")

# =====================================================================
# Object 4: Terrain with Water Holes (MUST be done before buildings)
# =====================================================================
print(f"\n{'=' * 70}")
print("  Object 4: Terrain with Water Holes")
print("=" * 70)
t_obj4 = time.time()

terrain_mesh = None

if elevation_grid is not None:
    result = build_terrain_with_water_holes_manifold(
        elevation_grid=elevation_grid,
        width_m=width_m,
        height_m=height_m,
        area_km2=area_km2,
        scale=scale,
        water_gdf=water_gdf,
        roads_gdf=roads_gdf,
        enable_roads_fusion=(roads_gdf is not None and len(roads_gdf) > 0),
    )
    
    terrain_mesh = result["mesh"]
    stats = result["stats"]
    
    print(f"  Vertices: {len(terrain_mesh.vertices)}")
    print(f"  Faces: {len(terrain_mesh.faces)}")
    print(f"  Watertight: {terrain_mesh.is_watertight}")
    print(f"  Boolean operations: {len(stats.get('boolean_ops', []))}")
    if stats.get("roads_faces", 0) > 0:
        print(f"  Road faces fused: {stats['roads_faces']}")
    
    obj4_path = os.path.join(OUTPUT_DIR, f"obj4_terrain_{CITY_NAME}.3mf")
    meshes = {
        "terrain_surface": terrain_mesh,
        "terrain_walls": None,
        "buildings": None,
        "roads": None,
        "water": None,
        "vegetation": None,
    }
    export_deepseek_3mf(meshes, obj4_path, extruders=4)
    print(f"  Exported: {obj4_path} ({os.path.getsize(obj4_path)/1024/1024:.2f} MB)")
else:
    print("  SKIPPED: No elevation data")

print(f"  Time: {time.time() - t_obj4:.1f}s")

# =====================================================================
# Object 2: Buildings (requires terrain_mesh from Object 4)
# =====================================================================
print(f"\n{'=' * 70}")
print("  Object 2: Buildings")
print("=" * 70)
t_obj2 = time.time()

if buildings_gdf is not None and len(buildings_gdf) > 0 and terrain_mesh is not None:
    building_mesh = build_deepseek_buildings(buildings_gdf, terrain_mesh, area_km2, scale)
    
    if building_mesh is not None:
        print(f"  Vertices: {len(building_mesh.vertices)}")
        print(f"  Faces: {len(building_mesh.faces)}")
        print(f"  Watertight: {building_mesh.is_watertight}")
        
        obj2_path = os.path.join(OUTPUT_DIR, f"obj2_buildings_{CITY_NAME}.3mf")
        meshes = {
            'terrain_surface': None,
            'terrain_walls': None,
            'buildings': building_mesh,
            'roads': None,
            'water': None,
            'vegetation': None,
        }
        export_deepseek_3mf(meshes, obj2_path, extruders=4)
        print(f"  Exported: {obj2_path} ({os.path.getsize(obj2_path)/1024/1024:.2f} MB)")
    else:
        print("  ERROR: Building mesh generation failed")
else:
    reason = []
    if buildings_gdf is None or len(buildings_gdf) == 0:
        reason.append("No building data")
    if terrain_mesh is None:
        reason.append("No terrain mesh")
    print(f"  SKIPPED: {', '.join(reason)}")

print(f"  Time: {time.time() - t_obj2:.1f}s")

# =====================================================================
# Object 3: Roads
# =====================================================================
print(f"\n{'=' * 70}")
print("  Object 3: Roads")
print("=" * 70)
t_obj3 = time.time()

if roads_gdf is not None and len(roads_gdf) > 0:
    road_mesh = build_deepseek_roads(roads_gdf, scale)
    
    if road_mesh is not None:
        print(f"  Vertices: {len(road_mesh.vertices)}")
        print(f"  Faces: {len(road_mesh.faces)}")
        print(f"  Watertight: {road_mesh.is_watertight}")
        
        obj3_path = os.path.join(OUTPUT_DIR, f"obj3_roads_{CITY_NAME}.3mf")
        meshes = {
            'terrain_surface': None,
            'terrain_walls': None,
            'buildings': None,
            'roads': road_mesh,
            'water': None,
            'vegetation': None,
        }
        export_deepseek_3mf(meshes, obj3_path, extruders=4)
        print(f"  Exported: {obj3_path} ({os.path.getsize(obj3_path)/1024/1024:.2f} MB)")
    else:
        print("  ERROR: Road mesh generation failed")
else:
    print("  SKIPPED: No road data")

print(f"  Time: {time.time() - t_obj3:.1f}s")

# =====================================================================
# Summary
# =====================================================================
print(f"\n{'=' * 70}")
print(f"  Chicago Model Generation Complete")
print(f"{'=' * 70}")
print(f"\nOutput files in: {OUTPUT_DIR}/")
for f in sorted(os.listdir(OUTPUT_DIR)):
    if f.endswith('.3mf'):
        path = os.path.join(OUTPUT_DIR, f)
        print(f"  {f} ({os.path.getsize(path)/1024/1024:.2f} MB)")

print(f"\n{'=' * 70}\n")
