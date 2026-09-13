"""Read-only actual-mesh comparison; orthographic projection, no Z exaggeration.

All visible triangles are drawn (no random sampling/QEM).  References use the
city assembly, not the separate labelled backing board.  Mesh and assembly
transforms are applied.  Flat directional shading is diagnostic, not a photo.
"""
import argparse
import json
from pathlib import Path
import sys
import ctypes
import hashlib
import subprocess
from zipfile import ZipFile
from xml.etree import ElementTree as ET

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.analyze_reference_3mf_style import _parse_transform, _volume_settings


def local(tag):
    return tag.rsplit('}', 1)[-1]


def raster_library():
    source = Path(__file__).with_name('mesh_depth_raster.c')
    fingerprint = hashlib.sha256(source.read_bytes()).hexdigest()[:12]
    target = Path(__file__).resolve().parents[1] / 'tmp' / ('mesh_depth_' + fingerprint + ('.dylib' if sys.platform == 'darwin' else '.so'))
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        subprocess.run(['cc', '-O3', '-shared', '-fPIC', str(source), '-o', str(target), '-lm'], check=True)
    return target


def meshes_from_file(path, reference=False):
    with ZipFile(path) as archive:
        root = ET.fromstring(archive.read('3D/3dmodel.model'))
        objects = {e.attrib['id']: e for e in root.iter() if local(e.tag) == 'object'}
        items = [e for e in root.iter() if local(e.tag) == 'item']
        item = max(items, key=lambda e: sum(local(c.tag) == 'component' for c in objects[e.attrib['objectid']].iter()))
        assembly = objects[item.attrib['objectid']]
        transforms = {}
        wanted = {}
        for e in assembly.iter():
            if local(e.tag) == 'component':
                oid = int(e.attrib['objectid'])
                transforms[oid] = _parse_transform(item.attrib.get('transform')) @ _parse_transform(e.attrib.get('transform'))
                source = next(v for k,v in e.attrib.items() if local(k) == 'path').lstrip('/')
                wanted.setdefault(source, set()).add(oid)
        volumes = _volume_settings(archive)
        raw = []
        for source, ids in wanted.items():
            with archive.open(source) as stream:
                oid = None; verts = []; faces = []
                for event,e in ET.iterparse(stream, events=('start','end')):
                    kind = local(e.tag)
                    if event == 'start' and kind == 'object':
                        oid = int(e.attrib['id']); verts = []; faces = []
                    elif event == 'end' and kind == 'vertex':
                        if oid in ids: verts.append(tuple(float(e.attrib[k]) for k in ('x','y','z')))
                        e.clear()
                    elif event == 'end' and kind == 'triangle':
                        if oid in ids: faces.append(tuple(int(e.attrib[k]) for k in ('v1','v2','v3')))
                        e.clear()
                    elif event == 'end' and kind == 'object':
                        if oid in ids: raw.append((oid,verts,faces))
                        oid = None; e.clear()
        result = []
        for oid, verts, triangles in raw:
            name = volumes.get(oid, {}).get('name', f'object_{oid}')
            vertices = np.asarray(verts, dtype=np.float64)
            faces = np.asarray(triangles, dtype=np.int32)
            if not len(vertices) or not len(faces):
                continue
            transform = transforms.get(oid, np.eye(4))
            vertices = vertices @ transform[:3, :3].T + transform[:3, 3]
            if reference:
                role = {1: 'urban_relief', 2: 'terrain', 3: 'negative_backing'}.get(volumes.get(oid, {}).get('extruder'), 'terrain')
            elif any(s in name.lower() for s in ('building', 'block', 'landmark')):
                role = 'urban_relief'
            elif 'water' in name.lower():
                role = 'water'
            elif 'road' in name.lower():
                role = 'road'
            else:
                role = 'terrain'
            result.append((name, vertices, faces, role))
    return result


