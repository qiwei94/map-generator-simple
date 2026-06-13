"""Hand-drawn brick style render — Voronoi + 圆角 + Perlin + 留白 三层架构。

Spec: doc/handdrawn_brick_style_spec.md

按 spec 三层实现：
  1. 骨架层（_perlin_seed_points + _voronoi_split + _round_corners）
     - Voronoi 图（不用 Delaunay 避尖角）
     - 种子点用 Perlin 偏移网格（不用纯随机）
     - buffer(r) → buffer(-r) 圆角化吃掉所有尖角
  2. 皮肉层（_perlin_edge_offset）
     - 边点用 opensimplex 1D 噪声做垂直法向偏移
     - **严禁 random.uniform** —— 那是狗啃锯齿根源
  3. 灵魂层（_shrink_edge）
     - 每条边起点终点各收缩 ratio（默认 4%），留灰缝

Usage:
    python tools/brick_render.py --city westlake
"""
import argparse
import math
import multiprocessing as mp
import os
import sys
import time
from functools import partial
from pathlib import Path
from typing import List, Tuple

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.collections import PathCollection, LineCollection
from matplotlib.path import Path as MplPath
from shapely.geometry import Polygon, MultiPolygon, Point, MultiPoint, LineString
from shapely.ops import voronoi_diagram

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from tools.tune_buildings_v2 import (  # noqa: E402
    CITY_PRESETS, load_data, build_city_blocks, OUT_DIR,
)
from _TEXTURE_STYLE_OF_DEEPSEEK._block_filter import (  # noqa: E402
    build_and_subtract_exclusions,
    _filter_blocks_with_buildings,
)
from _TEXTURE_STYLE_OF_DEEPSEEK._brick_transform import (  # noqa: E402
    _round_corners, _individual_perturb, _resample_ring,
    _perlin_edge_offset, _shrink_edges_in_ring,
    brick_transform_polygon, brick_transform_batch,
)


# ---------------------------------------------------------------------------
# 骨架层（spec section 1）
# ---------------------------------------------------------------------------

def _perlin_seed_points(polygon: Polygon, density: float,
                        perlin_amp: float, freq: float,
                        seed: int) -> List[Tuple[float, float]]:
    """spec §1：种子点 = 规则网格 + hash noise 偏移。

    density = 1 / (target_cell_area_m²)，即每平方米一个种子的密度反值。
    perlin_amp = 米，种子点偏移振幅（让排布不规则）。

    严禁纯随机：用规则网格保证整体均匀，noise 偏移制造"砖块大小角度微差"。
    """
    from _TEXTURE_STYLE_OF_DEEPSEEK._z_displacement import _hash_noise_2d

    minx, miny, maxx, maxy = polygon.bounds
    cell_size = max(math.sqrt(1.0 / max(density, 1e-9)), 1.0)
    nx = max(2, int(math.ceil((maxx - minx) / cell_size)))
    ny = max(2, int(math.ceil((maxy - miny) / cell_size)))

    ixs = np.arange(nx + 1)
    iys = np.arange(ny + 1)
    gix, giy = np.meshgrid(ixs, iys)
    xs_grid = minx + gix.ravel() * cell_size
    ys_grid = miny + giy.ravel() * cell_size

    dx = _hash_noise_2d(xs_grid * freq, ys_grid * freq, seed=seed) * perlin_amp
    dy = _hash_noise_2d(xs_grid * freq + 100.0, ys_grid * freq + 100.0, seed=seed) * perlin_amp
    px = xs_grid + dx
    py = ys_grid + dy

    from matplotlib.path import Path as _MPath
    poly_path = _MPath(np.array(polygon.exterior.coords))
    pts = np.column_stack([px, py])
    mask = poly_path.contains_points(pts)
    seeds = [(float(pts[i, 0]), float(pts[i, 1])) for i in np.where(mask)[0]]
    return seeds


