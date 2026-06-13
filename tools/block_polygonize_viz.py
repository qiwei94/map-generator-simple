"""Block-only diagnostic tool.

只画"路网 + 水网切割出的 city_blocks"，对照 _REFERENCE_HZ 看建筑层的真身。
不画建筑、地标、vegetation、water 填充、stadium —— 只看 block 本身的形态密度。

Usage:
    python tools/block_polygonize_viz.py --city westlake
    python tools/block_polygonize_viz.py --city westlake --convex
    python tools/block_polygonize_viz.py --city westlake --convex --filter-empty
"""
import argparse
import sys
import time
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from shapely.geometry import Polygon, MultiPolygon
from shapely.ops import unary_union
from shapely.strtree import STRtree

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from tools.tune_buildings_v2 import (  # noqa: E402
    CITY_PRESETS, load_data, build_city_blocks,
    _convex_quadrilateral, _polys_to_collection,
    _draw_jittered_block_layer, OUT_DIR,
)
from _TEXTURE_STYLE_OF_DEEPSEEK._block_filter import (  # noqa: E402
    build_and_subtract_exclusions,
    _filter_blocks_with_buildings,
)




def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", choices=list(CITY_PRESETS.keys()), default="westlake")
    ap.add_argument("--road-tier", type=int, default=4,
                    help="road tier (4 = 一般车行，5 = 含细支路)")
    ap.add_argument("--use-water", action=argparse.BooleanOptionalAction, default=True,
                    help="水体边界进 polygonize")
    ap.add_argument("--convex", action="store_true",
                    help="每个 block 用 convex_quadrilateral 简化（reference 风格）")
    ap.add_argument("--filter-empty", type=int, default=0,
                    metavar="MIN_BUILDINGS",
                    help="只保留含 ≥ N 栋建筑的 block（0 = 全部画）")
    ap.add_argument("--min-area", type=float, default=0.0,
                    help="block 面积下限 m²（剔太小细碎）")
    ap.add_argument("--max-area", type=float, default=0.0,
                    help="block 面积上限 m²（0 = 不限；常用 500000 剔大公园/湖块）")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--dpi", type=int, default=220)
    ap.add_argument("--fig-inches", type=float, default=18.0)
    ap.add_argument("--color", default="#e8e2d4", help="block 填充色")
    ap.add_argument("--edge", default="#aaaaaa", help="block 描边色")
    ap.add_argument("--lw", type=float, default=0.25, help="描边线宽")
    ap.add_argument("--inset", type=float, default=0.0,
                    help="block 向内 buffer N 米，块之间留出 2N 米的缝（砖块感）")
    ap.add_argument("--jitter", type=float, default=0.0,
                    help="block 边缘 jitter 幅度 米（0 = 不 jitter）")
    ap.add_argument("--jitter-seg", type=float, default=12.0,
                    help="jitter 重采样段长 米（越小边缘越碎）")
    ap.add_argument("--lowpoly", action="store_true",
                    help="叠加 low-poly 三角阴影（手绘感）")
    ap.add_argument("--subtract-exclusions", action=argparse.BooleanOptionalAction, default=True,
                    help="convex 前从 block 减去 water/vegetation/protected_area "
                         "（避免凸包吃湖/公园）；默认开；--no-subtract-exclusions 关")
    ap.add_argument("--subtract-min-area", type=float, default=100.0,
                    help="subtract 后的碎片下限 m²（小于此丢弃）")
    ap.add_argument("--road-inset", type=float, default=25.0,
                    help="道路 LineString buffer 半径 米（块跟块经道路被推开 2N）；0 = 不加宽路")
    ap.add_argument("--water-inset", type=float, default=40.0,
                    help="水体 polygon/LineString buffer 半径 米（让河面/湖岸视觉变宽）；0 = 不加宽水")
    ap.add_argument("--show-landmarks", action=argparse.BooleanOptionalAction, default=True,
                    help="叠加地标层（tag_landmark + 大面积建筑）；默认开")
    ap.add_argument("--landmark-area", type=float, default=3500.0,
                    help="size_landmark 面积阈值 m²（与 tune 的 print_limit 一致）")
    ap.add_argument("--landmark-min-printable", type=float, default=4000.0,
                    help="所有 landmark 面积下限 m²（默认 4000=MIN_PRINTABLE_AREA_M2，对应 1.25 nozzle 边长）；"
                         "tag 命中但 < 此值会被剔（PNG 跟 3MF 可打印性对齐）；0 = 关闭兜底")
    ap.add_argument("--landmark-color", default="#e85a2c", help="landmark 填充色（橙）")
    ap.add_argument("--landmark-edge", default="#3a1208", help="landmark 描边色")
    ap.add_argument("--convex-max-bloat", type=float, default=1.3,
                    help="凸包/原形面积比上限：超过则保留原形不做 convex "
                         "（防止 L 形/新月形 block 凸包过度膨胀）；1.0 = 完全禁用 convex；"
                         "默认 1.3（凸包最多膨胀 30%）")
    args = ap.parse_args()

    preset = CITY_PRESETS[args.city]
    lat1, lon1, lat2, lon2, pbf_base = preset
    print(f"  City: {args.city}  bbox=({lat1},{lon1})-({lat2},{lon2})  pbf={pbf_base}")

    print(f"\n=== Load data ({args.city}) ===")
    (polys, landmark_flags, roads_gdf, water_gdf, ctx,
     veg_landmark_polys, *_rest) = load_data(
        sub=False, force=args.no_cache,
        lat1=lat1, lon1=lon1, lat2=lat2, lon2=lon2,
        pbf_basename=pbf_base, cache_label=args.city)

    print(f"\n=== Polygonize ===")
    t0 = time.time()
    wgdf = water_gdf if args.use_water else None
    blocks = build_city_blocks(roads_gdf, ctx, args.road_tier, water_gdf=wgdf)
    print(f"  raw blocks: {len(blocks)}  ({time.time()-t0:.1f}s)")

    n_raw = len(blocks)

    if args.min_area > 0:
        before = len(blocks)
        blocks = [b for b in blocks if b.area >= args.min_area]
        print(f"  filter min_area ≥ {args.min_area} m²: {before} → {len(blocks)}")

    if args.max_area > 0:
        before = len(blocks)
        blocks = [b for b in blocks if b.area <= args.max_area]
        print(f"  filter max_area ≤ {args.max_area} m²: {before} → {len(blocks)}")

    if args.filter_empty > 0:
        before = len(blocks)
        blocks = _filter_blocks_with_buildings(blocks, polys, args.filter_empty)
        print(f"  filter ≥ {args.filter_empty} buildings/block: {before} → {len(blocks)}")

    if args.subtract_exclusions:
        before = len(blocks)
        n_water = 0 if water_gdf is None else len(water_gdf)
        n_veg = 0 if not veg_landmark_polys else len(veg_landmark_polys)
        n_road = 0 if roads_gdf is None else len(roads_gdf)
        print(f"  exclusion mask: water={n_water}(+{args.water_inset}m buffer) "
              f"+ road={n_road}(+{args.road_inset}m buffer) + veg={n_veg}")
        blocks = build_and_subtract_exclusions(
            blocks, water_gdf, veg_landmark_polys,
            roads_gdf=roads_gdf, road_inset=args.road_inset,
            water_inset=args.water_inset, min_area=args.subtract_min_area)
        print(f"  subtract exclusions: {before} → {len(blocks)} blocks "
              f"(grid-accelerated, 碎片 <{args.subtract_min_area}m² 丢弃)")

    if args.convex:
        out_blocks = []
        n_kept_original = 0
        for b in blocks:
            hull = _convex_quadrilateral(b)
            if not isinstance(hull, Polygon) or hull.is_empty:
                out_blocks.append(b)
                continue
            if b.area > 0 and hull.area / b.area > args.convex_max_bloat:
                out_blocks.append(b)  # 凸包膨胀太狠，保留原形
                n_kept_original += 1
            else:
                out_blocks.append(hull)
        blocks = [b for b in out_blocks if isinstance(b, Polygon) and not b.is_empty]
        print(f"  convex_quadrilateral applied: {len(blocks)} blocks "
              f"(max-bloat={args.convex_max_bloat}, {n_kept_original} 保留原形)")

    if args.inset > 0:
        before = len(blocks)
        inset_blocks = []
        for b in blocks:
            shrunk = b.buffer(-args.inset)
            if shrunk.is_empty:
                continue
            if isinstance(shrunk, Polygon):
                inset_blocks.append(shrunk)
            elif isinstance(shrunk, MultiPolygon):
                inset_blocks.extend(g for g in shrunk.geoms
                                    if isinstance(g, Polygon) and not g.is_empty)
        blocks = inset_blocks
        print(f"  inset {args.inset}m: {before} → {len(blocks)} blocks")

    n_kept = len(blocks)

    # ---- Render ----
    print(f"\n=== Render ===")
    fig, ax = plt.subplots(figsize=(args.fig_inches, args.fig_inches), dpi=args.dpi)
    if args.jitter > 0 or args.lowpoly:
        _draw_jittered_block_layer(
            ax, blocks, base_color=args.color,
            segment_m=args.jitter_seg, jitter_m=args.jitter,
            lowpoly=args.lowpoly, shadow_color="#a8a08c", seed_base=42)
    else:
        ax.add_collection(_polys_to_collection(
            blocks, facecolor=args.color, edgecolor=args.edge,
            linewidths=args.lw, alpha=1.0))

    # ---- landmark layer（在 block 之上）----
    n_landmarks = 0
    if args.show_landmarks:
        landmark_polys = []
        n_skipped_unprintable = 0
        for p, flag in zip(polys, landmark_flags):
            if not isinstance(p, Polygon) or p.is_empty:
                continue
            if flag or p.area >= args.landmark_area:
                if args.landmark_min_printable > 0 and p.area < args.landmark_min_printable:
                    n_skipped_unprintable += 1
                    continue
                s = p.simplify(25.0, preserve_topology=True)
                if isinstance(s, MultiPolygon):
                    s = max(s.geoms, key=lambda g: g.area)
                if isinstance(s, Polygon) and not s.is_empty:
                    landmark_polys.append(s)
        n_landmarks = len(landmark_polys)
        n_tag = sum(landmark_flags)
        print(f"  landmarks: {n_landmarks} "
              f"(tag={n_tag}, size≥{args.landmark_area:g}m² 触发, "
              f"剔 < {args.landmark_min_printable:g}m² 不可打印: {n_skipped_unprintable})")
        if landmark_polys:
            if args.jitter > 0 or args.lowpoly:
                # 同 block 走 jitter+lowpoly，shadow 用深橙保持橙色族
                _draw_jittered_block_layer(
                    ax, landmark_polys, base_color=args.landmark_color,
                    segment_m=args.jitter_seg, jitter_m=args.jitter,
                    lowpoly=args.lowpoly, shadow_color="#7a2810",
                    seed_base=2026)
            else:
                ax.add_collection(_polys_to_collection(
                    landmark_polys, facecolor=args.landmark_color,
                    edgecolor=args.landmark_edge, linewidths=0.5, alpha=0.95))

    bx = ctx["bbox_utm"]; ox, oy = ctx["origin"]
    ax.set_xlim(bx[0]-ox, bx[2]-ox); ax.set_ylim(bx[1]-oy, bx[3]-oy)
    ax.set_aspect("equal"); ax.set_xticks([]); ax.set_yticks([])

    opts = []
    if args.convex: opts.append(f"convex{args.convex_max_bloat:g}")
    if args.filter_empty > 0: opts.append(f"≥{args.filter_empty}bldg")
    if args.min_area > 0: opts.append(f"min{int(args.min_area)}")
    if args.max_area > 0: opts.append(f"max{int(args.max_area)}")
    if args.inset > 0: opts.append(f"inset{args.inset:g}m")
    if args.jitter > 0: opts.append(f"jit{args.jitter:g}m")
    if args.lowpoly: opts.append("lowpoly")
    if args.subtract_exclusions:
        opts.append(f"subexcl-r{args.road_inset:g}-w{args.water_inset:g}")
    if args.show_landmarks: opts.append(f"lm{int(args.landmark_area)}")
    opt_str = "_".join(opts) if opts else "raw"

    title = (f"city_blocks polygonize — {args.city}\n"
             f"raw={n_raw}  kept={n_kept}  tier={args.road_tier}  "
             f"water={args.use_water}  opts={opt_str}")
    ax.set_title(title, fontsize=14, family='monospace')

    out_path = OUT_DIR / f"_BLOCKS_{args.city}_{opt_str}.png"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fig.tight_layout(pad=0.5)
    fig.savefig(out_path, dpi=args.dpi, bbox_inches="tight",
                facecolor="white", pad_inches=0.2)
    plt.close(fig)
    print(f"  → {out_path}")


if __name__ == "__main__":
    main()
