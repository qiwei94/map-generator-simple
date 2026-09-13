#!/usr/bin/env python3
"""Small-window C experiment: change negative corridors, never paint roads.

Reconstruct the recoverable pre-cut carrier from retained and recorded removed
surfaces. Already discarded tiny fragments cannot be recovered. This is a 2D
experiment, not a change to PrinterProfile, S6 defaults, or mesh acceptance.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from shapely.geometry import box
from shapely.ops import unary_union
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.evaluate_urban_organization import load_checked
from _TEXTURE_STYLE_OF_DEEPSEEK.block_base import _polygon_parts, enforce_final_block_base_clearance
from aesthetic.review_render import _Rasterizer, _rasterize_mask

WIDTHS = [('wide', '当前缝宽', .55, .84), ('medium', '约一半缝宽', .28, .42),
          ('fine', '约四分之一缝宽', .14, .21)]


def nearby(items, region):
    if not items:
        return []
    tree = STRtree(items)
    return [items[int(i)] for i in tree.query(region, predicate='intersects')]


def cut_carrier(carrier, local_lines, major_lines, scale, local_mm, major_mm):
    polys, _, local = enforce_final_block_base_clearance(
        list(_polygon_parts(carrier)), None, local_lines, scale, local_mm,
        min_piece_area_m2=0.)
    polys, _, major = enforce_final_block_base_clearance(
        polys, None, major_lines, scale, major_mm, min_piece_area_m2=0.)
    return unary_union(polys), {'local': local, 'major': major}


def draw_negative(urban, heroes, water, bridges, bounds, target, pixels=1600,
                  visible_water_mask=None):
    raster = _Rasterizer(bounds, pixels*2, pixels*2)
    def mask(geom):
        parts = list(_polygon_parts(geom))
        assert all(p.is_valid for p in parts)
        return _rasterize_mask(raster, parts).astype(bool)
    # Road is the substrate exposed by the missing city surface. No stroke
    # and no road-colour overlay: protected open spaces share the substrate.
    rgb = np.full((pixels*2, pixels*2, 3), (160, 160, 157), dtype=np.uint8)
    rgb[mask(unary_union([urban, heroes]))] = (247, 247, 245)
    if visible_water_mask is None:
        visible_water_mask = mask(water.difference(bridges))
    if visible_water_mask.shape != rgb.shape[:2]:
        raise ValueError('Water stencil must match supersampled output dimensions')
    rgb[visible_water_mask.astype(bool)] = (0, 0, 0)
    Image.fromarray(rgb).resize((pixels,pixels), Image.Resampling.LANCZOS).save(target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment-dir', type=Path, required=True)
    parser.add_argument('--output-name', default='negative_width_center_v1')
    args = parser.parse_args()
    start = time.monotonic()
    root = args.experiment_dir.resolve()
    if Path(args.output_name).name != args.output_name:
        raise ValueError('output name must be a single directory name')
    out = root/args.output_name
    out.mkdir(exist_ok=False)
    manifest = json.loads((root/'capture_manifest.json').read_text())
    scale = manifest['scale_mm_per_m']
    layers = load_checked(root/'C/layers.pkl')
    core = box(-2500,-2500,2500,2500)
    work = core.buffer(250, join_style=2)
    retained = unary_union(nearby(list(layers.block_base)+list(layers.BO), work)).intersection(work)
    removed = unary_union(nearby(layers.surface_road_reveals, work)).intersection(work)
    carrier = unary_union([retained, removed])
    heroes = unary_union(nearby([p for p,h in layers.BL],work)).intersection(work)
    water = unary_union(nearby(list(layers.WL)+list(layers.WO),work)).intersection(work)
    bridges = unary_union(nearby(layers.surface_road_polygons,work)).intersection(water)
    local = nearby(layers.block_base_cut_lines, work)
    major = nearby(layers.block_base_major_cut_lines, work)
    (out/'recoverable_carrier.wkb').write_bytes(carrier.wkb)
    results = []
    print(f'已载入局部：普通道路 {len(local)}，主干 {len(major)}，仅重裁切与画 PNG', flush=True)
    previous = None
    for name,label,local_mm,major_mm in WIDTHS:
        tick = time.monotonic()
        city, clearance = cut_carrier(carrier,local,major,scale,local_mm,major_mm)
        city = city.intersection(core)
        lost = previous.difference(city).area if previous is not None else 0.
        # This experiment accepts a sub-pixel aggregate overlap discrepancy
        # from polygon buffer/overlay operations, but records the raw area.
        # It is not a relaxation of the production geometry verifier.
        pixel_area = core.area / (1600 * 1600)
        if lost > pixel_area:
            raise ValueError(f'Narrower negative corridor unexpectedly loses urban area: {lost} m2')
        previous = city
        target = out/f'{name}.png'
        draw_negative(city,heroes,water,bridges,core.bounds,target)
        (out/f'{name}.wkb').write_bytes(city.wkb)
        result = {'id':name,'label':label,'local_gap_mm':local_mm,'major_gap_mm':major_mm,
            'local_ground_m':local_mm/scale,'major_ground_m':major_mm/scale,
            'urban_area_m2':city.area,'frame_coverage':city.area/core.area,
            'loss_from_wider_variant_m2':lost,
            'two_dimensional_overlap_tolerance_m2':pixel_area,
            'clearance':clearance,'elapsed_seconds':time.monotonic()-tick,
            'path':str(target),'sha256':hashlib.sha256(target.read_bytes()).hexdigest()}
        if name == 'wide':
            result['reconstruction_recut_difference_m2'] = city.symmetric_difference(retained.intersection(core)).area
        results.append(result)
        print(name, result['elapsed_seconds'], result['frame_coverage'], flush=True)
    sheet = Image.new('RGB',(3120,1190),'#f7f7f5')
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.truetype('/System/Library/Fonts/STHeiti Medium.ttc',34)
    small = ImageFont.truetype('/System/Library/Fonts/STHeiti Medium.ttc',25)
    for i, (name,label,l,m) in enumerate(WIDTHS):
        draw.text((i*1040+15,12),label,font=font,fill='#222222')
        draw.text((i*1040+15,58),f'普通 {l:.2f} / 主干 {m:.2f} mm',font=small,fill='#555555')
        with Image.open(out/f'{name}.png') as image:
            sheet.paste(image.resize((1010,1010),Image.Resampling.LANCZOS),(i*1040+15,105))
    draw.text((15,1140),'同一中心 5 km，沿用 25 km 模型比例；只切街区、不描道路；纯 PNG 审美实验，不代表可打印。',font=small,fill='#555555')
    sheet.save(out/'comparison.png')
    (out/'evidence.json').write_text(json.dumps({
        'purpose':'diagnostic','verdict':'human_review','city':'Paris','variant':'C',
        'input_sha256':(root/'C/layers.sha256').read_text().strip(),
        'bbox_local_m':list(core.bounds),'scale_mm_per_m':scale,
        'source_reconstruction':'retained C surfaces union recorded removed surfaces; no dilation of final pixels',
        'new_fragment_area_filter_m2':0,
        'palette':{'substrate':[160,160,157],'city':[247,247,245],'water':[0,0,0]},
        'fixed_water_sha256':hashlib.sha256(water.wkb).hexdigest(),
        'fixed_heroes_sha256':hashlib.sha256(heroes.wkb).hexdigest(),
        'fixed_bridges_sha256':hashlib.sha256(bridges.wkb).hexdigest(),
        'limitations':['Earlier unrecorded discarded tiny pieces are not reconstructed',
            'Inherited C carrier voids remain; only recorded final road cuts vary',
            'Gray open space is not necessarily a road',
            'Palette is common across three candidates, not the prior C paper-background PNG',
            'No 3D generation or print acceptance'],
        'results':results,'elapsed_seconds':time.monotonic()-start},ensure_ascii=False,indent=2))
    print(out,flush=True)


if __name__ == '__main__': main()
