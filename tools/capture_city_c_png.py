"""Explicit offline C candidate: current S0-S5/S6 handoffs, then negative PNG.

Not the default production strategy. All network sockets are disabled in this
process: use node-local PBF/DEM/caches, never silently download overseas data.
"""
import argparse
from dataclasses import fields
import json
import os
from pathlib import Path
import socket
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tools.capture_organization_baseline import capture, save_pickle, digest


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--experiment-dir',type=Path,required=True)
    p.add_argument('generation_args',nargs=argparse.REMAINDER)
    a=p.parse_args(); out=a.experiment_dir.resolve()
    out.mkdir(parents=True,exist_ok=False)
    os.environ['OVERTURE_AUTO_DOWNLOAD']='0'
    def offline(*args,**kwargs):
        raise OSError('Explicit offline sample run: network disabled; require cached data')
    socket.socket.connect=offline
    socket.create_connection=offline
    import generate_city_legacy as engine
    from generate_model import canonical_arguments
    from aesthetic.pipeline_domain import thaw_layer_containers, thaw_json
    from aesthetic.organization_experiment import adapter
    original=engine.run_s6_building_roles
    def record(context,**kwargs):
        print('[C样品] 保存当前S5输入，跳过A/B候选',flush=True)
        save_pickle(out/'s5_input.pkl',capture(context))
        (out/'capture_manifest.json').write_text(json.dumps({
            'city':context.runtime.city, 'bbox_wgs84':context.runtime.bbox_wgs84,
            'bbox_local_m':context.runtime.bbox_local_m,'scale_mm_per_m':context.runtime.scale_mm_per_m,
            'source_counts':{f.name:(len(getattr(context.runtime.sources,f.name))
                if getattr(context.runtime.sources,f.name) is not None else 0) for f in fields(context.runtime.sources)},
            'sha256':digest(out/'s5_input.pkl'),
            'scope':'current canonical S0-S5 with explicit experimental C adapter at S6; PNG only',
        },ensure_ascii=False,indent=2))
        start=time.monotonic()
        result=original(context,apply_mass=adapter('C',context.runtime.sources),**kwargs)
        (out/'C').mkdir()
        save_pickle(out/'C/layers.pkl',thaw_layer_containers(result.layers))
        (out/'C/s6_evidence.json').write_text(json.dumps({
            'variant':'C','elapsed_seconds':time.monotonic()-start,
            'building_mass':thaw_json(result.building_mass_evidence)},ensure_ascii=False,indent=2))
        return result
    engine.run_s6_building_roles=record
    args=a.generation_args[1:] if a.generation_args[:1]==['--'] else a.generation_args
    if not all(x in args for x in ('--draft','--review-only','--review-png')):
        raise ValueError('PNG candidate requires --draft --review-only --review-png')
    try:
        engine.main(canonical_arguments(args))
    finally:
        engine.run_s6_building_roles=original
    from tools.render_negative_city_png import render
    render(out,out/'negative_bridges')


if __name__=='__main__':main()
