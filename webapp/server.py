# -*- coding: utf-8 -*-
from __future__ import annotations
"""城市浮雕工坊 Web 服务。

轻量 FastAPI 后端，不 import 重管线（geopandas/trimesh），只做三件事：
1. 扫描 output/ 下已有产物（画廊、draft GLB、3MF、param_decision）
2. 以子进程方式触发 generate_city_legacy.py（通用 draft / full），异步跟踪任务
3. 托管前端静态页 + 产物文件

启动：python webapp/server.py  （默认 0.0.0.0:8787，手机同局域网可访问）
"""
import hashlib
import json
import math
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import journey as journey_mod

ROOT = Path(__file__).resolve().parent.parent
# 以 `python webapp/server.py` 启动时 sys.path[0] 是 webapp/，
# 项目根不在路径上 → 无法 import _TEXTURE_STYLE_OF_DEEPSEEK（坐标转换等）
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUTPUT_DIR = ROOT / "output"
GALLERY_DIR = OUTPUT_DIR / "style_gallery"
STATIC_DIR = Path(__file__).resolve().parent / "static"
JOB_LOG_DIR = ROOT / "tmp" / "webapp_jobs"
JOB_LOG_DIR.mkdir(parents=True, exist_ok=True)

# 与 generate_city_legacy.py PRESETS 保持一致（轻量副本，避免 import 重管线）
PRESETS = {
    "westlake": {
        "title": "杭州 · 西湖",
        "bbox": [30.13, 120.01, 30.36, 120.29],
        "prototype": "landscape",
    },
    "chicago": {
        "title": "芝加哥 · Loop",
        "bbox": [41.76, -87.77, 42.00, -87.49],
        "prototype": "skyline",
    },
    "chongqing": {
        "title": "重庆 · 渝中",
        "bbox": [29.43, 106.41, 29.66, 106.66],
        "prototype": "terrain",
    },
}

# 景点目录 → 额外预设城市（顶部候选栏扩充）
# 与 PRESETS 合并后统一暴露给 /api/cities，生成时走 bbox+pbf 路径
_LANDMARK_PRESETS: dict | None = None


def _landmark_presets() -> dict:
    """从 data/landmarks/catalog.json 加载景点为预设城市字典。

    返回 {id: {title, bbox, prototype, country, city}} 格式，
    与 PRESETS 结构兼容（额外多 country/city 供前端分组）。
    """
    global _LANDMARK_PRESETS
    if _LANDMARK_PRESETS is not None:
        return _LANDMARK_PRESETS
    result = {}
    for lm in _load_landmarks():
        lid = lm["id"]
        if lid in PRESETS:  # 避免与硬编码预设冲突
            continue
        result[lid] = {
            "title": f"{lm.get('city', '')} · {lm['name']}",
            "bbox": lm["bbox"],
            "prototype": lm.get("style") or "landscape",
            "country": lm.get("country", ""),
            "city": lm.get("city", ""),
            "name_en": lm.get("name_en", ""),
        }
    _LANDMARK_PRESETS = result
    return result

# PBF 地理覆盖表（data/pbf_coverage.json，80 区域，osmium header 实测）
# 三态：本地已有 → 可直接生成；仅远端有 → 可 scp 拉取；都没有 → 无数据
COVERAGE_PATH = ROOT / "data" / "pbf_coverage.json"
PBF_DIR = ROOT / "pbf_cache"
_COVERAGE: dict | None = None


def _coverage() -> dict:
    global _COVERAGE
    if _COVERAGE is None:
        try:
            _COVERAGE = json.loads(COVERAGE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _COVERAGE = {"regions": {}, "remote_host": "", "remote_dir": ""}
    return _COVERAGE


def _match_regions(bbox) -> list:
    """完整覆盖 bbox 的区域列表，按面积升序（最贴合的在前 → 处理最快）。"""
    s, w, n, e = bbox
    hits = []
    for name, info in _coverage().get("regions", {}).items():
        cs, cw, cn, ce = info["bbox"]
        if cs <= s and cw <= w and n <= cn and e <= ce:
            area = (cn - cs) * (ce - cw)
            hits.append((area, name, info["file"]))
    hits.sort()
    return [{"region": n, "file": f} for _, n, f in hits]


def _pbf_status(bbox) -> dict:
    """bbox → 数据可用性三态。

    {"state": "local"|"fetchable"|"none", "pbf": 相对路径|None,
     "region": 区域名|None, "fetch": 待拉取区域名|None}
    """
    matches = _match_regions(bbox)
    for m in matches:
        if (PBF_DIR / m["file"]).exists():
            return {"state": "local", "pbf": f"pbf_cache/{m['file']}",
                    "region": m["region"], "fetch": None}
    if matches:
        return {"state": "fetchable", "pbf": None,
                "region": matches[0]["region"], "fetch": matches[0]["region"]}
    return {"state": "none", "pbf": None, "region": None, "fetch": None}


def _find_pbf(bbox) -> str | None:
    """向后兼容：仅当本地已有数据时返回可用的 PBF 相对路径。"""
    return _pbf_status(bbox)["pbf"]


def _state_fields(bbox) -> dict:
    """供搜索/目录结果展平的数据状态字段。"""
    st = _pbf_status(bbox)
    return {"available": st["state"] == "local",
            "data_state": st["state"],
            "region": st["region"],
            "fetch": st["fetch"]}


app = FastAPI(title="Map Relief Studio")

# ---------------------------------------------------------------------------
# 任务管理（内存态，单机自用足够）
# ---------------------------------------------------------------------------
JOBS: dict = {}
JOBS_LOCK = threading.Lock()
JOB_SUBMIT_LOCK = threading.Lock()
_ACTIVE_JOB_STATUSES = {"starting", "pending", "running"}


class CustomArea(BaseModel):
    bbox: list[float]        # [south, west, north, east] WGS84
    name: str = ""           # 展示用地点名（可空）


class GenerateRequest(BaseModel):
    city: str = ""
    mode: str = "draft"  # draft | full
    style: str | None = None  # 画廊风格名（可选，带参生成）
    generation_profile: str = "classic"
    area: CustomArea | None = None    # 自定义区域（与 city 二选一）
    markers: list[list[float]] = []   # [[lat, lon], ...] 标注点（附近最高处染红）


GENERATION_PROFILES = {
    "classic": {
        "label": "标准生成",
        "description": "适用于所有区域，可先生成快速 3D 预览。",
        "scope": "all",
        "draft": True,
    },
    "quality_flat": {
        "label": "精细模型 · 平整街区",
        "description": "街区轮廓清晰克制，并与道路、岸线保持自然留白。",
        "scope": "westlake",
        "draft": False,
    },
    "quality_textured": {
        "label": "精细模型 · 地块起伏",
        "description": "保留不同地块的细微高低变化，触感更丰富。",
        "scope": "westlake",
        "draft": False,
    },
}


# 面向用户的异常分类：内部日志不外露，失败时只返回其中一类
# (code, 用户可读提示)
ERROR_CATEGORIES = {
    "oom":           "系统内存不足，请换更小的区域重试",
    "timeout":       "计算超时，区域可能过大，请缩小范围重试",
    "data_missing":  "该区域地图数据不足，请换个区域或先下载数据",
    "network":       "网络异常，外部数据拉取失败，请稍后重试",
    "render_failed": "渲染失败，请稍后重试或调整区域",
}

# 日志关键字 → 异常分类（顺序即优先级）
_ERROR_PATTERNS = [
    ("timeout", ("TimeoutExpired", "计算超时", "timeout")),
    ("data_missing", ("Filtered PBF is empty", "no coverage", "数据不足",
                      "未覆盖", "PBF not found", "找不到对应区域", "no {tag_type}")),
    ("network", ("ConnectionError", "Max retries exceeded", "下载失败",
                 "Download failed", "requests.exceptions", "ConnectTimeout")),
    ("oom", ("MemoryError", "Cannot allocate memory", "内存不足", "Killed")),
]


def _classify_error_text(text: str, retcode: int | None = None) -> tuple:
    """把子进程日志/错误文本归到几类用户可读异常。返回 (code, msg)。"""
    if retcode == -9:  # SIGKILL，几乎必是 OOM killer
        return "oom", ERROR_CATEGORIES["oom"]
    for code, kws in _ERROR_PATTERNS:
        for kw in kws:
            if kw.lower() in (text or "").lower():
                return code, ERROR_CATEGORIES[code]
    return "render_failed", ERROR_CATEGORIES["render_failed"]


def _classify_job_error(job: dict, retcode: int) -> tuple:
    """读任务日志尾部做异常分类（日志本身不外露）。"""
    tail = ""
    try:
        with open(job["log_path"], "r", encoding="utf-8", errors="replace") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 8000))
            tail = f.read()
    except OSError:
        pass
    return _classify_error_text(tail, retcode)


