"""Capability-aware SQLite leases and user-visible progress protocol."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
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
import tools.cloud_worker as cloud_worker  # noqa: E402
import server  # noqa: E402
from tools.cloud_worker import (  # noqa: E402
    _prepare_command,
    _resolve_token,
    run_task,
)


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


def _pipeline_state(run_id, attempt_id, revision, marker):
    return {
        "schema_version": "pipeline-ledger-v1",
        "contract_version": server.CONTRACT_VERSION,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "revision": revision,
        "marker": marker,
        "stages": [{"id": stage.id} for stage in server.PIPELINE_STAGES],
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
    assert reclaimed[0]["pipeline_attempt_id"]
    assert reclaimed[0]["pipeline_attempt_id"] != ""
    assert reclaimed[0]["spec"]["env_extra"][
        "MAP_PIPELINE_ATTEMPT_ID"] == reclaimed[0]["pipeline_attempt_id"]
    assert reclaimed[0]["spec"]["env_extra"][
        "MAP_PIPELINE_RUN_ID"] == "job1"
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


def test_worker_spec_injects_pipeline_run_and_attempt_identity():
    spec = server._make_worker_spec(
        [sys.executable, "generate_city_legacy.py", "--city", "chicago"],
        "full", run_id="job-run-7", attempt_id="attempt-3",
    )

    assert spec["env_extra"]["MAP_PIPELINE_RUN_ID"] == "job-run-7"
    assert spec["env_extra"]["MAP_PIPELINE_ATTEMPT_ID"] == "attempt-3"
    assert spec["env_extra"]["MAP_PIPELINE_MODE"] == "full"


def test_cloud_worker_reads_only_current_attempt_and_highest_revision(
        monkeypatch, tmp_path):
    run_id = "job-run-7"
    attempt_id = "attempt-current"
    filename = f"pipeline_state.{run_id}.{attempt_id}.json"
    explicit_state_dir = tmp_path / "explicit-state"
    worker_root = tmp_path / "worker-root"
    studio_root = tmp_path / "studio-output"
    worker_state_dir = worker_root / "output" / "chicago" / ".pipeline_runs"
    current_dir = studio_root / "chicago" / ".pipeline_runs"
    explicit_state_dir.mkdir()
    worker_state_dir.mkdir(parents=True)
    current_dir.mkdir(parents=True)

    # A lower revision for this attempt may coexist in another configured
    # state root.  The worker should stream the newest revision it can see.
    (explicit_state_dir / filename).write_text(json.dumps(
        _pipeline_state(run_id, attempt_id, 2, "stale-current-attempt")),
        encoding="utf-8")
    # Same filename but mismatching payload identity must be ignored.
    (worker_state_dir / filename).write_text(json.dumps(
        _pipeline_state(run_id, "attempt-other", 900, "wrong-attempt")),
        encoding="utf-8")
    (current_dir / filename).write_text(json.dumps(
        _pipeline_state(run_id, attempt_id, 8, "current-attempt")),
        encoding="utf-8")
    (current_dir / f"pipeline_state.{run_id}.attempt-other.json").write_text(
        json.dumps(_pipeline_state(
            run_id, "attempt-other", 999, "unrelated-newer")),
        encoding="utf-8")
    env = {
        "MAP_PIPELINE_RUN_ID": run_id,
        "MAP_PIPELINE_ATTEMPT_ID": attempt_id,
        "MAP_PIPELINE_STATE_DIR": str(explicit_state_dir),
        "STUDIO_OUTPUT_DIR": str(studio_root),
    }
    monkeypatch.setattr(cloud_worker, "_ROOT", worker_root)

    snapshot = cloud_worker._pipeline_ledger_snapshot(
        [sys.executable, "generate_city_legacy.py", "--city", "chicago"],
        env,
    )

    assert snapshot is not None
    assert snapshot["attempt_id"] == attempt_id
    assert snapshot["revision"] == 8
    assert snapshot["marker"] == "current-attempt"
    assert cloud_worker._pipeline_ledger_artifact(
        [sys.executable, "generate_city_legacy.py", "--city", "chicago"],
        env,
    ) == current_dir / filename


def test_worker_token_file_takes_precedence(tmp_path):
    token_file = tmp_path / "worker.token"
    token_file.write_text("node-secret\n", encoding="utf-8")

    assert _resolve_token("legacy-secret", str(token_file)) == "node-secret"


def test_worker_token_file_rejects_empty_file(tmp_path):
    token_file = tmp_path / "worker.token"
    token_file.write_text("\n", encoding="utf-8")

    with pytest.raises(ValueError, match="为空"):
        _resolve_token("", str(token_file))


def test_dry_run_artifacts_are_isolated_from_production_output(
        monkeypatch, tmp_path):
    dry_root = tmp_path / "worker-dry-run"
    production_root = tmp_path / "production"
    monkeypatch.setenv("WORKER_DRY_RUN_OUTPUT_DIR", str(dry_root))
    monkeypatch.setenv("STUDIO_OUTPUT_DIR", str(production_root))

    ok, error, files = run_task({"task": {
        "entrypoint": "generate_city_legacy.py",
        "args": ["--city", "westlake"],
    }}, dry_run=True)

    assert ok is True
    assert error == ""
    assert files
    assert all(path.is_relative_to(dry_root) for path in files)
    assert not production_root.exists()


def test_cloud_worker_upload_binds_every_file_to_attempt(tmp_path):
    artifact = tmp_path / "preview.glb"
    artifact.write_bytes(b"attempt-bound")

    class Response:
        status_code = 200
        text = ""

    class Session:
        def __init__(self):
            self.calls = []

        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return Response()

    session = Session()
    manifest = cloud_worker.upload_files(
        session, "https://worker.test", "job-7", "attempt-4",
        "windows-primary", [artifact],
    )

    assert len(manifest) == 1
    assert session.calls[0][1]["params"]["attempt_id"] == "attempt-4"


@pytest.mark.parametrize("task_ok", (True, False))
def test_cloud_worker_returns_leased_attempt_on_heartbeat_and_finish(
        monkeypatch, task_ok, tmp_path):
    calls = []

    class Response:
        def __init__(self, payload):
            self.status_code = 200
            self.text = ""
            self._payload = payload

        def json(self):
            return self._payload

    class Session:
        def __init__(self):
            self.headers = {}
            self.verify = True

        def get(self, url, **kwargs):
            calls.append(("get", url, kwargs))
            return Response({
                "job_id": "job-7", "attempt_id": "attempt-4",
                "mode": "draft", "spec": {"task": {}},
            })

        def post(self, url, **kwargs):
            calls.append(("post", url, kwargs))
            return Response({"ok": True})

    produced = tmp_path / "preview.glb"
    produced.write_bytes(b"preview")

    def fake_run_task(spec, *, heartbeat, **_kwargs):
        assert spec["env_extra"]["MAP_PIPELINE_RUN_ID"] == "job-7"
        assert spec["env_extra"]["MAP_PIPELINE_ATTEMPT_ID"] == "attempt-4"
        assert heartbeat("working", {"progress_pct": 20}) is True
        if task_ok:
            return True, "", [produced]
        return False, "expected failure", []

    def fake_upload(_session, _server, job_id, attempt_id, worker_id,
                    files, progress=None):
        assert (job_id, attempt_id, worker_id) == (
            "job-7", "attempt-4", "test-worker")
        assert files == [produced]
        return [{"name": produced.name, "sha256": "a" * 64,
                 "size": produced.stat().st_size}]

    monkeypatch.setattr(cloud_worker.requests, "Session", Session)
    monkeypatch.setattr(cloud_worker, "detect_capabilities", lambda _c: {
        "cpu_threads": 6, "memory_mb": 16000, "pbf_files": [],
    })
    monkeypatch.setattr(cloud_worker, "run_task", fake_run_task)
    monkeypatch.setattr(cloud_worker, "upload_files", fake_upload)
    monkeypatch.setattr(sys, "argv", [
        "cloud_worker.py", "--server", "https://worker.test",
        "--token", "secret", "--worker-id", "test-worker",
        "--max-tasks", "1",
    ])

    cloud_worker.main()

    heartbeat = next(kwargs["json"] for method, url, kwargs in calls
                     if method == "post" and url.endswith("/heartbeat"))
    finish = next(kwargs["json"] for method, url, kwargs in calls
                  if method == "post" and url.endswith("/finish"))
    assert heartbeat["attempt_id"] == "attempt-4"
    assert finish["attempt_id"] == "attempt-4"
    assert finish["ok"] is task_ok


def test_successful_process_without_artifacts_is_not_a_success(
        monkeypatch, tmp_path):
    script = tmp_path / "generate_city_legacy.py"
    script.write_text("print('completed without output')\n", encoding="utf-8")
    monkeypatch.setattr(cloud_worker, "_ROOT", tmp_path)

    ok, error, files = run_task({"task": {
        "entrypoint": "generate_city_legacy.py",
        "args": ["--city", "empty-city"],
    }})

    assert ok is False
    assert "没有发现任何交付产物" in error
    assert files == []


def test_server_accepts_only_digest_bound_to_worker(monkeypatch, tmp_path):
    token_hashes = tmp_path / "worker-token-hashes.json"
    token_hashes.write_text(json.dumps({
        "windows-primary": hashlib.sha256(
            b"windows-secret").hexdigest(),
    }), encoding="utf-8")
    monkeypatch.setattr(server, "WORKER_TOKEN", "")
    monkeypatch.setattr(server, "WORKER_TOKEN_HASH_FILE", token_hashes)

    server._check_worker_token("windows-secret", "windows-primary")
    with pytest.raises(server.HTTPException) as wrong_worker:
        server._check_worker_token("windows-secret", "controller")
    assert wrong_worker.value.status_code == 401
    with pytest.raises(server.HTTPException) as wrong_token:
        server._check_worker_token("wrong", "windows-primary")
    assert wrong_token.value.status_code == 401


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
