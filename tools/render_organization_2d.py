#!/usr/bin/env python3
"""Render saved A/C S6 geometry only; no extraction, strategy or mesh work."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.evaluate_urban_organization import load_checked, fixed_digest
from aesthetic.review_render import render_review_bundle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment-dir', type=Path, required=True)
    args = parser.parse_args()
    root = args.experiment_dir.resolve()
    manifest = json.loads((root/'capture_manifest.json').read_text())
    out = root/'review_2d_AC'
    out.mkdir(exist_ok=False)
    started = time.monotonic()
    records, images = {}, {}
    for variant in ('A', 'C'):
        tick = time.monotonic()
        layers = load_checked(root/variant/'layers.pkl')
        bundle = render_review_bundle(layers,
            {'bbox_local': manifest['bbox_local_m'], 'scale': manifest['scale_mm_per_m']},
            4., str(out), f'paris_{variant}', vegetation_enabled=False)
        records[variant] = {
            'input_sha256': (root/variant/'layers.sha256').read_text().strip(),
            'fixed_elements_sha256': fixed_digest(layers),
            'topdown': bundle['topdown'],
            'topdown_sha256': hashlib.sha256(Path(bundle['topdown']).read_bytes()).hexdigest(),
            'seconds_including_load': time.monotonic()-tick,
            'ordinary_polygons': len(layers.BO)+len(layers.block_base),
        }
        images[variant] = Image.open(bundle['topdown']).convert('RGB')
        del bundle, layers
        print(variant, records[variant], flush=True)
    assert records['A']['fixed_elements_sha256'] == records['C']['fixed_elements_sha256']
    font = ImageFont.truetype('/System/Library/Fonts/STHeiti Medium.ttc', 28)
    xmin, ymin, xmax, ymax = manifest['bbox_local_m']
    width, height = images['A'].size
    crop = (round((-2500-xmin)/(xmax-xmin)*width), round((ymax-2500)/(ymax-ymin)*height),
            round((2500-xmin)/(xmax-xmin)*width), round((ymax+2500)/(ymax-ymin)*height))
    board = Image.new('RGB', (1640, 900), '#f7f7f5')
    draw = ImageDraw.Draw(board)
    for i, (variant, title) in enumerate([('A', 'A｜当前方案'), ('C', 'C｜街区主导')]):
        draw.text((i*820+20, 15), title, font=font, fill='#202020')
        local = images[variant].crop(crop)
        local.save(out/f'paris_{variant}_center_5km.png')
        local.thumbnail((800, 800))
        local = local.resize((800, 800), Image.Resampling.NEAREST)
        board.paste(local, (i*820+10, 65))
    board.save(out/'AC_center_comparison.png')
    (out/'evidence.json').write_text(json.dumps({
        'purpose': 'diagnostic', 'verdict': 'human_review',
        'representation': '2D raster of saved approved S6 polygons; NOT an actual mesh render',
        'bbox_wgs84': manifest['bbox_wgs84'], 'bbox_local_m': manifest['bbox_local_m'],
        'scale_mm_per_m': manifest['scale_mm_per_m'], 'variants': records,
        'crop_local_m': [-2500, -2500, 2500, 2500],
        'crop_note': 'same central 5 km window of full-frame output, not a rerun at a new physical scale; 2x nearest enlargement',
        'comparability': 'A system control; C also has explicit open-space protection',
        'no_new_s6_run': True, 'no_3d_acceptance': True,
        'height_image_note': 'auxiliary relative building-height raster; not terrain or oblique validation',
        'renderer_sha256': hashlib.sha256((ROOT/'aesthetic/review_render.py').read_bytes()).hexdigest(),
        'elapsed_seconds': time.monotonic()-started}, ensure_ascii=False, indent=2))
    print(out, flush=True)


if __name__ == '__main__':
    main()
