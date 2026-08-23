"""Identical web requests share one generation job and its artifacts."""

from concurrent.futures import ThreadPoolExecutor
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
def isolated_worker_jobs(monkeypatch):
    server.JOBS.clear()
    monkeypatch.setattr(server, "WORKER_MODE", True)
    monkeypatch.setattr(server, "_save_jobs", lambda: None)
    monkeypatch.setattr(
        server, "_pbf_status",
        lambda bbox: {"state": "local", "pbf": "fixture.osm.pbf"},
    )
    yield
    server.JOBS.clear()


def _styles_request(prototype="landscape"):
    return server.StylesRequest(
        bbox=[29.3777, 118.6099, 29.5134, 118.7646],
        name="千岛湖",
        prototype=prototype,
    )


def test_concurrent_identical_style_requests_share_one_job():
    request = _styles_request()

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: server.api_styles(request), range(8)))

    assert len(server.JOBS) == 1
    assert len({response["job_id"] for response in responses}) == 1
    assert sum(not response["reused"] for response in responses) == 1
    assert sum(response["reused"] for response in responses) == 7
    only_job = next(iter(server.JOBS.values()))
    assert only_job["spec"]["env_extra"]["PYTHONUNBUFFERED"] == "1"


def test_completed_style_request_returns_cached_job(monkeypatch):
    first = server.api_styles(_styles_request())
    server.JOBS[first["job_id"]]["status"] = "done"
    monkeypatch.setattr(server, "_job_artifacts_available", lambda job: True)

    second = server.api_styles(_styles_request())

    assert second["job_id"] == first["job_id"]
    assert second["reused"] is True
    assert second["cached"] is True
    assert len(server.JOBS) == 1


def test_different_style_parameters_do_not_share_running_output():
    server.api_styles(_styles_request("landscape"))

    with pytest.raises(HTTPException) as exc:
        server.api_styles(_styles_request("terrain"))

    assert exc.value.status_code == 409
    assert "另一组参数" in exc.value.detail


def test_identical_model_requests_share_one_job():
    request = server.GenerateRequest(
        city="westlake", mode="draft", generation_profile="classic",
    )

    first = server.api_generate(request)
    second = server.api_generate(request)

    assert second["job_id"] == first["job_id"]
    assert second["reused"] is True
    assert second["cached"] is False
    assert len(server.JOBS) == 1


def test_different_model_parameters_wait_for_current_output():
    server.api_generate(server.GenerateRequest(
        city="westlake", mode="draft", generation_profile="classic",
    ))

    with pytest.raises(HTTPException) as exc:
        server.api_generate(server.GenerateRequest(
            city="westlake", mode="full", generation_profile="classic",
        ))

    assert exc.value.status_code == 409
    assert "另一组参数" in exc.value.detail


def test_style_result_can_be_reused_while_model_for_same_city_runs(monkeypatch):
    styles = server.api_styles(server.StylesRequest(
        bbox=[30.13, 120.01, 30.36, 120.29],
        name="杭州 · 西湖",
        prototype="landscape",
        slug="westlake",
    ))
    server.JOBS[styles["job_id"]]["status"] = "done"
    monkeypatch.setattr(server, "_job_artifacts_available", lambda job: True)
    server.api_generate(server.GenerateRequest(
        city="westlake", mode="draft", generation_profile="classic",
    ))

    reused = server.api_styles(server.StylesRequest(
        bbox=[30.13, 120.01, 30.36, 120.29],
        name="杭州 · 西湖",
        prototype="landscape",
        slug="westlake",
    ))

    assert reused["job_id"] == styles["job_id"]
    assert reused["cached"] is True
    assert len(server.JOBS) == 2


def test_public_style_job_exposes_original_area_context():
    response = server.api_styles(_styles_request("terrain"))

    public = server.api_job(response["job_id"])

    assert public["bbox"] == [29.3777, 118.6099, 29.5134, 118.7646]
    assert public["prototype"] == "terrain"
    assert public["city_title"] == "千岛湖"


def test_model_request_rejects_gallery_from_another_bbox():
    bbox = [48.7906, 2.2229, 48.9262, 2.4277]
    request = server.GenerateRequest(
        mode="draft",
        area=server.CustomArea(bbox=bbox, name="巴黎"),
        style="baseline",
        gallery_slug="custom_bdb29b",
    )

    with pytest.raises(HTTPException) as exc:
        server.api_generate(request)

    assert exc.value.status_code == 409
    assert "不是同一区域" in exc.value.detail
