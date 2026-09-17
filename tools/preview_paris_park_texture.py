#!/usr/bin/env python3
"""Build a park-texture proof from the frozen Paris S6 inputs."""
from pathlib import Path
import pickle
import math
import sys
import json
import numpy as np
from PIL import Image, ImageDraw
from shapely.geometry import Point

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aesthetic.park_texture import build_park_texture_mesh
from aesthetic.z_texture import ZTexturePolicy, _polygons, allowed_ground


def main():
    source = ROOT / 'output/paris_BC_full_20260914_v3_inputs/surface_inputs.pkl'
    output = ROOT / 'output/paris_park_texture_proof_v1'
    output.mkdir(exist_ok=True)
    with source.open('rb') as f:
        layers, inputs = pickle.load(f)
    mesh, report = build_park_texture_mesh(
        layers, inputs['bbox_local'], inputs['scale'], inputs['terrain_surface_plan'])
    if mesh is None:
        raise RuntimeError(report)
    base = Image.open(ROOT / 'output/paris_BC_full_20260914_v9/paris_BC_full_20260914_v9_topdown.png').convert('RGB')
    draw = ImageDraw.Draw(base)
    xmin, ymin, xmax, ymax = inputs['bbox_local']; scale = inputs['scale']
    # Every prism has four top vertices followed by four bottom vertices;
    # draw the top quads in print-white over the existing gray park ground.
    verts = mesh.vertices
    for start in range(0, len(verts), 8):
        xy = verts[start + 4:start + 8, :2] / scale
        pixels = [((x - xmin) / (xmax - xmin) * base.width,
                   (ymax - y) / (ymax - ymin) * base.height) for x, y in xy]
        draw.polygon(pixels, fill=(245, 245, 242))
    # Park-local paths are intentionally drawn only where OSM lines intersect
    # the protected green geometry.  They will become shallow gray channels
    # in the exported mesh, rather than an extra black city-road layer.
    green = allowed_ground(layers, inputs['bbox_local'], scale, ZTexturePolicy())
    roads = inputs['source_roads']
    wanted = {'path', 'service', 'cycleway', 'track'}
    path_count = 0
    # Only large parks need this treatment at 25 km scale.  Querying each
    # component keeps the spatial index selective; querying the full
    # multipolygon envelope would touch almost every Paris road.
    for park in sorted(_polygons(green), key=lambda p: p.area, reverse=True)[:40]:
        if park.area * scale * scale < 1.5:
            continue
        ids = roads.sindex.query(park, predicate='intersects')
        candidates = roads.iloc[np.unique(ids)].query('highway in @wanted').copy()
        candidates['park_length_m'] = candidates.geometry.intersection(park).length
        # The raw OSM path graph contains every branch and service spur.  At
        # this scale retain a small long-route skeleton only; it is a visual
        # park hierarchy, not turn-by-turn navigation.
        candidates = candidates.nlargest(6, 'park_length_m')
        endpoints = []
        for geom in candidates.geometry:
            if geom is None or geom.is_empty:
                continue
            clipped = geom.intersection(park)
            lines = getattr(clipped, 'geoms', [clipped])
            for line in lines:
                if line.geom_type != 'LineString' or line.length < 30:
                    continue
                pts = [((x - xmin) / (xmax - xmin) * base.width,
                        (ymax - y) / (ymax - ymin) * base.height) for x, y in line.coords]
                draw.line(pts, fill=(110, 110, 107), width=2)
                path_count += 1
                endpoints.extend((line.coords[0], line.coords[-1]))
        # Aesthetic boundary portals: connect only path ends close to a park
        # boundary to a nearby external city road.  The quadratic control point
        # follows the path tangent, avoiding an invented straight cross-park road.
        urban = {'residential', 'tertiary', 'secondary', 'primary', 'service', 'pedestrian'}
        linked = 0
        for ex, ey in endpoints:
            if linked >= 2 or park.boundary.distance(Point(ex, ey)) > 170:
                continue
            nearby = roads.iloc[np.unique(roads.sindex.query(Point(ex, ey).buffer(180), predicate='intersects'))]
            nearby = nearby[nearby.highway.isin(urban)]
            if nearby.empty:
                continue
            target_lines = []
            for geom in nearby.geometry:
                if geom is None:
                    continue
                target_lines.extend([part for part in getattr(geom, 'geoms', [geom])
                                     if part.geom_type == 'LineString'])
            if not target_lines:
                continue
            target = min(target_lines, key=lambda geom: geom.distance(Point(ex, ey)))
            a, b = target.coords[0], target.coords[-1]
            tx, ty = min((a, b), key=lambda p: (p[0]-ex)**2+(p[1]-ey)**2)
            if not 12 < math.hypot(tx-ex, ty-ey) < 180:
                continue
            ctrl = ((2*ex+tx)/3, (2*ey+ty)/3)
            curve=[]
            for t in np.linspace(0, 1, 12):
                x=(1-t)**2*ex+2*(1-t)*t*ctrl[0]+t*t*tx; y=(1-t)**2*ey+2*(1-t)*t*ctrl[1]+t*t*ty
                curve.append(((x-xmin)/(xmax-xmin)*base.width,(ymax-y)/(ymax-ymin)*base.height))
            draw.line(curve, fill=(110,110,107), width=2)
            linked += 1
    base.save(output / 'paris_park_texture_proof_topdown.png')
    report.update(source=str(source), model_mesh='separate white printable prisms',
                  park_path_candidates=path_count)
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    mesh.export(output / 'paris_park_texture_white_mesh.stl')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
