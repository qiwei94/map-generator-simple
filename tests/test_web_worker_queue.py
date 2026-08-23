"""Durable single-worker queue leases, fairness, and failure refunds."""

from pathlib import Path
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
WEBAPP = ROOT / "webapp"
if str(WEBAPP) not in sys.path:
    sys.path.insert(0, str(WEBAPP))

from auth_store import AuthStore  # noqa: E402
import server  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_queue(monkeypatch, tmp_path):
    server.JOBS.clear()
    monkeypatch.setattr(server, "WORKER_TOKEN", "queue-secret")
    monkeypatch.setattr(server, "WORKER_LEASE_SECONDS", 90)
    monkeypatch.setattr(server, "_save_jobs", lambda: None)
    monkeypatch.setattr(server, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(server, "GALLERY_DIR", tmp_path / "gallery")
    monkeypatch.setattr(server, "_LAST_WORKER_OWNER", None)
    yield
    server.JOBS.clear()


def _job(job_id, owner, queued_at, *, status="pending"):
    return {
        "id": job_id,
        "city": f"city-{job_id}",
        "city_title": f"City {job_id}",
        "mode": "draft",
        "exec": "worker",
        "status": status,
        "started": queued_at,
        "queued_at": queued_at,
        "ended": None,
        "owner_ids": [owner],
        "quota_payer_id": owner,
        "log_path": str(ROOT / "tmp" / f"{job_id}.log"),
        "spec": {"cmd": ["python", "noop.py"]},
    }


def test_queue_alternates_accounts_when_both_are_waiting():
    server.JOBS.update({
        "a1": _job("a1", "account-a", 1),
        "a2": _job("a2", "account-a", 2),
        "b1": _job("b1", "account-b", 3),
    })

    first = server.worker_next("queue-secret", "mac-worker")
    server.JOBS[first["job_id"]]["status"] = "done"
    second = server.worker_next("queue-secret", "mac-worker")

    assert first["job_id"] == "a1"
    assert second["job_id"] == "b1"
    assert server.JOBS["a2"]["status"] == "pending"


def test_heartbeat_extends_only_the_matching_worker_lease(tmp_path):
    job = _job("lease1", "account-a", 1)
    job["log_path"] = str(tmp_path / "lease1.log")
    server.JOBS[job["id"]] = job
    server.worker_next("queue-secret", "mac-worker")
    old_expiry = server.JOBS["lease1"]["lease_expires"]
    time.sleep(0.01)

    result = server.worker_heartbeat(server.WorkerHeartbeat(
        job_id="lease1", token="queue-secret", worker_id="mac-worker",
        log_tail="[Stage 3] reading map features\n",
    ))

    assert result["ok"] is True
    assert server.JOBS["lease1"]["lease_expires"] > old_expiry
    assert Path(job["log_path"]).read_text(encoding="utf-8").startswith(
        "[Stage 3]")


def test_expired_lease_is_reclaimed_by_another_worker():
    job = _job("expired1", "account-a", 1, status="running")
    job["worker_id"] = "dead-worker"
    job["lease_expires"] = time.time() - 1
    server.JOBS[job["id"]] = job

    claimed = server.worker_next("queue-secret", "replacement-worker")

    assert claimed["job_id"] == "expired1"
    assert server.JOBS["expired1"]["worker_id"] == "replacement-worker"
    assert server.JOBS["expired1"]["retry_count"] == 1


def test_worker_failure_refunds_reserved_quota(monkeypatch, tmp_path):
    store = AuthStore(tmp_path / "studio.db", "test-secret", default_quota=10)
    store.request_email_code("user@example.com", "123456",
                             now=1000, min_interval_s=0)
    user, _ = store.verify_email_code("user@example.com", "123456", now=1001)
    store.reserve_quota(user.id, "failed1", 3, now=1002)
    monkeypatch.setattr(server, "_AUTH_STORE", store)
    job = _job("failed1", user.id, 1, status="running")
    job.update({"worker_id": "mac-worker", "quota_cost": 3,
                "lease_expires": time.time() + 90})
    server.JOBS[job["id"]] = job

    result = server.worker_finish(server.WorkerFinish(
        job_id="failed1", token="queue-secret", worker_id="mac-worker",
        ok=False, error="renderer failed", files=[],
    ))

    assert result["status"] == "failed"
    assert store.get_user(user.id).quota_used == 0
    assert server.JOBS["failed1"]["quota_refunded"] is True