def _voronoi_split(polygon: Polygon, seeds: List[Tuple[float, float]],
                   min_cell_area: float = 50.0) -> List[Polygon]:
    """spec §1：用 shapely.ops.voronoi_diagram 切 polygon → cell list。

    每个 cell intersect polygon 得到真正落在 polygon 内的形状。
    sliver（面积 < min_cell_area）丢弃，避免边界细长伪片。
    """
    if len(seeds) < 2:
        return [polygon]  # 种子太少，整 polygon 当一块砖
    try:
        vd = voronoi_diagram(MultiPoint(seeds), envelope=polygon.envelope)
    except Exception:
        return [polygon]
    cells: list = []
    for cell in vd.geoms:
        clipped = cell.intersection(polygon)
        if clipped.is_empty:
            continue
        if isinstance(clipped, Polygon):
            if clipped.area >= min_cell_area:
                cells.append(clipped)
        elif isinstance(clipped, MultiPolygon):
            for g in clipped.geoms:
                if isinstance(g, Polygon) and g.area >= min_cell_area:
                    cells.append(g)
    return cells




# ---------------------------------------------------------------------------
# 一块砖 → Path + 描边线段
# ---------------------------------------------------------------------------

def _brick_to_path_and_strokes(
    cell: Polygon, *, corner_r_m: float,
    rot_deg: float, shift_m: float,
    noise_seed: int, perlin_amp: float, perlin_freq: float,
    resample_m: float, shrink_ratio: float, brick_idx: int,
) -> Tuple[MplPath, List[List[Tuple[float, float]]]]:
    """组合四层 → 一砖的 fill Path + 多条描边线段。"""
    if cell.is_empty:
        return None, []

    rounded = _round_corners(cell, corner_r_m)
    if not isinstance(rounded, Polygon) or rounded.is_empty:
        return None, []

    perturbed = _individual_perturb(
        rounded, noise_seed, rot_deg=rot_deg, shift_m=shift_m,
        seed_offset=brick_idx * 0.13)
    if not isinstance(perturbed, Polygon) or perturbed.is_empty:
        return None, []

    ring = list(perturbed.exterior.coords)
    if len(ring) < 4:
        return None, []

    ring_resampled = _resample_ring(ring, resample_m)

    ring_jittered = _perlin_edge_offset(
        ring_resampled, noise_seed, perlin_amp, perlin_freq,
        seed_offset=brick_idx * 0.37)

    verts = list(ring_jittered)
    codes = [MplPath.MOVETO] + [MplPath.LINETO] * (len(verts) - 2) + [MplPath.CLOSEPOLY]
    fill_path = MplPath(verts, codes)

    strokes = _shrink_edges_in_ring(ring_jittered, shrink_ratio)

    return fill_path, strokes


# ---------------------------------------------------------------------------
# 并行 worker（进程级，不依赖不可 pickle 的对象）
# ---------------------------------------------------------------------------

def _process_brick_batch(batch, corner_r_m, rot_deg, shift_m,
                         perlin_amp, perlin_freq, resample_m,
                         shrink_ratio, noise_seed):
    """处理一批 (brick_idx, polygon) → 返回 (verts_list, strokes_list)。

    返回纯坐标列表，主进程再包装为 MplPath。
    """
    results = []
    for brick_idx, cell in batch:
        if not isinstance(cell, Polygon) or cell.is_empty:
            continue
        fp, strokes = _brick_to_path_and_strokes(
            cell, corner_r_m=corner_r_m,
            rot_deg=rot_deg, shift_m=shift_m,
            noise_seed=noise_seed, perlin_amp=perlin_amp, perlin_freq=perlin_freq,
            resample_m=resample_m, shrink_ratio=shrink_ratio,
            brick_idx=brick_idx)
        if fp is None:
            continue
        results.append((list(fp.vertices), list(fp.codes), strokes))
    return results


_NUM_WORKERS = max(1, os.cpu_count() - 1)


# ---------------------------------------------------------------------------
# 一层 polygon list → 渲染
# ---------------------------------------------------------------------------

