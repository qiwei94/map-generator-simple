#!/usr/bin/env python3
"""Compose existing road-gap variants and an unregistered reference crop."""
import argparse
import hashlib
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input-dir', type=Path, required=True)
    parser.add_argument('--reference', type=Path, required=True)
    args = parser.parse_args()
    items = [
        ('当前缝宽', '普通 0.55 / 主干 0.84 mm', args.input_dir / 'wide.png'),
        ('约一半宽', '普通 0.28 / 主干 0.42 mm', args.input_dir / 'medium.png'),
        ('约四分之一宽', '普通 0.14 / 主干 0.21 mm', args.input_dir / 'fine.png'),
        ('参考 demo', '中心 20% 局部 · 未地理配准', args.reference),
    ]
    font = ImageFont.truetype('/System/Library/Fonts/STHeiti Medium.ttc', 38)
    small = ImageFont.truetype('/System/Library/Fonts/STHeiti Medium.ttc', 26)
    sheet = Image.new('RGB', (4160, 1190), '#f7f7f5')
    draw = ImageDraw.Draw(sheet)
    records = []
    for i, (title, subtitle, source) in enumerate(items):
        draw.text((i * 1040 + 20, 12), title, fill='#222222', font=font)
        draw.text((i * 1040 + 20, 60), subtitle, fill='#555555', font=small)
        with Image.open(source) as original:
            panel = original.convert('RGB')
        crop = None
        if i == 3:
            w, h = panel.size
            crop = [round(w * .4), round(h * .4), round(w * .6), round(h * .6)]
            panel = panel.crop(crop)
        panel = ImageOps.contain(panel, (1000, 1000), Image.Resampling.LANCZOS)
        sheet.paste(panel, (i * 1040 + 20 + (1000 - panel.width) // 2,
                           110 + (1000 - panel.height) // 2))
        records.append({'label': title, 'source': str(source.resolve()),
                        'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                        'crop_pixels': crop})
    draw.text((20, 1140), '前三列：同一 5 km 局部，沿用 25 km 模型比例，仅改变道路挖空宽度。参考保留原色，仅作观感对照，非精确同位置比较。',
              fill='#555555', font=small)
    output = args.input_dir / 'comparison_with_reference.png'
    sheet.save(output)
    output.with_suffix('.json').write_text(json.dumps({
        'purpose': 'diagnostic', 'verdict': 'human_review', 'sources': records,
        'operations': ['reference center crop', 'aspect-preserving resize', 'labels'],
        'no_recolouring': True, 'no_geometry_generation': True,
        'limitations': ['reference not georeferenced', 'different reference rendering and palette',
                        'PNG comparison does not validate printability'],
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    print(output.resolve())


if __name__ == '__main__':
    main()
