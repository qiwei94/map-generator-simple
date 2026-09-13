#!/usr/bin/env python3
"""Small synthetic Z diagnostic. Not a city render or print-acceptance gate."""
import argparse
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
from shapely.geometry import Polygon
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import sample_terrain_surface_plan_z
from _TEXTURE_STYLE_OF_DEEPSEEK.prepared_surface import materialize_flat_surfaces
from aesthetic.surface_grounding import resolve_grounding, materialize_grounding


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    font_path = Path('/System/Library/Fonts/STHeiti Medium.ttc')
    if not font_path.exists():
        raise SystemExit('中文诊断图需要 STHeiti 字体；请明确提供运行环境字体后再生成')
    font = FontProperties(fname=str(font_path))
    yy, xx = np.mgrid[-2:2:17j, -2:2:17j]
    z = 1. + .35 * xx + .3 * np.exp(-2 * (xx*xx + yy*yy))
    terrain = SimpleNamespace(surface_z_grid_mm=z, width_m=4., height_m=4.,
                              scale_mm_per_m=1., fingerprint='synthetic-slope-hill-v1')
    poly = Polygon([(-1.6,-1.4),(1.6,-1.4),(1.6,1.4),(-1.6,1.4)],
                   holes=[[(-.3,-.4),(-.3,.4),(.3,.4),(.3,-.4)]])
    sampler = lambda x, y: sample_terrain_surface_plan_z(terrain, x, y)
    old, _ = materialize_flat_surfaces([poly], [.24], 1., sampler)
    records = []
    meshes = [old]
    for mode in ('draped_thickness', 'flat_roof_above_highest_support'):
        start = perf_counter()
        plan = resolve_grounding([poly], [.24], 1., terrain, [mode])
        mesh, proof = materialize_grounding(plan, [poly], [.24], 1.)
        elapsed = perf_counter() - start
        mesh.export(args.output / f'{mode}.glb')
        records.append(dict(plan=plan['evidence'], materialization=proof, seconds=elapsed))
        meshes.append(mesh)
    titles = ['旧：质心平挤出（悬空／穿坡）', '新：街块随地形保持厚度', '新：建筑底面贴地、屋顶水平']
    fig = plt.figure(figsize=(15, 8.5))
    for i, (mesh, title) in enumerate(zip(meshes, titles)):
        ax = fig.add_subplot(2, 3, i+1, projection='3d')
        ax.plot_surface(xx, yy, z, color='#c1b79f', alpha=.55, linewidth=0, antialiased=True)
        ax.add_collection3d(Poly3DCollection(mesh.triangles, facecolor='#679eaf',
            edgecolor='#325060', linewidth=.15, alpha=.92))
        ax.set(xlim=(-2,2), ylim=(-2,2), zlim=(0,2.4), xlabel='X / mm', ylabel='Y / mm', zlabel='Z / mm')
        ax.view_init(elev=20, azim=-115)
        ax.set_title(title, fontproperties=font, fontsize=12)
        bx = fig.add_subplot(2,3,i+4)
        # Exact shell/plane intersection: no invented illustrative roof curve.
        section = mesh.section(plane_origin=[0,-.8,0], plane_normal=[0,1,0])
        if section is not None:
            for curve in section.discrete:
                bx.plot(curve[:,0], curve[:,2], color='#217a99', linewidth=2)
        xs = np.linspace(-2,2,801)
        bx.plot(xs, sampler(xs, np.full_like(xs,-.8)), color='#675b3f', linewidth=2, label='地形')
        bx.set(xlim=(-2,2), ylim=(0,2.4), xlabel='X / mm', ylabel='Z / mm')
        bx.set_title('同位置剖面：蓝＝实体边界，棕＝地形', fontproperties=font, fontsize=11)
        bx.grid(alpha=.2)
    fig.suptitle('第 3 项贴地诊断｜同轮廓、同地形、同批准高度 0.24 mm\n合成小场景；不是西湖样品，也不是打印验收',
                 fontproperties=font, fontsize=16)
    fig.tight_layout(rect=(0,0,1,.92))
    path = args.output / 'grounding_comparison.png'
    fig.savefig(path, dpi=160)
    (args.output / 'report.json').write_text(json.dumps(dict(
        purpose='diagnostic', scene='synthetic_slope_hill_with_hole',
        node='controller-local', source_data='synthetic_no_download', model_span_mm=4,
        candidate_scope='urban_masses_only', records=records,
        limitations=['未验证真实城市', '未包含水体布尔裁切', '道路与桥面尚未修复', '未切片或试打印']),
        ensure_ascii=False, indent=2), encoding='utf-8')
    print(path)


if __name__ == '__main__':
    main()