def _read_job_log_tail(job: dict, max_bytes: int = 20_000) -> str:
    try:
        with open(job["log_path"], "r", encoding="utf-8",
                  errors="replace") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            return f.read()
    except OSError:
        return ""


def _job_progress(job: dict, log_tail: str) -> tuple[int, str]:
    """Estimate coarse progress from real pipeline markers.

    Percentages deliberately move in broad stages: they communicate that the
    process is alive without pretending we can measure geometry work exactly.
    """
    status = job.get("status")
    if status in ("starting", "pending"):
        return 2, "等待计算节点开始处理"
    if status == "done":
        label = ("风格方案已经生成" if job.get("mode") == "styles"
                 else "模型与交付文件已经生成")
        return 100, label
    if status == "failed":
        return int(job.get("progress_pct", 0)), "生成未完成"

    if job.get("fast_draft"):
        stages = (
            ("[glb] exported:", 97, "正在整理可旋转预览"),
            ("[postcheck] PASS", 92, "正在检查图层与地形接触关系"),
            ("[glb] block_base:", 76, "正在构建轻量 3D 图层"),
            ("[cache HIT] preprocess", 62, "已复用风格图层，准备 3D 预览"),
            ("[fast-draft] reopening", 18, "正在载入刚才选定的风格数据"),
        )
    elif job.get("mode") == "styles":
        stages = (
            ("contact sheet:", 98, "正在整理四种风格方案"),
            ("/minimal]", 95, "正在渲染第 4 种风格"),
            ("/dense_detail]", 92, "正在渲染第 3 种风格"),
            ("/block_fill]", 89, "正在渲染第 2 种风格"),
            ("/baseline]", 86, "正在渲染第 1 种风格"),
            ("[harness] prepared", 82, "地图要素已整理，准备渲染风格"),
            ("[Tile Cache] vegetation:", 64, "正在提取绿地与地表信息"),
            ("[Tile Cache] water:", 44, "正在提取湖泊与河流信息"),
            ("[Tile Cache] road:", 25, "正在提取道路网络"),
            ("[Tile Cache] building:", 8, "正在提取建筑与街区"),
            ("[area-gallery]", 4, "正在准备所选区域的地图数据"),
        )
    else:
        stages = (
            ("Exported:", 97, "正在整理与验证交付文件"),
            ("[Stage 5", 90, "正在构建可打印模型几何"),
            ("[Stage 4.8]", 83, "正在生成可旋转 3D 预览"),
            ("[Stage 4.65]", 76, "正在渲染高清俯视图"),
            ("[Stage 4.6]", 70, "正在渲染图层诊断图"),
            ("[Stage 4.5]", 62, "正在整理道路、建筑与水体图层"),
            ("[Stage 4]", 50, "正在构建地形与水体结构"),
            ("[Stage 3", 35, "正在读取城市地图要素"),
            ("[Stage 2", 20, "正在提取区域地图数据"),
            ("[Stage 1", 10, "正在准备高程数据"),
        )
    for marker, progress, label in stages:
        if marker in log_tail:
            return progress, label
    return 3, "正在准备地图与高程数据"


def _job_stage_label(job: dict, log_tail: str) -> str:
    return _job_progress(job, log_tail)[1]


def _job_duration_hint(job: dict) -> str:
    if job.get("mode") == "styles":
        return "首次生成通常需要 8–12 分钟；相同区域会直接复用"
    if job.get("mode") == "draft":
        if job.get("fast_draft"):
            return "复用风格数据后通常约 1–3 分钟；复杂山水区域可能更久"
        return "通常需要 5–15 分钟"
    if job.get("generation_profile") in ("quality_flat", "quality_textured"):
        return "精细模型通常需要 20–45 分钟"
    return "正式模型通常需要 15–40 分钟"


def _job_quality_warnings(log_tail: str) -> list[str]:
    warnings = []
    if "WL=0 WO=0" in log_tail:
        warnings.append("生成日志中的主要水体与普通水体数量均为 0，请核对源数据和预览")
    if "roads=0" in log_tail or "Roads: None" in log_tail:
        warnings.append("生成日志中的道路数量为 0，请核对源数据和预览")
    return warnings


def _job_quality_checks(log_tail: str) -> list[dict]:
    """把日志里的多源证据整理成前端可展示的验收项。

    这里只报告已经发生的检查，不把缺少第三方地图凭据误报成失败。
    """
    checks = []
    layer_match = re.search(r"WL=(\d+)\s+WO=(\d+).*?roads=(\d+)", log_tail)
    if layer_match:
        wl, wo, roads = (int(value) for value in layer_match.groups())
        checks.append({
            "id": "source_features",
            "label": "源数据结构层",
            "status": "pass" if (wl + wo > 0 and roads > 0) else "warning",
            "detail": f"水体 {wl + wo} · 道路 {roads}",
        })

    satellite_match = re.search(
        r"\[glb\] water: \+(\d+) satellite polys", log_tail)
    gaode_match = re.search(
        r"\[water_supplement\] \+(\d+) Gaode supplement polygons", log_tail)
    supplement = satellite_match or gaode_match
    if supplement:
        checks.append({
            "id": "secondary_map",
            "label": "第二地图源补强",
            "status": "pass",
            "detail": f"补入 {int(supplement.group(1))} 个水体面",
        })

    if "[postcheck] PASS" in log_tail or "[glb postcheck] PASS" in log_tail:
        checks.append({
            "id": "grounding",
            "label": "图层落地检查",
            "status": "pass",
            "detail": "道路、水体与地形接触关系通过",
        })
    return checks


