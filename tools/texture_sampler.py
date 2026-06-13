#!/usr/bin/env python3
"""Z-texture 样片生成器 — 生成 7 块 10mm×10mm 方块的 3MF，每块顶面施加不同位移纹理。

用法：
    venv/bin/python tools/texture_sampler.py
    venv/bin/python tools/texture_sampler.py --amp-scale 1.5 --grid-step 0.4

输出：output/texture_samples/z_texture_samples.3mf
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
import zipfile
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring

import numpy as np
from opensimplex import OpenSimplex
from scipy.spatial import cKDTree

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
TILE_SIZE_MM = 10.0
BASE_HEIGHT_MM = 3.0
GAP_MM = 2.0
GRID_STEP_MM = 0.5  # 顶面细分间距

REGIONS = [
    "residential",
    "commercial",
    "industrial",
    "farmland",
    "forest",
    "water",
    "unclassified",
    "veg_landmark",
    "veg_ordinary",
]

OUT_DIR = Path(__file__).resolve().parent.parent / "output" / "texture_samples"

# ---------------------------------------------------------------------------
# Displacement 函数（输入 x,y 数组 mm 坐标，返回 z 位移 mm）
# ---------------------------------------------------------------------------

_simplex = OpenSimplex(seed=2026)
_simplex2 = OpenSimplex(seed=7777)
_simplex3 = OpenSimplex(seed=1234)


def _simplex_field(xs, ys, freq, gen=None):
    """向量化 simplex noise，返回 [-1, 1] 范围。"""
    if gen is None:
        gen = _simplex
    return np.array([gen.noise2(x * freq, y * freq) for x, y in zip(xs, ys)])


def _fbm(xs, ys, octaves=6, persistence=0.5, lacunarity=2.0, base_freq=0.1, gen=None):
    """Fractal Brownian Motion (多频叠加)。"""
    result = np.zeros(len(xs))
    amp = 1.0
    freq = base_freq
    for _ in range(octaves):
        result += amp * _simplex_field(xs, ys, freq, gen)
        freq *= lacunarity
        amp *= persistence
    return result


def _voronoi_f1(xs, ys, cell_size=2.0, seed=42):
    """Voronoi F1 距离场（归一化到 [0,1]）。"""
    rng = np.random.default_rng(seed)
    x_min, x_max = xs.min() - cell_size, xs.max() + cell_size
    y_min, y_max = ys.min() - cell_size, ys.max() + cell_size
    cols = int((x_max - x_min) / cell_size) + 2
    rows = int((y_max - y_min) / cell_size) + 2
    gx = np.linspace(x_min, x_max, cols)
    gy = np.linspace(y_min, y_max, rows)
    grid_x, grid_y = np.meshgrid(gx, gy)
    offset_x = (rng.random(grid_x.shape) - 0.5) * cell_size * 0.8
    offset_y = (rng.random(grid_y.shape) - 0.5) * cell_size * 0.8
    points = np.column_stack([(grid_x + offset_x).ravel(),
                              (grid_y + offset_y).ravel()])
    tree = cKDTree(points)
    query = np.column_stack([xs, ys])
    dist, _ = tree.query(query, k=1)
    return np.clip(dist / (cell_size * 0.7), 0, 1)


def disp_residential(x, y, amp=0.15):
    """混凝土细颗粒：高频 Perlin。"""
    return _fbm(x, y, octaves=4, base_freq=0.8, persistence=0.6) * amp


def disp_commercial(x, y, amp=0.12):
    """规则网格线：Wave Bands 双向正交。"""
    gx = np.sin(x / 1.2 * 2 * np.pi)
    gy = np.sin(y / 1.2 * 2 * np.pi)
    grid = np.minimum(gx, gy) * 0.5 + 0.5
    noise = _simplex_field(x, y, 0.3) * 0.2
    return (grid + noise) * amp


def disp_industrial(x, y, amp=0.10):
    """金属拉丝：单向规则 + 极低 Perlin。"""
    bands = np.sin(x / 0.8 * 2 * np.pi) * 0.5 + 0.5
    noise = _simplex_field(x, y, 0.15, gen=_simplex2) * 0.15
    return (bands + noise) * amp


def disp_farmland(x, y, amp=0.25):
    """平行垄沟：Wave Bands 单向 + Perlin 扭曲。"""
    distort = _simplex_field(x, y, 0.2, gen=_simplex2) * 0.8
    phase = y / 1.5 + distort
    waves = np.sin(phase * 2 * np.pi) * 0.5 + 0.5
    return waves * amp


def disp_forest(x, y, amp=0.50):
    """树冠团块：Voronoi F1 + fBM。"""
    vor = _voronoi_f1(x, y, cell_size=2.5, seed=42)
    fbm_val = _fbm(x, y, octaves=5, base_freq=0.15, persistence=0.55)
    detail = _simplex_field(x, y, 0.6, gen=_simplex3) * 0.15
    return (vor * 0.6 + fbm_val * 0.3 + detail) * amp


def disp_water(x, y, amp=0.15):
    """涟漪：Wave Rings 多中心衰减。"""
    centers = [(3.0, 3.0), (7.0, 6.0), (5.0, 8.0)]
    result = np.zeros(len(x))
    for cx, cy in centers:
        dist = np.sqrt((x - cx)**2 + (y - cy)**2)
        wave = np.sin(dist / 1.0 * 2 * np.pi)
        decay = np.exp(-0.15 * dist)
        result += wave * decay
    result = result / len(centers)
    base = _simplex_field(x, y, 0.05) * 0.1
    return (result * 0.5 + 0.5 + base) * amp


def disp_unclassified(x, y, amp=0.08):
    """极低底噪。"""
    return _fbm(x, y, octaves=3, base_freq=0.2, persistence=0.4) * amp


def disp_veg_landmark(x, y, amp=0.50):
    """植被地标（大面积林区）：Voronoi 树冠团块 + 低频起伏。"""
    vor = _voronoi_f1(x, y, cell_size=2.0, seed=99)
    base = _fbm(x, y, octaves=4, base_freq=0.08, persistence=0.5, gen=_simplex2)
    detail = _simplex_field(x, y, 0.4, gen=_simplex3) * 0.2
    return (vor * 0.55 + base * 0.35 + detail) * amp


def disp_veg_ordinary(x, y, amp=0.15):
    """普通植被（草地/小绿化）：高频 Perlin 绒面感。"""
    fine = _fbm(x, y, octaves=5, base_freq=0.6, persistence=0.5, gen=_simplex3)
    base = _simplex_field(x, y, 0.08, gen=_simplex2) * 0.3
    return (fine * 0.7 + base) * amp


DISPLACEMENT_FUNCS = {
    "residential": disp_residential,
    "commercial": disp_commercial,
    "industrial": disp_industrial,
    "farmland": disp_farmland,
    "forest": disp_forest,
    "water": disp_water,
    "unclassified": disp_unclassified,
    "veg_landmark": disp_veg_landmark,
    "veg_ordinary": disp_veg_ordinary,
}

# ---------------------------------------------------------------------------
# Mesh 构建
# ---------------------------------------------------------------------------

def build_textured_tile(region: str, x_offset: float,
                        y_offset: float = 0.0,
                        grid_step: float = GRID_STEP_MM,
                        amp_scale: float = 1.0) -> tuple:
    """构建一个带顶面纹理的 watertight 方块 mesh。

    结构：顶面 n×n grid（带位移）+ 底面 n×n grid（平面）+ 四侧面共享边缘顶点。
    返回 (vertices: np.ndarray[N,3], faces: np.ndarray[M,3])
    """
    size = TILE_SIZE_MM
    h = BASE_HEIGHT_MM

    n = int(round(size / grid_step)) + 1
    xs_local = np.linspace(0, size, n)
    ys_local = np.linspace(0, size, n)
    gx, gy = np.meshgrid(xs_local, ys_local)
    gx_flat = gx.ravel()
    gy_flat = gy.ravel()

    # 位移
    disp_func = DISPLACEMENT_FUNCS[region]
    default_amps = {
        "residential": 0.15, "commercial": 0.12, "industrial": 0.10,
        "farmland": 0.25, "forest": 0.50, "water": 0.15, "unclassified": 0.08,
        "veg_landmark": 0.50, "veg_ordinary": 0.15,
    }
    dz = disp_func(gx_flat, gy_flat, amp=default_amps[region] * amp_scale)

    # 顶面顶点: indices [0, n*n)
    top_verts = np.column_stack([
        gx_flat + x_offset,
        gy_flat + y_offset,
        np.full(len(gx_flat), h) + dz
    ])

    # 底面顶点: indices [n*n, 2*n*n)  — 同样 n×n grid，z=0
    bot_verts = np.column_stack([
        gx_flat + x_offset,
        gy_flat + y_offset,
        np.zeros(len(gx_flat))
    ])

    vertices = np.vstack([top_verts, bot_verts])
    n2 = n * n  # bottom index offset

    faces = []

    # 顶面三角化（法线朝 +Z）
    for row in range(n - 1):
        for col in range(n - 1):
            i00 = row * n + col
            i10 = row * n + col + 1
            i01 = (row + 1) * n + col
            i11 = (row + 1) * n + col + 1
            faces.append([i00, i10, i11])
            faces.append([i00, i11, i01])

    # 底面三角化（法线朝 -Z，反向绕序）
    for row in range(n - 1):
        for col in range(n - 1):
            i00 = n2 + row * n + col
            i10 = n2 + row * n + col + 1
            i01 = n2 + (row + 1) * n + col
            i11 = n2 + (row + 1) * n + col + 1
            faces.append([i00, i11, i10])
            faces.append([i00, i01, i11])

    # 侧面：连接顶面和底面的对应边缘顶点
    # front (row=0, 法线朝 -Y)
    for col in range(n - 1):
        t0, t1 = col, col + 1
        b0, b1 = n2 + col, n2 + col + 1
        faces.append([t0, b0, b1])
        faces.append([t0, b1, t1])

    # back (row=n-1, 法线朝 +Y)
    for col in range(n - 1):
        t0 = (n - 1) * n + col
        t1 = (n - 1) * n + col + 1
        b0 = n2 + (n - 1) * n + col
        b1 = n2 + (n - 1) * n + col + 1
        faces.append([t0, t1, b1])
        faces.append([t0, b1, b0])

    # left (col=0, 法线朝 -X)
    for row in range(n - 1):
        t0 = row * n
        t1 = (row + 1) * n
        b0 = n2 + row * n
        b1 = n2 + (row + 1) * n
        faces.append([t0, t1, b1])
        faces.append([t0, b1, b0])

    # right (col=n-1, 法线朝 +X)
    for row in range(n - 1):
        t0 = row * n + (n - 1)
        t1 = (row + 1) * n + (n - 1)
        b0 = n2 + row * n + (n - 1)
        b1 = n2 + (row + 1) * n + (n - 1)
        faces.append([t0, b0, b1])
        faces.append([t0, b1, t1])

    return vertices, np.array(faces, dtype=np.int32)


# ---------------------------------------------------------------------------
# 3MF 导出
# ---------------------------------------------------------------------------

NS_3MF = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
NS_PROD = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
NS_BAMBU = "http://schemas.bambulab.com/package/2021"


def _uuid():
    return str(uuid.uuid4())


def _build_content_types():
    root = Element("Types")
    root.set("xmlns", "http://schemas.openxmlformats.org/package/2006/content-types")
    SubElement(root, "Default", Extension="rels",
               ContentType="application/vnd.openxmlformats-package.relationships+xml")
    SubElement(root, "Default", Extension="model",
               ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml")
    SubElement(root, "Default", Extension="config",
               ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml")
    return tostring(root, encoding="unicode", xml_declaration=True)


def _build_rels():
    root = Element("Relationships")
    root.set("xmlns", "http://schemas.openxmlformats.org/package/2006/relationships")
    SubElement(root, "Relationship", Target="/3D/3dmodel.model",
               Id="rel0", Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel")
    return tostring(root, encoding="unicode", xml_declaration=True)


def _build_model_rels():
    root = Element("Relationships")
    root.set("xmlns", "http://schemas.openxmlformats.org/package/2006/relationships")
    SubElement(root, "Relationship", Target="/3D/Objects/object_1.model",
               Id="rel1", Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel")
    return tostring(root, encoding="unicode", xml_declaration=True)


def _build_main_model(n_objects: int, labels: list):
    root = Element("model")
    root.set("xmlns", NS_3MF)
    root.set("xmlns:p", NS_PROD)
    root.set("xmlns:BambuStudio", NS_BAMBU)
    root.set("unit", "millimeter")
    root.set("xml:lang", "en-US")

    res = SubElement(root, "resources")

    # basematerials
    bm = SubElement(res, "basematerials", id="1")
    SubElement(bm, "base", name="White", displaycolor="#FFFFFF")

    # assembly object
    obj = SubElement(res, "object", id="100", type="model")
    obj.set("p:UUID", _uuid())
    components = SubElement(obj, "components")
    for i in range(n_objects):
        comp = SubElement(components, "component",
                          objectid=str(i + 1))
        comp.set("p:path", "/3D/Objects/object_1.model")
        comp.set("p:UUID", _uuid())

    build = SubElement(root, "build")
    item = SubElement(build, "item", objectid="100")
    item.set("p:UUID", _uuid())

    return tostring(root, encoding="unicode", xml_declaration=True)


def _build_objects_model(meshes: list, labels: list):
    root = Element("model")
    root.set("xmlns", NS_3MF)
    root.set("xmlns:p", NS_PROD)
    root.set("xmlns:BambuStudio", NS_BAMBU)
    root.set("unit", "millimeter")

    res = SubElement(root, "resources")

    for idx, ((verts, faces), label) in enumerate(zip(meshes, labels)):
        obj = SubElement(res, "object", id=str(idx + 1), type="model",
                         name=label, pid="1", pindex="0")
        obj.set("p:UUID", _uuid())
        mesh_el = SubElement(obj, "mesh")

        vertices_el = SubElement(mesh_el, "vertices")
        for v in verts:
            SubElement(vertices_el, "vertex",
                       x=f"{v[0]:.4f}", y=f"{v[1]:.4f}", z=f"{v[2]:.4f}")

        triangles_el = SubElement(mesh_el, "triangles")
        for f in faces:
            SubElement(triangles_el, "triangle",
                       v1=str(f[0]), v2=str(f[1]), v3=str(f[2]))

    return tostring(root, encoding="unicode", xml_declaration=True)


def _build_model_settings(n_objects: int):
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<config>',
             '  <plate>',
             '    <metadata key="plater_id" value="1"/>']
    for i in range(n_objects):
        lines.append(f'    <model_instance>')
        lines.append(f'      <metadata key="object_id" value="{i + 1}"/>')
        lines.append(f'      <metadata key="extruder" value="1"/>')
        lines.append(f'    </model_instance>')
    lines.append('  </plate>')
    lines.append('</config>')
    return "\n".join(lines)


def export_3mf(meshes: list, labels: list, output_path: Path):
    """将多个 mesh 导出为 Bambu Studio 兼容的 3MF。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    n = len(meshes)

    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _build_content_types())
        zf.writestr("_rels/.rels", _build_rels())
        zf.writestr("3D/_rels/3dmodel.model.rels", _build_model_rels())
        zf.writestr("3D/3dmodel.model", _build_main_model(n, labels))
        zf.writestr("3D/Objects/object_1.model", _build_objects_model(meshes, labels))
        zf.writestr("Metadata/model_settings.config", _build_model_settings(n))

    print(f"  → {output_path} ({output_path.stat().st_size / 1024:.0f} KB)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Z-texture 样片对比 3MF 生成器")
    ap.add_argument("--grid-step", type=float, default=GRID_STEP_MM,
                    help=f"顶面网格间距 mm（默认 {GRID_STEP_MM}）")
    ap.add_argument("--output", type=str, default=None,
                    help="输出路径（默认 output/texture_samples/z_texture_samples.3mf）")
    args = ap.parse_args()

    out_path = Path(args.output) if args.output else OUT_DIR / "z_texture_samples.3mf"

    amp_scales = [1.0, 2.0, 4.0]
    row_gap = GAP_MM + TILE_SIZE_MM  # Y 方向行间距

    print(f"Generating Z-texture sample tiles...")
    print(f"  Tile: {TILE_SIZE_MM}mm × {TILE_SIZE_MM}mm × {BASE_HEIGHT_MM}mm")
    print(f"  Grid step: {args.grid_step}mm")
    print(f"  Regions: {len(REGIONS)}")
    print(f"  Rows: {len(amp_scales)} (amp = {amp_scales})")
    print()

    meshes = []
    labels = []
    for row_idx, amp_s in enumerate(amp_scales):
        y_offset = row_idx * row_gap
        print(f"  --- Row {row_idx + 1}: amp={amp_s}x (y_offset={y_offset:.0f}mm) ---")
        for col_idx, region in enumerate(REGIONS):
            x_offset = col_idx * (TILE_SIZE_MM + GAP_MM)
            verts, faces = build_textured_tile(
                region, x_offset, y_offset=y_offset,
                grid_step=args.grid_step,
                amp_scale=amp_s)
            meshes.append((verts, faces))
            label = f"{region}_{amp_s:.0f}x"
            labels.append(label)
            print(f"    [{label:20s}] verts={len(verts):5d}  faces={len(faces):5d}  "
                  f"z_range=[{verts[:,2].min():.3f}, {verts[:,2].max():.3f}]")

    print()
    export_3mf(meshes, labels, out_path)


if __name__ == "__main__":
    main()
