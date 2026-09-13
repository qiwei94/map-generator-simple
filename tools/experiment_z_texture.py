"""Bounded PNG-only Z experiment on trusted local, frozen Paris S6 snapshots.

No production policy is changed. Vector XY stays frozen; a sampled height field
is used for comparison, not for export or print acceptance. Synthetic ground
texture is explicitly experimental, never inferred to be the reference method.
"""
import argparse
import ctypes
import hashlib
import json
from pathlib import Path
import pickle
import subprocess
import sys
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy.ndimage import distance_transform_edt, label as label_components
from shapely.geometry import box
from shapely import intersects_xy, prepare
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import sample_terrain_surface_plan_z
from tools.render_reference_actual_mesh import raster_library
from aesthetic.z_texture import choose_roof, continuous_field, ZTexturePolicy


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def ground_texture(xx, yy, allowed, pixel_mm, amplitude_mm=.07, wavelength_mm=.8):
    """Continuous model-coordinate field; zero outside semantic ground mask.

    Finite sum of phase-shifted waves, not measured DEM. Boundary fade prevents
    abrupt edges. Same coordinates give the same field on overlapping ROIs.
    """
    if amplitude_mm==0:return np.zeros_like(xx)
    field=continuous_field(xx,yy,ZTexturePolicy(amplitude_mm=amplitude_mm,wavelength_mm=wavelength_mm))
    edge = distance_transform_edt(np.pad(allowed, 1))[1:-1, 1:-1] * pixel_mm
    fade = np.clip((edge - pixel_mm) / .15, 0, 1)
    fade = fade * fade * (3 - 2 * fade)
    return field * fade * allowed


