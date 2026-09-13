"""Compare same registered PNG windows; keep reference original palette."""
import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--before', required=True, type=Path)
    p.add_argument('--after', required=True, type=Path)
    a = p.parse_args()
    before = json.loads((a.before/'evidence.json').read_text())
    after = json.loads((a.after/'evidence.json').read_text())
    for key in ('registration_sha256', 'input_sha256', 'scale_mm_per_m'):
        assert before[key] == after[key], f'Non-comparable {key}'
    assert after['physical_bridge_repair']
    font_path = '/System/Library/Fonts/STHeiti Medium.ttc'
    font = ImageFont.truetype(font_path, 30)
    small = ImageFont.truetype(font_path, 22)
    board = Image.new('RGB', (2520, 2850), '#f7f7f5')
    draw = ImageDraw.Draw(board)
    draw.text((20, 10), '巴黎塞纳河桥梁｜相同 3 km 窗口 · 普通留缝 0.28 / 主干及桥面 0.42 mm', font=font, fill='#222222')
    metrics = []
    for index, (old, new) in enumerate(zip(before['regions'], after['regions'])):
        assert old['region'] == new['region']
        rid = new['region']['id']
        plan = new['results'][0]['bridge_plan']
        assert plan['missing_source_water_span_m'] < 1e-5
        assert plan['unexpected_water_pixels'] == 0
        strip = Image.new('RGB', (2520, 920), '#f7f7f5')
        d = ImageDraw.Draw(strip)
        for col, (title, path) in enumerate([
            ('修复前：桥面被水覆盖', a.before/f'{rid}_medium.png'),
            ('修复后：源数据桥梁贯通', a.after/f'{rid}_medium.png'),
            ('参考 demo', a.after/f'{rid}_reference.png'),
        ]):
            d.text((col*840+20, 10), f'{rid} {new["region"]["label"]}｜{title}', font=font, fill='#222222')
            d.text((col*840+20, 55), '同一位置、比例、配准；参考配色不变', font=small, fill='#555555')
            with Image.open(path) as im:
                strip.paste(im.convert('RGB').resize((800,800), Image.Resampling.LANCZOS), (col*840+20, 95))
        strip.save(a.after/f'{rid}_bridge_before_after_reference.png')
        board.paste(strip, (0,60+index*920))
        metrics.append({'id':rid, 'missing_source_water_span_m':plan['missing_source_water_span_m'],
                        'bridge_water_area_m2':plan['bridge_water_area_m2'],
                        'unexpected_water_pixels':plan['unexpected_water_pixels']})
    draw.text((20,2820), '真实桥面来自原始 bridge 标签；不凭桥名猜测。仅局部 PNG 验证，未部署、未进行巴黎 3MF 打印验收。', font=small, fill='#555555')
    board.save(a.after/'bridge_before_after_reference.png')
    (a.after/'bridge_comparison.json').write_text(json.dumps({
        'purpose':'diagnostic', 'verdict':'human_review', 'regions':metrics,
        'scope':'three registered 3km Paris PNG windows; existing continuity repair retained',
        'limitations':['not a whole-city run', 'reference affine registration residual remains',
                       'not bridge structural engineering or print validation'],
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
