#!/usr/bin/env python3
"""Generate verified city samples sequentially for the landing page.

The checked-in plan keeps the production gallery defaults.  Operators can use
``--size-km`` for an isolated comparison batch without rewriting that plan or
overwriting its canonical output directories.
"""
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


def city_for_size(city: dict, size_km: float) -> dict:
    """Return an isolated runtime entry for a non-default sample size."""
    runtime = dict(city)
    size_label = f"{size_km:g}".replace(".", "p")
    runtime["slug"] = f"showcase_{city['key']}_{size_label}km"
    runtime["sample_size_km"] = size_km
    return runtime


def input_problems(cities: list[dict], expected_sizes: dict[str, int]) -> list[str]:
    """Describe missing or partial PBFs without accepting false-ready inputs."""
    problems = []
    for city in cities:
        filename = city["pbf"]
        path = PBF_DIR / filename
        expected = expected_sizes.get(filename)
        if not path.is_file():
            problems.append(f"{filename}: missing")
        elif expected is not None and path.stat().st_size != int(expected):
            problems.append(
                f"{filename}: {path.stat().st_size}/{int(expected)} bytes")
    return problems


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
    scene_type = str(meta.get("scene_type") or "")
    building_density = float(profile.get("building_density") or 0)
    road_density = float(profile.get("road_density_km_per_km2") or 0)
    water_ratio = float(profile.get("water_ratio") or 0)
    if scene_type == "urban" and road_density <= 0:
        return False, "urban road extraction returned zero density"
    if scene_type == "urban" and building_density <= 0:
        return False, "urban building extraction returned zero density"
    if water_ratio >= 0.92 and building_density >= 20:
        return False, "implausible water coverage for populated frame"
    signature = meta.get("city_signature") or {}
    hard_failures = signature.get("hard_failures") or []
    if hard_failures:
        return False, str(hard_failures[0])
    if signature and not signature.get("showcase_candidate", False):
        return False, (
            "city signature score is too weak "
            f"({float(signature.get('overall') or 0):.3f})")
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
        # A southern/western bbox starts with '-'.  Keep option and value in
        # one token so argparse never mistakes the coordinates for an option.
        f"--bbox={bbox_arg}",
        "--pbf", str(Path("pbf_cache") / city["pbf"]),
        "--slug", city["slug"],
        "--title", city["title"],
        "--prototype", city["prototype"],
    ]
    print(f"\n[showcase] {city['key']} bbox={bbox_arg}", flush=True)
    return subprocess.run(cmd, cwd=ROOT, check=False).returncode


def main() -> int:
    parser = argparse.ArgumentParser(
        description="顺序生成首页全球城市样品")
    parser.add_argument("--only", default="",
                        help="逗号分隔的城市 key；默认计划内全部")
    parser.add_argument("--limit", type=int, default=0,
                        help="最多处理几个尚未完成的城市")
    parser.add_argument("--force", action="store_true",
                        help="即使已有合格结果也重新生成")
    parser.add_argument("--size-km", type=float, default=None,
                        help="覆盖计划尺寸；输出隔离为 showcase_<key>_<size>km")
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument("--min-free-gb", type=float, default=8.0,
                        help="每个城市开始前要求的最小磁盘余量（默认 8GB）")
    parser.add_argument("--pbf-size-manifest", default="",
                        help="PBF 精确字节清单；用于拒绝未完成传输")
    parser.add_argument("--wait-seconds", type=int, default=0,
                        help="等待 PBF 就绪和前一批释放锁的最长秒数")
    parser.add_argument("--poll-seconds", type=int, default=30)
    args = parser.parse_args()

    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    size_km = (float(args.size_km) if args.size_km is not None
               else float(plan["size_km"]))
    if not 1 <= size_km <= 100:
        parser.error("--size-km must be between 1 and 100")
    selected = {key.strip() for key in args.only.split(",") if key.strip()}
    cities = [city for city in plan["cities"]
              if not selected or city["key"] in selected]
    if args.size_km is not None:
        cities = [city_for_size(city, size_km) for city in cities]
    if selected:
        missing = selected - {city["key"] for city in cities}
        if missing:
            parser.error(f"unknown city keys: {', '.join(sorted(missing))}")

    expected_sizes: dict[str, int] = {}
    if args.pbf_size_manifest:
        manifest_path = Path(args.pbf_size_manifest)
        if not manifest_path.is_absolute():
            manifest_path = ROOT / manifest_path
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected_sizes = {str(key): int(value)
                          for key, value in manifest.get("files", {}).items()}
    deadline = time.time() + max(0, args.wait_seconds)
    while True:
        problems = input_problems(cities, expected_sizes)
        if not problems:
            break
        if args.wait_seconds <= 0 or time.time() >= deadline:
            print("PBF inputs are not ready: " + "; ".join(problems),
                  file=sys.stderr, flush=True)
            return 4
        print("[showcase] waiting for PBF inputs: " + "; ".join(problems),
              flush=True)
        time.sleep(max(1, args.poll_seconds))

    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_file = LOCK_PATH.open("w")
    while True:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if args.wait_seconds <= 0 or time.time() >= deadline:
                print("another showcase batch is already running",
                      file=sys.stderr)
                return 2
            print("[showcase] waiting for the active batch lock", flush=True)
            time.sleep(max(1, args.poll_seconds))

    done: list[str] = []
    skipped: list[str] = []
    failed: dict[str, str] = {}
    processed = 0
    write_status(state="running", total=len(cities), current=None,
                 size_km=size_km,
                 done=done, skipped=skipped, failed=failed)

    for city in cities:
        free_gb = shutil.disk_usage(ROOT).free / (1024 ** 3)
        if free_gb < args.min_free_gb:
            failed[city["key"]] = (
                f"stopped safely: only {free_gb:.1f}GB free "
                f"(< {args.min_free_gb:.1f}GB)")
            write_status(state="blocked_low_disk", total=len(cities),
                         size_km=size_km,
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
                     size_km=size_km, slug=city["slug"],
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
                 size_km=size_km,
                 done=done, skipped=skipped, failed=failed)
    print(f"\n[showcase] {state}: generated={len(done)} "
          f"skipped={len(skipped)} failed={len(failed)}", flush=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
