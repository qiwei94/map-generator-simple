#!/usr/bin/env python3
"""buildings 聚合参数调优工具（独立 CLI，跳过完整 pipeline）。

用法：
    # 单组参数
    python tools/tune_buildings.py --print-limit 3500 --buffer 20 --simplify 15

    # 网格搜索（64 组）
    python tools/tune_buildings.py --grid

    # 渲染 reference 锚点
    python tools/tune_buildings.py --reference

    # 限定 5km 子区域（更快）
    python tools/tune_buildings.py --grid --sub

输入：tmp/osmium_building_*.geojson（pipeline Stage 2 的中间产物）
输出：output/tune_buildings/<参数串>.png

逻辑（精简版 buildings.py）：
  1. shapely.simplify 每栋楼
  2. ≥ print_limit 个体保留
  3. < print_limit 进 buffer-union-shrink-simplify 聚合
  4. matplotlib 画俯视图（灰=原 OSM，米白=个体，浅蓝=街区）
"""

from __future__ import annotations

import argparse
import os
import pickle
import sys
import time
from itertools import product
from pathlib import Path
from typing import List, Tuple

# Ensure project root on sys.path
_PROJECT = Path(__file__).resolve().parent.parent
if str(_PROJECT) not in sys.path:
    sys.path.insert(0, str(_PROJECT))

import numpy as np
import geopandas as gpd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon as MplPoly
from matplotlib.collections import PatchCollection
from shapely.geometry import MultiPolygon, Polygon, box
from shapely.ops import unary_union
import shapely

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import (
    bbox_to_utm, project_geodataframe,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GEOJSON_PATH = _PROJECT / "tmp" / "osmium_building_30.1300_120.0100_30.3600_120.2900.geojson"
OUT_DIR = _PROJECT / "output" / "tune_buildings"
PICKLE_PATH = _PROJECT / "tmp" / "tune_buildings_cache.pkl"

# Westlake bbox (与 generate_westlake_cli.py 一致)
LAT1, LON1, LAT2, LON2 = 30.13, 120.01, 30.36, 120.29

# Sub-region for fast iteration (West Lake center, ~5km)
SUB_LAT1, SUB_LON1, SUB_LAT2, SUB_LON2 = 30.20, 120.10, 30.27, 120.20


# ---------------------------------------------------------------------------
# 数据加载（一次性，pickle 缓存）
# ---------------------------------------------------------------------------

def load_polys(sub_region: bool = False, force_reload: bool = False
              ) -> Tuple[List[Polygon], dict]:
    """加载 + 投影建筑 polygon。返回 (polys, ctx)，ctx 含 bbox / scale。"""
    cache_key = "sub" if sub_region else "full"
    cache_file = PICKLE_PATH.with_suffix(f".{cache_key}.pkl")

    if cache_file.exists() and not force_reload:
        with open(cache_file, "rb") as f:
            data = pickle.load(f)
        print(f"  [cache hit] {cache_file.name}: {len(data['polys'])} polygons")
        return data["polys"], data["ctx"]

    print(f"  Loading {GEOJSON_PATH.name} ...")
    if not GEOJSON_PATH.exists():
        sys.exit(f"❌ {GEOJSON_PATH} 不存在 — 请先跑过 generate_westlake_cli.py")
    gdf = gpd.read_file(GEOJSON_PATH)
    # 只保留 polygon 类型
    gdf = gdf[gdf.geometry.type.isin(["Polygon", "MultiPolygon"])].copy()
    print(f"  Raw OSM buildings: {len(gdf)}")

    if sub_region:
        bbox = bbox_to_utm(SUB_LAT1, SUB_LON1, SUB_LAT2, SUB_LON2)
    else:
        bbox = bbox_to_utm(LAT1, LON1, LAT2, LON2)
    gdf_p = project_geodataframe(gdf, bbox["utm_crs"], bbox["origin"], clip_bbox=bbox["utm_bbox"])
    print(f"  After projection + clip: {len(gdf_p)}")

    polys: list[Polygon] = []
    for geom in gdf_p.geometry:
        if geom is None or geom.is_empty:
            continue
        if isinstance(geom, MultiPolygon):
            polys.extend(g for g in geom.geoms if not g.is_empty)
        elif isinstance(geom, Polygon):
            polys.append(geom)

    ctx = {
        "bbox_utm": bbox["utm_bbox"],
        "origin": bbox["origin"],
        "width_m": bbox["width_m"],
        "height_m": bbox["height_m"],
        "wgs84": bbox["wgs84_bbox"],
    }
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_file, "wb") as f:
        pickle.dump({"polys": polys, "ctx": ctx}, f)
    print(f"  [cached] {cache_file.name}")
    return polys, ctx


