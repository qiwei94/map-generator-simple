#!/usr/bin/env python3
"""Batch city generator — runs pipeline for multiple cities with fault tolerance.

Usage:
    python3 tools/batch_generate.py cities.json --output /mnt/map-output/results
    python3 tools/batch_generate.py cities.json --output /mnt/map-output/results --workers 3
    python3 tools/batch_generate.py cities.json --output /mnt/map-output/results --limit 5
    python3 tools/batch_generate.py cities.json --output /mnt/map-output/results --only paris_eiffel,tokyo_shibuya
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PBF_BASE = os.environ.get("PBF_BASE", "/mnt/map-cache/pbf_cache")
PBF_CACHE_DIR = "/opt/map-generator/pbf_cache_local"
PBF_CACHE_MAX_GB = 10
DATA_SERVER_IP = "172.16.164.53"
DATA_SERVER_PBF_BASE = "/root/map-cache/pbf_cache"
VENV_PYTHON = os.environ.get("VENV_PYTHON", "/opt/pipeline-venv/bin/python3")
TIMEOUT_SECONDS = 900  # 15 min per city
MAX_RETRIES = 1


def check_resources():
    """Check disk and memory before starting."""
    import shutil
    errors = []

    for path, label, min_gb in [("/mnt/map-output", "output disk", 5), ("/", "system disk", 2)]:
        usage = shutil.disk_usage(path)
        free_gb = usage.free / 1024**3
        if free_gb < min_gb:
            errors.append(f"{label}: {free_gb:.1f}GB free < {min_gb}GB minimum")

    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    avail_gb = int(line.split()[1]) / 1024 / 1024
                    if avail_gb < 4:
                        errors.append(f"memory: {avail_gb:.1f}GB available < 4GB minimum")
                    break
    except FileNotFoundError:
        pass

    return errors


def _precache_pbfs(pending: list[dict]) -> None:
    """Pre-copy unique PBFs from data_server to local SSD via SCP.

    Copies PBFs in affinity order (same as pending sort) so that
    same-PBF cities cluster together. Earlier groups stay cached
    while their cities are processed, then get evicted organically.
    """
    unique = {c["pbf"] for c in pending}
    os.makedirs(PBF_CACHE_DIR, exist_ok=True)

    already_cached = sum(1 for p in unique if os.path.exists(os.path.join(PBF_CACHE_DIR, p)))
    to_copy = len(unique) - already_cached

    print(f"\n  PBFs: {already_cached} cached, {to_copy} to SCP "
          f"(affinity-sorted, limit {PBF_CACHE_MAX_GB}GB)")

    if to_copy == 0:
        print(f"  All PBFs already cached!\n")
        return

    t_start = time.time()
    copied_mb = 0

    # Copy in affinity order (pending is already sorted by PBF)
    seen = set()
    for c in pending:
        pbf_fn = c["pbf"]
        if pbf_fn in seen:
            continue
        seen.add(pbf_fn)

        local = os.path.join(PBF_CACHE_DIR, pbf_fn)
        if os.path.exists(local):
            continue

        remote = f"root@{DATA_SERVER_IP}:{DATA_SERVER_PBF_BASE}/{pbf_fn}"
        print(f"    ⬇️  {pbf_fn}...", end=" ", flush=True)
        t0 = time.time()
        ret = subprocess.run(
            ["scp", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=30",
             remote, local],
            capture_output=True, timeout=600,
        )
        elapsed = time.time() - t0
        if ret.returncode != 0:
            err = ret.stderr.decode().strip()
            print(f"FAILED ({err}) — NFS fallback")
            continue
        sz = os.path.getsize(local) / 1024**2
        rate = sz / elapsed if elapsed > 0 else 0
        copied_mb += sz
        print(f"{sz:.0f}MB in {elapsed:.0f}s ({rate:.0f}MB/s)")

        # LRU eviction
        total = sum(
            os.path.getsize(os.path.join(PBF_CACHE_DIR, f))
            for f in os.listdir(PBF_CACHE_DIR)
            if os.path.isfile(os.path.join(PBF_CACHE_DIR, f))
        ) / 1024**3
        if total > PBF_CACHE_MAX_GB:
            cached = sorted(
                (f, os.path.getmtime(os.path.join(PBF_CACHE_DIR, f)))
                for f in os.listdir(PBF_CACHE_DIR)
                if f.endswith(".pbf") and f != pbf_fn
            )
            for fn, _ in cached:
                fp = os.path.join(PBF_CACHE_DIR, fn)
                sz_gb = os.path.getsize(fp) / 1024**3
                os.remove(fp)
                total -= sz_gb
                print(f"    🗑️  Evicted {fn} ({sz_gb:.1f}GB)")
                if total <= PBF_CACHE_MAX_GB * 0.8:
                    break

    elapsed = time.time() - t_start
    rate = copied_mb / elapsed if elapsed > 0 else 0
    print(f"  Pre-cache done: {copied_mb:.0f}MB in {elapsed:.0f}s ({rate:.0f}MB/s)\n")


def _resolve_pbf(pbf_filename: str) -> str:
    """Return local PBF path if cached, otherwise NFS path."""
    local = os.path.join(PBF_CACHE_DIR, pbf_filename)
    if os.path.exists(local):
        return local
    return os.path.join(PBF_BASE, pbf_filename)


def run_one_city(city_config: dict, output_base: str, generate_script: str, timeout: int = 900) -> dict:
    """Run pipeline for a single city. Returns status dict."""
    city = city_config["city"]
    bbox = city_config["bbox"]
    pbf = _resolve_pbf(city_config["pbf"])
    city_output = os.path.join(output_base, city)

    result = {
        "city": city,
        "country": city_config.get("country", "unknown"),
        "bbox": bbox,
        "status": "pending",
        "stages": {},
        "start_time": datetime.now().isoformat(),
    }

    if not os.path.exists(pbf):
        result["status"] = "skipped"
        result["error"] = f"PBF not found: {pbf}"
        return result

    os.makedirs(city_output, exist_ok=True)
    log_path = os.path.join(city_output, "pipeline.log")

    cmd = [
        VENV_PYTHON, generate_script,
        f"--bbox={bbox}",
        "--pbf", pbf,
        "--city", city,
        "--auto-params",
        "--png",
    ]

    result["status"] = "running"
    t0 = time.time()

    try:
        with open(log_path, "w") as log_file:
            proc = subprocess.run(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                cwd=str(PROJECT_ROOT),
                env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT)},
            )

        duration = time.time() - t0
        result["duration_s"] = round(duration, 1)

        if proc.returncode == 0:
            # Check output files exist
            output_files = list(Path(city_output).glob("*.3mf")) + list(Path(city_output).glob("*.png"))
            if output_files:
                result["status"] = "success"
                result["output_files"] = [str(f) for f in output_files]

                # Move 3MF and PNG to results dir
                for src in PROJECT_ROOT.glob(f"output/{city}/*"):
                    dst = Path(city_output) / src.name
                    if not dst.exists():
                        shutil.move(str(src), str(dst))
            else:
                # Check if outputs went to output/ dir instead
                alt_output = PROJECT_ROOT / "output" / city
                if alt_output.exists():
                    alt_files = list(alt_output.glob("*"))
                    for src in alt_files:
                        dst = Path(city_output) / src.name
                        if not dst.exists():
                            shutil.move(str(src), str(dst))
                    result["status"] = "success"
                    result["output_files"] = [str(Path(city_output) / f.name) for f in alt_files]
                else:
                    result["status"] = "failed"
                    result["error"] = "pipeline returned 0 but no output files"
        else:
            result["status"] = "failed"
            result["error"] = f"exit code {proc.returncode}"

    except subprocess.TimeoutExpired:
        result["status"] = "timeout"
        result["error"] = f"exceeded {TIMEOUT_SECONDS}s limit"
        result["duration_s"] = TIMEOUT_SECONDS

    except Exception as e:
        result["status"] = "failed"
        result["error"] = str(e)
        result["duration_s"] = round(time.time() - t0, 1)

    result["end_time"] = datetime.now().isoformat()

    # Write per-city status
    status_path = os.path.join(city_output, "status.json")
    with open(status_path, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    return result


def main():
    parser = argparse.ArgumentParser(description="Batch city generator")
    parser.add_argument("cities_json", help="Path to cities.json")
    parser.add_argument("--output", default="/mnt/map-output/results", help="Output base directory")
    parser.add_argument("--workers", type=int, default=0, help="Parallel workers (0=auto)")
    parser.add_argument("--limit", type=int, default=0, help="Only run first N cities")
    parser.add_argument("--only", type=str, default="", help="Comma-separated city names to run")
    parser.add_argument("--retry-failed", action="store_true", help="Retry previously failed cities")
    parser.add_argument("--timeout", type=int, default=0, help="Per-city timeout in seconds (0=use default 900)")
    args = parser.parse_args()

    if args.timeout > 0:
        TIMEOUT_SECONDS = args.timeout

    with open(args.cities_json) as f:
        cities = json.load(f)

    # Filter
    if args.only:
        names = set(args.only.split(","))
        cities = [c for c in cities if c["city"] in names]
    if args.limit > 0:
        cities = cities[:args.limit]

    # Skip completed
    pending = []
    skipped_done = 0
    for c in cities:
        status_file = os.path.join(args.output, c["city"], "status.json")
        if os.path.exists(status_file):
            with open(status_file) as f:
                prev = json.load(f)
            if prev.get("status") == "success":
                skipped_done += 1
                continue  # always skip success
            if prev.get("status") in ("failed", "timeout") and not args.retry_failed:
                skipped_done += 1
                continue  # skip failed unless --retry-failed
        pending.append(c)

    # PBF-affinity scheduling: sort by PBF so same-PBF cities cluster together.
    # This maximizes local-cache hits and minimizes eviction churn.
    pending.sort(key=lambda c: c["pbf"])

    # Auto workers: cap at CPU count, use memory as abundance signal
    if args.workers <= 0:
        try:
            cpu_count = len(os.sched_getaffinity(0))
        except AttributeError:
            cpu_count = os.cpu_count() or 4
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        total_gb = int(line.split()[1]) / 1024 / 1024
                        args.workers = max(1, min(cpu_count, int(total_gb // 2)))
                        break
        except FileNotFoundError:
            args.workers = cpu_count

    if args.timeout > 0:
        TIMEOUT_SECONDS = args.timeout
    else:
        TIMEOUT_SECONDS = 900

    print("=" * 60)
    print(f"  Batch Generate — {len(cities)} cities total")
    print(f"  Skipped (done): {skipped_done}")
    print(f"  Pending: {len(pending)}")
    print(f"  Workers: {args.workers}")
    print(f"  Output: {args.output}")
    print(f"  Timeout: {TIMEOUT_SECONDS}s per city")
    print("=" * 60)

    # Pre-cache PBFs to local SSD (sequential, before workers start)
    _precache_pbfs(pending)

    # Resource check
    errors = check_resources()
    if errors:
        for e in errors:
            print(f"  ⚠️  {e}")
        print("  Continuing anyway...")
    print()

    generate_script = str(PROJECT_ROOT / "generate_city_legacy.py")
    os.makedirs(args.output, exist_ok=True)

    results = []
    success = 0
    failed = 0
    t_start = time.time()

    if args.workers == 1:
        for i, city in enumerate(pending):
            print(f"[{i+1}/{len(pending)}] {city['city']} ...", end=" ", flush=True)
            r = run_one_city(city, args.output, generate_script, TIMEOUT_SECONDS)
            results.append(r)
            if r["status"] == "success":
                success += 1
                print(f"✅ {r.get('duration_s', 0):.0f}s")
            else:
                failed += 1
                print(f"❌ {r['status']}: {r.get('error', '')}")
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            futures = {}
            for city in pending:
                f = pool.submit(run_one_city, city, args.output, generate_script, TIMEOUT_SECONDS)
                futures[f] = city

            for i, f in enumerate(as_completed(futures)):
                city = futures[f]
                try:
                    r = f.result()
                except Exception as e:
                    r = {"city": city["city"], "status": "failed", "error": str(e)}
                results.append(r)
                if r["status"] == "success":
                    success += 1
                    print(f"[{i+1}/{len(pending)}] ✅ {r['city']} ({r.get('duration_s', 0):.0f}s)")
                else:
                    failed += 1
                    print(f"[{i+1}/{len(pending)}] ❌ {r['city']}: {r['status']} - {r.get('error', '')}")

    total_time = time.time() - t_start

    # Write batch report
    report = {
        "total": len(cities),
        "pending": len(pending),
        "success": success,
        "failed": failed,
        "skipped_done": skipped_done,
        "duration_hours": round(total_time / 3600, 2),
        "timestamp": datetime.now().isoformat(),
        "cities": results,
    }
    report_path = os.path.join(args.output, "batch_report.json")
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print()
    print("=" * 60)
    print(f"  DONE — {success} ✅ / {failed} ❌ / {skipped_done} skipped")
    print(f"  Time: {total_time/60:.1f} min")
    print(f"  Report: {report_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