def render_height(xx, yy, zz, roles, output, elevation, origin_z, width=1400):
    """Common-camera depth-tested scientific surface render, no Z stretch."""
    n, m = zz.shape
    points = np.column_stack([xx.ravel()-xx.mean(), yy.ravel()-yy.mean(), zz.ravel()-origin_z])
    a = (np.arange(n-1)[:, None]*m + np.arange(m-1)).ravel()
    faces = np.vstack([np.column_stack([a,a+1,a+m]), np.column_stack([a+1,a+m+1,a+m])])
    # Row coordinates increase northward, giving upward-facing triangles.
    p = points[faces]
    normal = np.cross(p[:,1]-p[:,0], p[:,2]-p[:,0])
    normal /= np.maximum(np.linalg.norm(normal, axis=1)[:,None], 1e-12)
    theta = np.deg2rad(elevation)
    view = np.array([0.,-np.cos(theta),np.sin(theta)])
    up = np.array([0.,np.sin(theta),np.cos(theta)])
    light = np.array([-.4,-.5,.76]); light /= np.linalg.norm(light)
    palette = np.array([167., 248., 17., 102.])
    face_roles = roles.ravel()[faces[:,0]]
    gray = np.clip(palette[face_roles]*(.52+.48*np.maximum(normal@light,0)),0,255).astype(np.uint8)
    keep = normal@view > 0
    p, gray = p[keep], gray[keep]
    tri = np.stack([p[:,:,0],p@up,p@view],axis=-1)
    span = float(xx.max()-xx.min())
    xmin,xmax = -span/2-.4, span/2+.4
    # Fixed frame shared by all variants, including fixed landmarks.
    ymin,ymax = -span*np.sin(theta)/2-.6,span*np.sin(theta)/2+4.
    scale = width/(xmax-xmin); height=int(np.ceil((ymax-ymin)*scale))
    tri[:,:,0]=(tri[:,:,0]-xmin)*scale;tri[:,:,1]=(ymax-tri[:,:,1])*scale
    tri=np.ascontiguousarray(tri,dtype=np.float64);gray=np.ascontiguousarray(gray)
    depth=np.full((height,width),-np.inf,dtype=np.float64)
    rgb=np.full((height,width,3),245,dtype=np.uint8)
    lib=ctypes.CDLL(str(raster_library()))
    lib.raster.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_size_t,ctypes.c_int,
                         ctypes.c_int,ctypes.c_void_p,ctypes.c_void_p]
    lib.raster(tri.ctypes.data,gray.ctypes.data,len(tri),width,height,depth.ctypes.data,rgb.ctypes.data)
    Image.fromarray(rgb).save(output)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--layers',type=Path,required=True)
    p.add_argument('--inputs',type=Path,required=True)
    p.add_argument('--registration',type=Path,required=True)
    p.add_argument('--region',default='R3')
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--resolution',type=int,default=800)
    p.add_argument('--flat-relief-limit-mm',type=float,default=.24)
    p.add_argument('--texture-amplitude-mm',type=float,default=.07)
    p.add_argument('--texture-wavelength-mm',type=float,default=.8)
    args=p.parse_args()
    if args.resolution<200 or args.flat_relief_limit_mm<=0 or args.texture_amplitude_mm<0 or args.texture_wavelength_mm<=0:
        raise ValueError('Invalid experimental parameters')
    start=time.monotonic();args.output.mkdir(parents=True,exist_ok=False)
    registration=json.loads(args.registration.read_text())
    region=next(r for r in registration['regions'] if r['id']==args.region)
    core=box(*region['bbox_local_m'])
    with args.layers.open('rb') as stream: layers=pickle.load(stream)
    with args.inputs.open('rb') as stream: _,kwargs=pickle.load(stream)
    terrain=kwargs['terrain_surface_plan'];scale=kwargs['scale']
    assert layers.surface_grounding['city']['terrain_fingerprint']==terrain.fingerprint
    n=args.resolution;x0,y0,x1,y1=core.bounds
    dx=(x1-x0)*scale/n
    xs=(x0+(np.arange(n)+.5)*(x1-x0)/n)*scale
    ys=(y0+(np.arange(n)+.5)*(y1-y0)/n)*scale
    xx,yy=np.meshgrid(xs,ys)
    ground=sample_terrain_surface_plan_z(terrain,xx,yy)
    def mask(polygons):
        # Test actual sample centres against vector footprints. A paint-based
        # rasterizer can include an outside edge pixel whose terrain exceeds
        # the polygon's true support maximum, falsely making a flat roof dip.
        result=np.zeros((n,n),bool)
        for g in polygons:
            if not g.intersects(core):continue
            g=g.intersection(core)
            prepare(g)
            a,b,c,d=g.bounds
            ix=np.flatnonzero((xs>=a*scale)&(xs<=c*scale))
            iy=np.flatnonzero((ys>=b*scale)&(ys<=d*scale))
            if not len(ix) or not len(iy):continue
            selection=np.ix_(iy,ix)
            result[selection] |= intersects_xy(g,xx[selection]/scale,yy[selection]/scale)
        return result

    print('冻结地形加载完成；计算局部水体和街块掩膜',flush=True)
    A=ground.copy();B=ground.copy();roles=np.zeros((n,n),np.uint8)
    water=mask(list(layers.WL)+list(layers.WO))
    # Explicit fixed visual proxy only: formal bank-connected water/bridges are
    # not reconstructed here and are not part of this Z comparison's verdict.
    for poly in list(layers.WL)+list(layers.WO):
        if not poly.intersects(core):continue
        m=mask([poly])
        if m.any():A[m]=B[m]=float(np.min(ground[m]))-.02
    roles[water]=2
    bridges=mask(layers.surface_road_polygons)&water
    for poly in layers.surface_road_polygons:
        if not poly.intersects(core):continue
        m=mask([poly])&water
        if m.any():A[m]=B[m]=float(np.max(ground[m]))+.05
    roles[bridges]=3
    patches=layers.surface_grounding['city']['patches']
    polys=list(layers.block_base)+list(layers.BO)
    stats=[];urban=np.zeros((n,n),bool)
    for idx,(poly,patch) in enumerate(zip(polys,patches)):
        if not poly.intersects(core):continue
        m=mask([poly])&~water
        if not m.any():continue
        bottom,top=np.asarray(patch['bottom_z']),np.asarray(patch['top_z'])
        if patch['mode']=='draped_thickness':z=ground[m]+float(np.median(top-bottom))
        else:z=np.full(m.sum(),float(np.max(top)))
        A[m]=np.maximum(A[m],z)
        roof,policy=choose_roof(bottom,top,patch['mode'],args.flat_relief_limit_mm)
        B[m]=np.maximum(B[m],z if roof is None else roof)
        roles[m]=1;urban|=m
        stats.append(dict(source_polygon=idx,policy=policy,underfoot_range_mm=float(np.ptp(bottom)),
                          sample_pixels=int(m.sum()),top_A_range_mm=float(np.ptp(z)),
                          top_B_range_mm=float(np.ptp(z)) if roof is None else 0.,
                          max_lift_mm=0. if roof is None else float(np.max(roof-z))))
    for (poly,h),patch in zip(layers.BL,layers.surface_grounding['landmarks']['patches']):
        if not poly.intersects(core):continue
        m=mask([poly])&~water
        if m.any():
            z=float(np.max(patch['top_z']));A[m]=np.maximum(A[m],z);B[m]=np.maximum(B[m],z)
            roles[m]=1;urban|=m
    vegetation=[g for g in list(layers.VL)+list(layers.VO) if g.intersects(core)]
    greens=mask(vegetation)
    # Preserve source-selected road gaps, not only currently drawn bridges.
    roads=[g for g in list(layers.block_base_cut_lines)+list(layers.block_base_major_cut_lines)
           if g.intersects(core.buffer(50))]
    road_clearance=mask([unary_union(roads).buffer(.16/scale)])
    allowed=greens&~urban&~water&~road_clearance
    bump=ground_texture(xx,yy,allowed,dx,args.texture_amplitude_mm,args.texture_wavelength_mm)
    C=B+bump
    assert np.array_equal(A[water],B[water]) and np.array_equal(B[water],C[water])
    assert np.array_equal(B[~allowed],C[~allowed])
    assert np.all(B>=A-1e-9) and np.max(bump)<=args.texture_amplitude_mm+1e-9
    print('局部冻结完成，街块数',len(stats),'纹理覆盖',float(allowed.mean()),flush=True)
    frozen=time.monotonic()
    np.savez_compressed(args.output/'height_fields.npz',x_mm=xs,y_mm=ys,ground=ground,
                        A=A,B=B,C=C,roles=roles,texture_mask=allowed,texture_delta=bump)
    origin_z=float(min(A.min(),B.min(),C.min()))
    for label,z in [('A',A),('B',B),('C',C)]:
        for elevation in (30,18):
            render_height(xx,yy,z,roles,args.output/f'{label}_oblique_{elevation}.png',elevation,origin_z)
        print(label,'斜视完成',flush=True)

    components,count=label_components(allowed)
    sizes=np.bincount(components.ravel());sizes[0]=0
    rows,cols=np.where(components==int(np.argmax(sizes))) if count else (np.array([n//2]),np.array([n//2]))
    cy,cx=int(np.median(rows)),int(np.median(cols))
    half=int(round(n/6))  # 1 km detail within the same frozen 3 km crop
    cy=int(np.clip(cy,half,n-half));cx=int(np.clip(cx,half,n-half))
    detail=np.s_[cy-half:cy+half,cx-half:cx+half]
    detail_origin=float(min(A[detail].min(),B[detail].min(),C[detail].min()))
    for key,z in [('A',A),('B',B),('C',C)]:
        render_height(xx[detail],yy[detail],z[detail],roles[detail],
                      args.output/f'{key}_green_detail.png',30,detail_origin,width=1100)

    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.font_manager import FontProperties
    font=FontProperties(fname='/System/Library/Fonts/STHeiti Medium.ttc')
    # Select strongest measurable green-texture section rather than inventing
    # a visually flattering crop; record the exact row for reproducibility.
    row=int(np.argmax(bump.sum(axis=1))) if np.any(bump) else n//2
    fig,ax=plt.subplots(2,1,figsize=(15,5),sharex=True)
    colors=['#666666','#d46a32','#217798']
    for label,z,color in zip(['A 当前随地形','B 平顶街块','C 平顶＋地表微纹理'],[A,B,C],colors):
        ax[0].plot(xs-xs[0],z[row],label=label,color=color,lw=1)
    ax[0].plot(xs-xs[0],ground[row],color='#bbb',lw=.7,label='冻结地形')
    ax[0].legend(prop=font,ncol=4);ax[0].set_ylabel('模型 Z / mm',fontproperties=font)
    ax[1].plot(xs-xs[0],C[row]-B[row],color=colors[2]);ax[1].set_ylim(-.003,args.texture_amplitude_mm+.003)
    ax[1].set_ylabel('C − B / mm',fontproperties=font);ax[1].set_xlabel('剖面模型距离 / mm',fontproperties=font)
    for a in ax:a.grid(alpha=.2)
    fig.suptitle(f'同一剖面：局部北向坐标 {ys[row]/scale:.1f} m；下图单独显示微起伏差值',fontproperties=font)
    fig.tight_layout();fig.savefig(args.output/'sections.png',dpi=150);plt.close(fig)
    # Chinese evidence board: all three views use identical frame and lighting.
    ft=ImageFont.truetype('/System/Library/Fonts/STHeiti Medium.ttc',30)
    small=ImageFont.truetype('/System/Library/Fonts/STHeiti Medium.ttc',23)
    board=Image.new('RGB',(2100,1510),'#f7f7f5');draw=ImageDraw.Draw(board)
    draw.text((25,15),'巴黎 R3｜同一 3 km 局部、同一 XY 街块与街缝，只改变 Z',font=ft,fill='#222')
    draw.text((25,60),f'沿用整城比例：局部宽 {(x1-x0)*scale:.2f} mm；斜视 Z 不放大；非正式 3MF / 非切片验收',font=small,fill='#555')
    labels=['A 当前随地形定厚','B 低坡街块改平顶','C B＋绿地表面微起伏']
    for i,label in enumerate(labels):
        draw.text((25+700*i,115),label,font=ft,fill='#222')
        for j,e in enumerate((30,18)):
            im=Image.open(args.output/f'{"ABC"[i]}_oblique_{e}.png');im.thumbnail((680,370))
            board.paste(im,(10+700*i,170+j*380))
            draw.text((20+700*i,155+j*380),f'仰角 {e}°｜同光照',font=small,fill='#555')
    im=Image.open(args.output/'sections.png');im.thumbnail((2020,530));board.paste(im,(40,970))
    board.save(args.output/'comparison_zh.png')
    detail_board=Image.new('RGB',(2100,1000),'#f7f7f5');dd=ImageDraw.Draw(detail_board)
    dd.text((25,20),'同一绿地近景｜约 1 km · Z 不放大 · A / B / C',font=ft,fill='#222')
    dd.text((25,70),'C 为有界合成纹理实验，不代表参考作者算法；地标高度保持原样，尚未切片验收。',font=small,fill='#555')
    for i,title in enumerate(labels):
        dd.text((25+700*i,130),title,font=ft,fill='#222')
        im=Image.open(args.output/f'{"ABC"[i]}_green_detail.png');im.thumbnail((690,820))
        detail_board.paste(im,(5+700*i,180))
    detail_board.save(args.output/'green_detail_comparison_zh.png')
    # Plan view explicitly marks the semantic mask / exact section location.
    fig,axs=plt.subplots(1,3,figsize=(15,5))
    extent=[x0,x1,y0,y1]
    axs[0].imshow(roles,origin='lower',extent=extent,cmap=matplotlib.colors.ListedColormap(['#aaa','#eee','#111','#666']),vmin=0,vmax=3)
    axs[0].axhline(ys[row]/scale,color='#d46a32');axs[0].set_title('固定 XY；橙线为剖面',fontproperties=font)
    v=axs[1].imshow(B-A,origin='lower',extent=extent,cmap='magma',vmin=0,vmax=max(args.flat_relief_limit_mm,.01));fig.colorbar(v,ax=axs[1],label='mm')
    axs[1].set_title('B − A：仅低坡块顶抬平',fontproperties=font)
    v=axs[2].imshow(bump,origin='lower',extent=extent,cmap='viridis',vmin=0,vmax=max(args.texture_amplitude_mm,.01));fig.colorbar(v,ax=axs[2],label='mm')
    axs[2].set_title('C − B：仅绿地允许区',fontproperties=font)
    fig.tight_layout();fig.savefig(args.output/'masks_and_deltas_zh.png',dpi=150);plt.close(fig)
    report=dict(verdict='human_review',purpose='diagnostic',city='Paris',region=region,
        node='controller',code_revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        working_tree='dirty; production edits pre-exist; this tool is opt-in only',
        inputs=[dict(path=str(q.resolve()),sha256=digest(q)) for q in [args.layers,args.inputs,args.registration]],
        tool_sha256=digest(__file__),terrain_fingerprint=terrain.fingerprint,
        scale_mm_per_m=scale,model_crop_width_mm=(x1-x0)*scale,sampling_pitch_mm=dx,
        xy_vector_fingerprint=layers.surface_grounding['city']['input_geometry_fingerprint'],
        shared_roles_sha256=hashlib.sha256(roles.tobytes()).hexdigest(),
        parameters=dict(max_whole_block_relief_mm=args.flat_relief_limit_mm,
            texture_amplitude_mm=args.texture_amplitude_mm,wavelength_mm=args.texture_wavelength_mm,
            texture_boundary_fade_mm=.15,reference_algorithm_claim=False),
        water_and_bridge_proxy='fixed across A/B/C, water local minimum terrain minus .02 mm; bridge maximum plus .05 mm; not formal bank-connected output',
        counts={k:sum(s['policy']==k for s in stats) for k in set(s['policy'] for s in stats)},
        texture=dict(mask_area_fraction=float(allowed.mean()),max_delta_mm=float(bump.max()),
                     p95_delta_mm=float(np.percentile(bump[allowed],95)) if allowed.any() else 0.,
                     source='frozen VL/VO footprint candidates minus urban, water and road buffer; no trees enabled'),
        checks=dict(water_unchanged=True,texture_outside_allowed_zero=True,xy_selection_unchanged=True,
                    whole_polygon_support_used=True,production_defaults_changed=False),
        section=dict(row=row,y_local_m=float(ys[row]/scale)),blocks=stats,
        detail_bbox_local_m=[float(xs[cx-half]/scale),float(ys[cy-half]/scale),
                             float(xs[cx+half-1]/scale),float(ys[cy+half-1]/scale)],
        synthetic_field='24 deterministic phase-shifted directions; seed 20260908; tanh bound; experimental only',
        timings_seconds=dict(load_crop_and_height_fields=frozen-start,total=time.monotonic()-start),
        limitations=['sampled 2.5D diagnostic; height discontinuities interpolated across one grid cell',
            'not a watertight export; not slicer validation; no formal bridge comparison',
            'reference 3MF supplied design evidence, not co-registered 3D geometry',
            'synthetic texture is a hypothesis, not source terrain detail or proof of reference author method'])
    (args.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({k:report[k] for k in ['counts','texture','timings_seconds']},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