# ---------------------------------------------------------------------------
# 聚合算法（纯 footprint，不挤出）
# ---------------------------------------------------------------------------

def split_individuals_smalls(polys: List[Polygon],
                              print_limit_m2: float,
                              simplify_tol_m: float,
                              min_area_m2: float = 1.0
                             ) -> Tuple[List[Polygon], List[Polygon], int]:
    """按 print_limit 分流。返回 (individuals, smalls, n_dropped)。

    min_area_m2 默认 1.0 ≈ 不过滤（OSM 偶尔有退化几何 < 1m² 的需排除）。
    所有正常 OSM polygon 都会进入聚合管道，让 buffer-union 自己消化噪声，
    避免丢失偏远住宅区的密度感。
    """
    individuals = []
    smalls = []
    n_dropped = 0
    for p in polys:
        if p.area < min_area_m2:
            n_dropped += 1
            continue
        s = p.simplify(simplify_tol_m, preserve_topology=True)
        if s.is_empty or s.area < 1.0:
            continue
        if isinstance(s, MultiPolygon):
            s = max(s.geoms, key=lambda g: g.area)
        if not isinstance(s, Polygon) or s.is_empty:
            continue
        if s.area >= print_limit_m2:
            individuals.append(s)
        else:
            smalls.append(s)
    return individuals, smalls, n_dropped


def aggregate_smalls(smalls: List[Polygon],
                     buffer_m: float,
                     shrink_slack_m: float,
                     simplify_m: float,
                     min_area_m2: float
                    ) -> List[Polygon]:
    """buffer-union-shrink-simplify 聚合，过滤 < min_area 的碎片。"""
    if not smalls:
        return []
    buffered = [p.buffer(buffer_m, join_style=2) for p in smalls if not p.is_empty]
    if not buffered:
        return []
    merged = unary_union(buffered)
    if merged.is_empty:
        return []
    shrunk = merged.buffer(-(buffer_m - shrink_slack_m), join_style=2)
    if shrunk.is_empty:
        return []
    shrunk = shrunk.simplify(simplify_m, preserve_topology=True)
    if shrunk.is_empty:
        return []
    polys = list(shrunk.geoms) if isinstance(shrunk, MultiPolygon) else [shrunk]
    return [p for p in polys
            if isinstance(p, Polygon) and not p.is_empty
            and p.area >= min_area_m2]


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def _polys_to_collection(polys, **kwargs):
    patches = []
    for p in polys:
        if not isinstance(p, Polygon) or p.is_empty:
            continue
        patches.append(MplPoly(np.array(p.exterior.coords)))
    return PatchCollection(patches, **kwargs)


def render_one(out_path: Path, raw_polys, individuals, blocks, ctx,
               title: str, params: dict, stats: dict):
    fig, ax = plt.subplots(figsize=(10, 10), dpi=120)

    # 灰底：原 OSM 轮廓（半透明）
    if raw_polys:
        ax.add_collection(_polys_to_collection(
            raw_polys, facecolor="#d8d8d8", edgecolor="none", alpha=0.5))

    # 街区（先画底层）：浅蓝填充 + 黑色细边线
    if blocks:
        ax.add_collection(_polys_to_collection(
            blocks, facecolor="#a8c8e8", edgecolor="#1a1a1a", linewidths=0.4, alpha=0.85))

    # 个体（未被聚合的大楼）：橙红填充 + 黑边线，地标一眼可见
    if individuals:
        ax.add_collection(_polys_to_collection(
            individuals, facecolor="#e85a2c", edgecolor="#1a1a1a", linewidths=0.5, alpha=0.95))

    bx = ctx["bbox_utm"]
    ox, oy = ctx["origin"]
    ax.set_xlim(bx[0] - ox, bx[2] - ox)
    ax.set_ylim(bx[1] - oy, bx[3] - oy)
    ax.set_aspect("equal")
    ax.set_xticks([]); ax.set_yticks([])

    # 标题 + stats
    info = " ".join(f"{k}={v}" for k, v in params.items())
    stat = " ".join(f"{k}={v}" for k, v in stats.items())
    ax.set_title(f"{title}\n{info}\n{stat}", fontsize=11, family='monospace')

    fig.tight_layout(pad=0.5)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=120, bbox_inches="tight", facecolor="white", pad_inches=0.2)
    plt.close(fig)


