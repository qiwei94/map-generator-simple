"""Expand frozen ROI centers without fitting a new registration or geometry."""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registration', type=Path, required=True)
    parser.add_argument('--span-m', type=float, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.span_m <= 0:
        raise ValueError('span must be positive')
    report = json.loads(args.registration.read_text())
    images = []
    for source in report['sources']:
        path = Path(source['path'])
        assert hashlib.sha256(path.read_bytes()).hexdigest() == source['sha256']
        images.append(Image.open(path).convert('RGB'))
    src, ref = images
    matrix = np.array(report['source_to_reference_affine'])
    offset = np.array(report['offset'])
    fontpath = '/System/Library/Fonts/STHeiti Medium.ttc'
    font, small = ImageFont.truetype(fontpath, 30), ImageFont.truetype(fontpath, 23)
    args.output.mkdir(exist_ok=False)
    sheet = Image.new('RGB', (1480, 3 * 800 + 80), '#f7f7f5')
    draw = ImageDraw.Draw(sheet)
    draw.text((20, 15), '左：当前 C 原图　｜　右：参考 demo（沿用已保存配准）', font=font, fill='#222222')
    overview = Image.new('RGB', (1660, 900), '#f7f7f5')
    thumbs = [im.resize((800, 800)) for im in images]
    colors = ['#ed4c24', '#1475cc', '#9c36b5']
    for i, region in enumerate(report['regions']):
        old = np.array(region['bbox_local_m'], dtype=float)
        oldpixels = np.array(region['source_pixel_bounds'])
        center = (old[:2] + old[2:]) / 2
        bounds_m = np.r_[center - args.span_m / 2, center + args.span_m / 2]
        pixelcenter = (oldpixels[:2] + oldpixels[2:]) / 2
        halfpixels = (oldpixels[2:] - oldpixels[:2]) / (old[2:] - old[:2]) * args.span_m / 2
        bounds = np.r_[pixelcenter - halfpixels, pixelcenter + halfpixels]
        assert np.all(bounds[:2] >= 0) and np.all(bounds[2:] <= src.size)
        step = np.diag((bounds[2:] - bounds[:2]) / 700)
        current = src.transform((700,700), Image.Transform.AFFINE,
            (step[0,0],0,bounds[0],0,step[1,1],bounds[1]), Image.Resampling.BICUBIC)
        mat = matrix @ step; off = matrix @ bounds[:2] + offset
        aligned = ref.transform((700,700), Image.Transform.AFFINE,
            (mat[0,0],mat[0,1],off[0],mat[1,0],mat[1,1],off[1]), Image.Resampling.BICUBIC)
        draw.text((20,65+i*800), f"{region['id']} {region['label']}｜{args.span_m/1000:g} km × {args.span_m/1000:g} km",font=font,fill=colors[i])
        pair = Image.new('RGB', (1480,800), '#f7f7f5')
        pd = ImageDraw.Draw(pair)
        pd.text((20,12), f"{region['id']}｜当前 C",font=font,fill=colors[i])
        pd.text((760,12), '参考 demo｜3 km × 3 km',font=font,fill='#222222')
        for j, im in enumerate([current,aligned]):
            sheet.paste(im,(20+j*740,110+i*800)); pair.paste(im,(20+j*740,65))
        pair.save(args.output/f"{region['id']}_comparison.png")
        corners = np.array([[bounds[0],bounds[1]],[bounds[2],bounds[1]],
                            [bounds[2],bounds[3]],[bounds[0],bounds[3]]])
        quad = corners @ matrix.T + offset
        assert np.all(quad >= 0) and np.all(quad <= np.array(ref.size))
        for j, points in enumerate([corners,quad]):
            xy = [tuple(p) for p in points/np.array(images[j].size)*800]
            d = ImageDraw.Draw(thumbs[j]); d.line(xy+[xy[0]], fill=colors[i], width=3)
            d.text(xy[0],region['id'],font=font,fill=colors[i])
        region.update(bbox_local_m=bounds_m.tolist(), source_pixel_bounds=bounds.tolist(),
                      reference_pixel_quad=quad.tolist(), center_local_m=center.tolist())
    draw.text((20,2450),'只扩窗、不重跑；保留原色。配准存在残差，放大不增加原图细节，非毫米级精度验证。',font=small,fill='#555555')
    sheet.save(args.output/'roi_baselines_3km.png')
    od = ImageDraw.Draw(overview)
    for j, thumb in enumerate(thumbs):
        overview.paste(thumb,(20+j*820,65))
        od.text((20+j*820,15),['当前 C｜3 km 固定窗口','参考 demo｜同一窗口映射'][j],font=font,fill='#222222')
    overview.save(args.output/'roi_locations_3km.png')
    report.update(parent_registration=str(args.registration.resolve()), span_m=args.span_m,
                  registration_refitted=False, centers_changed=False)
    (args.output/'registration.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(args.output.resolve())


if __name__ == '__main__':
    main()
