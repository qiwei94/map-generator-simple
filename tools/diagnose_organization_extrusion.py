#!/usr/bin/env python3
"""Record the first strict materialization failure without relaxing its gate."""
import argparse
import json
from pathlib import Path
import sys

import numpy as np
from shapely.affinity import scale as scale_geometry
from shapely.geometry import Polygon
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.evaluate_urban_organization import load_checked
from _TEXTURE_STYLE_OF_DEEPSEEK import prepared_surface
from aesthetic.city_surface_plan import surface_heights


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment-dir', type=Path, required=True)
    parser.add_argument('--variant', choices=list('ABC'), required=True)
    args = parser.parse_args()
    root = args.experiment_dir
    layers = load_checked(root/args.variant/'layers.pkl')
    scale = json.loads((root/'capture_manifest.json').read_text())['scale_mm_per_m']
    original = prepared_surface.verify_flat_polygon
    counter = [0]

    def record(mesh, poly, scale, base, height):
        index = counter[0]
        counter[0] += 1
        try:
            return original(mesh, poly, scale, base, height)
        except ValueError as error:
            expected = scale_geometry(poly, xfact=scale, yfact=scale, origin=(0, 0))
            tops = mesh.triangles[mesh.face_normals[:,2] > .99, :, :2]
            actual = unary_union([Polygon(t) for t in tops])
            evidence = {'index': index, 'error': str(error), 'scale': scale,
                'base_mm': base, 'height_mm': height,
                'input_area_m2': poly.area, 'expected_area_mm2': expected.area,
                'area_error_mm2': actual.symmetric_difference(expected).area,
                'hausdorff_mm': actual.hausdorff_distance(expected),
                'boundary_limit_mm': max(1e-5, np.linalg.norm(mesh.extents[:2])*1e-6),
                'input_minimum_clearance_m': poly.minimum_clearance,
                'expected_bounds_mm': list(expected.bounds),
                'actual_bounds_mm': list(actual.bounds),
                'expected_vertices': len(poly.exterior.coords),
                'actual_vertices': len(mesh.vertices),
                'actual_top_type': actual.geom_type,
                'no_verifier_relaxation': True}
            out = root/args.variant/'extrusion_failure'
            out.mkdir(exist_ok=True)
            (out/'approved.wkb').write_bytes(poly.wkb)
            (out/'actual_top.wkb').write_bytes(actual.wkb)
            (out/'evidence.json').write_text(json.dumps(evidence, indent=2))
            print(json.dumps(evidence, indent=2), flush=True)
            raise

    prepared_surface.verify_flat_polygon = record
    try:
        prepared_surface.materialize_flat_surfaces(
            list(layers.block_base)+list(layers.BO), surface_heights(layers),
            scale, lambda x,y: np.zeros(len(x)))
    finally:
        prepared_surface.verify_flat_polygon = original


if __name__ == '__main__':
    main()
