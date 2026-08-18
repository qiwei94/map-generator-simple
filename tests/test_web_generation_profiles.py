"""Web generation profiles keep legacy and quality generation isolated."""

from pathlib import Path
import sys

import pytest
from fastapi import HTTPException


ROOT = Path(__file__).resolve().parents[1]
WEBAPP = ROOT / "webapp"
if str(WEBAPP) not in sys.path:
    sys.path.insert(0, str(WEBAPP))

import server  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_jobs(monkeypatch):
    server.JOBS.clear()
    monkeypatch.setattr(server, "WORKER_MODE", True)
    monkeypatch.setattr(server, "_save_jobs", lambda: None)
    monkeypatch.setattr(server, "_city_running", lambda city: False)
    yield
    server.JOBS.clear()


def _queued_job(response):
    return server.JOBS[response["job_id"]]


@pytest.mark.parametrize(
    ("profile", "block_mode"),
    [("quality_flat", "flat"), ("quality_textured", "textured")],
)
def test_quality_profiles_use_isolated_westlake_entry(profile, block_mode):
    response = server.api_generate(server.GenerateRequest(
        city="westlake",
        mode="full",
        generation_profile=profile,
    ))

    job = _queued_job(response)
    cmd = job["spec"]["cmd"]
    assert cmd[1] == "generate_city.py"
    assert cmd[cmd.index("--block-base-mode") + 1] == block_mode
    assert cmd[cmd.index("--block-base-edge-retreat-mm") + 1] == "2"
    assert job["spec"]["env_extra"]["PYTHONUNBUFFERED"] == "1"
    assert response["city"] == f"westlake_{profile}"
    assert job["generation_profile"] == profile


def test_classic_profile_keeps_legacy_entry():
    response = server.api_generate(server.GenerateRequest(
        city="westlake",
        mode="draft",
        generation_profile="classic",
    ))

    job = _queued_job(response)
    assert job["spec"]["cmd"][1] == "generate_city_legacy.py"
    assert "--draft" in job["spec"]["cmd"]
    assert "--preview-fast" in job["spec"]["cmd"]
    assert "--no-vegetation" in job["spec"]["cmd"]
    assert "--png" not in job["spec"]["cmd"]
    assert "--review-png" not in job["spec"]["cmd"]
    assert job["spec"]["cmd"][
        job["spec"]["cmd"].index("--base-thickness-mm") + 1] == "0.40"


def test_custom_draft_reuses_selected_gallery_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(server, "_pbf_status", lambda bbox: {
        "state": "local", "pbf": "pbf_cache/zhejiang-latest.osm.pbf",
        "region": "zhejiang", "fetch": None,
    })
    monkeypatch.setattr(server, "_load_gallery", lambda city: {
        "prototype": "landscape",
        "scene_type": "water_landscape",
        "styles": {"baseline": {"params": {
            "road_width_multiplier": 2.0,
        }}},
    })

    response = server.api_generate(server.GenerateRequest(
        mode="draft", style="baseline", generation_profile="classic",
        area=server.CustomArea(
            bbox=[29.5372, 118.89, 29.6728, 119.045], name="千岛湖"),
    ))

    job = _queued_job(response)
    cmd = job["spec"]["cmd"]
    assert cmd[1] == "tools/generate_gallery_draft.py"
    assert "--scene-type" in cmd
    assert cmd[cmd.index("--scene-type") + 1] == "water_landscape"
    assert "--png" not in cmd
    assert job["fast_draft"] is True
    assert "1–3 分钟" in server._job_duration_hint(job)


def test_classic_full_keeps_print_render_outputs():
    response = server.api_generate(server.GenerateRequest(
        city="westlake", mode="full", generation_profile="classic",
    ))

    cmd = _queued_job(response)["spec"]["cmd"]
    assert "--draft" not in cmd
    assert "--preview-fast" not in cmd
    assert "--png" in cmd
    assert "--review-png" in cmd


@pytest.mark.parametrize(
    "generation_request",
    [
        server.GenerateRequest(
            city="westlake", mode="draft", generation_profile="quality_flat",
        ),
        server.GenerateRequest(
            city="chicago", mode="full", generation_profile="quality_flat",
        ),
        server.GenerateRequest(
            mode="full",
            generation_profile="quality_flat",
            area=server.CustomArea(bbox=[30.22, 120.11, 30.27, 120.17]),
        ),
    ],
)
def test_quality_profile_rejects_unsupported_targets(generation_request):
    with pytest.raises(HTTPException) as exc:
        server.api_generate(generation_request)

    assert exc.value.status_code == 400