def render_layer(ax, polys: List[Polygon], *,
                 face_color: str, edge_color: str, edge_lw: float,
                 brick_density: float, seed_perlin_amp: float,
                 corner_r_m: float, rot_deg: float, shift_m: float,
                 perlin_amp: float, perlin_freq: float,
                 resample_m: float, shrink_ratio: float,
                 noise_seed: int, label: str = "") -> int:
    """对每个 polygon → 三层处理 → 渲染。返回总砖数。

    brick_density <= 0（默认推荐）：polygon 自己就是一块砖（不再切）。
        适用 city_block / landmark — 它们已经被路网/水网切好。
    brick_density > 0：Voronoi 再切（用于无几何源需生成"砖石填充"的场景）。
    """
    # 展开所有 cells 并编号
    indexed_cells: List[Tuple[int, Polygon]] = []
    brick_idx = 0
    for poly_idx, poly in enumerate(polys):
        if not isinstance(poly, Polygon) or poly.is_empty:
            continue
        if brick_density > 0:
            seeds = _perlin_seed_points(
                poly, density=brick_density,
                perlin_amp=seed_perlin_amp, freq=0.02,
                seed=noise_seed + poly_idx)
            cells = _voronoi_split(poly, seeds)
        else:
            cells = [poly]
        for cell in cells:
            indexed_cells.append((brick_idx, cell))
            brick_idx += 1

    # 分批并行（每批 ~200 砖，粒度适中以支持进度汇报）
    total = len(indexed_cells)
    n_workers = min(_NUM_WORKERS, max(1, total // 50))
    batch_size = max(50, math.ceil(total / (n_workers * 4)))
    batches = [indexed_cells[i:i+batch_size]
               for i in range(0, total, batch_size)]

    worker_fn = partial(
        _process_brick_batch,
        corner_r_m=corner_r_m, rot_deg=rot_deg, shift_m=shift_m,
        perlin_amp=perlin_amp, perlin_freq=perlin_freq,
        resample_m=resample_m, shrink_ratio=shrink_ratio,
        noise_seed=noise_seed)

    all_paths: list = []
    all_strokes: list = []
    done_count = 0

    if n_workers > 1 and total > 100:
        ctx = mp.get_context("fork" if hasattr(os, "fork") else "spawn")
        with ctx.Pool(n_workers) as pool:
            for batch_res in pool.imap_unordered(worker_fn, batches):
                for verts, codes, strokes in batch_res:
                    all_paths.append(MplPath(verts, codes))
                    all_strokes.extend(strokes)
                done_count += batch_size
                pct = min(100, done_count * 100 // total)
                print(f"\r    [{label}] {pct:3d}% ({min(done_count, total)}/{total})", end="", flush=True)
    else:
        batch_res = worker_fn(indexed_cells)
        for verts, codes, strokes in batch_res:
            all_paths.append(MplPath(verts, codes))
            all_strokes.extend(strokes)

    print()  # 换行

    if all_paths:
        ax.add_collection(PathCollection(
            all_paths, facecolors=face_color, edgecolors='none',
            antialiaseds=True))
    if all_strokes and edge_lw > 0:
        ax.add_collection(LineCollection(
            all_strokes, colors=edge_color, linewidths=edge_lw,
            antialiaseds=True))

    print(f"  layer '{label}': {len(polys)} polygons → {len(all_paths)} bricks")
    return len(all_paths)




# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", choices=list(CITY_PRESETS.keys()), default="westlake")
    ap.add_argument("--road-tier", type=int, default=4)
    ap.add_argument("--use-water", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--no-cache", action="store_true")
    # block 几何过滤（同 block_polygonize_viz）
    ap.add_argument("--filter-empty", type=int, default=1)
    ap.add_argument("--max-area", type=float, default=500000.0)
    ap.add_argument("--road-inset", type=float, default=25.0)
    ap.add_argument("--water-inset", type=float, default=40.0)
    # landmark
    ap.add_argument("--show-landmarks", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--landmark-area", type=float, default=3500.0)
    ap.add_argument("--landmark-min-printable", type=float, default=4000.0)
    # 砖石参数（spec 三层）
    ap.add_argument("--brick-density", type=float, default=0.0,
                    help="Voronoi 切砖密度 1/m²。0=polygon 自己当砖（推荐：block 已经是路网切的，"
                         "不再切）；>0 会在每个 block 内撒种子切成 N 块小砖")
    ap.add_argument("--seed-perlin-amp", type=float, default=10.0,
                    help="种子点 Perlin 偏移振幅 米（错位幅度）")
    ap.add_argument("--corner-radius-m", type=float, default=4.0,
                    help="圆角半径 米（spec §1 v2：极小固定值 ~0.5-1 像素，"
                         "25km/18in/220dpi ≈ 6.3 m/px → 4m ≈ 0.6 px）")
    ap.add_argument("--rot-deg", type=float, default=1.0,
                    help="个体微旋振幅 度（spec §2 微扰层，±1° 渐变）")
    ap.add_argument("--shift-m", type=float, default=8.0,
                    help="个体微移振幅 米（spec §2 微扰层，~1-2 像素）")
    ap.add_argument("--perlin-amp", type=float, default=8.0,
                    help="边偏移振幅 米（spec §3 皮肉层，~1-2 像素 ≈ 6-12m）")
    ap.add_argument("--perlin-freq", type=float, default=0.15,
                    help="边噪声频率，spec §3")
    ap.add_argument("--resample-m", type=float, default=12.0,
                    help="边重采样段长 米（Perlin 需要密集点，12m 兼顾性能与质量）")
    ap.add_argument("--shrink-ratio", type=float, default=0.04,
                    help="边端点收缩比，spec §4 灵魂层（默认 4%）")
    # 渲染
    ap.add_argument("--block-color", default="#e8e2d4", help="city_block 砖填充")
    ap.add_argument("--block-edge", default="#b6a890", help="city_block 砖描边")
    ap.add_argument("--landmark-color", default="#e85a2c", help="landmark 砖填充")
    ap.add_argument("--landmark-edge", default="#7a2810", help="landmark 砖描边")
    ap.add_argument("--edge-lw", type=float, default=0.35, help="描边线宽")
    ap.add_argument("--seed", type=int, default=2026, help="OpenSimplex base seed")
    ap.add_argument("--dpi", type=int, default=220)
    ap.add_argument("--fig-inches", type=float, default=18.0)
    args = ap.parse_args()

    preset = CITY_PRESETS[args.city]
    lat1, lon1, lat2, lon2, pbf_base = preset
    print(f"  City: {args.city}  bbox=({lat1},{lon1})-({lat2},{lon2})")

    print(f"\n=== Load data ({args.city}) ===")
    (polys, landmark_flags, roads_gdf, water_gdf, ctx,
     veg_landmark_polys, *_rest) = load_data(
        sub=False, force=args.no_cache,
        lat1=lat1, lon1=lon1, lat2=lat2, lon2=lon2,
        pbf_basename=pbf_base, cache_label=args.city)

    print(f"\n=== Build city_blocks ===")
    t0 = time.time()
    wgdf = water_gdf if args.use_water else None
    blocks = build_city_blocks(roads_gdf, ctx, args.road_tier, water_gdf=wgdf)
    print(f"  raw blocks: {len(blocks)}  ({time.time()-t0:.1f}s)")

    if args.max_area > 0:
        before = len(blocks)
        blocks = [b for b in blocks if b.area <= args.max_area]
        print(f"  max_area ≤ {args.max_area}: {before} → {len(blocks)}")
    if args.filter_empty > 0:
        before = len(blocks)
        blocks = _filter_blocks_with_buildings(blocks, polys, args.filter_empty)
        print(f"  ≥ {args.filter_empty} buildings/block: {before} → {len(blocks)}")

    # 减 water + veg + road corridor (grid-accelerated)
    before = len(blocks)
    blocks = build_and_subtract_exclusions(
        blocks, water_gdf, veg_landmark_polys,
        roads_gdf=roads_gdf,
        road_inset=args.road_inset, water_inset=args.water_inset)
    print(f"  subtract exclusions: {before} → {len(blocks)} blocks")

    # landmarks
    landmark_polys: list = []
    if args.show_landmarks:
        for p, flag in zip(polys, landmark_flags):
            if not isinstance(p, Polygon) or p.is_empty:
                continue
            if flag or p.area >= args.landmark_area:
                if args.landmark_min_printable > 0 and p.area < args.landmark_min_printable:
                    continue
                s = p.simplify(25.0, preserve_topology=True)
                if isinstance(s, MultiPolygon):
                    s = max(s.geoms, key=lambda g: g.area)
                if isinstance(s, Polygon) and not s.is_empty:
                    landmark_polys.append(s)
        print(f"  landmarks: {len(landmark_polys)} (≥{args.landmark_min_printable:g}m² printable)")

    # ---- Render ----
    print(f"\n=== Render brick layers ===")
    fig, ax = plt.subplots(figsize=(args.fig_inches, args.fig_inches), dpi=args.dpi)

    t_render = time.time()
    n_block_bricks = render_layer(
        ax, blocks,
        face_color=args.block_color, edge_color=args.block_edge,
        edge_lw=args.edge_lw,
        brick_density=args.brick_density,
        seed_perlin_amp=args.seed_perlin_amp,
        corner_r_m=args.corner_radius_m,
        rot_deg=args.rot_deg, shift_m=args.shift_m,
        perlin_amp=args.perlin_amp, perlin_freq=args.perlin_freq,
        resample_m=args.resample_m, shrink_ratio=args.shrink_ratio,
        noise_seed=args.seed, label="city_block")

    n_landmark_bricks = 0
    if landmark_polys:
        n_landmark_bricks = render_layer(
            ax, landmark_polys,
            face_color=args.landmark_color, edge_color=args.landmark_edge,
            edge_lw=args.edge_lw,
            brick_density=args.brick_density,
            seed_perlin_amp=args.seed_perlin_amp,
            corner_r_m=args.corner_radius_m,
        rot_deg=args.rot_deg, shift_m=args.shift_m,
            perlin_amp=args.perlin_amp, perlin_freq=args.perlin_freq,
            resample_m=args.resample_m, shrink_ratio=args.shrink_ratio,
            noise_seed=args.seed + 7777, label="landmark")
    print(f"  render done in {time.time()-t_render:.1f}s")

    bx = ctx["bbox_utm"]; ox, oy = ctx["origin"]
    ax.set_xlim(bx[0]-ox, bx[2]-ox); ax.set_ylim(bx[1]-oy, bx[3]-oy)
    ax.set_aspect("equal"); ax.set_xticks([]); ax.set_yticks([])

    density_str = (f"1/{1/args.brick_density:.0f}m²" if args.brick_density > 0
                   else "polygon=brick")
    density_tag = (f"d{int(1/args.brick_density)}" if args.brick_density > 0
                   else "polybrick")
    title = (f"BRICK style v2 — {args.city}\n"
             f"city_block bricks: {n_block_bricks}  "
             f"landmark bricks: {n_landmark_bricks}\n"
             f"density={density_str}  "
             f"corner_r={args.corner_radius_m}m  "
             f"rot=±{args.rot_deg}°  shift={args.shift_m}m  "
             f"edge_amp={args.perlin_amp}m  shrink={args.shrink_ratio:.0%}")
    ax.set_title(title, fontsize=12, family='monospace')

    out_path = OUT_DIR / (f"_BRICK_v2_{args.city}_{density_tag}_"
                          f"cr{args.corner_radius_m:g}_rot{args.rot_deg:g}_"
                          f"sh{args.shift_m:g}_ea{args.perlin_amp:g}_"
                          f"s{args.shrink_ratio:g}.png")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fig.tight_layout(pad=0.5)
    fig.savefig(out_path, dpi=args.dpi, bbox_inches="tight",
                facecolor="white", pad_inches=0.2)
    plt.close(fig)
    print(f"\n  → {out_path}")


if __name__ == "__main__":
    main()
