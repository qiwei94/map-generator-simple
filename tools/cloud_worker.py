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
import socket
import subprocess
import sys
import threading
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


def run_task(spec: dict, dry_run: bool = False, heartbeat=None,
             timeout_s: int = 7200) -> tuple[bool, str, list[Path]]:
    """执行 spec.cmd，返回 (ok, error_msg, produced_files)。

    注意：spec["cwd"] 是云端路径，本机 worker 必须用自己的 _ROOT。
    """
    cmd = list(spec["cmd"])  # copy，避免修改原 spec
    # cmd[0] 是云端的 sys.executable，替换为本机 Python
    cmd[0] = sys.executable
    cwd = str(_ROOT)  # 始终用本机项目根，不用云端路径
    env = os.environ.copy()
    env.update(spec.get("env_extra", {}))
    # spec 的 PATH 是服务端的，会覆盖本机 PATH，冲掉 pyosmium shim。
    # 强制把「python3→python3.9」shim 和本机 python bin 加回最前，
    # 让 tools/osmium 的 shebang(#!/usr/bin/env python3) 跑在带 pyosmium 的环境。
    _pybin = os.path.dirname(sys.executable)
    _shim = "/opt/pyshim"
    env["PATH"] = os.pathsep.join(
        [p for p in (_shim, _pybin) if os.path.isdir(p)]
        + [env.get("PATH", "")])

    if dry_run:
        # dry-run：不真跑管线，生成一个假产物验证回路
        city = cmd[cmd.index("--city") + 1] if "--city" in cmd else "dryrun"
        slug = cmd[cmd.index("--slug") + 1] if "--slug" in cmd else ""
        output_root = Path(os.environ.get(
            "STUDIO_OUTPUT_DIR", Path(cwd) / "output"))
        out_dir = (output_root / "style_gallery" / slug
                   if slug else output_root / city)
        out_dir.mkdir(parents=True, exist_ok=True)
        fake_png = out_dir / f"{city}_preview.png"
        fake_png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        if slug:
            fake_json = out_dir / "gallery.json"
            fake_json.write_text('{"styles": {}}', encoding="utf-8")
            produced = [fake_png, fake_json]
        else:
            fake_glb = out_dir / f"{city}_draft.glb"
            fake_glb.write_bytes(
                b"FAKE_GLB_" + time.strftime("%H%M%S").encode())
            produced = [fake_glb, fake_png]
        print("  [dry-run] 生成假产物: " +
              ", ".join(path.name for path in produced))
        return True, "", produced

    print(f"  [worker] 执行: {' '.join(cmd[:4])}...")
    t0 = time.time()
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    output_lines: list[str] = []
    output_lock = threading.Lock()
    stop_heartbeat = threading.Event()
    timed_out = threading.Event()
    lease_lost = threading.Event()

    def heartbeat_loop():
        failures = 0
        while not stop_heartbeat.wait(15):
            if time.time() - t0 > timeout_s:
                timed_out.set()
                proc.kill()
                return
            with output_lock:
                tail = "".join(output_lines[-200:])[-20_000:]
            if heartbeat is not None:
                try:
                    alive = heartbeat(tail)
                except Exception:
                    alive = False
                failures = 0 if alive else failures + 1
                if failures >= 3:
                    lease_lost.set()
                    proc.terminate()
                    return

    beat_thread = threading.Thread(target=heartbeat_loop, daemon=True)
    beat_thread.start()
    assert proc.stdout is not None
    for line in proc.stdout:
        with output_lock:
            output_lines.append(line)
            if len(output_lines) > 1000:
                del output_lines[:500]
    proc.wait()
    stop_heartbeat.set()
    beat_thread.join(timeout=2)
    with output_lock:
        complete_tail = "".join(output_lines)[-20_000:]
    if heartbeat is not None and not lease_lost.is_set():
        try:
            heartbeat(complete_tail)
        except Exception:
            pass
    wall = time.time() - t0
    if timed_out.is_set():
        print(f"  [worker] 超时 (>{timeout_s}s)")
        return False, f"计算超时（>{timeout_s}s），区域可能过大", []
    if lease_lost.is_set():
        return False, "计算节点与任务队列失去连接，已停止以避免重复计算", []
    if proc.returncode != 0:
        err = complete_tail[-1000:]
        print(f"  [worker] 失败 ({wall:.0f}s): {err[:200]}")
        return False, err, []

    # 收集产物
    city = cmd[cmd.index("--city") + 1] if "--city" in cmd else ""
    slug = cmd[cmd.index("--slug") + 1] if "--slug" in cmd else city
    output_root = Path(os.environ.get(
        "STUDIO_OUTPUT_DIR", Path(cwd) / "output"))
    out_dir = (output_root / "style_gallery" / slug
               if "--slug" in cmd else output_root / city)
    produced = []
    if out_dir.is_dir():
        for ext in ("*.glb", "*.png", "*.3mf", "*.json"):
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
    ap.add_argument("--worker-id", default=socket.gethostname(),
                    help="稳定的计算节点 ID（默认主机名）")
    ap.add_argument("--task-timeout", type=int, default=7200,
                    help="单任务最长秒数（默认 7200，即 2 小时）")
    ap.add_argument("--max-tasks", type=int, default=0,
                    help="完成指定数量后退出；0 表示持续轮询")
    args = ap.parse_args()

    server = args.server.rstrip("/")
    print(f"[worker] 连接 {server}，轮询间隔 {args.poll_interval}s"
          f"{'（DRY-RUN）' if args.dry_run else ''}")

    completed_tasks = 0
    while True:
        try:
            r = requests.get(f"{server}/api/worker/next",
                             params={"token": args.token,
                                     "worker_id": args.worker_id}, timeout=15)
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

        def send_heartbeat(log_tail: str) -> bool:
            response = requests.post(
                f"{server}/api/worker/heartbeat",
                json={"job_id": job_id, "token": args.token,
                      "worker_id": args.worker_id, "log_tail": log_tail},
                timeout=15,
            )
            return response.status_code == 200

        ok, err, produced = run_task(
            spec, dry_run=args.dry_run, heartbeat=send_heartbeat,
            timeout_s=args.task_timeout)

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
                                    "worker_id": args.worker_id,
                                    "ok": ok, "error": err, "files": manifests},
                              timeout=15)
            print(f"  [worker] finish → {r.status_code} {r.json()}")
        except requests.RequestException as e:
            print(f"  [worker] finish 请求失败: {e}")
        completed_tasks += 1
        if args.max_tasks and completed_tasks >= args.max_tasks:
            print(f"[worker] 已完成 {completed_tasks} 个任务，按配置退出")
            return


if __name__ == "__main__":
    main()