def render(meshes, path, elevation, pixel_size=2400, crop=None, frame_width_mm=None):
    lo = np.min([v.min(axis=0) for _, v, _, _ in meshes], axis=0)
    hi = np.max([v.max(axis=0) for _, v, _, _ in meshes], axis=0)
    origin = np.array([(lo[0] + hi[0])/2, (lo[1] + hi[1])/2, lo[2]])
    angle = np.deg2rad(elevation)
    view = np.array([0., -np.cos(angle), np.sin(angle)])
    up = np.array([0., np.sin(angle), np.cos(angle)])
    right = np.array([1., 0., 0.])
    light = np.array([-0.4, -0.5, 0.76]); light /= np.linalg.norm(light)
    palette = {'urban_relief': 248., 'terrain': 167., 'road': 102., 'water': 17., 'negative_backing': 17.}
    triangles, colors, depths = [], [], []
    for name, v, f, role in meshes:
        p = (v - origin)[f]
        normals = np.cross(p[:, 1]-p[:, 0], p[:, 2]-p[:, 0])
        norm = np.linalg.norm(normals, axis=1)
        visible = (norm > 1e-10) & (normals @ view > 1e-10)
        if crop is not None:
            x0, y0, x1, y1 = crop
            visible &= ((p[:,:,0].max(axis=1) >= x0) & (p[:,:,0].min(axis=1) <= x1) &
                        (p[:,:,1].max(axis=1) >= y0) & (p[:,:,1].min(axis=1) <= y1))
        p = p[visible]
        normals = normals[visible] / norm[visible, None]
        shade = 0.52 + 0.48 * np.maximum(normals @ light, 0.)
        colors.append(np.clip(palette[role] * shade, 0, 255).astype(np.uint8))
        triangles.append(np.stack((p @ right, p @ up, p @ view), axis=-1))
        depths.append(p.mean(axis=1) @ view)
    t = np.concatenate(triangles); c = np.concatenate(colors); d = np.concatenate(depths)
    if crop is None:
        half_width = 105. if frame_width_mm is None else float(frame_width_mm)/2
        xmin, xmax = -half_width, half_width
        ymin, ymax = float(t[:,:,1].min())-4, float(t[:,:,1].max())+4
    else:
        xmin, ymin, xmax, ymax = crop
        if elevation != 90:
            raise ValueError('Crops currently top-down only')
    width = pixel_size
    height = int(round(width * (ymax-ymin)/(xmax-xmin)))
    scale = width/(xmax-xmin)
    t[:,:,0] = (t[:,:,0]-xmin)*scale
    t[:,:,1] = (ymax-t[:,:,1])*scale
    t=np.ascontiguousarray(t,dtype=np.float64);c=np.ascontiguousarray(c,dtype=np.uint8)
    depth=np.full((height,width),-np.inf,dtype=np.float64)
    rgb=np.empty((height,width,3),dtype=np.uint8);rgb[:]=(245,244,241)
    lib=ctypes.CDLL(str(raster_library()))
    lib.raster.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_size_t,ctypes.c_int,ctypes.c_int,ctypes.c_void_p,ctypes.c_void_p]
    lib.raster(t.ctypes.data,c.ctypes.data,len(t),width,height,depth.ctypes.data,rgb.ctypes.data)
    Image.fromarray(rgb).save(path)
    return {'path': str(path), 'elevation_degrees': elevation, 'z_exaggeration': 1.,
            'visible_triangles': len(t), 'pixels_per_mm': scale, 'bounds_mm': [lo.tolist(), hi.tolist()],
            'warning': 'Diagnostic directional shading with per-pixel depth testing; not a photo; reference bbox is not georeferenced.'}


def main():
    p=argparse.ArgumentParser();p.add_argument('input',type=Path);p.add_argument('--output-dir',type=Path,required=True)
    p.add_argument('--reference',action='store_true');a=p.parse_args()
    a.output_dir.mkdir(parents=True,exist_ok=True)
    meshes=meshes_from_file(a.input,a.reference)
    if not meshes: raise RuntimeError('No mesh geometry')
    report={'source':str(a.input.resolve()),'objects':[
        {'name':n,'vertices':len(v),'faces':len(f),'role':r,'bounds_mm':[v.min(axis=0).tolist(),v.max(axis=0).tolist()]}
        for n,v,f,r in meshes], 'views':[]}
    for e,name,crop in [(90,'actual_topdown',None),(30,'actual_oblique',None),(90,'actual_center_detail',(-30.,-30.,30.,30.))]:
        print(f'Rendering {name}',flush=True)
        report['views'].append(render(meshes,a.output_dir/f'{name}.png',e,crop=crop))
    (a.output_dir/'actual_mesh_render.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False),flush=True)

if __name__=='__main__':main()
