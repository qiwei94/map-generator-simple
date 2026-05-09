#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Bridge filter test - Performance analysis"""

import os
import sys
import time

_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

print("=" * 60)
print("  Bridge Filter Test - Performance Analysis")
print("=" * 60)

# Test area: Hangzhou West Lake (small area for quick test)
LAT1, LON1 = 30.22, 120.12  # West Lake core area
LAT2, LON2 = 30.26, 120.16

print(f"\nTest area: ({LAT1}, {LON1}) -> ({LAT2}, {LON2})")
area_km2 = (LAT2 - LAT1) * 111 * (LON2 - LON1) * 111 * 0.866
print(f"Area size: {area_km2:.1f} km2")

# ========================================
# Step 1: Fetch road data
# ========================================
print("\n" + "=" * 60)
print("[Step 1] Fetch road data")
print("=" * 60)

t_start = time.time()
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import fetch_roads

roads_gdf = fetch_roads(LAT1, LON1, LAT2, LON2)
t_fetch_roads = time.time() - t_start

print(f"  Time: {t_fetch_roads:.1f}s")
if roads_gdf is not None:
    print(f"  Total roads: {len(roads_gdf)}")
    if 'highway' in roads_gdf.columns:
        highway_counts = roads_gdf['highway'].value_counts()
        print(f"  Road types:")
        for highway, count in highway_counts.head(5).items():
            print(f"    - {highway}: {count}")
else:
    print("  Failed to fetch road data")
    sys.exit(1)

# ========================================
# Step 2: Fetch water data
# ========================================
print("\n" + "=" * 60)
print("[Step 2] Fetch water data")
print("=" * 60)

t_start = time.time()
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import fetch_water

water_gdf = fetch_water(LAT1, LON1, LAT2, LON2)
t_fetch_water = time.time() - t_start

print(f"  Time: {t_fetch_water:.1f}s")
if water_gdf is not None and len(water_gdf) > 0:
    print(f"  Total water features: {len(water_gdf)}")
    water_area = water_gdf.geometry.area.sum()
    print(f"  Water area: {water_area:.1f} m2")
else:
    print("  No water data")

# ========================================
# Step 3: Bridge statistics (original)
# ========================================
print("\n" + "=" * 60)
print("[Step 3] Bridge statistics (original)")
print("=" * 60)

t_start = time.time()
from _TEXTURE_STYLE_OF_DEEPSEEK.bridge_filter import get_bridge_statistics

stats_original = get_bridge_statistics(roads_gdf, water_gdf)
t_stats = time.time() - t_start

print(f"  Time: {t_stats:.2f}s")
print(f"  Total roads: {stats_original['total_roads']}")
print(f"  Tagged as bridge=yes: {stats_original['tagged_bridges']}")
print(f"  Roads intersecting water: {stats_original['water_intersecting']}")
print(f"  Bridge length: {stats_original['bridge_length_m']:.1f} m")

# ========================================
# Step 4: Execute bridge filtering
# ========================================
print("\n" + "=" * 60)
print("[Step 4] Execute bridge filtering")
print("=" * 60)

t_start = time.time()
from _TEXTURE_STYLE_OF_DEEPSEEK.bridge_filter import filter_bridges_only

bridge_roads = filter_bridges_only(
    roads_gdf,
    water_gdf,
    extract_water_crossing_only=True
)
t_filter = time.time() - t_start

print(f"  Time: {t_filter:.2f}s")
print(f"  Input roads: {len(roads_gdf)}")
print(f"  Output bridges: {len(bridge_roads)}")
print(f"  Filter rate: {(1 - len(bridge_roads)/len(roads_gdf))*100:.1f}%")

if len(bridge_roads) > 0:
    bridge_length = bridge_roads.geometry.length.sum()
    print(f"  Bridge total length: {bridge_length:.1f} m")

    # Show bridge types
    if 'highway' in bridge_roads.columns:
        highway_counts = bridge_roads['highway'].value_counts()
        print(f"\n  Bridge types:")
        for highway, count in highway_counts.items():
            print(f"    - {highway}: {count}")

    # Show first 5 bridges
    print(f"\n  First 5 bridges:")
    for idx, row in bridge_roads.head(5).iterrows():
        highway = row.get('highway', 'unknown')
        length = row.geometry.length
        bridge_tag = row.get('bridge', 'no')
        print(f"    [{idx}] highway={highway}, length={length:.1f}m, bridge={bridge_tag}")

# ========================================
# Performance summary
# ========================================
print("\n" + "=" * 60)
print("Performance Summary")
print("=" * 60)

total_time = t_fetch_roads + t_fetch_water + t_stats + t_filter

print(f"\nTime breakdown:")
print(f"  [Step 1] Road fetch: {t_fetch_roads:.1f}s ({t_fetch_roads/total_time*100:.1f}%)")
print(f"  [Step 2] Water fetch: {t_fetch_water:.1f}s ({t_fetch_water/total_time*100:.1f}%)")
print(f"  [Step 3] Statistics: {t_stats:.2f}s ({t_stats/total_time*100:.1f}%)")
print(f"  [Step 4] Filtering: {t_filter:.2f}s ({t_filter/total_time*100:.1f}%)")
print(f"\nTotal time: {total_time:.1f}s")

print(f"\nFiltering results:")
print(f"  Input: {len(roads_gdf)} roads")
print(f"  Output: {len(bridge_roads)} bridges")
print(f"  Removed: {len(roads_gdf) - len(bridge_roads)} roads")

print(f"\nKey metrics:")
print(f"  - Bridge ratio: {len(bridge_roads)/len(roads_gdf)*100:.2f}%")
if len(bridge_roads) > 0:
    print(f"  - Avg bridge length: {bridge_length/len(bridge_roads):.1f}m")

print("\n" + "=" * 60)
print("Test Complete")
print("=" * 60)