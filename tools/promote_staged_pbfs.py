#!/usr/bin/env python3
"""Promote complete staged PBFs into a worker's local hot filesystem."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="等待暂存 PBF 完整后原子复制到 worker 本地目录")
    parser.add_argument("--source-dir", required=True)
    parser.add_argument("--dest-dir", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--files", required=True,
                        help="逗号分隔的 PBF 文件名")
    parser.add_argument("--wait-seconds", type=int, default=43200)
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--detach", action="store_true")
    parser.add_argument("--log-name", default="promote_staged_pbfs.log")
    return parser.parse_args()


def selected_files(raw: str, sizes: dict[str, int]) -> list[str]:
    names = [value.strip() for value in raw.split(",") if value.strip()]
    for name in names:
        if Path(name).name != name or name not in sizes:
            raise ValueError(f"untrusted or unknown PBF name: {name}")
    return names


def promote(source: Path, destination: Path, expected_size: int) -> bool:
    if not source.is_file() or source.stat().st_size != expected_size:
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    incoming = destination.parent / f".{destination.name}.incoming"
    shutil.copy2(source, incoming)
    if incoming.stat().st_size != expected_size:
        raise OSError(f"copy size mismatch for {destination.name}")
    os.replace(incoming, destination)
    return True


def detach(args: argparse.Namespace) -> int:
    log_path = ROOT / "tmp" / Path(args.log_name).name
    log_path.parent.mkdir(parents=True, exist_ok=True)
    child_args = [value for value in sys.argv[1:] if value != "--detach"]
    with log_path.open("ab", buffering=0) as stream:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), *child_args],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    time.sleep(0.5)
    print(json.dumps({"pid": process.pid, "running": process.poll() is None,
                      "log": str(log_path)}))
    return 0 if process.poll() is None else int(process.returncode or 1)


def main() -> int:
    args = parse_args()
    if args.detach:
        return detach(args)

    manifest_path = Path(args.manifest)
    if not manifest_path.is_absolute():
        manifest_path = ROOT / manifest_path
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    sizes = {str(key): int(value)
             for key, value in manifest.get("files", {}).items()}
    try:
        pending = selected_files(args.files, sizes)
    except ValueError as exc:
        raise SystemExit(str(exc))

    source_dir = Path(args.source_dir).expanduser().resolve()
    dest_dir = Path(args.dest_dir).expanduser().resolve()
    deadline = time.time() + max(0, args.wait_seconds)
    while pending:
        for name in list(pending):
            if promote(source_dir / name, dest_dir / name, sizes[name]):
                pending.remove(name)
                print(f"[promote] ready: {name} ({sizes[name]} bytes)",
                      flush=True)
        if not pending:
            break
        if time.time() >= deadline:
            print("[promote] timed out: " + ", ".join(pending),
                  file=sys.stderr, flush=True)
            return 2
        print("[promote] waiting: " + ", ".join(pending), flush=True)
        time.sleep(max(1, args.poll_seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
