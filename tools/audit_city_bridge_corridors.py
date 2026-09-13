"""Read-only bridge source/corridor audit against trusted experiment snapshots."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shapely.geometry import box
from shapely.ops import unary_union
from tools.evaluate_urban_organization import load_checked
from aesthetic.bridge_sources import extract_bridge_sources, water_bridge_lines, bridge_corridor

p = argparse.ArgumentParser()
p.add_argument('root', type=Path)
a = p.parse_args()
m = json.loads((a.root/'capture_manifest.json').read_text())
l = load_checked(a.root/'C/layers.pkl')
roads = load_checked(a.root/'s5_input.pkl')['runtime']['sources'].roads
l.bridge_lines, _ = extract_bridge_sources(roads)
physical = water_bridge_lines(l)
water = unary_union(list(l.WL)+list(l.WO))
core = box(*m['bbox_local_m'])
corridor = bridge_corridor(physical, m['scale_mm_per_m'], .42, core)
spans = unary_union(physical).intersection(water).intersection(core)
missing = spans.difference(corridor.buffer(1e-6))
bad = []
for g in physical:
    lost = g.intersection(water).intersection(core).difference(corridor.buffer(1e-6))
    if lost.length > 1e-7:
        bad.append({'line': g.wkt, 'missing': lost.wkt, 'length_m': lost.length})
print(json.dumps({'water_ratio':water.intersection(core).area/core.area,
    'water_bounds':water.bounds, 'water_bridge_parts':len(physical),
    'missing_from_buffer_m':missing.length, 'bad_sources':bad}, ensure_ascii=False))
