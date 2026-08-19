#!/usr/bin/env python3
"""Generate verified 15 km city samples sequentially for the landing page."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image, ImageStat


ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = ROOT / "data" / "showcase_cities.json"
PBF_DIR = ROOT / "pbf_cache"
GALLERY_DIR = ROOT / "output" / "style_gallery"
STATUS_PATH = ROOT / "tmp" / "showcase_batch_status.json"
LOCK_PATH = ROOT / "tmp" / "showcase_batch.lock"
REQUIRED_STYLES = ("baseline", "block_fill", "dense_detail", "minimal")


def bbox_around(center: list[float], size_km: float) -> list[float]:
    """Return south, west, north, east for a physical square at center."""
    lat, lon = center
    half = size_km / 2.0
    lat_delta = half / 110.574
    lon_delta = half / (111.320 * max(0.01, math.cos(math.radians(lat))))
    return [lat - lat_delta, lon - lon_delta, lat + lat_delta, lon + lon_delta]


def write_status(**payload) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_PATH.with_suffix(".tmp")
    payload["updated_at"] = time.time()
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, STATUS_PATH)


def load_metadata(slug: str) -> dict | None:
    path = GALLERY_DIR / slug / "gallery_metadata.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def validate_gallery(city: dict, size_km: float) -> tuple[bool, str]:
    """Reject false-success galleries as well as missing or malformed renders."""
    meta = load_metadata(city["slug"])
    if not meta:
        return False, "missing gallery_metadata.json"
    profile = meta.get("profile", {})
    area = float(profile.get("area_km2") or 0)
    expected_area = size_km * size_km
    if not expected_area * 0.88 <= area <= expected_area * 1.12:
        return False, f"area {area:.1f} km2 is not {expected_area:.0f} km2"
    feature_total = sum(float(profile.get(key) or 0) for key in (
        "building_density", "road_density_km_per_km2", "water_ratio"))
    if feature_total <= 0:
        return False, "OSM extraction returned zero buildings, roads, and water"
    render_hashes: set[str] = set()
    for style in REQUIRED_STYLES:
        filename = (meta.get("styles", {}).get(style, {})
                    .get("renders", {}).get("topdown"))
        path = GALLERY_DIR / city["slug"] / str(filename or "")
        if not filename or not path.is_file():
            return False, f"missing {style} topdown render"
        try:
            with Image.open(path) as image:
                if image.width < 1600 or image.width != image.height:
                    return False, f"invalid {style} image size {image.size}"
                thumb = image.convert("L").resize((64, 64))
                if ImageStat.Stat(thumb).stddev[0] < 2.0:
                    return False, f"{style} render is visually blank"
            render_hashes.add(hashlib.sha256(path.read_bytes()).hexdigest())
        except OSError as exc:
            return False, f"cannot read {style}: {exc}"
    if len(render_hashes) < 2:
        return False, "all style renders are byte-identical"
    return True, "verified"


def generate(city: dict, size_km: float) -> int:
    bbox = bbox_around(city["center"], size_km)
    bbox_arg = ",".join(f"{value:.7f}" for value in bbox)
    cmd = [
        sys.executable,
        str(ROOT / "tools" / "gen_area_gallery.py"),
        "--bbox", bbox_arg,
        "--pbf", str(Path("pbf_cache") / city["pbf"]),
        "--slug", city["slug"],
        "--title", city["title"],
        "--prototype", city["prototype"],
    ]
    print(f"\n[showcase] {city['key']} bbox={bbox_arg}", flush=True)
    return subprocess.run(cmd, cwd=ROOT, check=False).returncode


def main() -> int:
    parser = argparse.ArgumentParser(
        description="顺序生成首页 15×15 km 全球城市样品")
    parser.add_argument("--only", default="",
                        help="逗号分隔的城市 key；默认计划内全部")
    parser.add_argument("--limit", type=int, default=0,
                        help="最多处理几个尚未完成的城市")
    parser.add_argument("--force", action="store_true",
                        help="即使已有合格结果也重新生成")
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument("--min-free-gb", type=float, default=8.0,
                        help="每个城市开始前要求的最小磁盘余量（默认 8GB）")
    args = parser.parse_args()

    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    size_km = float(plan["size_km"])
    selected = {key.strip() for key in args.only.split(",") if key.strip()}
    cities = [city for city in plan["cities"]
              if not selected or city["key"] in selected]
    if selected:
        missing = selected - {city["key"] for city in cities}
        if missing:
            parser.error(f"unknown city keys: {', '.join(sorted(missing))}")

    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_file = LOCK_PATH.open("w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another showcase batch is already running", file=sys.stderr)
        return 2

    done: list[str] = []
    skipped: list[str] = []
    failed: dict[str, str] = {}
    processed = 0
    write_status(state="running", total=len(cities), current=None,
                 done=done, skipped=skipped, failed=failed)

    for city in cities:
        free_gb = shutil.disk_usage(ROOT).free / (1024 ** 3)
        if free_gb < args.min_free_gb:
            failed[city["key"]] = (
                f"stopped safely: only {free_gb:.1f}GB free "
                f"(< {args.min_free_gb:.1f}GB)")
            write_status(state="blocked_low_disk", total=len(cities),
                         current=None, done=done, skipped=skipped,
                         failed=failed, free_gb=round(free_gb, 2))
            print(f"[showcase] {failed[city['key']]}", file=sys.stderr,
                  flush=True)
            return 3
        pbf = PBF_DIR / city["pbf"]
        if not pbf.is_file():
            failed[city["key"]] = f"missing PBF: {city['pbf']}"
            continue
        valid, detail = validate_gallery(city, size_km)
        if valid and not args.force:
            print(f"[showcase] skip {city['key']}: already verified", flush=True)
            skipped.append(city["key"])
            continue
        if args.limit and processed >= args.limit:
            break
        processed += 1
        write_status(state="running", total=len(cities), current=city["key"],
                     done=done, skipped=skipped, failed=failed)
        code = generate(city, size_km)
        valid, detail = validate_gallery(city, size_km)
        if code == 0 and valid:
            done.append(city["key"])
            print(f"[showcase] verified {city['key']}", flush=True)
        else:
            failed[city["key"]] = f"exit={code}; {detail}"
            print(f"[showcase] failed {city['key']}: {failed[city['key']]}",
                  file=sys.stderr, flush=True)
            if args.fail_fast:
                break

    state = "done" if not failed else "done_with_failures"
    write_status(state=state, total=len(cities), current=None,
                 done=done, skipped=skipped, failed=failed)
    print(f"\n[showcase] {state}: generated={len(done)} "
          f"skipped={len(skipped)} failed={len(failed)}", flush=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