# ---------------------------------------------------------------------------
# 一组参数 → PNG
# ---------------------------------------------------------------------------

def run_one(polys, ctx, *, print_limit, buffer_m, simplify_m, shrink_slack=5.0,
            min_individual_simplify=25.0, sub_label="", out_dir: Path = OUT_DIR):
    t0 = time.time()
    indv, smalls, n_drop = split_individuals_smalls(
        polys, print_limit, min_individual_simplify, min_area_m2=100.0)
    blocks = aggregate_smalls(
        smalls, buffer_m, shrink_slack, simplify_m, min_area_m2=print_limit)
    elapsed = time.time() - t0

    median_indv = float(np.median([p.area for p in indv])) if indv else 0
    median_block = float(np.median([p.area for p in blocks])) if blocks else 0

    stats = {
        "indv": len(indv),
        "blocks": len(blocks),
        "total": len(indv) + len(blocks),
        "med_indv_m²": f"{median_indv:.0f}",
        "med_block_m²": f"{median_block:.0f}",
        "elapsed": f"{elapsed:.1f}s",
    }
    params = {
        "PRINT_LIMIT": int(print_limit),
        "BUFFER": int(buffer_m),
        "SIMPLIFY": int(simplify_m),
        "SLACK": int(shrink_slack),
    }

    sub_tag = "_sub" if sub_label else ""
    name = (f"P{int(print_limit)}_B{int(buffer_m)}_S{int(simplify_m)}"
            f"_K{int(shrink_slack)}{sub_tag}.png")
    out_path = out_dir / name
    render_one(out_path, polys, indv, blocks, ctx,
               f"buildings tune {sub_label}", params, stats)
    print(f"  → {name}  individuals={len(indv):>5}  blocks={len(blocks):>5}  "
          f"total={stats['total']:>5}  ({elapsed:.1f}s)")
    return stats


# ---------------------------------------------------------------------------
# Reference 锚点
# ---------------------------------------------------------------------------