def _job_public(job: dict, include_log: bool = False) -> dict:
    """任务对外视图（去掉 proc 等内部字段）。

    运行日志默认不外露；include_log=True 仅供管理页排障用。
    失败时返回分类后的 error_code/error_msg，不返回原始日志。
    """
    elapsed = (job.get("ended") or time.time()) - job["started"]
    public_status = "pending" if job["status"] == "starting" else job["status"]
    out = {
        "id": job["id"],
        "city": job["city"],
        "city_title": job.get("city_title") or job["city"],
        "mode": job["mode"],
        "style": job.get("style"),
        "generation_profile": job.get("generation_profile", "classic"),
        "status": public_status,
        "elapsed_s": round(elapsed, 1),
    }
    log_tail = _read_job_log_tail(job)
    progress_pct, stage_label = _job_progress(job, log_tail)
    out["stage_label"] = stage_label
    out["progress_pct"] = progress_pct
    out["duration_hint"] = _job_duration_hint(job)
    quality_warnings = _job_quality_warnings(log_tail)
    if quality_warnings:
        out["quality_warnings"] = quality_warnings
    quality_checks = _job_quality_checks(log_tail)
    if quality_checks:
        out["quality_checks"] = quality_checks
    if job["status"] == "failed":
        out["error_code"] = job.get("error_code", "render_failed")
        out["error_msg"] = job.get("error_msg",
                                   ERROR_CATEGORIES["render_failed"])
    if include_log:
        out["log_tail"] = log_tail[-4000:]
    return out


def _watch_job(job: dict):
    """后台线程：等子进程结束，更新状态。

    拉取任务额外做一步：.part → 正式名（只有完整下载才算就绪）。
    """
    ret = job["proc"].wait()
    ok = ret == 0
    paths = job.get("fetch_paths")
    if paths:
        tmp, dest = paths
        if ok and tmp.exists() and tmp.stat().st_size > 0:
            try:
                tmp.replace(dest)
                _COVERAGE_STATE_DIRTY.set()
            except OSError as e:
                ok = False
                with open(job["log_path"], "a", encoding="utf-8") as f:
                    f.write(f"\nrename failed: {e}\n")
        else:
            ok = False
            tmp.unlink(missing_ok=True)
    with JOBS_LOCK:
        job["ended"] = time.time()
        job["status"] = "done" if ok else "failed"
        if not ok:
            job["error_code"], job["error_msg"] = _classify_job_error(job, ret)
        _save_jobs()


# 拉取完成后置位：提示前端重拉一次区域状态（无需重启服务）
_COVERAGE_STATE_DIRTY = threading.Event()


def _city_running(city: str) -> bool:
    with JOBS_LOCK:
        return any(j["city"] == city and
                   j["status"] in _ACTIVE_JOB_STATUSES
                   for j in JOBS.values())


def _request_key(kind: str, payload: dict) -> str:
    """Return a stable identity for one shareable generation request."""
    raw = json.dumps({"kind": kind, **payload}, ensure_ascii=False,
                     sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _job_artifacts_available(job: dict) -> bool:
    """Only reuse a completed job while its expected output still exists."""
    city = job["city"]
    if job.get("mode") == "styles":
        return bool(_load_gallery(city))
    artifacts = _scan_artifacts(city)
    if job.get("mode") == "draft":
        return bool(artifacts["draft_glb"] or artifacts["preview_png"])
    return bool(artifacts["models_3mf"])


def _claim_or_reuse_job(job: dict) -> tuple[dict, bool, bool]:
    """Atomically claim a city output slot or reuse an identical request.

    Returns ``(job, reused, cached)``. Identical concurrent callers share one
    job id; an identical completed request is returned from disk. A different
    request for the same output directory must wait so two processes cannot
    overwrite each other's artifacts.
    """
    city = job["city"]
    request_key = job["request_key"]
    output_group = "styles" if job.get("mode") == "styles" else "model"
    with JOB_SUBMIT_LOCK:
        with JOBS_LOCK:
            city_jobs = sorted(
                (existing for existing in JOBS.values()
                 if existing.get("city") == city and
                 ("styles" if existing.get("mode") == "styles" else
                  "model") == output_group),
                key=lambda existing: existing.get("started", 0),
                reverse=True,
            )
            active = next(
                (existing for existing in city_jobs
                 if existing.get("status") in _ACTIVE_JOB_STATUSES),
                None,
            )
            if active is not None:
                if active.get("request_key") == request_key:
                    return active, True, False
                raise HTTPException(
                    409, "该区域正在使用另一组参数生成，请等待当前任务完成",
                )

            latest_done = next(
                (existing for existing in city_jobs
                 if existing.get("status") == "done"),
                None,
            )
            if (latest_done is not None and
                    latest_done.get("request_key") == request_key and
                    _job_artifacts_available(latest_done)):
                return latest_done, True, True

            JOBS[job["id"]] = job
            _save_jobs()
    return job, False, False


def _fetch_running(region: str) -> bool:
    with JOBS_LOCK:
        return any(j.get("fetch_region") == region and j["status"] == "running"
                   for j in JOBS.values())


# ---------------------------------------------------------------------------
# Worker 模式（云接单 + 本机计算）与任务持久化
# ---------------------------------------------------------------------------
# WORKER_MODE=1 时，/api/generate 与 /api/styles 不本地起子进程，而是把任务
# 入队（status=pending）落盘，等本机 worker 通过 /api/worker/* 拉取执行并回传。
# 这样低配云机只当门面+数据仓库，重计算交给内存充足的本机。
def _env(name: str, default: str = "") -> str:
    v = os.environ.get(name, "").strip()
    if v:
        return v
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip()
    return default


WORKER_MODE = _env("WORKER_MODE") in ("1", "true", "yes", "on")
WORKER_TOKEN = _env("WORKER_TOKEN")
JOBS_PATH = JOB_LOG_DIR / "_jobs.json"
_SKIP_SERIALIZE = {"proc", "fetch_paths"}   # 运行时对象/本地路径，不入库


def _serialize_job(job: dict) -> dict:
    out = {}
    for k, v in job.items():
        if k in _SKIP_SERIALIZE:
            continue
        out[k] = str(v) if isinstance(v, Path) else v
    return out


def _save_jobs():
    """落盘任务表（原子写）。调用方通常在 JOBS_LOCK 内。"""
    data = {jid: _serialize_job(j) for jid, j in JOBS.items()}
    tmp = JOBS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, JOBS_PATH)


