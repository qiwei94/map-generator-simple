#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""为任意 bbox 区域生成风格画廊（4 种风格的 2D 图 + 评分）。

与 batch_generate_gallery 的区别：那个只服务内置预设城市，这个接受
任意 bbox——用户在地图上框完就能看到自己那块地的四种风格。

复用同一条链路：CityPreset 动态注册 → CityHarness.prepare（走
PipelineCache）→ 每风格 preprocess + PIL 评审渲染。实测 10km 见方
约 49s 出 4 张。

用法:
    python tools/gen_area_gallery.py --bbox 30.20,120.09,30.29,120.20 \
        --pbf pbf_cache/zhejiang-latest.osm.pbf --slug custom_abc123 \
        --title "西湖" --prototype landscape
"""
import argparse
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

if hasattr(sys.stdout, "reconfigure"):
    # Long 25 km stages can otherwise sit in an 8 KiB redirected-output
    # buffer for many minutes and look hung to the batch monitor.
    sys.stdout.reconfigure(
        errors="replace", line_buffering=True, write_through=True)

from aesthetic.presets import CityPreset, register_preset  # noqa: E402
from tools.batch_generate_gallery import (  # noqa: E402
    STYLE_VARIANTS, generate_city_gallery)

# 参考图目录按原型选（仅用于可选的 VLM 评审，缺失不影响渲染）
_REF_DIR = {"landscape": "杭州", "skyline": "芝加哥",
            "terrain": "重庆", "minimal": "杭州"}


def main():
    ap = argparse.ArgumentParser(description="任意区域风格画廊生成")
    ap.add_argument("--bbox", required=True,
                    help="south,west,north,east（WGS84）")
    ap.add_argument("--pbf", required=True, help="PBF 相对路径")
    ap.add_argument("--slug", required=True, help="输出目录名（区域标识）")
    ap.add_argument("--title", default="", help="展示名")
    ap.add_argument("--prototype", default="landscape",
                    choices=["landscape", "skyline", "terrain", "minimal"])
    ap.add_argument("--styles", default=None, help="逗号分隔，默认全部 4 种")
    ap.add_argument("--out-dir",
                    default=os.path.join(_ROOT, "output", "style_gallery"))
    args = ap.parse_args()

    try:
        s, w, n, e = (float(x) for x in args.bbox.split(","))
    except ValueError:
        ap.error("--bbox 格式: south,west,north,east")
    if not (n > s and e > w):
        ap.error("--bbox 南北/东西颠倒")

    styles = ([x.strip() for x in args.styles.split(",") if x.strip()]
              if args.styles else list(STYLE_VARIANTS))
    for st in styles:
        if st not in STYLE_VARIANTS:
            ap.error(f"未知风格 '{st}'，可选: {list(STYLE_VARIANTS)}")

    register_preset(CityPreset(
        name=args.slug,
        bbox=(s, w, n, e),
        pbf=args.pbf,
        prototype=args.prototype,
        reference_dir=_REF_DIR.get(args.prototype, "杭州"),
        description=args.title or args.slug,
    ))

    print(f"[area-gallery] {args.slug} bbox=({s},{w},{n},{e}) "
          f"pbf={args.pbf} prototype={args.prototype}")
    t0 = time.time()
    meta = generate_city_gallery(args.slug, styles, args.out_dir,
                                 use_cache=True)
    # 补上展示名，前端直接用
    if args.title:
        meta["title"] = args.title
        import json
        mp = os.path.join(args.out_dir, args.slug, "gallery_metadata.json")
        with open(mp, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
    ok = sum(1 for v in meta.get("styles", {}).values() if "renders" in v)
    print(f"[area-gallery] done in {time.time() - t0:.1f}s, "
          f"{ok}/{len(styles)} styles OK")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
