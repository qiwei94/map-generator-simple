"""Durable single-worker queue leases, fairness, and failure refunds."""

import asyncio
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

from auth_store import AuthStore  # noqa: E402
from job_store import JobStore  # noqa: E402
from aesthetic.pipeline_contract import (  # noqa: E402
    S10_REQUIRED_ARTIFACT_BUNDLE,
    feature_source_counts_fingerprint,
    stage_by_id,
    stages_for_mode,
)
from aesthetic.pipeline_ledger import PipelineLedger  # noqa: E402
from aesthetic.pipeline_gates import evaluate_feature_survival  # noqa: E402
import server  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_queue(monkeypatch, tmp_path):
    server.JOBS.clear()
    monkeypatch.setattr(server, "WORKER_TOKEN", "queue-secret")
    monkeypatch.setattr(server, "WORKER_LEASE_SECONDS", 90)
    monkeypatch.setattr(server, "_JOB_STORE",
                        JobStore(tmp_path / "jobs.sqlite3"))
    monkeypatch.setattr(server, "JOBS_PATH", tmp_path / "jobs.json")
    monkeypatch.setattr(server, "WORKER_REQUIRE_CAPABILITIES", False)
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
        "pipeline_attempt_id": "attempt-1",
        "owner_ids": [owner],
        "quota_payer_id": owner,
        "log_path": str(ROOT / "tmp" / f"{job_id}.log"),
        "spec": {"cmd": ["python", "noop.py"]},
    }


