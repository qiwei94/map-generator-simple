"""Server-enforced guest allowance, ownership, reuse and account claiming."""

from pathlib import Path
import sqlite3
import sys

import pytest
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
WEBAPP = ROOT / "webapp"
if str(WEBAPP) not in sys.path:
    sys.path.insert(0, str(WEBAPP))

from auth_store import AuthStore  # noqa: E402
import server  # noqa: E402


class _MemoryJobEvents:
    def __init__(self):
        self.events = {}

    def append_event(self, job_id, kind, payload):
        rows = self.events.setdefault(job_id, [])
        rows.append({"id": len(rows) + 1, "kind": kind, "payload": payload})

    def list_events(self, job_id, after_id=0):
        return [row for row in self.events.get(job_id, [])
                if row["id"] > after_id]


@pytest.fixture
def guest_server(monkeypatch, tmp_path):
    auth_store = AuthStore(tmp_path / "guest-http.db", "guest-test-secret")
    monkeypatch.setattr(server, "_AUTH_STORE", auth_store)
    monkeypatch.setattr(server, "AUTH_DEV_ECHO_CODE", True)
    monkeypatch.setattr(server, "AUTH_COOKIE_SECURE", False)
    monkeypatch.setattr(server, "AUTH_REQUIRED", True)
    monkeypatch.setattr(server, "GUEST_GENERATION_LIMIT", 3)
    monkeypatch.setattr(server, "WORKER_MODE", True)
    monkeypatch.setattr(server, "_save_jobs", lambda: None)
    monkeypatch.setattr(server, "_JOB_STORE", _MemoryJobEvents())
    monkeypatch.setattr(server, "_pbf_status", lambda bbox: {
        "state": "local", "pbf": "fixture.osm.pbf",
    })
    server.JOBS.clear()
    yield auth_store
    server.JOBS.clear()


def _style_payload(index=0):
    south = 30.10 + index * 0.02
    return {
        "bbox": [south, 120.10, south + 0.06, 120.17],
        "name": f"游客区域 {index}",
        "prototype": "landscape",
    }


def _login(client, email="guest-owner@example.com"):
    started = client.post("/api/auth/email/start", json={"email": email})
    assert started.status_code == 200
    verified = client.post("/api/auth/email/verify", json={
        "email": email, "code": started.json()["dev_code"],
    })
    assert verified.status_code == 200
    return verified


def test_first_visit_gets_http_only_guest_with_three_server_side_uses(
        guest_server):
    client = TestClient(server.app)

    first_me = client.get("/api/auth/me")
    assert first_me.status_code == 200
    assert first_me.json()["authenticated"] is False
    assert first_me.json()["guest"]["quota_remaining"] == 3
    assert "studio_guest=" in first_me.headers["set-cookie"]
    assert "HttpOnly" in first_me.headers["set-cookie"]

    submissions = [
        ("/api/styles", _style_payload(0)),
        ("/api/generate", {
            "city": "westlake", "mode": "draft",
            "generation_profile": "classic",
        }),
        ("/api/generate", {
            "city": "chicago", "mode": "full",
            "generation_profile": "classic",
        }),
    ]
    for index, (endpoint, payload) in enumerate(submissions):
        submitted = client.post(endpoint, json=payload)
        assert submitted.status_code == 200
        assert client.get("/api/auth/me").json()["guest"][
            "quota_remaining"] == 2 - index

    rejected = client.post("/api/styles", json=_style_payload(4))
    assert rejected.status_code == 429
    assert rejected.json()["detail"] == {
        "code": "guest_quota_exhausted",
        "message": "游客的 3 次免费生成机会已用完，请登录后继续",
    }
    assert len(server.JOBS) == 3


