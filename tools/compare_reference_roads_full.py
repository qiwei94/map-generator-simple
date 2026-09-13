"""Map reference road appearance onto the full registered Paris PNG, diagnostic only."""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy import ndimage as ndi
from compare_reference_road_mask import extract, ROOT


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--current', type=Path, help='Canonical PNG with adjacent .pipeline_runs ledger')
    ap.add_argument('--label', default='负空间道路＋连续性＋桥梁 v4')
    args = ap.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    base = ROOT/'output/paris_organization_20260905_v1'
    regpath = base/'registered_roi_3km_v1/registration.json'
    reg = json.loads(regpath.read_text())
    cp = args.current.resolve() if args.current else ROOT/'output/paris_negative_bridges_25km_20260905_v4/city_topdown.png'
    rp = Path(reg['sources'][1]['path'])
    sp = Path(reg['sources'][0]['path'])
    for entry in reg['sources']:
        assert hashlib.sha256(Path(entry['path']).read_bytes()).hexdigest() == entry['sha256']
    current = Image.open(cp).convert('RGB')
    reference = Image.open(rp).convert('RGB')
    source_size = Image.open(sp).size
    manifest = json.loads((base/'capture_manifest.json').read_text())
    if args.current:
        matching=[]
        for ledger_path in (cp.parent/'.pipeline_runs').glob('pipeline_state.*.json'):
            ledger=json.loads(ledger_path.read_text())
            if ledger['status'] != 'completed':
                continue
            context=ledger['stages'][0]['context_out']['value']
            if (np.allclose(context['bbox_wgs84'],manifest['bbox_wgs84'],rtol=0,atol=1e-8)
                    and abs(context['scale_mm_per_m']-manifest['scale_mm_per_m'])<1e-10):
                matching.append(ledger_path)
        if len(matching)!=1:
            raise ValueError('Require one completed ledger matching registered bbox and scale')
        metadata={'bbox_local_m':manifest['bbox_local_m'],'widths_mm':None,
                  'ledger':str(matching[0]),'label':args.label}
    else:
        metadata = json.loads((cp.parent/'report.json').read_text())
    assert np.allclose(metadata['bbox_local_m'], manifest['bbox_local_m'])
    a = np.array(reg['source_to_reference_affine']) @ np.diag(np.array(source_size)/current.size)
    off = np.array(reg['offset'])
    coeff = (a[0,0],a[0,1],off[0],a[1,0],a[1,1],off[1])
    # Extract at native reference resolution, not on an upsampled view.
    quad = np.array(reg['regions'][0]['reference_pixel_quad'])
    native_factor = np.linalg.norm(quad[1]-quad[0])/1000
    radius = 12*native_factor
    cand, uncertain = extract(np.asarray(reference), radius=radius,
                              min_component=max(2, round(12*native_factor**2)))
    valid = np.ones(cand.shape, dtype=bool)
    # Remove screenshot frame/margins; also mark out-of-reference output pixels.
    border = int(round(min(reference.size)*.025))
    valid[:border] = False; valid[-border:] = False
    valid[:,:border] = False; valid[:,-border:] = False
    def warp_mask(mask):
        return np.asarray(Image.fromarray(mask.astype('uint8')*255).transform(
            current.size, Image.Transform.AFFINE, coeff, Image.Resampling.NEAREST, fillcolor=0)) > 127
    valid = warp_mask(valid)
    cand, uncertain = warp_mask(cand)&valid, warp_mask(uncertain)&valid
    aligned = reference.transform(current.size, Image.Transform.AFFINE, coeff,
                                  Image.Resampling.BICUBIC, fillcolor=(235,225,245))
    cr = np.asarray(current); rr = np.asarray(aligned)
    cg = cr.mean(axis=2)
    gaps = (cg >= 130)&(cg <= 185)
    tolerance = max(1, round(6*3000/(manifest['bbox_local_m'][2]-manifest['bbox_local_m'][0])*current.width/1000))
    water = (cg < 60)|(rr.mean(axis=2)<60)
    water = ndi.binary_dilation(water, iterations=tolerance)
    uncertain |= cand&water
    cand &= ~water
    distance = ndi.distance_transform_edt(~gaps)
    near = cand&(distance <= tolerance)
    inside = cand&~near&(cg > 230)
    unknown = uncertain|(cand&~near&~inside)
    overlay = cr.astype(float)
    for mask,color in [(near,(0,165,205)),(inside,(235,45,75)),(unknown,(215,157,15))]:
        overlay[mask] = overlay[mask]*.18+np.array(color)*.82
    yy,xx = np.indices(valid.shape)
    hatch = ~valid & (((xx+yy)//10)%2 == 0)
    overlay[hatch] = .45*overlay[hatch]+.55*np.array([165,120,200])
    extracted = np.full_like(cr,250)
    extracted[cand] = (40,65,80); extracted[uncertain] = (230,185,75)
    extracted[~valid] = (235,225,245)
    Image.fromarray(overlay.astype('uint8')).save(args.output/'overlay_25km.png')
    Image.fromarray(extracted).save(args.output/'reference_road_candidates_25km.png')
    aligned.save(args.output/'reference_aligned_25km.png')
    Image.fromarray(cand.astype('uint8')*255).save(args.output/'candidate_mask.png')
    Image.fromarray(valid.astype('uint8')*255).save(args.output/'valid_mask.png')
    transparent = np.zeros((*valid.shape,4),dtype='uint8')
    transparent[cand] = (235,45,75,200)
    Image.fromarray(transparent).save(args.output/'candidate_transparent.png')
    font = ImageFont.truetype('/System/Library/Fonts/STHeiti Medium.ttc',28)
    small = ImageFont.truetype('/System/Library/Fonts/STHeiti Medium.ttc',22)
    sheet = Image.new('RGB',(2480,2620),'#f7f7f5'); draw=ImageDraw.Draw(sheet)
    draw.text((25,12),'巴黎完整 25 km｜参考路网候选映射（仅诊断，不改模型）',font=font,fill='#222')
    draw.text((25,51),'红：穿过我方白块；青：靠近灰色留空；黄：暂不判断；紫色斜纹：参考覆盖不足。',font=small,fill='#444')
    panels = [current,aligned,Image.fromarray(extracted),Image.fromarray(overlay.astype('uint8'))]
    titles = [f'现有整图：{args.label}','参考 demo：固定仿射映射到同范围','参考灰色走廊候选（非真实道路标签）','候选叠加到我方整图']
    for k,(im,title) in enumerate(zip(panels,titles)):
        x=25+(k%2)*1230; y=135+(k//2)*1230
        draw.text((x,y-38),title,font=font,fill='#222')
        sheet.paste(im.resize((1200,1200),Image.Resampling.LANCZOS),(x,y))
    draw.text((25,2580),'沿用局部配准，外围误差尚未逐路核验；红色不等于漏路；只比较外观，不修改生成结果。',font=small,fill='#555')
    sheet.save(args.output/'comparison_25km.png')
    report = {'purpose':'diagnostic','verdict':'human_review','production_changed':False,
              'registration':reg,'current_metadata':metadata,
              'sources':[{'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in [cp,rp,regpath]],
              'parameters':{'native_radius_px':radius,'native_border_px':border,'comparison_tolerance_px':tolerance},
              'valid_fraction':float(valid.mean()),'candidate_pixels':int(cand.sum()),
              'limitations':['Appearance masks are not semantic roads.','Existing affine fit is not locally verified across the full extent.',
                             'Gray regions also include open space; proximity is not proof of a matching street.',
                             'No print or geometry acceptance; current image is identified by path and hash.']}
    (args.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(args.output/'comparison_25km.png')


if __name__ == '__main__':
    main()