def _pipeline_state(job_id, attempt_id="attempt-1", revision=1,
                    marker="current"):
    mode = "draft"
    stages = []
    for stage in server.PIPELINE_STAGES:
        applicable = stage.applies_to(mode)
        stages.append({
            "id": stage.id,
            "name": stage.name,
            "order": stage.order,
            "context_in_type": stage.context_in,
            "context_out_type": stage.context_out,
            "applicable": applicable,
            "status": "pending" if applicable else "not_applicable",
            "started_at": None,
            "completed_at": None,
            "context_in": None,
            "context_out": None,
            "artifacts": [],
            "error": None,
        })
    return {
        "schema_version": "pipeline-ledger-v1",
        "contract_version": server.CONTRACT_VERSION,
        "run_id": job_id,
        "attempt_id": attempt_id,
        "revision": revision,
        "marker": marker,
        "mode": mode,
        "status": "initialized",
        "current_stage_id": None,
        "artifacts": [],
        "stages": stages,
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
    assert first["attempt_id"] == "attempt-1"
    assert second["job_id"] == "b1"
    assert server.JOBS["a2"]["status"] == "pending"


def test_same_worker_old_attempt_cannot_renew_current_lease(tmp_path):
    job = _job("stale-heartbeat", "account-a", 1, status="running")
    job.update({
        "worker_id": "mac-worker",
        "pipeline_attempt_id": "attempt-current",
        "lease_expires": time.time() + 90,
        "progress_pct": 12,
        "log_path": str(tmp_path / "stale-heartbeat.log"),
    })
    server.JOBS[job["id"]] = job
    before = dict(job)

    with pytest.raises(server.HTTPException) as rejected:
        server.worker_heartbeat(server.WorkerHeartbeat(
            job_id=job["id"], attempt_id="attempt-old",
            token="queue-secret", worker_id="mac-worker",
            progress_pct=90, log_tail="must not be written",
        ))

    assert rejected.value.status_code == 409
    assert server.JOBS[job["id"]]["lease_expires"] == before[
        "lease_expires"]
    assert server.JOBS[job["id"]]["progress_pct"] == 12
    assert not Path(job["log_path"]).exists()


class _UnreadUpload:
    async def read(self, _size):
        raise AssertionError("stale attempt must be rejected before upload")


class _BytesUpload:
    def __init__(self, content):
        self.content = content

    async def read(self, _size):
        content, self.content = self.content, b""
        return content


def test_current_attempt_upload_publishes_verified_part_file():
    content = b"attempt-bound upload"
    job = _job("current-upload", "account-a", 1, status="running")
    job.update({
        "worker_id": "mac-worker",
        "pipeline_attempt_id": "attempt-current",
        "lease_expires": time.time() + 90,
    })
    server.JOBS[job["id"]] = job

    result = asyncio.run(server.worker_upload(
        attempt_id="attempt-current", token="queue-secret",
        job_id=job["id"], filename="preview.glb",
        sha256=hashlib.sha256(content).hexdigest(), worker_id="mac-worker",
        file=_BytesUpload(content),
    ))

    assert result["size"] == len(content)
    destination = server.OUTPUT_DIR / job["city"]
    assert (destination / "preview.glb.part").read_bytes() == content
    assert len(list(destination.iterdir())) == 1


def test_same_worker_old_attempt_cannot_upload_into_current_attempt(tmp_path):
    job = _job("stale-upload", "account-a", 1, status="running")
    job.update({
        "worker_id": "mac-worker",
        "pipeline_attempt_id": "attempt-current",
        "lease_expires": time.time() + 90,
    })
    server.JOBS[job["id"]] = job

    with pytest.raises(server.HTTPException) as rejected:
        asyncio.run(server.worker_upload(
            attempt_id="attempt-old", token="queue-secret",
            job_id=job["id"], filename="stale.glb", worker_id="mac-worker",
            file=_UnreadUpload(),
        ))

    assert rejected.value.status_code == 409
    assert not (server.OUTPUT_DIR / job["city"]).exists()


def test_upload_rechecks_attempt_before_publishing_part_file():
    job = _job("upload-attempt-cas", "account-a", 1, status="running")
    job.update({
        "worker_id": "mac-worker",
        "pipeline_attempt_id": "attempt-current",
        "lease_expires": time.time() + 90,
    })
    server.JOBS[job["id"]] = job

    class SwitchingUpload:
        sent = False

        async def read(self, _size):
            if self.sent:
                return b""
            self.sent = True
            server.JOBS[job["id"]]["pipeline_attempt_id"] = "attempt-next"
            return b"stale bytes"

    with pytest.raises(server.HTTPException) as rejected:
        asyncio.run(server.worker_upload(
            attempt_id="attempt-current", token="queue-secret",
            job_id=job["id"], filename="stale.glb", worker_id="mac-worker",
            file=SwitchingUpload(),
        ))

    assert rejected.value.status_code == 409
    destination = server.OUTPUT_DIR / job["city"]
    assert not (destination / "stale.glb.part").exists()
    assert not list(destination.iterdir())


@pytest.mark.parametrize("ok", (True, False))
def test_same_worker_old_attempt_cannot_finish_current_attempt(ok):
    job = _job("stale-finish", "account-a", 1, status="running")
    job.update({
        "worker_id": "mac-worker",
        "pipeline_attempt_id": "attempt-current",
        "lease_expires": time.time() + 90,
    })
    server.JOBS[job["id"]] = job

    with pytest.raises(server.HTTPException) as rejected:
        server.worker_finish(server.WorkerFinish(
            job_id=job["id"], attempt_id="attempt-old",
            token="queue-secret", worker_id="mac-worker",
            ok=ok, error="stale failure", files=[],
        ))

    assert rejected.value.status_code == 409
    assert server.JOBS[job["id"]]["status"] == "running"
    assert "error" not in server.JOBS[job["id"]]


def test_heartbeat_extends_only_the_matching_worker_lease(tmp_path):
    job = _job("lease1", "account-a", 1)
    job["log_path"] = str(tmp_path / "lease1.log")
    server.JOBS[job["id"]] = job
    server.worker_next("queue-secret", "mac-worker")
    old_expiry = server.JOBS["lease1"]["lease_expires"]
    time.sleep(0.01)

    result = server.worker_heartbeat(server.WorkerHeartbeat(
        job_id="lease1", attempt_id="attempt-1",
        token="queue-secret", worker_id="mac-worker",
        log_tail="[Stage 3] reading map features\n",
    ))

    assert result["ok"] is True
    assert server.JOBS["lease1"]["lease_expires"] > old_expiry
    assert Path(job["log_path"]).read_text(encoding="utf-8").startswith(
        "[Stage 3]")


@pytest.mark.parametrize("identity_patch", (
    {"run_id": "another-job"},
    {"attempt_id": "another-attempt"},
))
def test_heartbeat_rejects_mismatched_pipeline_identity_before_mutation(
        tmp_path, identity_patch):
    job = _job("identity1", "account-a", 1)
    job["log_path"] = str(tmp_path / "identity1.log")
    server.JOBS[job["id"]] = job
    server.worker_next("queue-secret", "mac-worker")
    before = dict(server.JOBS[job["id"]])
    ledger = _pipeline_state(job["id"])
    ledger.update(identity_patch)

    with pytest.raises(server.HTTPException) as rejected:
        server.worker_heartbeat(server.WorkerHeartbeat(
            job_id=job["id"], attempt_id="attempt-1",
            token="queue-secret",
            worker_id="mac-worker", progress_pct=80,
            pipeline_state=ledger,
        ))

    assert rejected.value.status_code == 400
    current = server.JOBS[job["id"]]
    assert current["lease_expires"] == before["lease_expires"]
    assert current.get("progress_pct") == before.get("progress_pct")
    assert "pipeline_ledger" not in current


def test_heartbeat_rejects_revision_regression_and_keeps_equal_idempotent(
        tmp_path):
    job = _job("revision1", "account-a", 1)
    job["log_path"] = str(tmp_path / "revision1.log")
    server.JOBS[job["id"]] = job
    server.worker_next("queue-secret", "mac-worker")

    accepted = server.worker_heartbeat(server.WorkerHeartbeat(
        job_id=job["id"], attempt_id="attempt-1",
        token="queue-secret", worker_id="mac-worker",
        progress_pct=20,
        pipeline_state=_pipeline_state(
            job["id"], revision=5, marker="accepted"),
    ))
    assert accepted["ok"] is True
    after_accepted = dict(server.JOBS[job["id"]])

    with pytest.raises(server.HTTPException) as stale:
        server.worker_heartbeat(server.WorkerHeartbeat(
            job_id=job["id"], attempt_id="attempt-1",
            token="queue-secret",
            worker_id="mac-worker", progress_pct=90,
            pipeline_state=_pipeline_state(
                job["id"], revision=4, marker="stale"),
        ))
    assert stale.value.status_code == 409
    current = server.JOBS[job["id"]]
    assert current["lease_expires"] == after_accepted["lease_expires"]
    assert current["progress_pct"] == 20
    assert current["pipeline_ledger"]["marker"] == "accepted"

    # Repeated delivery of the same revision renews the lease/progress but
    # cannot rewrite that immutable revision with different content.
    same_revision = server.worker_heartbeat(server.WorkerHeartbeat(
        job_id=job["id"], attempt_id="attempt-1",
        token="queue-secret", worker_id="mac-worker",
        progress_pct=22,
        pipeline_state=_pipeline_state(
            job["id"], revision=5, marker="must-not-rewrite"),
    ))
    assert same_revision["ok"] is True
    assert server.JOBS[job["id"]]["progress_pct"] == 22
    assert server.JOBS[job["id"]]["pipeline_ledger"]["marker"] == (
        "accepted")


@pytest.mark.parametrize("revision", ("6", True, -1))
def test_heartbeat_rejects_noncanonical_pipeline_revision(
        tmp_path, revision):
    job = _job("badrevision1", "account-a", 1)
    job["log_path"] = str(tmp_path / "badrevision1.log")
    server.JOBS[job["id"]] = job
    server.worker_next("queue-secret", "mac-worker")
    ledger = _pipeline_state(job["id"])
    ledger["revision"] = revision

    with pytest.raises(server.HTTPException) as rejected:
        server.worker_heartbeat(server.WorkerHeartbeat(
            job_id=job["id"], attempt_id="attempt-1",
            token="queue-secret",
            worker_id="mac-worker", pipeline_state=ledger,
        ))

    assert rejected.value.status_code == 400


def test_heartbeat_rejects_context_hash_tampering_before_mutation(tmp_path):
    job = _job("hashcheck1", "account-a", 1)
    job["log_path"] = str(tmp_path / "hashcheck1.log")
    server.JOBS[job["id"]] = job
    server.worker_next("queue-secret", "mac-worker")
    ledger = _pipeline_state(job["id"])
    ledger["status"] = "running"
    ledger["current_stage_id"] = "S0"
    ledger["stages"][0]["status"] = "running"
    ledger["stages"][0]["context_in"] = {
        "type": "RunRequest",
        "sha256": "0" * 64,
        "value": {"request": "tampered"},
    }

    with pytest.raises(server.HTTPException) as rejected:
        server.worker_heartbeat(server.WorkerHeartbeat(
            job_id=job["id"], attempt_id="attempt-1",
            token="queue-secret",
            worker_id="mac-worker", pipeline_state=ledger,
        ))

    assert rejected.value.status_code == 400
    assert "pipeline_ledger" not in server.JOBS[job["id"]]


def test_expired_lease_is_reclaimed_by_another_worker():
    job = _job("expired1", "account-a", 1, status="running")
    job["worker_id"] = "dead-worker"
    job["lease_expires"] = time.time() - 1
    server.JOBS[job["id"]] = job

    claimed = server.worker_next("queue-secret", "replacement-worker")

    assert claimed["job_id"] == "expired1"
    assert claimed["attempt_id"] == server.JOBS["expired1"][
        "pipeline_attempt_id"]
    assert server.JOBS["expired1"]["worker_id"] == "replacement-worker"
    assert server.JOBS["expired1"]["retry_count"] == 1
    assert server.JOBS["expired1"]["pipeline_attempt_id"] != "attempt-1"
    assert server.JOBS["expired1"]["spec"]["env_extra"][
        "MAP_PIPELINE_ATTEMPT_ID"] == server.JOBS["expired1"][
            "pipeline_attempt_id"]


def test_worker_cannot_finish_successfully_with_an_empty_manifest():
    job = _job("emptyfinish", "account-a", 1, status="running")
    job.update({"worker_id": "mac-worker",
                "lease_expires": time.time() + 90})
    server.JOBS[job["id"]] = job

    with pytest.raises(server.HTTPException) as rejected:
        server.worker_finish(server.WorkerFinish(
            job_id=job["id"], attempt_id="attempt-1",
            token="queue-secret",
            worker_id="mac-worker", ok=True, files=[],
        ))
    assert rejected.value.status_code == 400
    assert server.JOBS[job["id"]]["status"] == "running"


def test_local_zero_exit_without_attempt_bound_pipeline_artifacts_fails(
        tmp_path):
    class SuccessfulProcess:
        @staticmethod
        def wait():
            return 0

    log_path = tmp_path / "local-full.log"
    log_path.write_text("process exited zero\n", encoding="utf-8")
    job = _job("local-no-ledger", "account-a", 1, status="running")
    job.update({
        "mode": "full",
        "exec": "local",
        "proc": SuccessfulProcess(),
        "log_path": str(log_path),
    })
    server.JOBS[job["id"]] = job

    server._watch_job(job)

    assert job["status"] == "failed"
    assert "completion evidence rejected" in log_path.read_text(
        encoding="utf-8")


def test_styles_manifest_uses_the_real_gallery_metadata_name():
    job = _job("styles-manifest", "account-a", 1)
    job["mode"] = "styles"
    valid = [
        {"name": "gallery_metadata.json", "sha256": "a" * 64, "size": 10},
        {"name": "dense_detail_topdown.png", "sha256": "b" * 64,
         "size": 10},
    ]

    assert server._validated_worker_manifest(job, valid) == valid
    with pytest.raises(server.HTTPException, match="gallery_metadata.json"):
        server._validated_worker_manifest(job, [
            {"name": "gallery.json", "sha256": "a" * 64, "size": 10},
            valid[1],
        ])

def _valid_context(stage_id, *, mode="full"):
    value = {
        key: f"{stage_id}:{key}"
        for key in stage_by_id(stage_id).required_context_keys
    }
    preprocess_parameters = {
        "schema_version": "preprocess-parameters-v1", "effective_overrides": {}}
    preprocess_fingerprint = hashlib.sha256(json.dumps(
        preprocess_parameters, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    source_counts = {name: (1 if name == "roads" else 0) for name in (
        "roads", "water", "buildings", "vegetation")}
    source_fingerprint = feature_source_counts_fingerprint(source_counts)
    terrain_fingerprint = "a" * 64
    order = int(stage_id[1:])
    if 2 <= order <= 10:
        value.update(
            preprocess_parameters_fingerprint=preprocess_fingerprint,
            source_feature_counts_fingerprint=source_fingerprint,
            terrain_surface_fingerprint=terrain_fingerprint,
        )
    if 4 <= order <= 10:
        value["scene_character_fingerprint"] = "b" * 64
    if 5 <= order <= 10:
        value["scene_policy_fingerprint"] = "c" * 64
    if stage_id == "S0":
        value.update(
            city="worker-city", mode=mode,
            bbox_wgs84=[30.1, 120.0, 30.3, 120.3],
            scale_mm_per_m=0.04, printer_profile_id="test-printer")
    elif stage_id == "S1":
        value.update(
            raw_feature_counts={**source_counts, "landuse": 0},
            dem_evidence={
                "status": "ready", "source": "test", "flat_fallback": False},
            osmium_backend="native_osmium")
    elif stage_id == "S2":
        value.update(
            projected_feature_counts=source_counts,
            bbox_local_m=[0.0, 0.0, 1000.0, 1000.0],
            terrain_surface_plan={"fingerprint": terrain_fingerprint},
            preprocess_parameters=preprocess_parameters,
            preprocess_parameters_fingerprint=preprocess_fingerprint,
        )
    elif stage_id == "S3":
        value.update(
            layer_counts={
                "BL": 0, "BO": 0, "WL": 0, "WO": 0, "VL": 0,
                "VO": 0, "roads": 1, "block_base": 0},
            preprocess_policy_version="test-policy-v1")
    elif stage_id == "S4":
        value.update(
            scene_character_version="scene-character-v1",
            scene_status="ready", source_quality={})
    elif stage_id == "S5":
        value.update(
            scene_policy_version="scene-policy-v1", activation="audit_only",
            scene_class="urban", archetype="grid")
    elif stage_id == "S6":
        value.update(
            final_layer_counts={
                "BL": 0, "BO": 0, "WL": 0, "WO": 0, "VL": 0,
                "VO": 0, "roads": 1, "block_base": 0},
            building_mass_status="inactive",
            height_hierarchy_status="inactive")
    elif stage_id == "S7":
        value["review_artifacts"] = []
    elif stage_id == "S8":
        value.update(
            mesh_summary_version="semantic-mesh-summary-v2",
            mesh_summary={
                "terrain": {
                    "status": "ready", "vertices": 8, "faces": 12,
                    "watertight": True, "winding_consistent": True,
                    "bounds_mm": [
                        [0.0, 0.0, 0.0], [1.0, 1.0, 1.0]],
                },
                "roads": {
                    "status": "ready", "vertices": 6, "faces": 8,
                    "watertight": True, "winding_consistent": True,
                    "bounds_mm": [
                        [0.0, 0.0, 0.0], [1.0, 1.0, 0.2]],
                },
            },
            water_relief_status="not_applicable")
    elif stage_id == "S9":
        value.update(
            passed=True, errors=[], warnings=[],
            gate_version="semantic-mesh-gate-v1",
            required_roles=["terrain", "roads"],
            mesh_metrics={
                "terrain": {
                    "present": True, "vertices": 8, "faces": 12,
                    "watertight": True, "winding_consistent": True,
                    "bounds_mm": [
                        [0.0, 0.0, 0.0], [1.0, 1.0, 1.0]],
                },
                "roads": {
                    "present": True, "vertices": 6, "faces": 8,
                    "watertight": True, "winding_consistent": True,
                    "bounds_mm": [
                        [0.0, 0.0, 0.0], [1.0, 1.0, 0.2]],
                },
            },
            feature_survival=evaluate_feature_survival(
                source_counts, {"roads": 1}),
        )
    elif stage_id == "S10":
        value.update(
            status="generated_pending_validation",
            artifact_bundle=list(S10_REQUIRED_ARTIFACT_BUNDLE),
            s9_errors=0,
            s9_warnings=0,
        )
    elif stage_id == "S11":
        value["artifact_sha256"] = "d" * 64
    return value


def _prepare_full_worker_finish(tmp_path, job_id="fullfinish"):
    job = _job(job_id, "account-a", 1, status="running")
    job.update({
        "mode": "full",
        "worker_id": "mac-worker",
        "lease_expires": time.time() + 90,
    })
    server.JOBS[job["id"]] = job
    source = tmp_path / "source"
    source.mkdir()
    artifacts = {}
    for name in S10_REQUIRED_ARTIFACT_BUNDLE:
        filename = (
            "model.3mf" if name == "3mf" else
            (f"design_spec.{job['id']}."
             f"{job['pipeline_attempt_id']}.json")
            if name == "design_spec" else
            f"{name}.html" if name.endswith("_html") else
            f"{name}.json"
        )
        path = source / filename
        path.write_bytes(f"bound:{name}".encode())
        artifacts[name] = path
    ledger_name = (
        f"pipeline_state.{job['id']}.{job['pipeline_attempt_id']}.json")
    ledger = PipelineLedger.create(
        source / ledger_name,
        output_dir=source,
        run_id=job["id"],
        attempt_id=job["pipeline_attempt_id"],
        mode="full",
    )
    for stage in stages_for_mode("full"):
        if stage.id == "S11":
            break
        ledger.start_stage(
            stage.id,
            context_in={"request": 1} if stage.id == "S0" else None,
        )
        ledger.complete_stage(
            stage.id,
            context_out=_valid_context(stage.id),
            artifacts=artifacts if stage.id == "S10" else None,
        )

    destination = server.OUTPUT_DIR / job["city"]
    destination.mkdir(parents=True)
    deliverables = list(artifacts.values()) + [ledger.path]
    manifest = []
    for path in deliverables:
        content = path.read_bytes()
        (destination / f"{path.name}.part").write_bytes(content)
        manifest.append({
            "name": path.name,
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
        })

    return job, artifacts, destination, manifest, ledger_name


def _replace_uploaded_ledger(destination, manifest, ledger_name, ledger):
    encoded = json.dumps(ledger, sort_keys=True).encode()
    (destination / f"{ledger_name}.part").write_bytes(encoded)
    entry = next(item for item in manifest if item["name"] == ledger_name)
    entry["sha256"] = hashlib.sha256(encoded).hexdigest()
    entry["size"] = len(encoded)


class _SuccessfulLocalProcess:
    @staticmethod
    def wait():
        return 0


def _make_local_job(job, tmp_path):
    log_path = tmp_path / f"{job['id']}.log"
    log_path.write_text("process exited zero\n", encoding="utf-8")
    job.update({
        "exec": "local",
        "proc": _SuccessfulLocalProcess(),
        "log_path": str(log_path),
        "status": "running",
    })
    return log_path


def _publish_prepared_worker_files(destination, manifest):
    for item in manifest:
        (destination / f"{item['name']}.part").replace(
            destination / item["name"])


def _prepare_partial_local_attempt(tmp_path, *, job_id, mode, stop_stage,
                                   role, filename):
    job = _job(job_id, "account-a", 1, status="running")
    job["mode"] = mode
    city_dir = server.OUTPUT_DIR / job["city"]
    city_dir.mkdir(parents=True)
    artifact = city_dir / filename
    artifact.write_bytes(f"partial:{role}".encode())
    ledger = PipelineLedger.create(
        city_dir / (
            f"pipeline_state.{job['id']}."
            f"{job['pipeline_attempt_id']}.json"),
        output_dir=city_dir,
        run_id=job["id"],
        attempt_id=job["pipeline_attempt_id"],
        mode=mode,
    )
    for stage in stages_for_mode(mode):
        ledger.start_stage(
            stage.id,
            context_in={"request": 1} if stage.id == "S0" else None,
        )
        if stage.id == stop_stage:
            ledger.record_artifact(
                stage.id, name=role, path=artifact)
            break
        ledger.complete_stage(
            stage.id,
            context_out=_valid_context(stage.id, mode=mode),
        )
    _make_local_job(job, tmp_path)
    server.JOBS[job["id"]] = job
    return job


def test_local_full_zero_exit_rejects_running_s9_even_with_bound_3mf(
        tmp_path):
    job = _prepare_partial_local_attempt(
        tmp_path, job_id="local-running-s9", mode="full",
        stop_stage="S9", role="3mf", filename="premature.3mf")

    assert server._scan_job_artifacts(job)["artifacts"]["models_3mf"]
    assert server._job_artifacts_available(job) is False
    server._watch_job(job)

    assert job["status"] == "failed"
    assert "completion evidence rejected" in Path(
        job["log_path"]).read_text(encoding="utf-8")


def test_local_full_zero_exit_rejects_incomplete_physical_s10_bundle(
        tmp_path):
    job, artifacts, destination, manifest, _ = _prepare_full_worker_finish(
        tmp_path, "local-missing-sidecar")
    _publish_prepared_worker_files(destination, manifest)
    (destination / artifacts["pipeline_observation_html"].name).unlink()
    _make_local_job(job, tmp_path)

    assert server._job_artifacts_available(job) is False
    server._watch_job(job)

    assert job["status"] == "failed"
    assert "pipeline_observation_html" in Path(
        job["log_path"]).read_text(encoding="utf-8")


def test_local_draft_zero_exit_rejects_glb_claimed_before_terminal_s7(
        tmp_path):
    job = _prepare_partial_local_attempt(
        tmp_path, job_id="local-early-draft", mode="draft",
        stop_stage="S6", role="draft_glb", filename="premature.glb")

    assert server._scan_job_artifacts(job)["artifacts"]["draft_glb"]
    assert server._job_artifacts_available(job) is False
    server._watch_job(job)

    assert job["status"] == "failed"
    assert "completion evidence rejected" in Path(
        job["log_path"]).read_text(encoding="utf-8")


def test_local_full_terminal_s10_bundle_is_reusable_and_completes(tmp_path):
    job, _artifacts, destination, manifest, _ = (
        _prepare_full_worker_finish(tmp_path, "local-complete-full")
    )
    _publish_prepared_worker_files(destination, manifest)
    _make_local_job(job, tmp_path)

    assert server._job_artifacts_available(job) is True
    server._watch_job(job)

    assert job["status"] == "done"


def test_local_draft_terminal_s7_glb_is_reusable_and_completes(tmp_path):
    job = _job("local-complete-draft", "account-a", 1,
               status="running")
    city_dir = server.OUTPUT_DIR / job["city"]
    city_dir.mkdir(parents=True)
    glb = city_dir / "draft.preview.glb"
    glb.write_bytes(b"terminal draft glb")
    ledger = PipelineLedger.create(
        city_dir / (
            f"pipeline_state.{job['id']}."
            f"{job['pipeline_attempt_id']}.json"),
        output_dir=city_dir,
        run_id=job["id"],
        attempt_id=job["pipeline_attempt_id"],
        mode="draft",
    )
    for stage in stages_for_mode("draft"):
        ledger.start_stage(
            stage.id,
            context_in={"request": 1} if stage.id == "S0" else None,
        )
        ledger.complete_stage(
            stage.id,
            context_out=_valid_context(stage.id, mode="draft"),
            artifacts={"draft_glb": glb} if stage.id == "S7" else None,
        )
    _make_local_job(job, tmp_path)
    server.JOBS[job["id"]] = job

    assert server._job_artifacts_available(job) is True
    server._watch_job(job)

    assert job["status"] == "done"


def test_full_worker_finish_requires_and_accepts_hash_bound_s10_bundle(
        tmp_path):
    job, artifacts, destination, manifest, _ = _prepare_full_worker_finish(
        tmp_path)
    result = server.worker_finish(server.WorkerFinish(
        job_id=job["id"], attempt_id=job["pipeline_attempt_id"],
        token="queue-secret",
        worker_id="mac-worker", ok=True, files=manifest,
    ))

    assert result["status"] == "done"
    assert server.JOBS[job["id"]]["status"] == "done"
    assert all((destination / item["name"]).is_file() for item in manifest)
    assert (destination / "design_spec.json").read_bytes() == artifacts[
        "design_spec"].read_bytes()
    assert not list(destination.glob("*.part"))

    # A later same-city file or mutable convenience alias must never enter
    # this historical job's artifact response.
    (destination / "later-attempt.3mf").write_bytes(b"other attempt")
    (destination / "design_spec.json").write_bytes(b"mutable alias")
    bound = server._scan_job_artifacts(server.JOBS[job["id"]])
    assert bound["job_id"] == job["id"]
    assert bound["attempt_id"] == job["pipeline_attempt_id"]
    assert bound["integrity"] == "ledger_hash_verified"
    assert [item["name"] for item in bound["artifacts"]["models_3mf"]] == [
        artifacts["3mf"].name]
    assert bound["artifacts"]["design_spec"]["name"] == artifacts[
        "design_spec"].name
    assert "later-attempt.3mf" not in json.dumps(bound)
    assert bound["artifacts"]["design_spec"]["url"] != (
        f"/files/{job['city']}/design_spec.json")


def test_worker_finish_rechecks_attempt_before_publishing(
        tmp_path, monkeypatch):
    job, _artifacts, destination, manifest, _ = (
        _prepare_full_worker_finish(tmp_path, "finish-attempt-cas")
    )
    original = server._validate_uploaded_full_ledger

    def switch_attempt_during_validation(current_job, files, checks):
        result = original(current_job, files, checks)
        # Model a lease-reclaim/retry racing with a large artifact hash pass.
        replacement = dict(server.JOBS[current_job["id"]])
        replacement["pipeline_attempt_id"] = "replacement-attempt"
        replacement["worker_id"] = "replacement-worker"
        replacement["lease_expires"] = time.time() + 90
        server.JOBS[current_job["id"]] = replacement
        return result

    monkeypatch.setattr(
        server, "_validate_uploaded_full_ledger",
        switch_attempt_during_validation,
    )

    with pytest.raises(server.HTTPException, match="切换 attempt") as rejected:
        server.worker_finish(server.WorkerFinish(
            job_id=job["id"], attempt_id=job["pipeline_attempt_id"],
            token="queue-secret",
            worker_id="mac-worker", ok=True, files=manifest,
        ))

    assert rejected.value.status_code == 409
    assert server.JOBS[job["id"]]["status"] == "running"
    assert server.JOBS[job["id"]]["pipeline_attempt_id"] == (
        "replacement-attempt")
    assert not any((destination / item["name"]).exists()
                   for item in manifest)
    assert all((destination / f"{item['name']}.part").is_file()
               for item in manifest)


@pytest.mark.parametrize("tamper", ("s9_survival", "context_hash", "order"))
def test_full_worker_finish_rejects_semantically_tampered_ledger(
        tmp_path, tamper):
    job, _artifacts, destination, manifest, ledger_name = (
        _prepare_full_worker_finish(tmp_path, f"tamper-{tamper}")
    )
    ledger_path = destination / f"{ledger_name}.part"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    if tamper == "s9_survival":
        value = ledger["stages"][9]["context_out"]["value"]
        value["feature_survival"] = {
            "passed": False, "errors": ["roads disappeared"],
        }
        # Re-hash the tampered Context so this case proves semantic validation,
        # rather than merely exercising hash-integrity rejection.
        canonical = json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        ).encode()
        ledger["stages"][9]["context_out"]["sha256"] = hashlib.sha256(
            canonical).hexdigest()
        ledger["stages"][10]["context_in"] = dict(
            ledger["stages"][9]["context_out"])
    elif tamper == "context_hash":
        ledger["stages"][5]["context_out"]["sha256"] = "0" * 64
        ledger["stages"][6]["context_in"]["sha256"] = "0" * 64
    else:
        ledger["stages"][5]["order"] = 99
    _replace_uploaded_ledger(destination, manifest, ledger_name, ledger)

    with pytest.raises(server.HTTPException) as rejected:
        server.worker_finish(server.WorkerFinish(
            job_id=job["id"], attempt_id=job["pipeline_attempt_id"],
            token="queue-secret",
            worker_id="mac-worker", ok=True, files=manifest,
        ))

    assert rejected.value.status_code == 400
    assert server.JOBS[job["id"]]["status"] == "running"
    assert not (destination / ledger_name).exists()


def test_expired_worker_cannot_finish_even_before_reclaim():
    job = _job("latefinish", "account-a", 1, status="running")
    job.update({
        "worker_id": "mac-worker",
        "lease_expires": time.time() - 1,
    })
    server.JOBS[job["id"]] = job

    with pytest.raises(server.HTTPException) as rejected:
        server.worker_finish(server.WorkerFinish(
            job_id=job["id"], attempt_id=job["pipeline_attempt_id"],
            token="queue-secret",
            worker_id="mac-worker", ok=True,
            files=[{"name": "preview.glb", "sha256": "0" * 64,
                    "size": 1}],
        ))

    assert rejected.value.status_code == 409
    assert server.JOBS[job["id"]]["status"] == "running"


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
        job_id="failed1", attempt_id="attempt-1",
        token="queue-secret", worker_id="mac-worker",
        ok=False, error="renderer failed", files=[],
    ))

    assert result["status"] == "failed"
    assert store.get_user(user.id).quota_used == 0
    assert server.JOBS["failed1"]["quota_refunded"] is True
