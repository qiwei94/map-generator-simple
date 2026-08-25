"""Capability-aware SQLite leases and user-visible progress protocol."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
WEBAPP = ROOT / "webapp"
if str(WEBAPP) not in sys.path:
    sys.path.insert(0, str(WEBAPP))

from job_store import JobStore, worker_can_run  # noqa: E402
from progress_protocol import progress_from_log  # noqa: E402
import server  # noqa: E402
from tools.cloud_worker import _prepare_command  # noqa: E402


def _job(job_id="job1", pbf="illinois-latest.osm.pbf"):
    return {
        "id": job_id,
        "city": "chicago",
        "mode": "full",
        "exec": "worker",
        "status": "pending",
        "started": 1.0,
        "queued_at": 1.0,
        "log_path": str(ROOT / "tmp" / f"{job_id}.log"),
        "requirements": {
            "job_class": "full",
            "minimum_memory_mb": 12000,
            "pbf_file": pbf,
        },
        "spec": {"version": 1, "task": {
            "entrypoint": "generate_city_legacy.py",
            "args": ["--city", "chicago"],
        }},
    }


def _caps(pbf="illinois-latest.osm.pbf", memory=24000):
    return {
        "job_classes": ["styles", "draft", "full"],
        "memory_mb": memory,
        "pbf_files": [pbf],
        "native_osmium": True,
    }


def test_capabilities_block_missing_pbf_and_low_memory():
    job = _job()
    assert worker_can_run(job, _caps()) is True
    assert worker_can_run(job, _caps("zhejiang-latest.osm.pbf")) is False
    assert worker_can_run(job, _caps(memory=8000)) is False
    assert worker_can_run(job, None, require_capabilities=True) is False


def test_sqlite_lease_is_atomic_across_workers(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    store.save_job(_job())

    def claim(worker_id):
        return store.lease_next(
            worker_id, _caps(), 90, None,
            require_capabilities=True)[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(claim, ("windows-primary", "controller")))

    assert sum(job is not None for job in claimed) == 1
    persisted = store.load_jobs()["job1"]
    assert persisted["status"] == "running"
    assert persisted["worker_id"] in {"windows-primary", "controller"}


def test_expired_sqlite_lease_is_requeued_with_event(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    job = _job()
    job.update({"status": "running", "worker_id": "offline",
                "lease_expires": time.time() - 1})
    store.save_job(job)

    claimed, _, reclaimed = store.lease_next(
        "windows-primary", _caps(), 90, None,
        require_capabilities=True)

    assert reclaimed[0]["retry_reason"] == "worker_lease_expired"
    assert claimed["worker_id"] == "windows-primary"
    assert [event["type"] for event in store.list_events("job1")] == [
        "requeued", "leased"]


def test_progress_uses_real_pipeline_markers_and_counter():
    progress = progress_from_log(
        {"status": "running", "mode": "full", "progress_pct": 40},
        "[Stage 4.5] Preprocessing layers\n"
        "[amap] fetched 18/24 tiles\n"
        "[preprocess] road_continuity: restored\n",
    )

    assert progress["progress_pct"] == 63
    assert progress["stage_current"] == 18
    assert progress["stage_total"] == 24
    assert "18/24" in progress["stage_detail"]


def test_versioned_worker_task_rejects_unknown_entrypoint():
    with pytest.raises(ValueError, match="白名单"):
        _prepare_command({"task": {
            "entrypoint": "tools/arbitrary.py", "args": []}})


def test_inline_params_are_materialized_and_path_rewritten():
    cmd, temp_dir = _prepare_command({
        "task": {
            "entrypoint": "generate_city_legacy.py",
            "args": ["--city", "chicago", "--params-json",
                     "/root/map-generator-simple/tmp/a.json"],
        },
        "inline_files": [{
            "source_path": "/root/map-generator-simple/tmp/a.json",
            "name": "a.json",
            "content": '{"road_tier": "major"}',
        }],
    })
    try:
        params = Path(cmd[cmd.index("--params-json") + 1])
        assert params.exists()
        assert params.read_text(encoding="utf-8").startswith("{")
    finally:
        assert temp_dir is not None
        temp_dir.cleanup()


def test_public_status_exposes_heartbeat_and_eta(monkeypatch, tmp_path):
    job = _job()
    job.update({"status": "running", "claimed_at": time.time() - 70,
                "last_heartbeat": time.time() - 4,
                "progress_pct": 50, "stage_code": "stage_4.5",
                "stage_label": "正在整理图层"})
    job["log_path"] = str(tmp_path / "job.log")
    Path(job["log_path"]).write_text("[Stage 4.5]\n", encoding="utf-8")
    monkeypatch.setattr(server, "WORKER_LEASE_SECONDS", 90)

    public = server._job_public(job)

    assert public["worker_connection"] == "healthy"
    assert public["last_heartbeat_age_s"] < 10
    assert public["eta"]["low_s"] >= 0
    assert public["eta"]["high_s"] > public["eta"]["low_s"]
