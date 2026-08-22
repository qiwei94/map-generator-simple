#!/usr/bin/env python3
"""Launch a detached, logged showcase batch on macOS or Linux/WSL."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def build_command(args: argparse.Namespace) -> list[str]:
    command = [
        sys.executable,
        str(ROOT / "tools" / "generate_showcase_samples.py"),
        "--size-km", f"{args.size_km:g}",
        "--only", args.only,
        "--min-free-gb", f"{args.min_free_gb:g}",
    ]
    if args.force:
        command.append("--force")
    if args.fail_fast:
        command.append("--fail-fast")
    if args.pbf_size_manifest:
        command.extend(["--pbf-size-manifest", args.pbf_size_manifest])
    if args.wait_seconds:
        command.extend(["--wait-seconds", str(args.wait_seconds),
                        "--poll-seconds", str(args.poll_seconds)])
    return command


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="后台启动带日志的城市样品批处理")
    parser.add_argument("--only", required=True,
                        help="逗号分隔的城市 key")
    parser.add_argument("--size-km", type=float, default=25.0)
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--osmium-bin", default="")
    parser.add_argument("--log-name", default="showcase_25km.log")
    parser.add_argument("--min-free-gb", type=float, default=8.0)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument("--pbf-size-manifest", default="")
    parser.add_argument("--wait-seconds", type=int, default=0)
    parser.add_argument("--poll-seconds", type=int, default=30)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.only.strip():
        raise SystemExit("--only must contain at least one city key")

    log_path = ROOT / "tmp" / Path(args.log_name).name
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cache_dir = Path(args.cache_dir).expanduser().resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["MAP_GEN_CACHE_DIR"] = str(cache_dir)
    if args.osmium_bin:
        osmium_bin = Path(args.osmium_bin).expanduser().resolve()
        if not osmium_bin.is_file():
            raise SystemExit(f"osmium binary not found: {osmium_bin}")
        env["OSMIUM_BIN"] = str(osmium_bin)

    command = build_command(args)
    with log_path.open("ab", buffering=0) as stream:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )

    time.sleep(0.5)
    return_code = process.poll()
    result = {
        "pid": process.pid,
        "running": return_code is None,
        "return_code": return_code,
        "log": str(log_path),
        "size_km": args.size_km,
        "cities": [key.strip() for key in args.only.split(",")
                   if key.strip()],
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if return_code is None else int(return_code or 1)


if __name__ == "__main__":
    raise SystemExit(main())
