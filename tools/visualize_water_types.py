"""Visualize water feature distribution"""
import sys, os
sys.path.insert(0, '.')
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
import numpy as np

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm, project_geodataframe
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import fetch_water
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

# Create figure
fig, ax = plt.subplots(figsize=(12, 10))

# Plot polygons by type
polygons = water_proj[water_proj.geometry.type.isin(['Polygon', 'MultiPolygon'])].copy()

# River polygons (water=river) - RED
river_polys = polygons[polygons.get('water', '') == 'river']
for geom in river_polys.geometry:
    if geom.geom_type == 'MultiPolygon':
        for g in geom.geoms:
            x, y = g.exterior.coords.xy
            ax.fill(x, y, alpha=0.5, color='red', edgecolor='darkred', linewidth=0.5)
    else:
        x, y = geom.exterior.coords.xy
        ax.fill(x, y, alpha=0.5, color='red', edgecolor='darkred', linewidth=0.5)

# Lake polygons (water=lake) - BLUE
lake_polys = polygons[polygons.get('water', '') == 'lake']
for geom in lake_polys.geometry:
    if geom.geom_type == 'MultiPolygon':
        for g in geom.geoms:
            x, y = g.exterior.coords.xy
            ax.fill(x, y, alpha=0.5, color='blue', edgecolor='darkblue', linewidth=0.5)
    else:
        x, y = geom.exterior.coords.xy
        ax.fill(x, y, alpha=0.5, color='blue', edgecolor='darkblue', linewidth=0.5)

# Generic water polygons (natural=water only, no water tag) - CYAN
natural_only = polygons[(polygons.get('natural', '') == 'water') & (polygons.get('water', '') == '')]
for geom in natural_only.geometry:
    if geom.geom_type == 'MultiPolygon':
        for g in geom.geoms:
            x, y = g.exterior.coords.xy
            ax.fill(x, y, alpha=0.3, color='cyan', edgecolor='teal', linewidth=0.5)
    else:
        x, y = geom.exterior.coords.xy
        ax.fill(x, y, alpha=0.3, color='cyan', edgecolor='teal', linewidth=0.5)

# LineString (waterway=river) - GREEN lines with buffer visualization
linestrings = water_proj[water_proj.geometry.type == 'LineString']
river_lines = linestrings[linestrings.get('waterway', '') == 'river']
for geom in river_lines.geometry:
    x, y = geom.coords.xy
    ax.plot(x, y, color='green', linewidth=1, alpha=0.7)

# Add legend
legend_elements = [
    Patch(facecolor='red', alpha=0.5, edgecolor='darkred', label='Polygon: water=river'),
    Patch(facecolor='blue', alpha=0.5, edgecolor='darkblue', label='Polygon: water=lake'),
    Patch(facecolor='cyan', alpha=0.3, edgecolor='teal', label='Polygon: natural=water'),
    plt.Line2D([0], [0], color='green', linewidth=2, label='LineString: waterway=river'),
]
ax.legend(handles=legend_elements, loc='upper right', fontsize=10)

# Add grid and labels
ax.set_xlim(0, width_m)
ax.set_ylim(0, height_m)
ax.set_xlabel('X (meters)')
ax.set_ylabel('Y (meters)')
ax.set_title('Water Feature Distribution - Hangzhou West Lake Area\nRed: River Polygons (real shapes), Green: River Lines (need buffer)')
ax.grid(True, alpha=0.3)

# Add note
ax.text(0.02, 0.02, 'Red polygons are REAL river shapes from OSM\nGreen lines are centerlines (buffered with 60m width)',
        transform=ax.transAxes, fontsize=9, verticalalignment='bottom',
        bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.5))

plt.tight_layout()
output_path = 'tmp/water_types_map.png'
plt.savefig(output_path, dpi=150)
print('Map saved to:', output_path)
print('\nLegend:')
print('  RED = Polygon water=river (real river surface shape)')
print('  BLUE = Polygon water=lake')
print('  CYAN = Polygon natural=water (generic water)')
print('  GREEN lines = LineString waterway=river (centerlines, buffered 60m)')