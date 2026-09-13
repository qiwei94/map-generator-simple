#!/usr/bin/env python3
"""Explicit experimental recorder around canonical S6; no production defaults change.

The canonical run finishes normally in review mode. This records its exact S5
input and A/S6 output for offline organization comparisons, not a second source
pipeline. Pickles are trusted local artifacts and must never come from users.
"""
from __future__ import annotations
import argparse
from dataclasses import fields
import hashlib
import json
from pathlib import Path
import pickle
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def save_pickle(path, value):
    tmp = path.with_suffix('.part')
    with tmp.open('wb') as f:
        pickle.dump(value, f, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(path)
    path.with_suffix('.sha256').write_text(digest(path) + '\n')


def capture(context):
    from aesthetic.pipeline_domain import thaw_json, thaw_layer_containers
    runtime = {f.name: getattr(context.runtime, f.name) for f in fields(context.runtime)}
    runtime['amap_evidence'] = thaw_json(runtime['amap_evidence'])
    return {'schema': 'organization-s5-snapshot-v1', 'runtime': runtime,
            'layers': thaw_layer_containers(context.layers),
            'scene_character': thaw_json(context.scene_character),
            'scene_policy': thaw_json(context.scene_policy),
            'scene_character_fingerprint': context.scene_character_fingerprint,
            'scene_policy_fingerprint': context.scene_policy_fingerprint}


def restore(payload):
    from aesthetic.pipeline_domain import (RuntimeInputs, PipelineContextV3Runtime,
        PipelineContextV4Runtime, PipelineContextV5Runtime)
    v3 = PipelineContextV3Runtime(RuntimeInputs(**payload['runtime']), payload['layers'])
    v4 = PipelineContextV4Runtime(v3, payload['scene_character'], payload['scene_character_fingerprint'])
    return PipelineContextV5Runtime(v4, payload['scene_policy'], payload['scene_policy_fingerprint'])


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--experiment-dir', type=Path, required=True)
    p.add_argument('generation_args', nargs=argparse.REMAINDER)
    a = p.parse_args()
    out = a.experiment_dir.resolve()
    out.mkdir(parents=True, exist_ok=False)
    import generate_city_legacy as engine
    from generate_model import canonical_arguments
    from aesthetic.pipeline_domain import thaw_layer_containers, thaw_json
    original = engine.run_s6_building_roles

    def record(context, **kwargs):
        print('[组织实验] 保存同源 S5 快照，之后不再重复获取数据', flush=True)
        save_pickle(out / 's5_input.pkl', capture(context))
        (out / 'capture_manifest.json').write_text(json.dumps({
            'city': context.runtime.city, 'bbox_wgs84': context.runtime.bbox_wgs84,
            'bbox_local_m': context.runtime.bbox_local_m,
            'scale_mm_per_m': context.runtime.scale_mm_per_m,
            'source_counts': {f.name: (len(getattr(context.runtime.sources, f.name))
                if getattr(context.runtime.sources, f.name) is not None else 0)
                for f in fields(context.runtime.sources)},
            'sha256': digest(out / 's5_input.pkl'),
            'terrain_fingerprint': context.runtime.terrain_surface_plan.fingerprint,
            'scene_policy_fingerprint': context.scene_policy_fingerprint,
            'scope': 'read-only recording of canonical S5 input and S6 output',
            'formal_print_acceptance': False,
        }, ensure_ascii=False, indent=2))
        print('[组织实验] A 当前方案：开始 S6', flush=True)
        start = time.monotonic()
        result = original(context, **kwargs)
        (out / 'A').mkdir()
        save_pickle(out / 'A' / 'layers.pkl', thaw_layer_containers(result.layers))
        (out / 'A' / 's6_evidence.json').write_text(json.dumps({
            'variant': 'A', 'label': '当前 canonical 方案',
            'elapsed_seconds': time.monotonic() - start,
            'building_mass': thaw_json(result.building_mass_evidence),
            'height_hierarchy': thaw_json(result.height_hierarchy_evidence),
        }, ensure_ascii=False, indent=2))
        print('[组织实验] A S6 已完成并保存', flush=True)
        return result

    engine.run_s6_building_roles = record
    args = a.generation_args
    if args and args[0] == '--':
        args = args[1:]
    if not all(flag in args for flag in ('--draft', '--review-only', '--review-png')):
        raise ValueError('Baseline capture requires canonical review mode')
    try:
        engine.main(canonical_arguments(args))
    finally:
        engine.run_s6_building_roles = original


if __name__ == '__main__':
    main()