def render_reference(out_dir: Path = OUT_DIR):
    """渲染杭州 reference 建筑层（mesh 1 + mesh 2）作为锚点。"""
    import re, zipfile, trimesh
    p = _PROJECT / "demo" / "杭州" / "杭州25Km城市肌理P.3mf"
    if not p.exists():
        print("  ⚠ 杭州 reference 文件不存在，跳过 reference"); return
    with zipfile.ZipFile(p) as zf:
        sub = zf.read("3D/Objects/object_1.model").decode()
        main = zf.read("3D/3dmodel.model").decode()
    # 取 mesh 1 和 mesh 2 (都是建筑层 ext=E1)
    comp_tz = {}
    for c in re.findall(r'<component\s+([^>]+?)/>', main):
        oid_m = re.search(r'objectid="(\d+)"', c)
        tm_m = re.search(r'transform="([^"]+)"', c)
        if oid_m and tm_m:
            comp_tz[int(oid_m.group(1))] = float(tm_m.group(1).split()[11])

    polys = []  # 反推回 footprint：每个 body 的 XY 投影 hull
    for mid in (1, 2):
        m_match = re.search(rf'<object\s+id="{mid}"[^>]*>(.*?)</object>', sub, re.DOTALL)
        if not m_match: continue
        body = m_match.group(1)
        V = np.array([[float(x), float(y), float(z)] for x, y, z in
                      re.findall(r'<vertex\s+x="([^"]+)"\s+y="([^"]+)"\s+z="([^"]+)"', body)])
        F = np.array([[int(a), int(b), int(c)] for a, b, c in
                      re.findall(r'<triangle\s+v1="(\d+)"\s+v2="(\d+)"\s+v3="(\d+)"', body)],
                     dtype=np.int64)
        if len(V) == 0: continue
        m = trimesh.Trimesh(vertices=V, faces=F, process=False)
        for body_mesh in m.split(only_watertight=False):
            bv = body_mesh.vertices[:, :2]
            # 取 convex hull 作 footprint approximation
            try:
                from shapely.geometry import MultiPoint
                hull = MultiPoint(list(map(tuple, bv))).convex_hull
                if isinstance(hull, Polygon) and hull.area > 1.0:
                    polys.append(hull)
            except Exception:
                pass

    print(f"  reference 杭州 footprints: {len(polys)}")
    if not polys:
        return

    # ctx：杭州 reference 内部坐标空间（约 ±127mm）
    all_x = np.concatenate([np.array(p.exterior.coords)[:, 0] for p in polys])
    all_y = np.concatenate([np.array(p.exterior.coords)[:, 1] for p in polys])
    half = max(all_x.max() - all_x.min(), all_y.max() - all_y.min()) / 2 * 1.05
    cx, cy = (all_x.max() + all_x.min()) / 2, (all_y.max() + all_y.min()) / 2
    ctx = {"bbox_utm": (cx - half, cy - half, cx + half, cy + half),
           "origin": (0, 0)}

    out_path = out_dir / "_REFERENCE_HZ.png"
    fig, ax = plt.subplots(figsize=(18, 18), dpi=220)
    ax.add_collection(_polys_to_collection(
        polys, facecolor="#f0e4cc", edgecolor="#1a1a1a", linewidths=0.2, alpha=0.95))
    ax.set_xlim(cx - half, cx + half); ax.set_ylim(cy - half, cy + half)
    ax.set_aspect("equal"); ax.set_xticks([]); ax.set_yticks([])
    ax.set_title(f"HZ reference (mesh 1+2 building bodies)  {len(polys)} bodies — convex hull approx",
                 fontsize=14, family='monospace')
    fig.tight_layout(pad=0.5)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=220, bbox_inches="tight", facecolor="white", pad_inches=0.2)
    plt.close(fig)
    print(f"  → {out_path.name}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--print-limit", type=float, default=3500,
                    help="footprint 阈值 (m²)")
    ap.add_argument("--buffer", type=float, default=20,
                    help="聚合 buffer 半径 (m)")
    ap.add_argument("--simplify", type=float, default=15,
                    help="街区合并后 simplify 容差 (m)")
    ap.add_argument("--shrink-slack", type=float, default=5,
                    help="收缩时少收的余量 (m)，留街区边缘块感")
    ap.add_argument("--grid", action="store_true",
                    help="网格搜索 4×4×4 = 64 组")
    ap.add_argument("--reference", action="store_true",
                    help="渲染杭州 reference 锚点")
    ap.add_argument("--sub", action="store_true",
                    help="只用 5km 西湖中心子区域（快）")
    ap.add_argument("--no-cache", action="store_true",
                    help="忽略 pickle 缓存重读 GeoJSON")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.reference:
        print("=== 渲染 reference 锚点 ===")
        render_reference(OUT_DIR)

    sub_label = "5km sub-region" if args.sub else "25km full"
    print(f"\n=== 加载建筑数据 ({sub_label}) ===")
    polys, ctx = load_polys(sub_region=args.sub, force_reload=args.no_cache)
    print(f"  total polys: {len(polys)}\n")

    if args.grid:
        print("=== 网格搜索 ===")
        print_limits = [2500, 3500, 5000, 7500]
        buffers = [15, 20, 25, 30]
        simplifies = [10, 15, 20, 25]
        n = len(print_limits) * len(buffers) * len(simplifies)
        print(f"  {n} 组参数  ({sub_label})\n")
        for i, (pl, b, s) in enumerate(product(print_limits, buffers, simplifies), 1):
            print(f"[{i}/{n}] ", end="")
            run_one(polys, ctx,
                    print_limit=pl, buffer_m=b, simplify_m=s,
                    shrink_slack=args.shrink_slack,
                    sub_label=sub_label if args.sub else "")
        print(f"\n→ 输出在 {OUT_DIR}")
    else:
        print("=== 单组参数 ===")
        run_one(polys, ctx,
                print_limit=args.print_limit,
                buffer_m=args.buffer,
                simplify_m=args.simplify,
                shrink_slack=args.shrink_slack,
                sub_label=sub_label if args.sub else "")


if __name__ == "__main__":
    main()
