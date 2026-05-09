"""Backup and re-download West Lake water data from Overpass API."""

import os
import sys
import json
import shutil
import time

# Force UTF-8
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import fetch_water

# West Lake bounding box (tight bbox around West Lake)
# West Lake center: 30.2424, 120.1551
# Using a ~5km x 5km area
WEST_LAKE_SOUTH = 30.21
WEST_LAKE_WEST = 120.11
WEST_LAKE_NORTH = 30.27
WEST_LAKE_EAST = 120.19


def _get_backup_dir():
    """获取备份目录，支持跨平台和环境变量"""
    env_cache_dir = os.environ.get("MAP_GEN_CACHE_DIR")
    if env_cache_dir:
        return os.path.join(env_cache_dir, "west_lake_water_backup")
    
    if os.name == 'nt':  # Windows
        return "F:/map_gen_cache/west_lake_water_backup"
    else:  # macOS / Linux
        return os.path.join(os.path.expanduser("~/map_gen_cache"), "west_lake_water_backup")


def _get_cache_dirs():
    """获取缓存目录列表，支持跨平台和环境变量"""
    env_cache_dir = os.environ.get("MAP_GEN_CACHE_DIR")
    if env_cache_dir:
        return [
            os.path.join(env_cache_dir, "attaraction/cache/osm"),
            os.path.join(env_cache_dir, "project_cache/osm"),
        ]
    
    if os.name == 'nt':  # Windows
        return [
            "F:/map_gen_cache/attaraction/cache/osm",
            "F:/map_gen_cache/project_cache/osm",
        ]
    else:  # macOS / Linux
        home_cache = os.path.expanduser("~/map_gen_cache")
        return [
            os.path.join(home_cache, "attaraction/cache/osm"),
            os.path.join(home_cache, "project_cache/osm"),
        ]


BACKUP_DIR = _get_backup_dir()
CACHE_DIRS = _get_cache_dirs()

print("=" * 60)
print("  West Lake Water Data - Backup & Re-download")
print("=" * 60)

# Step 1: Check current cache state
print("\n[Step 1] Checking current cache state...")
for cache_dir in CACHE_DIRS:
    if os.path.isdir(cache_dir):
        files = [f for f in os.listdir(cache_dir) if f.endswith('.json')]
        print(f"  {cache_dir}: {len(files)} files")
    else:
        print(f"  {cache_dir}: NOT FOUND")

# Step 2: Fetch current water data to see what we have
print("\n[Step 2] Fetching current West Lake water data (from cache)...")
try:
    current_water = fetch_water(
        WEST_LAKE_SOUTH, WEST_LAKE_WEST,
        WEST_LAKE_NORTH, WEST_LAKE_EAST,
        use_cache=True
    )
    print(f"  Current: {len(current_water)} water features")
    
    # Count geometry types
    if not current_water.empty:
        types = current_water.geometry.type.value_counts()
        for geom_type, count in types.items():
            print(f"    {geom_type}: {count}")
        
        # Convert to projected CRS for area/length calculations
        current_water_proj = current_water.to_crs(epsg=3857)
        
        # Calculate areas
        polygons = current_water_proj[current_water_proj.geometry.type.isin(['Polygon', 'MultiPolygon'])]
        if not polygons.empty:
            total_area = polygons.geometry.area.sum()
            print(f"    Total polygon area: {total_area:,.0f} m² ({total_area/1e6:.2f} km²)")
        
        linestrings = current_water_proj[current_water_proj.geometry.type.isin(['LineString', 'MultiLineString'])]
        if not linestrings.empty:
            total_length = linestrings.geometry.length.sum()
            print(f"    Total linestring length: {total_length:,.0f} m ({total_length/1000:.1f} km)")
except Exception as e:
    print(f"  Error fetching current data: {e}")

# Step 3: Force re-download
print("\n[Step 3] Re-downloading West Lake water data from Overpass API...")
print("  (This may take 30-60 seconds)")
t0 = time.time()

try:
    fresh_water = fetch_water(
        WEST_LAKE_SOUTH, WEST_LAKE_WEST,
        WEST_LAKE_NORTH, WEST_LAKE_EAST,
        use_cache=False  # Force re-download
    )
    
    elapsed = time.time() - t0
    print(f"\n  Re-download complete in {elapsed:.1f}s")
    print(f"  Fresh: {len(fresh_water)} water features")
    
    # Count geometry types
    if not fresh_water.empty:
        types = fresh_water.geometry.type.value_counts()
        for geom_type, count in types.items():
            print(f"    {geom_type}: {count}")
        
        # Convert to projected CRS for area/length calculations
        fresh_water_proj = fresh_water.to_crs(epsg=3857)
        
        # Calculate areas
        polygons = fresh_water_proj[fresh_water_proj.geometry.type.isin(['Polygon', 'MultiPolygon'])]
        if not polygons.empty:
            total_area = polygons.geometry.area.sum()
            print(f"    Total polygon area: {total_area:,.0f} m² ({total_area/1e6:.2f} km²)")
        
        linestrings = fresh_water_proj[fresh_water_proj.geometry.type.isin(['LineString', 'MultiLineString'])]
        if not linestrings.empty:
            total_length = linestrings.geometry.length.sum()
            print(f"    Total linestring length: {total_length:,.0f} m ({total_length/1000:.1f} km)")
    
    # Save statistics - use projected CRS for accurate measurements
    fresh_water_proj = fresh_water.to_crs(epsg=3857) if not fresh_water.empty else fresh_water
    
    stats = {
        'bbox': {
            'south': WEST_LAKE_SOUTH,
            'west': WEST_LAKE_WEST,
            'north': WEST_LAKE_NORTH,
            'east': WEST_LAKE_EAST,
        },
        'feature_count': len(fresh_water),
        'geometry_types': {},
        'download_time_seconds': elapsed,
    }
    
    if not fresh_water.empty:
        stats['geometry_types'] = fresh_water.geometry.type.value_counts().to_dict()
        
        polygons = fresh_water_proj[fresh_water_proj.geometry.type.isin(['Polygon', 'MultiPolygon'])]
        if not polygons.empty:
            stats['total_polygon_area_m2'] = float(polygons.geometry.area.sum())
        
        linestrings = fresh_water_proj[fresh_water_proj.geometry.type.isin(['LineString', 'MultiLineString'])]
        if not linestrings.empty:
            stats['total_linestring_length_m'] = float(linestrings.geometry.length.sum())
    
    stats_file = os.path.join(_project_root, 'tmp', 'west_lake_water_stats.json')
    os.makedirs(os.path.dirname(stats_file), exist_ok=True)
    with open(stats_file, 'w', encoding='utf-8') as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)
    print(f"\n  Statistics saved to: {stats_file}")
    
except Exception as e:
    print(f"\n  ERROR during re-download: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

print("\n" + "=" * 60)
print("  Done!")
print("=" * 60)