def test_profiles_endpoint_describes_scope_and_draft_support():
    profiles = server.api_generation_profiles()["profiles"]

    assert profiles["classic"]["scope"] == "all"
    assert profiles["classic"]["label"] == "标准生成"
    assert profiles["classic"]["draft"] is True
    assert profiles["quality_flat"]["scope"] == "westlake"
    assert "西湖质量" not in profiles["quality_flat"]["label"]
    assert profiles["quality_flat"]["draft"] is False


def test_job_status_exposes_real_stage_and_zero_feature_warning(tmp_path):
    log_path = tmp_path / "job.log"
    log_path.write_text(
        "[Stage 4.5] Preprocessing layers\n"
        "BL=6 BO=376 WL=0 WO=0 block_base=575 roads=390\n"
        "  [glb] water: +21 satellite polys (true shape)\n"
        "  [postcheck] PASS: 全部图层落地\n",
        encoding="utf-8",
    )
    job = {
        "id": "testjob",
        "city": "westlake",
        "city_title": "杭州 · 西湖",
        "mode": "draft",
        "style": None,
        "generation_profile": "classic",
        "status": "running",
        "started": 1.0,
        "ended": None,
        "log_path": str(log_path),
    }

    public = server._job_public(job)

    assert public["stage_label"] == "正在整理道路、建筑与水体图层"
    assert public["progress_pct"] == 62
    assert public["duration_hint"] == "通常需要 5–15 分钟"
    assert "水体" in public["quality_warnings"][0]
    checks = {check["id"]: check for check in public["quality_checks"]}
    assert checks["source_features"]["status"] == "warning"
    assert checks["secondary_map"]["detail"] == "补入 21 个水体面"
    assert checks["grounding"]["status"] == "pass"


def test_style_job_progress_uses_real_gallery_markers(tmp_path):
    log_path = tmp_path / "styles.log"
    log_path.write_text(
        "[area-gallery] custom bbox=(...)\n"
        "[Tile Cache] building: cold frame\n"
        "[Tile Cache] road: cold frame\n"
        "[Tile Cache] water: cold frame\n"
        "[Tile Cache] vegetation: cold frame\n",
        encoding="utf-8",
    )
    job = {
        "id": "stylejob",
        "city": "custom_test",
        "city_title": "千岛湖",
        "mode": "styles",
        "status": "running",
        "started": 1.0,
        "ended": None,
        "log_path": str(log_path),
    }

    public = server._job_public(job)

    assert public["progress_pct"] == 64
    assert public["stage_label"] == "正在提取绿地与地表信息"
    assert "8–15 分钟" in public["duration_hint"]


def test_style_duration_hint_scales_with_selected_area():
    small = {"mode": "styles", "bbox": [48.838, 2.3125, 48.8833, 2.3807]}
    large = {"mode": "styles", "bbox": [48.7906, 2.2229, 48.9262, 2.4277]}

    assert "8–15 分钟" in server._job_duration_hint(small)
    assert "20–40 分钟" in server._job_duration_hint(large)


@pytest.mark.parametrize(("completed_marker", "progress", "label"), [
    ("[custom/baseline] score=5.0", 89, "正在渲染第 2 种风格"),
    ("[custom/block_fill] score=5.0", 92, "正在渲染第 3 种风格"),
    ("[custom/dense_detail] score=5.0", 95, "正在渲染第 4 种风格"),
    ("[custom/minimal] score=5.0", 97, "四种风格已经渲染完成"),
])
def test_style_progress_reports_next_style_after_completed_marker(
        tmp_path, completed_marker, progress, label):
    log_path = tmp_path / "styles.log"
    log_path.write_text(
        f"[harness] prepared in 1.0s\n  {completed_marker}\n",
        encoding="utf-8",
    )
    job = {
        "id": "stylejob",
        "city": "custom_test",
        "city_title": "巴黎",
        "mode": "styles",
        "status": "running",
        "started": 1.0,
        "ended": None,
        "log_path": str(log_path),
    }

    public = server._job_public(job)

    assert public["progress_pct"] == progress
    assert public["stage_label"] == label