@pytest.mark.parametrize("payload", [
    {
        "bbox": [30.10, 120.10, 30.16, 120.17],
        "name": "目录穿越",
        "prototype": "landscape",
        "slug": "../../outside",
    },
    {
        "bbox": [20.0, 100.0, 40.0, 130.0],
        "name": "超大范围",
        "prototype": "landscape",
    },
])
def test_invalid_style_request_is_rejected_before_guest_is_created(
        guest_server, payload):
    client = TestClient(server.app)

    rejected = client.post("/api/styles", json=payload)

    assert rejected.status_code == 400
    assert "studio_guest=" not in rejected.headers.get("set-cookie", "")
    with sqlite3.connect(guest_server.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM guest_sessions").fetchone()[0] == 0


def test_identical_active_work_is_shared_without_charging_second_guest(
        guest_server):
    first_client = TestClient(server.app)
    second_client = TestClient(server.app)
    first_client.get("/api/auth/me")
    second_client.get("/api/auth/me")

    first = first_client.post("/api/styles", json=_style_payload())
    shared = second_client.post("/api/styles", json=_style_payload())

    assert first.status_code == 200
    assert shared.status_code == 200
    assert shared.json()["reused"] is True
    assert shared.json()["job_id"] == first.json()["job_id"]
    assert first_client.get("/api/auth/me").json()["guest"][
        "quota_remaining"] == 2
    assert second_client.get("/api/auth/me").json()["guest"][
        "quota_remaining"] == 3
    assert second_client.get(
        f"/api/jobs/{first.json()['job_id']}").status_code == 200


def test_guest_jobs_are_private_even_when_global_auth_is_optional(
        guest_server, monkeypatch):
    monkeypatch.setattr(server, "AUTH_REQUIRED", False)
    owner = TestClient(server.app)
    stranger = TestClient(server.app)
    owner.get("/api/auth/me")
    stranger.get("/api/auth/me")
    submitted = owner.post("/api/styles", json=_style_payload())
    job_id = submitted.json()["job_id"]

    assert stranger.get(f"/api/jobs/{job_id}").status_code == 404
    assert stranger.get(f"/api/jobs/{job_id}/events").status_code == 404
    assert stranger.get("/api/jobs?mine=true").json()["jobs"] == []
    assert owner.get("/api/jobs?mine=true").json()["jobs"][0]["id"] == job_id


def test_failed_guest_work_refunds_once(guest_server):
    client = TestClient(server.app)
    client.get("/api/auth/me")
    submitted = client.post("/api/styles", json=_style_payload())
    job = server.JOBS[submitted.json()["job_id"]]

    server._refund_job_quota(job)
    server._refund_job_quota(job)

    assert client.get("/api/auth/me").json()["guest"][
        "quota_remaining"] == 3


def test_restart_marks_local_work_failed_and_refunds_guest(
        guest_server, monkeypatch):
    guest, _ = guest_server.create_guest_session(quota_limit=3)
    guest_server.reserve_guest_generation(guest.id, "restart01")
    recovered = {
        "restart01": {
            "id": "restart01", "city": "custom_restart",
            "mode": "styles", "exec": "local", "status": "running",
            "started": 1.0, "ended": None, "log_path": "",
            "quota_payer_kind": "guest", "quota_payer_id": guest.id,
            "quota_cost": 1, "guest_owner_ids": [guest.id],
        },
    }

    class _RecoveredJobs(_MemoryJobEvents):
        def load_jobs(self):
            return recovered

    monkeypatch.setattr(server, "_JOB_STORE", _RecoveredJobs())
    monkeypatch.setattr(server, "_save_jobs", lambda: None)
    server.JOBS.clear()

    server._load_jobs()

    assert server.JOBS["restart01"]["status"] == "failed"
    assert server.JOBS["restart01"]["quota_refunded"] is True
    assert guest_server.get_guest(guest.id).quota_remaining == 3


def test_email_login_claims_guest_jobs_for_account_history(guest_server):
    client = TestClient(server.app)
    client.get("/api/auth/me")
    submitted = client.post("/api/styles", json=_style_payload())
    job_id = submitted.json()["job_id"]

    verified = _login(client)
    mine = client.get("/api/jobs?mine=true")

    assert verified.json()["claimed_guest_jobs"] == 1
    assert verified.json()["user"]["id"] in server.JOBS[job_id]["owner_ids"]
    assert server.JOBS[job_id]["guest_owner_ids"] == []
    assert mine.status_code == 200
    assert mine.json()["jobs"][0]["id"] == job_id
    assert client.get("/api/auth/me").json()["guest"] is None


def test_legacy_public_jobs_do_not_pollute_guest_task_history(guest_server):
    server.JOBS["legacy01"] = {
        "id": "legacy01", "city": "westlake", "city_title": "历史样品",
        "mode": "full", "status": "done", "started": 1.0,
        "ended": 2.0, "log_path": "",
    }
    client = TestClient(server.app)
    client.get("/api/auth/me")

    assert client.get("/api/jobs/legacy01").status_code == 200
    assert client.get("/api/jobs?mine=true").json()["jobs"] == []
