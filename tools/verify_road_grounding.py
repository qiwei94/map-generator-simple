#!/usr/bin/env python3
"""Persist synthetic bank-deck and blocked-support reports for review."""
import argparse
from dataclasses import replace
import hashlib
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from shapely.geometry import LineString, box
from aesthetic.city_surface_plan import finalize_city_surfaces, materialize_road_surfaces
from aesthetic.geometry_inspection import build_geometry_inspection
from aesthetic.measurement_report import build_measurement_report, write_measurement_report
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import resolve_terrain_surface_plan
from _TEXTURE_STYLE_OF_DEEPSEEK.render_glb import _TerrainSurfacePlanSampler
from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf
from _TEXTURE_STYLE_OF_DEEPSEEK.print_profile import DEFAULT_PRINTER_PROFILE
from _TEXTURE_STYLE_OF_DEEPSEEK._layer_preprocess import LayerPolygons


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    terrain = resolve_terrain_surface_plan(np.zeros((3,3)),4.,4.,1.,max_surface_edge_mm=.4)
    ny,nx=terrain.surface_z_grid_mm.shape
    _,xx=np.mgrid[-2:2:complex(ny),-2:2:complex(nx)]
    z=1.+.08*xx-.8*np.maximum(0,1-np.abs(xx)/.6)
    terrain=replace(terrain,surface_z_grid_mm=z,
        fingerprint=hashlib.sha256(b'synthetic_bank_fixture_v1'+z.tobytes()).hexdigest())
    for name,bridge in [('supported',LineString([(-1.5,0),(1.5,0)])),
                        ('missing_bank',LineString([(-.2,0),(1.5,0)]))]:
        layers=LayerPolygons(WL=[box(-.5,-2,.5,2)],
            block_base_cut_lines=[LineString([(-2,0),(2,0)])],bridge_lines=[bridge])
        finalize_city_surfaces(layers,bbox_local=(-2,-2,2,2),scale=1.,
            printer_profile=DEFAULT_PRINTER_PROFILE,terrain_surface_plan=terrain)
        sampler=_TerrainSurfacePlanSampler(terrain,(-2,-2,2,2),1.)
        mesh=None
        if name=='supported':
            mesh,_=materialize_road_surfaces(layers,1.,sampler.z_mm_vec)
            mesh.export(args.output/'diagnostic_roads_only.glb')
            export_deepseek_3mf({'roads':mesh},str(args.output/'diagnostic_roads_only.3mf'))
        inspection=build_geometry_inspection(layers,{'roads':mesh} if mesh is not None else None)
        report=build_measurement_report(run={'city':f'合成桥岸小样：{name}（非城市／非打印成品）',
            'model_span_mm':4.,'node':'controller-local'},source_features={'source':'synthetic_no_download'},
            scene_character={},scene_policy={},generation_outcomes={'geometry_inspection':inspection},
            status='measurement_error' if inspection['status']=='error' else 'generated_pending_validation')
        print(write_measurement_report(args.output,report,stem=f'pipeline_measurement_report_{name}'))


if __name__=='__main__':
    main()
