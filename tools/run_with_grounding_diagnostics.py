#!/usr/bin/env python3
"""Run the canonical generator unchanged, recording failed grounding inputs.

The pickle is a trusted local diagnostic, not a resumable generation result.
No validation is bypassed and the original exception is re-raised.
"""
import argparse
from pathlib import Path
import pickle
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--diagnostic-dir', type=Path, required=True)
    parser.add_argument('generation_args', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    args.diagnostic_dir.mkdir(parents=True, exist_ok=False)
    import aesthetic.surface_grounding as grounding
    original = grounding._patch_mesh
    calls = 0

    def record(poly, terrain, **kwargs):
        nonlocal calls
        calls += 1
        try:
            return original(poly, terrain, **kwargs)
        except Exception:
            path = args.diagnostic_dir / ('grounding_failure_%d.pkl' % calls)
            with path.open('xb') as stream:
                pickle.dump({'poly': poly, 'terrain': terrain}, stream,
                            protocol=pickle.HIGHEST_PROTOCOL)
            print('[诊断] 贴地失败输入已保存：%s' % path, flush=True)
            raise

    grounding._patch_mesh = record
    import aesthetic.city_surface_plan as surface_plan
    original_finalize = surface_plan.finalize_city_surfaces

    def capture_surfaces(layers, **kwargs):
        path = args.diagnostic_dir / 'surface_inputs.pkl'
        with path.open('xb') as stream:
            pickle.dump((layers, kwargs), stream, protocol=pickle.HIGHEST_PROTOCOL)
        print('[诊断] S6 表面输入已保存，可独立复现贴地环节', flush=True)
        return original_finalize(layers, **kwargs)

    surface_plan.finalize_city_surfaces = capture_surfaces
    from generate_model import main as generate
    argv = args.generation_args
    return generate(argv[1:] if argv[:1] == ['--'] else argv)


if __name__ == '__main__':
    main()
