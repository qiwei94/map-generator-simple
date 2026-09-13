"""Diagnostic raster road candidates; never a source of production geometry.

Reuse frozen Paris registration. Gray gaps in the reference may also be shadows,
terrain or open space; color classes below are appearance evidence, not labels.
"""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy import ndimage as ndi

ROOT = Path(__file__).resolve().parents[1]


def extract(rgb, radius=12, min_component=12):
    gray = rgb.mean(axis=2)
    neutral = np.ptp(rgb.astype(np.int16), axis=2) <= 8
    raw = neutral & (gray >= 105) & (gray <= 190)
    labels, _ = ndi.label(raw)
    sizes = np.bincount(labels.ravel())
    raw &= sizes[labels] >= min_component
    # Broad gray regions cannot confidently be called road corridors.
    wide = ndi.distance_transform_edt(raw) > radius
    uncertain = raw & ndi.binary_dilation(wide, iterations=max(1, round(radius)))
    return raw & ~uncertain, uncertain


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--output', required=True, type=Path)
    args = ap.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    base = ROOT / 'output/paris_organization_20260905_v1'
    ours = ROOT / 'output/paris_block_occupancy_20260906_v2'
    registration = base / 'registered_roi_3km_v1/registration.json'
    reg = json.loads(registration.read_text())
    font = ImageFont.truetype('/System/Library/Fonts/STHeiti Medium.ttc', 23)
    small = ImageFont.truetype('/System/Library/Fonts/STHeiti Medium.ttc', 19)
    sheet = Image.new('RGB', (2440, 2020), '#f7f7f5')
    draw = ImageDraw.Draw(sheet)
    draw.text((20, 10), '巴黎 3 km 局部｜参考路网候选 → 当前占用支撑 PNG（未改变模型）', fill='#222', font=font)
    draw.text((20, 43), '红：候选穿过我方白色块面；青：靠近我方灰色留空；黄：宽灰区或水岸，暂不判断。配准并非像素级真值。', fill='#444', font=small)
    records = []
    for i, region in enumerate(reg['regions']):
        rid = region['id']
        rp = base / f'registered_roi_bridges_v2/{rid}_reference.png'
        cp = ours / f'{rid}_1.png'
        ref = np.asarray(Image.open(rp).convert('RGB'))
        current = np.asarray(Image.open(cp).convert('RGB'))
        if ref.shape != current.shape or ref.shape[:2] != (1000, 1000):
            raise ValueError('Frozen comparison requires matching 1000 pixel frames')
        candidate, uncertain = extract(ref)
        cg = current.mean(axis=2)
        gaps = (cg >= 130) & (cg <= 185)
        water = (cg < 60) | (ref.mean(axis=2) < 60)
        water_band = ndi.binary_dilation(water, iterations=6)
        uncertain |= candidate & water_band
        candidate &= ~water_band
        distance = ndi.distance_transform_edt(~gaps)
        near = candidate & (distance <= 6)
        inside = candidate & ~near & (cg > 230)
        unknown = uncertain | (candidate & ~near & ~inside)
        overlay = current.copy().astype(float)
        for mask, color in [(near, (0, 165, 205)), (inside, (235, 45, 75)), (unknown, (215, 157, 15))]:
            overlay[mask] = .18 * overlay[mask] + .82 * np.array(color)
        extracted = np.full_like(current, 250)
        extracted[candidate] = (40, 65, 80)
        extracted[uncertain] = (230, 185, 75)
        panels = [current, ref, extracted, overlay.astype(np.uint8)]
        for j, (panel, title) in enumerate(zip(panels, ['我们的 PNG', '参考 demo', '提取的灰色走廊候选', '候选映射到我们的 PNG'])):
            y = 112 + i * 630
            draw.text((20 + j*605, y-28), f'{rid} {region["label"]}｜{title}', fill='#222', font=small)
            sheet.paste(Image.fromarray(panel).resize((590, 590), Image.Resampling.LANCZOS), (20+j*605, y))
        Image.fromarray(candidate.astype('uint8')*255).save(args.output/f'{rid}_road_candidate.png')
        Image.fromarray(uncertain.astype('uint8')*255).save(args.output/f'{rid}_uncertain.png')
        Image.fromarray(overlay.astype('uint8')).save(args.output/f'{rid}_overlay.png')
        rgba = np.zeros((*candidate.shape, 4), dtype=np.uint8)
        rgba[candidate] = (235, 45, 75, 200)
        Image.fromarray(rgba).save(args.output/f'{rid}_road_candidate_transparent.png')
        records.append({'id': rid, 'bbox_local_m': region['bbox_local_m'],
                        'candidate_pixels': int(candidate.sum()), 'uncertain_pixels': int(uncertain.sum()),
                        'candidate_inside_white_farther_than_6px': int(inside.sum()),
                        'near_current_gray_fraction_by_tolerance_px': {
                            str(t): float((candidate & (distance <= t)).sum()/max(1,candidate.sum()))
                            for t in [3,6,12]},
                        'sources': [{'path': str(p), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in [rp,cp]]})
    sheet.save(args.output/'comparison.png')
    report = {'purpose': 'diagnostic', 'verdict': 'human_review', 'registration': reg,
              'parameters': {'gray_range': [105,190], 'max_rgb_spread': 8, 'min_component_px': 12,
                             'wide_region_radius_px': 12, 'overlay_tolerance_px': 6},
              'scope': 'Three frozen 3 km windows; occupancy-support v2, without edge chamfer',
              'limitations': ['Gray candidates include non-road rendering; no semantic ground truth.',
                             'Affine water registration has unquantified local road error. Red is a discrepancy candidate, not a proven missing road.',
                             'Reference crop is upsampled from approximately 277 pixels to 1000; fine jaggedness is not evidence of deliberate random geometry.',
                             'Tolerance sweep is a display sensitivity check, not road recall or print accuracy.'],
              'production_geometry_changed': False, 'regions': records}
    (args.output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(args.output/'comparison.png')


if __name__ == '__main__':
    main()