def _load_jobs():
    """启动恢复任务表。proc 无法恢复：
    worker 模式 running → pending（重派，产物覆盖式幂等）；
    local 模式 running → failed（子进程随重启消失）。"""
    if not JOBS_PATH.exists():
        return
    try:
        data = json.loads(JOBS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    for jid, j in data.items():
        if j.get("status") in ("starting", "running"):
            if j.get("exec") == "worker":
                j["status"] = "pending"
            else:
                j["status"] = "failed"
                j["ended"] = time.time()
        JOBS[jid] = j


def _check_worker_token(token: str):
    if not WORKER_TOKEN:
        raise HTTPException(503, "worker 协议未启用（未配置 WORKER_TOKEN）")
    if token != WORKER_TOKEN:
        raise HTTPException(401, "worker token 无效")


_load_jobs()


# ---------------------------------------------------------------------------
# 产物扫描
# ---------------------------------------------------------------------------

def _scan_artifacts(city: str) -> dict:
    """扫描 output/{city}/ 下的最新产物。"""
    d = OUTPUT_DIR / city
    art = {"models_3mf": [], "draft_glb": None, "preview_png": None,
           "topdown_png": None, "height_png": None,
           "param_decision": None, "design_spec": None}
    if not d.is_dir():
        return art
    for p in sorted(d.glob("*.3mf"), key=lambda p: p.stat().st_mtime,
                    reverse=True)[:3]:
        art["models_3mf"].append({
            "name": p.name,
            "url": f"/files/{city}/{p.name}",
            "size_mb": round(p.stat().st_size / 1e6, 1),
            "mtime": time.strftime("%m-%d %H:%M",
                                   time.localtime(p.stat().st_mtime)),
        })
    glb = d / f"{city}_draft.glb"
    if glb.exists():
        art["draft_glb"] = {
            "url": f"/files/{city}/{glb.name}",
            "size_mb": round(glb.stat().st_size / 1e6, 1),
            "mtime": time.strftime("%m-%d %H:%M",
                                   time.localtime(glb.stat().st_mtime)),
        }
    png = d / f"{city}_preview.png"
    if png.exists():
        art["preview_png"] = f"/files/{city}/{png.name}"
    # 画廊级俯视图（review_render 产物，无文字、超采样）；
    # 本区域没有则回退到该区域风格画廊的 baseline 图
    gdir = GALLERY_DIR / city
    if gdir.is_dir():
        for cand in ("baseline_topdown.png", "minimal_topdown.png"):
            if (gdir / cand).exists():
                art["topdown_png"] = f"/files/style_gallery/{city}/{cand}"
                break
    top = d / f"{city}_topdown.png"
    if top.exists():
        art["topdown_png"] = f"/files/{city}/{top.name}"
    hgt = d / f"{city}_height.png"
    if hgt.exists():
        art["height_png"] = f"/files/{city}/{hgt.name}"
    pd = d / "param_decision.json"
    if pd.exists():
        try:
            art["param_decision"] = json.loads(pd.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    ds = d / "design_spec.json"
    if ds.exists():
        art["design_spec"] = {
            "url": f"/files/{city}/{ds.name}",
            "name": ds.name,
        }
    return art


def _load_gallery(city: str):
    meta_path = GALLERY_DIR / city / "gallery_metadata.json"
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    base = f"/files/style_gallery/{city}"
    for style in meta.get("styles", {}).values():
        renders = style.get("renders", {})
        style["images"] = {k: f"{base}/{v}" for k, v in renders.items()}
    meta["contact_sheet_url"] = f"{base}/{meta.get('contact_sheet', '')}"
    return meta


# ---------------------------------------------------------------------------
# 自定义区域：照片 EXIF GPS（PBF 覆盖判定见顶部 _pbf_status）
# ---------------------------------------------------------------------------

def _exif_gps(fp):
    """从照片提取 EXIF GPS → (lat, lon)，无则 None。"""
    from PIL import Image
    img = Image.open(fp)
    gps = img.getexif().get_ifd(0x8825)   # GPSInfo IFD
    if not gps or 2 not in gps or 4 not in gps:
        return None

    def to_deg(v):
        return float(v[0]) + float(v[1]) / 60.0 + float(v[2]) / 3600.0

    try:
        lat = to_deg(gps[2])
        lon = to_deg(gps[4])
    except (TypeError, ValueError, ZeroDivisionError, IndexError):
        return None
    if str(gps.get(1, "N")).upper() == "S":
        lat = -lat
    if str(gps.get(3, "E")).upper() == "W":
        lon = -lon
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    return lat, lon


def _custom_slug(bbox) -> str:
    """同一区域复用同一输出目录：bbox 取三位小数哈希。"""
    key = ",".join(f"{v:.3f}" for v in bbox)
    return "custom_" + hashlib.md5(key.encode()).hexdigest()[:6]


GAZETTEER_DIR = ROOT / "data" / "gazetteer"
_GAZ_CACHE: dict = {}


def _load_gazetteer(pbf_rel: str) -> list:
    """PBF 相对路径 → 对应地名表（内存缓存；无文件返回空表）。"""
    stem = Path(pbf_rel).name.replace(".osm.pbf", "").replace(".pbf", "")
    if stem not in _GAZ_CACHE:
        p = GAZETTEER_DIR / f"{stem}.json"
        try:
            _GAZ_CACHE[stem] = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _GAZ_CACHE[stem] = []
    return _GAZ_CACHE[stem]


LANDMARKS_PATH = ROOT / "data" / "landmarks" / "catalog.json"
_LANDMARKS_CACHE: list | None = None


def _load_landmarks() -> list:
    """景点目录（预设取景框库），启动后首次访问时加载并缓存。"""
    global _LANDMARKS_CACHE
    if _LANDMARKS_CACHE is None:
        try:
            data = json.loads(LANDMARKS_PATH.read_text(encoding="utf-8"))
            _LANDMARKS_CACHE = data.get("landmarks", [])
        except (OSError, json.JSONDecodeError):
            _LANDMARKS_CACHE = []
    return _LANDMARKS_CACHE


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.get("/api/cities")
def api_cities():
    cities = []
    # 硬编码预设（有完整画廊的优先展示）
    for name, info in PRESETS.items():
        cities.append({
            "name": name,
            "title": info["title"],
            "bbox": info["bbox"],
            "prototype": info["prototype"],
            "has_gallery": (GALLERY_DIR / name / "gallery_metadata.json").exists(),
            "running": _city_running(name),
            "artifacts": _scan_artifacts(name),
            "group": "精选",
        })
    # 景点目录扩充（按国家分组）
    for lid, info in _landmark_presets().items():
        cities.append({
            "name": lid,
            "title": info["title"],
            "bbox": info["bbox"],
            "prototype": info["prototype"],
            "has_gallery": (GALLERY_DIR / lid / "gallery_metadata.json").exists(),
            "running": _city_running(lid),
            "artifacts": _scan_artifacts(lid),
            "group": info.get("country", ""),
        })
    return {"cities": cities}


@app.get("/api/gallery/{city}")
def api_gallery(city: str):
    meta = _load_gallery(city)
    if meta is None:
        raise HTTPException(404, f"{city} 暂无风格画廊")
    return meta


@app.post("/api/photo-location")
async def api_photo_location(photo: UploadFile = File(...)):
    """照片 → EXIF GPS → 建议 bbox + 本地数据覆盖情况。"""
    import io
    raw = await photo.read()
    if len(raw) > 30 * 1024 * 1024:
        raise HTTPException(413, "照片超过 30MB")
    try:
        gps = _exif_gps(io.BytesIO(raw))
    except Exception:
        raise HTTPException(400, "无法读取图片（格式不支持？）")
    if gps is None:
        raise HTTPException(422, "照片里没有 GPS 信息（微信/截图会抹掉 EXIF，请用原图）")
    lat, lon = gps
    # 默认建议框：以照片点为中心 ≈ 6km 见方
    half_lat = 0.027
    half_lon = 0.027 / max(math.cos(math.radians(lat)), 0.2)
    bbox = [round(lat - half_lat, 4), round(lon - half_lon, 4),
            round(lat + half_lat, 4), round(lon + half_lon, 4)]
    return {
        "lat": round(lat, 6),
        "lon": round(lon, 6),
        "suggested_bbox": bbox,
        "pbf": _find_pbf(bbox),   # null → 该区域无本地 OSM 数据
    }


@app.get("/api/artifacts/{city}")
def api_artifacts(city: str):
    """单城市产物刷新（自定义区域任务完成后前端拉取）。"""
    if "/" in city or "\\" in city or ".." in city:
        raise HTTPException(400, "非法城市名")
    return {"city": city, "artifacts": _scan_artifacts(city)}


@app.post("/api/journey")
async def api_journey(photos: list[UploadFile] = File(...)):
    """多照片 → 时间轴轨迹：逐张 GPS/时间 + 停留聚类 + 建议 bbox/分章。"""
    import io
    if len(photos) > 10:
        raise HTTPException(413, "一次最多 10 张照片（精选不同地点的代表照即可）")
    metas = []
    for ph in photos:
        raw = await ph.read()
        meta = {"name": ph.filename, "lat": None, "lon": None, "time": None}
        if len(raw) <= 30 * 1024 * 1024:
            try:
                meta.update(journey_mod.extract_photo_meta(io.BytesIO(raw)))
            except Exception:
                pass   # 读不了的图按无 GPS 处理，报告里可见
        metas.append(meta)
    result = journey_mod.analyze_journey(metas)
    result["pbf"] = _find_pbf(result["suggested_bbox"]) \
        if result["suggested_bbox"] else None
    # 停留点就近命名（本地地名表，确定性；命不了名保持 None）
    if result["pbf"] and result["clusters"]:
        journey_mod.name_clusters(result["clusters"],
                                  _load_gazetteer(result["pbf"]))
    # 缺口追问（只问照片里没有的信息；无缺口则为空）
    result["gaps"] = journey_mod.detect_gaps(result)
    return result


@app.get("/api/landmarks")
def api_landmarks(q: str = ""):
    """景点目录搜索：名称/城市/国家/标签子串匹配，带数据三态。

    空 q → 本地就绪的在前（可立即生成），兼做发现入口。"""
    q = q.strip().lower()
    items = []
    for lm in _load_landmarks():
        hay = " ".join([lm["name"], lm.get("name_en", ""),
                        lm.get("city", ""), lm.get("country", ""),
                        " ".join(lm.get("tags", []))]).lower()
        if q and q not in hay:
            continue
        items.append({**lm, **_state_fields(lm["bbox"])})
    # 本地就绪 > 可拉取 > 无数据
    order = {"local": 0, "fetchable": 1, "none": 2}
    items.sort(key=lambda x: order.get(x["data_state"], 3))
    return {"landmarks": items[:12], "total": len(items)}


@app.get("/api/regions")
def api_regions():
    """数据区域概览：本地已有 / 远端可拉取。"""
    cov = _coverage()
    local, remote = [], []
    for name, info in sorted(cov.get("regions", {}).items()):
        p = PBF_DIR / info["file"]
        if p.exists():
            local.append({"region": name,
                          "size_mb": round(p.stat().st_size / 1e6)})
        else:
            remote.append({"region": name})
    return {"local": local, "remote": remote,
            "remote_host": cov.get("remote_host", ""),
            "total": len(local) + len(remote)}


class FetchRequest(BaseModel):
    region: str


@app.post("/api/fetch-pbf")
def api_fetch_pbf(req: FetchRequest):
    """从数据源服务器 scp 拉取指定区域的 PBF（异步任务）。

    实测 scp 约 13 MB/s：中国省份 2–12s，欧美大区最多 约 2.5 分钟。
    拉一次永久复用。
    """
    cov = _coverage()
    info = cov.get("regions", {}).get(req.region)
    if not info:
        raise HTTPException(404, f"未知区域: {req.region}")
    dest = PBF_DIR / info["file"]
    if dest.exists():
        return {"job_id": None, "state": "local",
                "detail": f"{req.region} 本地已有"}
    host, rdir = cov.get("remote_host"), cov.get("remote_dir")
    if not host or not rdir:
        raise HTTPException(503, "数据源服务器未配置")
    if _fetch_running(req.region):
        raise HTTPException(409, f"{req.region} 正在拉取中")

    job_id = uuid.uuid4().hex[:8]
    log_path = JOB_LOG_DIR / f"{job_id}_fetch_{req.region}.log"
    PBF_DIR.mkdir(parents=True, exist_ok=True)
    # 先落 .part 临时名，成功后重命名——中断不会留下残缺文件被误认为就绪
    tmp_dest = dest.with_name(dest.name + ".part")
    cmd = ["scp", "-o", "BatchMode=yes",
           f"{host}:{rdir}/{info['file']}", str(tmp_dest)]
    log_f = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(cmd, stdout=log_f, stderr=subprocess.STDOUT)
    job = {"id": job_id, "city": f"fetch:{req.region}",
           "city_title": f"拉取 {req.region} 数据", "mode": "fetch",
           "style": None, "exec": "local", "proc": proc,
           "log_path": str(log_path),
           "status": "running", "started": time.time(), "ended": None,
           "fetch_region": req.region, "fetch_paths": (tmp_dest, dest)}
    with JOBS_LOCK:
        JOBS[job_id] = job
        _save_jobs()
    threading.Thread(target=_watch_job, args=(job,), daemon=True).start()
    return {"job_id": job_id, "state": "fetching", "region": req.region}


def _bbox_around(lat: float, lon: float, half_lat: float = 0.036) -> list:
    """中心点 → 默认取景框（约 8km 见方，落在细节甜区）。"""
    half_lon = half_lat / max(math.cos(math.radians(lat)), 0.2)
    return [round(lat - half_lat, 4), round(lon - half_lon, 4),
            round(lat + half_lat, 4), round(lon + half_lon, 4)]


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def _amap_key() -> str:
    k = os.environ.get("AMAP_KEY", "")
    if k:
        return k
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("AMAP_KEY="):
                return line.split("=", 1)[1].strip()
    return ""


def _name_relevant(q: str, name: str) -> bool:
    """名称相关性：挡掉高德的模糊匹配噪音。

    搜境外地名时高德会回国内同名店铺（实测：“堪培拉” → 天津
    “美闻比萨”），这种与查询词毫无字面关系的结果必须剔除。
    """
    q, name = q.strip().lower(), (name or "").strip().lower()
    if not q or not name:
        return False
    if q in name or name in q:
        return True
    # 中文：按字算重叠率（“浙大紫金港” vs “浙江大学(紫金港校区)”）
    qs = set(q) - set(" ()（）·")
    if not qs:
        return False
    return len(qs & set(name)) / len(qs) >= 0.5


def _amap_search(q: str) -> list:
    """高德 POI 检索（中文地名/POI 质量远胜 Nominatim）。

    关键：高德返回 GCJ-02，必须转 WGS84 才能与 OSM 数据对齐
    （实测拙政园偏差 494m → 转换后 31m）。无 key/失败 → []。
    """
    key = _amap_key()
    if not key:
        return []
    try:
        import requests
        from _TEXTURE_STYLE_OF_DEEPSEEK._water_supplement import _gcj02_to_wgs84
        r = requests.get("https://restapi.amap.com/v3/place/text",
                         params={"keywords": q, "key": key, "offset": 8,
                                 "extensions": "base"},
                         timeout=8)
        d = r.json()
        if d.get("status") != "1":
            return []
        pois = d.get("pois") or []
    except Exception as e:
        print(f"[amap] search failed: {type(e).__name__}: {e}")
        return []

    out, seen = [], set()
    for p in pois:
        loc = (p.get("location") or "").split(",")
        if len(loc) != 2:
            continue
        try:
            glon, glat = float(loc[0]), float(loc[1])
        except ValueError:
            continue
        lon, lat = _gcj02_to_wgs84(glon, glat)     # GCJ-02 → WGS84
        name = p.get("name") or q
        if name in seen or not _name_relevant(q, name):
            continue
        seen.add(name)
        bbox = _bbox_around(lat, lon)
        out.append({
            "id": f"amap_{p.get('id', len(out))}",
            "name": name,
            "city": p.get("cityname") or p.get("adname") or "",
            "country": p.get("adname") or "",
            "center": [round(lat, 6), round(lon, 6)],
            "bbox": bbox,
            "style": None,
            "source": "amap",
            **_state_fields(bbox),
        })
    return out


def _nominatim_search(q: str) -> list:
    """Nominatim 全球地理编码（WGS84 原生，境外主力）。

    注：需 User-Agent（无头会 403）且限流 1 req/s。
    """
    try:
        import requests
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": q, "format": "jsonv2", "limit": 6,
                    "accept-language": "zh-CN,en"},
            headers={"User-Agent": "MapReliefStudio/0.3 (self-hosted)"},
            timeout=8,
        )
        if r.status_code != 200:
            return []
        raw = r.json()
    except Exception:
        return []

    out = []
    for it in raw:
        try:
            lat, lon = float(it["lat"]), float(it["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        parts = [p.strip() for p in (it.get("display_name") or "").split(",")
                 if p.strip()]
        # Nominatim 的 boundingbox 常是整个行政区（过大），不直接用
        bbox = _bbox_around(lat, lon)
        out.append({
            "id": f"geo_{it.get('osm_id', len(out))}",
            "name": parts[0] if parts else q,
            "city": parts[1] if len(parts) > 1 else "",
            "country": parts[-1] if len(parts) > 2 else "",
            "center": [round(lat, 6), round(lon, 6)],
            "bbox": bbox,
            "style": None,
            "source": "nominatim",
            **_state_fields(bbox),
        })
    return out


class StylesRequest(BaseModel):
    bbox: list[float]                 # [south, west, north, east]
    name: str = ""
    prototype: str = "landscape"      # landscape|skyline|terrain|minimal
    slug: str = ""                    # 可选：指定输出目录名（景点城市用 ID）


@app.post("/api/styles")
def api_styles(req: StylesRequest):
    """为任意区域生成风格画廊（4 种风格的 2D 图）。

    实测 10km 见方 ≈ 49s 出 4 张（每张 7–8s + 一次 prepare）。
    同 bbox 命中 PipelineCache 后更快。
    """
    bbox = req.bbox
    if len(bbox) != 4:
        raise HTTPException(400, "bbox 需为 [south, west, north, east]")
    s, w, n, e = bbox
    if not (n > s and e > w):
        raise HTTPException(400, "bbox 南北/东西颠倒")
    st = _pbf_status(bbox)
    if st["state"] == "fetchable":
        raise HTTPException(409, "该区域数据正在准备中，敬请期待")
    if st["state"] == "none":
        raise HTTPException(422, "该区域即将开放，敬请期待")

    slug = req.slug.strip() if req.slug.strip() else _custom_slug(bbox)
    request_key = _request_key("styles", {
        "bbox": [round(value, 7) for value in bbox],
        "prototype": req.prototype,
    })
    job_id = uuid.uuid4().hex[:8]
    log_path = JOB_LOG_DIR / f"{job_id}_styles_{slug}.log"
    cmd = [sys.executable, "tools/gen_area_gallery.py",
           "--bbox", f"{s},{w},{n},{e}", "--pbf", st["pbf"],
           "--slug", slug, "--prototype", req.prototype]
    if req.name.strip():
        cmd += ["--title", req.name.strip()]

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["PATH"] = str(ROOT / "tools") + os.pathsep + env.get("PATH", "")

    if WORKER_MODE:
        job = {"id": job_id, "city": slug,
               "city_title": req.name.strip() or "自定义区域",
               "mode": "styles", "style": None, "exec": "worker",
               "request_key": request_key, "prototype": req.prototype,
               "bbox": bbox,
               "log_path": str(log_path), "status": "pending",
               "started": time.time(), "ended": None,
               "spec": {"cmd": cmd, "cwd": str(ROOT), "env_extra": {
                   "PYTHONIOENCODING": "utf-8",
                   "PYTHONUNBUFFERED": "1",
                   "PATH": env["PATH"]}}}
        claimed, reused, cached = _claim_or_reuse_job(job)
        return {"job_id": claimed["id"], "slug": slug,
                "queued": claimed["status"] == "pending",
                "reused": reused, "cached": cached}

    job = {"id": job_id, "city": slug,
           "city_title": req.name.strip() or "自定义区域",
           "mode": "styles", "style": None, "exec": "local",
           "request_key": request_key, "prototype": req.prototype,
           "bbox": bbox,
           "log_path": str(log_path), "status": "starting",
           "started": time.time(), "ended": None}
    claimed, reused, cached = _claim_or_reuse_job(job)
    if reused:
        return {"job_id": claimed["id"], "slug": slug,
                "queued": claimed["status"] in ("starting", "pending"),
                "reused": True, "cached": cached}

    try:
        with open(log_path, "w", encoding="utf-8") as log_f:
            proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=log_f,
                                    stderr=subprocess.STDOUT, env=env)
    except (OSError, subprocess.SubprocessError):
        with JOBS_LOCK:
            job["status"] = "failed"
            job["ended"] = time.time()
            job["error_code"] = "render_failed"
            job["error_msg"] = "生成任务无法启动，请稍后重试"
            _save_jobs()
        raise HTTPException(500, "生成任务无法启动，请稍后重试")
    with JOBS_LOCK:
        job["proc"] = proc
        job["status"] = "running"
        _save_jobs()
    threading.Thread(target=_watch_job, args=(job,), daemon=True).start()
    return {"job_id": job_id, "slug": slug,
            "reused": False, "cached": False}


