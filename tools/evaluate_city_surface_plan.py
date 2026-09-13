#!/usr/bin/env python3
"""Offline, bounded S3/S6/S7 regression from explicit local GeoJSON sources.

This is not a full-city pipeline or print acceptance run. All variants share
one source crop, physical scale, mass geometry and printer profile; only the
final road-cut collection differs. No source downloads or mesh generation.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import pickle
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import geopandas as gpd
from PIL import Image, ImageDraw, ImageFont
from _TEXTURE_STYLE_OF_DEEPSEEK._layer_preprocess import preprocess_layers
from _TEXTURE_STYLE_OF_DEEPSEEK.buildings import ROAD_TIERS
from _TEXTURE_STYLE_OF_DEEPSEEK.print_profile import DEFAULT_PRINTER_PROFILE as PROFILE
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm, project_geodataframe
from aesthetic.building_mass_strategy import apply_building_mass_to_layers
from aesthetic.city_surface_plan import finalize_city_surfaces
from aesthetic.review_render import render_review_bundle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('buildings', 'roads', 'water', 'scene-policy', 'output-dir'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--bbox', required=True, help='south,west,north,east focus crop')
    parser.add_argument('--scale', type=float, required=True, help='parent model mm/metre')
    parser.add_argument('--model-span-mm', type=float, default=196.0)
    args = parser.parse_args()
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=False)
    start = time.monotonic()
    south, west, north, east = map(float, args.bbox.split(','))
    projection = bbox_to_utm(south, west, north, east)
    origin, extent = projection['origin'], projection['utm_bbox']
    bbox = (extent[0] - origin[0], extent[1] - origin[1],
            extent[2] - origin[0], extent[3] - origin[1])
    sources = {}
    for name in ('buildings', 'roads', 'water'):
        path = getattr(args, name)
        source = gpd.read_file(path, bbox=(west, south, east, north))
        if name == 'buildings':
            from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import _estimate_building_heights
            source['est_height'] = _estimate_building_heights(source)
        sources[name] = project_geodataframe(
            source, projection['utm_crs'], origin, clip_bbox=extent)
        print(name, len(sources[name]), flush=True)
    layers = preprocess_layers(
        buildings_gdf=sources['buildings'], roads_gdf=sources['roads'],
        water_gdf=sources['water'], vegetation_gdf=None,
        bbox_local=bbox, scale=args.scale,
        merge_mode=True,
        area_km2=(bbox[2]-bbox[0])*(bbox[3]-bbox[1])/1e6,
        printer_profile=PROFILE,
        bbox_wgs84=(south, west, north, east),
        utm_crs=projection['utm_crs'], origin=origin)
    policy = json.loads(args.scene_policy.read_text())
    mass = apply_building_mass_to_layers(
        layers, sources['buildings'], sources['roads'], sources['water'], bbox,
        printer_profile=PROFILE, scene_policy=policy,
        topology_blocks=layers.city_blocks,
        topology_evidence=layers.road_roles.get('scale_aware_topology'),
        model_span_mm=args.model_span_mm, downstream_final_clearance=True)
    # Save trusted local intermediate for reproducible, inexpensive re-review.
    with (out / 's6_before_surface.pkl').open('wb') as stream:
        pickle.dump(layers, stream, protocol=pickle.HIGHEST_PROTOCOL)
    before = copy.deepcopy(layers)
    before.block_base_cut_lines = []
    before.block_base_major_cut_lines = []
    before.roads_lines = []
    full_cut = copy.deepcopy(layers)
    roads = sources['roads']
    full_cut.block_base_cut_lines = list(
        roads.loc[roads.highway.isin(ROAD_TIERS[3])].geometry)
    full_proof = finalize_city_surfaces(
        full_cut, bbox_local=bbox, scale=args.scale, printer_profile=PROFILE)
    proof = finalize_city_surfaces(
        layers, bbox_local=bbox, scale=args.scale, printer_profile=PROFILE)
    variants = []
    for name, variant in [('before', before), ('full_cut', full_cut), ('shared', layers)]:
        variants.append(render_review_bundle(
            variant, {'bbox_local': bbox, 'scale': args.scale},
            1.0, str(out), name)['topdown'])
    report = {
        'language': 'zh-CN', 'status': 'human_review_required',
        '说明': '巴黎局部同源对照；保持母模型打印比例，不等同于完整25公里构图或3MF验收。',
        'bbox_wgs84': [south, west, north, east], 'bbox_local_m': bbox,
        'scale_mm_per_m': args.scale, 'parent_model_span_mm': args.model_span_mm,
        'sources': {n: str(getattr(args, n).resolve()) for n in sources},
        'source_counts': {n: len(v) for n, v in sources.items()},
        'scope': 'standalone S3/S6 surface comparison; no terrain/hero-height runtime stages',
        'mass': mass, 'full_cut': full_proof, 'shared': proof,
        'elapsed_seconds': time.monotonic() - start,
        'formal_3mf_generated': False,
    }
    (out / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    write_contact_sheet(out)
    print(json.dumps({'full_cut': full_proof['after'], 'shared': proof['after'],
                      'elapsed_seconds': report['elapsed_seconds']}, ensure_ascii=False), flush=True)


def write_contact_sheet(out):
    """Rebuild only the presentation from persisted diagnostic images."""
    variants = [out / f'{name}_topdown.png' for name in ('before', 'full_cut', 'shared')]
    canvas = Image.new('RGB', (2160, 850), '#f7f7f5')
    draw = ImageDraw.Draw(canvas)
    candidates = [
        '/System/Library/Fonts/STHeiti Medium.ttc',
        '/System/Library/Fonts/PingFang.ttc',
        '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
        'C:/Windows/Fonts/msyh.ttc',
    ]
    font_path = next((p for p in candidates if Path(p).is_file()), None)
    if font_path is None:
        raise RuntimeError('需要已安装的中文字体；几何与报告已保存，无需重算')
    font = ImageFont.truetype(font_path, 26)
    labels = ['① 聚合后：尚未最终裁切', '② 反例：完整结构路网重切', '③ 新方案：只切已确认道路']
    for i, (path, label) in enumerate(zip(variants, labels)):
        with Image.open(path) as im:
            canvas.paste(im.convert('RGB').resize((710, 710)), (i*720+5, 75))
        draw.text((i*720+12, 22), label, font=font, fill='#222222')
    draw.text((15, 801), '同源局部对照，保持 25 km 母模型比例；不是完整城市成品，也不是打印验收。',
              font=font, fill='#444444')
    canvas.save(out / 'comparison_zh.png')


if __name__ == '__main__':
    main()
