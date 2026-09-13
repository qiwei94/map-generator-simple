#!/usr/bin/env python3
"""Deterministic C/A/reference contact sheets; preserve source artwork."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

from PIL import Image, ImageDraw, ImageFont, ImageOps


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input-dir', type=Path, required=True)
    parser.add_argument('--reference', type=Path, required=True)
    args = parser.parse_args()
    out = args.input_dir / 'reference_comparison'
    out.mkdir(exist_ok=False)
    shutil.copy2(args.reference, out/'reference_original.png')
    items = [('C｜街区主导', args.input_dir/'paris_C_topdown.png'),
             ('A｜当前方案', args.input_dir/'paris_A_topdown.png'),
             ('参考 demo｜用户提供原图', out/'reference_original.png')]
    font = ImageFont.truetype('/System/Library/Fonts/STHeiti Medium.ttc', 38)
    small = ImageFont.truetype('/System/Library/Fonts/STHeiti Medium.ttc', 26)
    records = []
    for name, source in items:
        records.append({'label': name, 'source': str(source.resolve()),
                        'sha256': hashlib.sha256(source.read_bytes()).hexdigest()})
    for mode in ('full', 'center'):
        sheet = Image.new('RGB', (3660, 1340), '#f7f7f5')
        draw = ImageDraw.Draw(sheet)
        for i, (name, source) in enumerate(items):
            draw.text((i*1220+20, 14), name, fill='#222222', font=font)
            with Image.open(source) as original:
                image = original.convert('RGB')
            if mode == 'center':
                w, h = image.size
                image = image.crop((round(w*.40), round(h*.40), round(w*.60), round(h*.60)))
            image = ImageOps.contain(image, (1180, 1180), Image.Resampling.LANCZOS)
            sheet.paste(image, (i*1220+20+(1180-image.width)//2, 80+(1180-image.height)//2))
        note = ('整图等比缩放，保留原色与边框。A/C 同源同取景；参考的配色与渲染方式不同，未做地理配准。'
                if mode == 'full' else
                '各图中心 20% 局部，等比放大；参考仅为近似对应区域，不能作精确地理或毫米级比较。')
        draw.text((20, 1290), note, fill='#555555', font=small)
        sheet.save(out/f'C_A_reference_{mode}.png')
    (out/'comparison_evidence.json').write_text(json.dumps({
        'purpose': 'diagnostic', 'verdict': 'human_review', 'sources': records,
        'operations': ['equal panel size', 'aspect-preserving resize', 'center 20 percent crop in detail sheet only'],
        'no_recolouring': True, 'no_geometry_generation': True,
        'limitations': ['reference not georeferenced', 'reference rendering/palette differs from A/C',
                        'not a printability or mesh acceptance result']}, ensure_ascii=False, indent=2))
    print(out.resolve())


if __name__ == '__main__':
    main()
