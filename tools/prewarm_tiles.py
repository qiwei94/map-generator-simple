# -*- coding: utf-8 -*-
"""热门区域缓存预热工具（Phase 3）。

对 cities.json 中的热门取景框逐个跑 generate_city.py --draft，
把"量化框共享缓存"提前填满：
  - tmp/osmium_{layer}_{snapbbox}.geojson  各图层 GeoJSON
  - 高程网格缓存（cache/grids）
  - cache/pipeline/snap_{snapbbox}/        preprocess 阶段缓存

预热后，用户请求落在已预热的量化格内时，draft 生成约 30~60 秒。

用法：
  python tools/prewarm_tiles.py                 # 预热全部（pbf 存在的条目）
  python tools/prewarm_tiles.py --only hangzhou_westlake,shanghai_bund
  python tools/prewarm_tiles.py --list          # 只列出将预热的量化格
  python tools/prewarm_tiles.py --dry-run       # 打印命令不执行
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from _TEXTURE_STYLE_OF_DEEPSEEK._tile_grid import snap_bbox  # noqa: E402

CITIES_JSON = os.path.join(_ROOT, "cities.json")
PBF_DIR = os.path.join(_ROOT, "pbf_cache")


def load_entries():
    with open(CITIES_JSON, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser(description="热门区域缓存预热")
    ap.add_argument("--only", type=str, default="",
                    help="逗号分隔的 city 名过滤")
    ap.add_argument("--list", action="store_true",
                    help="只列出将预热的量化格，不执行")
    ap.add_argument("--dry-run", action="store_true",
                    help="打印命令但不执行")
    args = ap.parse_args()

    entries = load_entries()
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        entries = [e for e in entries if e["city"] in wanted]

    # 按量化格去重：同一 snap 格只需预热一次
    plan = []
    seen_cells = set()
    skipped_no_pbf = []
    for e in entries:
        s, w, n, east = [float(x) for x in e["bbox"].split(",")]
        pbf_path = os.path.join(PBF_DIR, e["pbf"])
        if not os.path.exists(pbf_path):
            skipped_no_pbf.append(e["city"])
            continue
        cell = snap_bbox(s, w, n, east)
        if cell in seen_cells:
            continue
        seen_cells.add(cell)
        plan.append((e, cell, pbf_path))

    print(f"预热计划: {len(plan)} 个量化格"
          f"（跳过缺 PBF: {len(skipped_no_pbf)}）")
    if skipped_no_pbf:
        print(f"  缺 PBF: {skipped_no_pbf}")
    for e, cell, pbf in plan:
        print(f"  {e['city']:<28} snap=({cell[0]:.2f},{cell[1]:.2f},"
              f"{cell[2]:.2f},{cell[3]:.2f})  pbf={os.path.basename(pbf)}")

    if args.list:
        return

    ok, failed = 0, []
    for i, (e, cell, pbf) in enumerate(plan, 1):
        city_name = f"prewarm_{e['city']}"
        cmd = [sys.executable, os.path.join(_ROOT, "generate_city.py"),
               "--bbox", e["bbox"], "--pbf", pbf,
               "--city", city_name, "--auto-params", "--draft"]
        print(f"\n[{i}/{len(plan)}] {e['city']} ...")
        if args.dry_run:
            print("  " + " ".join(cmd))
            continue
        t0 = time.time()
        r = subprocess.run(cmd, cwd=_ROOT)
        dt = time.time() - t0
        if r.returncode == 0:
            ok += 1
            print(f"[{i}/{len(plan)}] {e['city']} 预热完成 ({dt:.0f}s)")
        else:
            failed.append(e["city"])
            print(f"[{i}/{len(plan)}] {e['city']} 预热失败 (rc={r.returncode})")

    if not args.dry_run:
        print(f"\n预热汇总: 成功 {ok}, 失败 {len(failed)}"
              + (f" ({failed})" if failed else ""))


if __name__ == "__main__":
    main()
