#!/usr/bin/env python3
"""Assemble a Chinese, evidence-linked contact sheet of completed experiments."""
import argparse
import hashlib
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
LABELS = {'A': 'A｜当前方案', 'B': 'B｜建筑群主导', 'C': 'C｜街区主导'}


def read(path):
    return json.loads(path.read_text())


def board(root, reference, field, target, font_path):
    width, height, margin = 680, 700, 20
    result = Image.new('RGB', (4 * width, height + 120), '#f5f4f1')
    draw = ImageDraw.Draw(result)
    font = ImageFont.truetype(str(font_path), 27)
    small = ImageFont.truetype(str(font_path), 19)
    items = [('参考 demo｜仅比较设计语言', reference / f'actual_{field}.png')]
    items += [(LABELS[v], root / v / f'{field}.png') for v in 'ABC']
    for i, (label, path) in enumerate(items):
        draw.text((i * width + margin, 15), label, font=font, fill='#202020')
        with Image.open(path) as source:
            image = source.convert('RGB')
            image.thumbnail((width - 2 * margin, height - 30), Image.Resampling.LANCZOS)
            result.paste(image, (i * width + (width-image.width)//2, 65 + (height-30-image.height)//2))
    draw.text((margin, height+65), '实际三角网格；不夸大 Z。A/B/C 同源同取景；参考未地理配准，不能按像素判断地理误差。', font=small, fill='#555555')
    result.save(target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment-dir', type=Path, required=True)
    parser.add_argument('--reference-dir', type=Path, required=True)
    parser.add_argument('--font', type=Path, default=Path('/System/Library/Fonts/STHeiti Medium.ttc'))
    args = parser.parse_args()
    root, reference = args.experiment_dir.resolve(), args.reference_dir.resolve()
    manifest = read(root / 'capture_manifest.json')
    metrics = {v: read(root/v/'measurements.json') for v in 'ABC'}
    render = {v: read(root/v/'render_evidence.json') for v in 'ABC'}
    evidence = {v: read(root/v/'s6_evidence.json') for v in 'ABC'}
    assert len({m['fixed_elements_sha256'] for m in metrics.values()}) == 1, 'Shared elements differ'
    for v in 'ABC':
        actual = hashlib.sha256((root/v/'experimental.glb').read_bytes()).hexdigest()
        assert actual == render[v]['glb_sha256'], f'GLB checksum mismatch: {v}'
    for view in ('topdown', 'oblique', 'center_detail'):
        board(root, reference, view, root/f'comparison_{view}.png', args.font)
    lines = ['# 巴黎 25 km：空间组织对照实验', '',
        '这是实验几何比较，不是正式 3MF 或打印验收。没有修改生产默认策略，也没有上线。', '',
        '## 比较条件', '',
        f'- 共用 S5 快照：`{manifest["sha256"]}`。',
        '- A 是当前方案的系统对照；B/C 共用额外的开放空间保护。B 与 C 才是更严格的组织方式对照。',
        '- 数据、取景、模型比例、地形、地标、水体及道路输入保持一致；最终可见道路面域可能因建筑占地改变。',
        '- 参考采用实际 3MF 网格渲染，不是宣传照片；参考 bbox 未地理配准，仅作设计语言比较。',
        '- 地表高程和方向光保持一致；渲染没有使用 QEM、随机抽样或额外 Z 拉伸。', '',
        '## 实际网格对比', '']
    for view, label in [('topdown','整图俯视'),('oblique','30° 斜视'),('center_detail','中心局部')]:
        lines += [f'### {label}', '', f'![{label}]({root/f"comparison_{view}.png"})', '']
    lines += ['## 几何测量（不是审美总分）', '',
        '| 指标 | A 当前 | B 建筑群 | C 街区 |', '|---|---:|---:|---:|']
    rows = [
        ('最终普通城市块面数', lambda v: f'{metrics[v]["polygon_count"]:,}'),
        ('普通城市块面占全幅面积', lambda v: f'{metrics[v]["union_coverage_of_frame"]:.1%}'),
        ('短轴 P50（mm，抽样）', lambda v: f'{metrics[v]["short_axis_mm"]["p50"]:.3f}'),
        ('短轴 P10（mm，抽样）', lambda v: f'{metrics[v]["short_axis_mm"]["p10"]:.3f}'),
        ('短轴低于彩色条带阈值（抽样）', lambda v: f'{metrics[v]["below_colored_strip_sample_fraction"]:.1%}'),
        ('长宽比超过 4（抽样）', lambda v: f'{metrics[v]["elongated_over_4_sample_fraction"]:.1%}'),
        ('S6 成功计算用时（秒）', lambda v: f'{evidence[v]["elapsed_seconds"]:.1f}'),
        ('Windows 网格与测量/视图用时（秒）', lambda v: f'{render[v]["elapsed_seconds"]:.1f}'),
    ]
    for name, value in rows:
        lines.append('| '+name+' | '+' | '.join(value(v) for v in 'ABC')+' |')
    lines += ['', '覆盖率按去重面域计算；宽度是最终裁切后的包围短轴抽样，不是逐点最小壁厚。',
        'S6 用时不能当作设备性能横评：A/B/C 算法不同，且部分阶段并发运行；失败重试另有日志。', '',
        '## 单组完整产物', '']
    for v in 'ABC':
        lines += [f'- {LABELS[v]}：[俯视]({root/v/"topdown.png"}) · [斜视]({root/v/"oblique.png"}) · '
                  f'[GLB]({root/v/"experimental.glb"}) · [测量原始值]({root/v/"measurements.json"}) · '
                  f'[严格挤出证据]({root/v/"render_evidence.json"})']
    lines += ['', '## 验收边界', '',
        '- 已核验共用要素哈希、同源快照和 GLB 校验和。几何挤出证据见各组 JSON。',
        '- 本轮没有 S11 正式导出、切片器验证、支撑/地形接触验证，不能据此宣称可打印。',
        '- 最终裁切可能再次产生窄片、碎片；短轴抽样结果不能被“道路留缝通过”掩盖。',
        '- 城市身份、密度、粒度与视觉秩序仍需看图判断；保留改善与回退两方面，不合并成一个分数。', '']
    (root/'报告.md').write_text('\n'.join(lines))
    print(root/'报告.md')


if __name__ == '__main__':
    main()