@app.get("/api/geocode")
def api_geocode(q: str = "", near_lat: float | None = None,
                near_lon: float | None = None):
    """地名兜底检索（目录未命中时用）：高德 + Nominatim 合并。

    高德中文 POI 强但仅限国内，搜境外地名会回国内同名店铺（实测
    “西雅图” → 河北某店）；所以两家都查、合并展示、标清来源与所在地，
    让用户自己分辨。两者都只拿坐标与名称，取景框统一由本地推导。

    near_lat/near_lon：缺口追问场景传当前旅程位置，近处结果优先
    （用户答“灵隐寺”应该是身边那个，不是外地同名）。
    """
    q = q.strip()
    if len(q) < 2:
        return {"results": []}

    amap = _amap_search(q)
    nomi = _nominatim_search(q)

    # 合并去重：同名保留高德（中文名更友好）
    merged, seen = [], set()
    for it in amap + nomi:
        k = it["name"].strip().lower()
        if k in seen:
            continue
        seen.add(k)
        merged.append(it)

    if not merged:
        raise HTTPException(404, "没找到这个地方，换个说法试试")

    if near_lat is not None and near_lon is not None:
        for it in merged:
            it["dist_km"] = round(_haversine_km(
                near_lat, near_lon, it["center"][0], it["center"][1]), 1)
        # 近处优先（同时保持可生成排前）
        merged.sort(key=lambda x: (not x["available"], x["dist_km"]))
    else:
        merged.sort(key=lambda x: (not x["available"],
                                  x["name"].strip().lower() != q.lower()))
    return {"results": merged[:8]}


