#!/usr/bin/env python3
"""Generate real S6/S8 checks for a tiny synthetic fixture, not a city pass."""
from pathlib import Path
import argparse
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from shapely.geometry import box
from aesthetic.city_surface_plan import finalize_city_surfaces, materialize_city_role
from aesthetic.geometry_inspection import build_geometry_inspection
from aesthetic.measurement_report import build_measurement_report, write_measurement_report
from _TEXTURE_STYLE_OF_DEEPSEEK._layer_preprocess import LayerPolygons
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import resolve_terrain_surface_plan
from _TEXTURE_STYLE_OF_DEEPSEEK.render_glb import _TerrainSurfacePlanSampler
from _TEXTURE_STYLE_OF_DEEPSEEK.print_profile import DEFAULT_PRINTER_PROFILE


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    layers = LayerPolygons(block_base=[box(-1.5,-1.5,-.25,1.5)],
                          BO=[box(.25,-1.5,1.5,1.5)], BO_heights=[.4])
    terrain = resolve_terrain_surface_plan(np.array([[0.,1.,2.],[1.,2.,2.],[0.,1.,2.]]), 4.,4.,1.)
    finalize_city_surfaces(layers, bbox_local=(-2,-2,2,2), scale=1.,
                          printer_profile=DEFAULT_PRINTER_PROFILE, terrain_surface_plan=terrain)
    sampler = _TerrainSurfacePlanSampler(terrain, (-2,-2,2,2), 1.)
    mesh, _ = materialize_city_role(layers, 'city', 1., sampler.z_mm_vec)
    for stage, meshes in [('s7', None), ('s8', {'block_base':mesh})]:
        report = build_measurement_report(run={'city':'合成坡地小样（不是城市成品）', 'model_span_mm':4},
            source_features={'source':'synthetic_no_download'}, scene_character={}, scene_policy={},
            generation_outcomes={'geometry_inspection':build_geometry_inspection(layers, meshes)},
            status='measured' if stage=='s7' else 'generated_pending_validation')
        print(write_measurement_report(args.output, report, stem=f'pipeline_measurement_report_{stage}'))


if __name__ == '__main__':
    main()
