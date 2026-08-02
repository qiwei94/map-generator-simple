#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机 worker：轮询云端 server，拉取任务 → 本地执行 → 上传产物 → 标记完成。

用法：
    python tools/cloud_worker.py --server http://8.136.0.235:8788 --token SECRET
    python tools/cloud_worker.py --server http://127.0.0.1:8788 --token test --dry-run

数据完整性保证：
- 产物上传走 .part 隔离 + sha256 端到端校验
- finish 时 server 验证磁盘文件完整后才改状态
- 失败时 server 清理 .part 残留，前端不会看到坏数据
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def run_task(spec: dict, dry_run: bool = False) -> tuple[bool, str, list[Path]]:
    """执行 spec.cmd，返回 (ok, error_msg, produced_files)。

    注意：spec["cwd"] 是云端路径，本机 worker 必须用自己的 _ROOT。
    """
    cmd = list(spec["cmd"])  # copy，避免修改原 spec
    # cmd[0] 是云端的 sys.executable，替换为本机 Python
    cmd[0] = sys.executable
    cwd = str(_ROOT)  # 始终用本机项目根，不用云端路径
    env = os.environ.copy()
    env.update(spec.get("env_extra", {}))

    if dry_run:
        # dry-run：不真跑管线，生成一个假产物验证回路
        city = cmd[cmd.index("--city") + 1] if "--city" in cmd else "dryrun"
        out_dir = Path(cwd) / "output" / city
        out_dir.mkdir(parents=True, exist_ok=True)
        fake_glb = out_dir / f"{city}_draft.glb"
        fake_glb.write_bytes(b"FAKE_GLB_" + time.strftime("%H%M%S").encode())
        fake_png = out_dir / f"{city}_preview.png"
        fake_png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        print(f"  [dry-run] 生成假产物: {fake_glb.name}, {fake_png.name}")
        return True, "", [fake_glb, fake_png]

    print(f"  [worker] 执行: {' '.join(cmd[:4])}...")
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=600)
    wall = time.time() - t0
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "")[-500:]
        print(f"  [worker] 失败 ({wall:.0f}s): {err[:200]}")
        return False, err, []

    # 收集产物
    city = cmd[cmd.index("--city") + 1] if "--city" in cmd else ""
    slug = cmd[cmd.index("--slug") + 1] if "--slug" in cmd else city
    out_dir = Path(cwd) / "output" / (slug or city)
    produced = []
    if out_dir.is_dir():
        for ext in ("*.glb", "*.png", "*.3mf"):
            produced.extend(out_dir.glob(ext))
    print(f"  [worker] 完成 ({wall:.0f}s), 产物 {len(produced)} 个")
    return True, "", produced


def upload_files(server: str, token: str, job_id: str,
                 files: list[Path]) -> list[dict]:
    """上传产物到 server，返回 [{name, sha256, size}]。"""
    manifests = []
    for f in files:
        h = sha256_file(f)
        size = f.stat().st_size
        print(f"  [upload] {f.name} ({size/1024:.0f} KB, sha256={h[:12]}...)")
        with open(f, "rb") as fh:
            r = requests.post(
                f"{server}/api/worker/upload",
                params={"token": token, "job_id": job_id,
                        "filename": f.name, "sha256": h},
                files={"file": (f.name, fh)},
                timeout=120,
            )
        if r.status_code != 200:
            raise RuntimeError(f"上传失败 {f.name}: {r.status_code} {r.text[:200]}")
        manifests.append({"name": f.name, "sha256": h, "size": size})
    return manifests


def main():
    ap = argparse.ArgumentParser(description="本机 worker（轮询云端拉任务）")
    ap.add_argument("--server", required=True, help="云端 server URL")
    ap.add_argument("--token", required=True, help="WORKER_TOKEN")
    ap.add_argument("--poll-interval", type=int, default=5,
                    help="无任务时轮询间隔秒数（默认 5）")
    ap.add_argument("--dry-run", action="store_true",
                    help="不真跑管线，生成假产物验证回路")
    args = ap.parse_args()

    server = args.server.rstrip("/")
    print(f"[worker] 连接 {server}，轮询间隔 {args.poll_interval}s"
          f"{'（DRY-RUN）' if args.dry_run else ''}")

    while True:
        try:
            r = requests.get(f"{server}/api/worker/next",
                             params={"token": args.token}, timeout=15)
            if r.status_code == 401:
                print("[worker] token 无效，退出")
                sys.exit(1)
            if r.status_code != 200:
                print(f"[worker] next 异常: {r.status_code}")
                time.sleep(args.poll_interval)
                continue
            data = r.json()
        except requests.RequestException as e:
            print(f"[worker] 连接失败: {e}")
            time.sleep(args.poll_interval)
            continue

        if not data.get("job_id"):
            time.sleep(args.poll_interval)
            continue

        job_id = data["job_id"]
        spec = data.get("spec", {})
        print(f"\n[worker] 接到任务 {job_id} ({data.get('mode')})")

        ok, err, produced = run_task(spec, dry_run=args.dry_run)

        if ok and produced:
            try:
                manifests = upload_files(server, args.token, job_id, produced)
            except Exception as e:
                ok = False
                err = f"上传失败: {e}"
                manifests = []
        else:
            manifests = []

        # 标记完成
        try:
            r = requests.post(f"{server}/api/worker/finish",
                              json={"job_id": job_id, "token": args.token,
                                    "ok": ok, "error": err, "files": manifests},
                              timeout=15)
            print(f"  [worker] finish → {r.status_code} {r.json()}")
        except requests.RequestException as e:
            print(f"  [worker] finish 请求失败: {e}")


if __name__ == "__main__":
    main()