@app.post("/api/generate")
def api_generate(req: GenerateRequest):
    if req.mode not in ("draft", "full"):
        raise HTTPException(400, f"未知模式: {req.mode}")

    profile = req.generation_profile.strip() or "classic"
    if profile not in GENERATION_PROFILES:
        raise HTTPException(400, f"未知生成方式: {profile}")

    quality_profile = profile in ("quality_flat", "quality_textured")
    fast_draft_context = None
    if quality_profile:
        if req.area is not None or req.city != "westlake":
            raise HTTPException(400, "精细模型目前只支持‘杭州 · 西湖’预设")
        if req.mode != "full":
            raise HTTPException(400, "精细模型直接生成正式文件，不提供快速预览")
        if req.style:
            raise HTTPException(400, "精细模型使用已验证的固定视觉参数，不叠加画廊风格")

        block_mode = "flat" if profile == "quality_flat" else "textured"
        city = f"westlake_{profile}"
        city_title = f"杭州 · 西湖（{GENERATION_PROFILES[profile]['label']}）"
        base_cmd = [
            sys.executable, "generate_city.py",
            "--city", city,
            "--output-dir", str(OUTPUT_DIR / city),
            "--block-base-mode", block_mode,
            "--block-base-edge-retreat-mm", "2",
            "--png", "--review-png",
        ]

    # ── 目标解析：预设城市 / 景点目录 / 自定义区域 ──
    elif req.area is not None:
        bbox = req.area.bbox
        if len(bbox) != 4:
            raise HTTPException(400, "bbox 需为 [south, west, north, east]")
        s, w, n, e = bbox
        if not (n > s and e > w):
            raise HTTPException(400, "bbox 南北/东西颠倒")
        if (n - s) < 0.005 or (e - w) < 0.005:
            raise HTTPException(400, "区域太小（每边至少约 0.5km）")
        if (n - s) > 0.4 or (e - w) > 0.5:
            raise HTTPException(400, "区域太大（建议单边不超过约 40km）")
        st = _pbf_status(bbox)
        if st["state"] == "fetchable":
            raise HTTPException(409, "该区域数据正在准备中，敬请期待")
        if st["state"] == "none":
            raise HTTPException(422, "该区域即将开放，敬请期待")
        pbf = st["pbf"]
        city = _custom_slug(bbox)
        city_title = req.area.name.strip() or "自定义区域"
        fast_draft_context = {"bbox": bbox, "pbf": pbf}
        base_cmd = [sys.executable, "generate_city_legacy.py",
                    "--bbox", f"{s},{w},{n},{e}", "--pbf", pbf,
                    "--city", city, "--auto-params"]
        # 记录区域信息，刷新后前端仍能展示
        area_dir = OUTPUT_DIR / city
        area_dir.mkdir(parents=True, exist_ok=True)
        (area_dir / "area.json").write_text(
            json.dumps({"name": city_title, "bbox": bbox,
                        "markers": req.markers}, ensure_ascii=False),
            encoding="utf-8")
    elif req.city in PRESETS:
        city = req.city
        city_title = PRESETS[city]["title"]
        base_cmd = [sys.executable, "generate_city_legacy.py", "--preset", city,
                    "--auto-params"]
    elif req.city in _landmark_presets():
        # 景点目录城市：用 bbox + pbf 路径（与自定义区域相同）
        lm_info = _landmark_presets()[req.city]
        bbox = lm_info["bbox"]
        s, w, n, e = bbox
        st = _pbf_status(bbox)
        if st["state"] == "fetchable":
            raise HTTPException(409, "该区域数据正在准备中，敬请期待")
        if st["state"] == "none":
            raise HTTPException(422, "该区域即将开放，敬请期待")
        pbf = st["pbf"]
        city = req.city
        city_title = lm_info["title"]
        fast_draft_context = {"bbox": bbox, "pbf": pbf}
        base_cmd = [sys.executable, "generate_city_legacy.py",
                    "--bbox", f"{s},{w},{n},{e}", "--pbf", pbf,
                    "--city", city, "--auto-params"]
        area_dir = OUTPUT_DIR / city
        area_dir.mkdir(parents=True, exist_ok=True)
        (area_dir / "area.json").write_text(
            json.dumps({"name": city_title, "bbox": bbox,
                        "markers": req.markers}, ensure_ascii=False),
            encoding="utf-8")
    else:
        raise HTTPException(400, f"未知城市: {req.city}")

    request_key = _request_key("generate", {
        "city": city,
        "mode": req.mode,
        "style": req.style,
        "generation_profile": profile,
        "markers": req.markers,
    })
    job_id = uuid.uuid4().hex[:8]
    log_path = JOB_LOG_DIR / f"{job_id}_{city}_{req.mode}.log"
    # draft 也出 2D 图：诊断图（带图例统计）+ 画廊级俯视图（无文字）
    if quality_profile:
        cmd = base_cmd
    else:
        cmd = base_cmd + ["--png", "--review-png"]
        if req.mode == "draft":
            cmd.append("--draft")

    # 标注点（照片 GPS 点）：draft GLB 将其附近最高处染红
    if not quality_profile:
        for mk in req.markers:
            if len(mk) == 2:
                cmd += ["--marker", f"{mk[0]},{mk[1]}"]

    # 画廊风格参数 → 落盘 JSON → --params-json（管线内最高优先级覆盖）
    params_path = None
    gallery_meta = None
    if req.style and not quality_profile:
        gallery_meta = _load_gallery(city)
        if not gallery_meta or req.style not in gallery_meta.get("styles", {}):
            raise HTTPException(400, f"{city} 无风格: {req.style}")
        params = gallery_meta["styles"][req.style].get("params", {})
        params_path = JOB_LOG_DIR / f"{job_id}_params.json"
        params_path.write_text(json.dumps(params, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        cmd += ["--params-json", str(params_path)]

    # 用户刚完成风格画廊时，快速预览直接复用同一个 CityHarness cache。
    # 这条路径不再提取 draft 不消费的 landuse，也不重复生成诊断 PNG。
    fast_draft = bool(
        req.mode == "draft" and req.style and fast_draft_context
        and gallery_meta and params_path
    )
    if fast_draft:
        fast_bbox = fast_draft_context["bbox"]
        cmd = [
            sys.executable, "tools/generate_gallery_draft.py",
            "--bbox", ",".join(str(value) for value in fast_bbox),
            "--pbf", fast_draft_context["pbf"],
            "--city", city,
            "--prototype", gallery_meta.get("prototype", "landscape"),
            "--scene-type", gallery_meta.get("scene_type", "urban"),
            "--params-json", str(params_path),
        ]
        for mk in req.markers:
            if len(mk) == 2:
                cmd += ["--marker", f"{mk[0]},{mk[1]}"]

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["PATH"] = str(ROOT / "tools") + os.pathsep + env.get("PATH", "")

    if WORKER_MODE:
        # ── Worker 模式：入队等本机 worker 拉取，不本地起进程 ──
        job = {"id": job_id, "city": city, "city_title": city_title,
               "mode": req.mode, "style": req.style,
               "generation_profile": profile, "exec": "worker",
               "fast_draft": fast_draft,
               "request_key": request_key,
               "log_path": str(log_path), "status": "pending",
               "started": time.time(), "ended": None,
               "spec": {"cmd": cmd, "cwd": str(ROOT), "env_extra": {
                   "PYTHONIOENCODING": "utf-8",
                   "PYTHONUNBUFFERED": "1",
                   "PATH": env["PATH"]}}}
        claimed, reused, cached = _claim_or_reuse_job(job)
        if reused and params_path is not None:
            params_path.unlink(missing_ok=True)
        return {"job_id": claimed["id"], "city": city,
                "generation_profile": profile,
                "queued": claimed["status"] == "pending",
                "reused": reused, "cached": cached}

    # ── 本地模式：直接起子进程 ──
    job = {"id": job_id, "city": city, "city_title": city_title,
           "mode": req.mode, "style": req.style,
           "generation_profile": profile, "exec": "local",
           "fast_draft": fast_draft,
           "request_key": request_key,
           "log_path": str(log_path), "status": "starting",
           "started": time.time(), "ended": None}
    claimed, reused, cached = _claim_or_reuse_job(job)
    if reused:
        if params_path is not None:
            params_path.unlink(missing_ok=True)
        return {"job_id": claimed["id"], "city": city,
                "generation_profile": profile,
                "queued": claimed["status"] in ("starting", "pending"),
                "reused": True, "cached": cached}

    try:
        with open(log_path, "w", encoding="utf-8") as log_f:
            proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=log_f,
                                    stderr=subprocess.STDOUT, env=env)
    except (OSError, subprocess.SubprocessError):
        with JOBS_LOCK:
            job["status"] = "failed"
            job["ended"] = time.time()
            job["error_code"] = "render_failed"
            job["error_msg"] = "生成任务无法启动，请稍后重试"
            _save_jobs()
        raise HTTPException(500, "生成任务无法启动，请稍后重试")
    with JOBS_LOCK:
        job["proc"] = proc
        job["status"] = "running"
        _save_jobs()
    threading.Thread(target=_watch_job, args=(job,), daemon=True).start()
    return {"job_id": job_id, "city": city,
            "generation_profile": profile,
            "reused": False, "cached": False}


@app.get("/api/generation-profiles")
def api_generation_profiles():
    return {"profiles": GENERATION_PROFILES}


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str, include_log: bool = False):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    return _job_public(job, include_log=include_log)


