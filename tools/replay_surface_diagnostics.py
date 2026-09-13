#!/usr/bin/env python3
"""Replay trusted local S6 snapshots; never publish this as a formal run."""
import argparse
import pickle
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('input', type=Path)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=False)
    import aesthetic.surface_grounding as grounding
    original = grounding._patch_mesh
    calls = 0

    def record(poly, terrain):
        nonlocal calls
        calls += 1
        if calls % 1000 == 0:
            print('Grounding polygons:', calls, flush=True)
        try:
            return original(poly, terrain)
        except Exception:
            with (a.output / 'failed_patch.pkl').open('xb') as stream:
                pickle.dump({'poly': poly, 'terrain': terrain}, stream)
            raise

    grounding._patch_mesh = record
    with a.input.open('rb') as stream:
        layers, kwargs = pickle.load(stream)
    from aesthetic.city_surface_plan import finalize_city_surfaces
    evidence = finalize_city_surfaces(layers, **kwargs)
    with (a.output / 'resolved_surfaces.pkl').open('xb') as stream:
        pickle.dump(layers, stream, protocol=pickle.HIGHEST_PROTOCOL)
    print('S6 surfaces resolved:', evidence['output_polygons'], evidence['numerical_cleanup'], flush=True)


if __name__ == '__main__':
    main()