@app.get("/api/jobs")
def api_jobs(include_log: bool = False):
    with JOBS_LOCK:
        jobs = list(JOBS.values())
    return {"jobs": [_job_public(j, include_log=include_log) for j in
                     sorted(jobs, key=lambda j: j["started"], reverse=True)]}


# ---------------------------------------------------------------------------
# Worker 协议（本机 worker 拉取任务 + 上传产物 + 标记完成）
# ---------------------------------------------------------------------------

class WorkerFinish(BaseModel):
    job_id: str
    token: str
    ok: bool = True
    error: str = ""
    files: list[dict] = []   # [{name, sha256, size}]


@app.get("/api/worker/next")
def worker_next(token: str = ""):
    """Worker 拉取一个 pending 任务（乐观锁：pending → running 原子落盘）。"""
    _check_worker_token(token)
    with JOBS_LOCK:
        for jid, j in JOBS.items():
            if j.get("status") == "pending" and j.get("exec") == "worker":
                j["status"] = "running"
                j["started"] = time.time()
                _save_jobs()
                return {"job_id": jid, "spec": j.get("spec"),
                        "city": j["city"], "mode": j["mode"],
                        "style": j.get("style")}
    return {"job_id": None}  # 无待处理任务


@app.post("/api/worker/upload")
async def worker_upload(token: str = "", job_id: str = "",
                        filename: str = "", sha256: str = "",
                        file: UploadFile = File(...)):
    """Worker 上传产物文件（.part 隔离 + sha256 校验）。"""
    _check_worker_token(token)
    if not job_id or not filename:
        raise HTTPException(400, "缺 job_id 或 filename")
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    city = job["city"]
    # 安全：filename 不能含路径分隔符
    safe_name = Path(filename).name
    if not safe_name or safe_name != filename:
        raise HTTPException(400, "非法文件名")
    dest_dir = OUTPUT_DIR / city
    dest_dir.mkdir(parents=True, exist_ok=True)
    part_path = dest_dir / (safe_name + ".part")
    # 流式写入 .part
    import hashlib
    h = hashlib.sha256()
    total = 0
    with open(part_path, "wb") as f:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            total += len(chunk)
    # 校验 sha256
    if sha256 and h.hexdigest() != sha256.lower():
        part_path.unlink(missing_ok=True)
        raise HTTPException(400, f"sha256 不匹配：期望 {sha256}，"
                                 f"实际 {h.hexdigest()}")
    return {"ok": True, "name": safe_name, "size": total,
            "sha256": h.hexdigest()}


@app.post("/api/worker/finish")
def worker_finish(req: WorkerFinish):
    """Worker 标记任务完成（先验证产物完整性 → 再改状态）。

    完整性保证：
    - 所有声明的 files 必须在磁盘存在且 sha256 匹配
    - .part → 正式名（原子 rename）
    - 全部通过后才改 status=done
    """
    _check_worker_token(req.token)
    with JOBS_LOCK:
        job = JOBS.get(req.job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    # 幂等：已 done 直接返回
    if job.get("status") == "done":
        return {"ok": True, "already_done": True}
    city = job["city"]
    dest_dir = OUTPUT_DIR / city

    if req.ok:
        # 验证所有产物文件
        import hashlib
        for finfo in req.files:
            name = Path(finfo["name"]).name
            part = dest_dir / (name + ".part")
            final = dest_dir / name
            # 优先检查 .part（刚上传的）
            check = part if part.exists() else final
            if not check.exists():
                raise HTTPException(400, f"产物缺失: {name}")
            if finfo.get("sha256"):
                h = hashlib.sha256(check.read_bytes()).hexdigest()
                if h != finfo["sha256"].lower():
                    raise HTTPException(400, f"{name} sha256 不匹配")
            # .part → 正式名
            if part.exists():
                os.replace(str(part), str(final))
        with JOBS_LOCK:
            job["status"] = "done"
            job["ended"] = time.time()
            _save_jobs()
    else:
        # worker 报告失败
        with JOBS_LOCK:
            job["status"] = "failed"
            job["ended"] = time.time()
            # worker 回传的错误文本同样归类，不直接外露
            job["error_code"], job["error_msg"] = _classify_error_text(
                req.error)
            job["error"] = req.error
            _save_jobs()
        # 清理可能的 .part 残留
        for finfo in req.files:
            part = dest_dir / (Path(finfo["name"]).name + ".part")
            part.unlink(missing_ok=True)
    return {"ok": True, "status": job["status"]}


# ---------------------------------------------------------------------------
# 会话持久化（跨设备恢复，无需登录）
# ---------------------------------------------------------------------------
SESSION_DIR = ROOT / "data" / "sessions"
SESSION_DIR.mkdir(parents=True, exist_ok=True)


class SessionSave(BaseModel):
    id: str
    data: dict


@app.post("/api/session/save")
def api_session_save(req: SessionSave):
    """保存会话状态到云端（跨设备恢复用）。"""
    sid = req.id.strip()
    if not sid or len(sid) > 64 or not sid.isalnum():
        raise HTTPException(400, "非法 session id")
    path = SESSION_DIR / f"{sid}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(req.data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return {"ok": True}


@app.get("/api/session/{sid}")
def api_session_get(sid: str):
    """读取会话状态。"""
    if not sid.isalnum() or len(sid) > 64:
        raise HTTPException(400, "非法 session id")
    path = SESSION_DIR / f"{sid}.json"
    if not path.exists():
        raise HTTPException(404, "会话不存在")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise HTTPException(500, "会话文件损坏")


# 产物与前端静态托管（放在 API 路由之后）
app.mount("/files", StaticFiles(directory=str(OUTPUT_DIR)), name="files")
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


@app.exception_handler(404)
async def not_found(request, exc):
    return JSONResponse({"detail": "not found"}, status_code=404)


def _lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("STUDIO_PORT", "8787"))
    print(f"[studio] 本机:   http://127.0.0.1:{port}")
    print(f"[studio] 手机端: http://{_lan_ip()}:{port}  (同一 WiFi)")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
