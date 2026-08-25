"""SQLite-backed jobs, worker capabilities, leases, and progress events.

The web process keeps a small in-memory mirror for compatibility with the
existing API code, but SQLite is the source of truth for worker leases.  Each
lease is selected and written inside ``BEGIN IMMEDIATE`` so two workers cannot
claim the same job.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Iterable


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def worker_can_run(job: dict, capabilities: dict | None, *,
                   require_capabilities: bool = False) -> bool:
    """Return whether a worker is compatible with a queued job.

    Old installations can keep leasing without a capability record.  Once
    ``require_capabilities`` is enabled, unknown workers receive no jobs.
    """
    if not capabilities:
        return not require_capabilities
    requirements = job.get("requirements") or {}
    classes = set(capabilities.get("job_classes") or [])
    job_class = requirements.get("job_class") or job.get("mode")
    if classes and job_class not in classes:
        return False

    minimum_memory_mb = int(requirements.get("minimum_memory_mb") or 0)
    memory_mb = int(capabilities.get("memory_mb") or 0)
    if minimum_memory_mb and memory_mb < minimum_memory_mb:
        return False

    if requirements.get("native_osmium") and not capabilities.get(
            "native_osmium"):
        return False

    required_pbf = str(requirements.get("pbf_file") or "").strip()
    if required_pbf:
        pbfs = {Path(str(item)).name
                for item in (capabilities.get("pbf_files") or [])}
        if Path(required_pbf).name not in pbfs:
            return False

    required_dem = set(requirements.get("dem_tiles") or [])
    if required_dem:
        available_dem = set(capabilities.get("dem_tiles") or [])
        if not required_dem.issubset(available_dem):
            return False
    return True


class JobStore:
    """Durable queue store suitable for the project's five-node fleet."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    queued_at REAL NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_status_queue
                    ON jobs(status, queued_at);
                CREATE TABLE IF NOT EXISTS workers (
                    worker_id TEXT PRIMARY KEY,
                    capabilities_json TEXT NOT NULL,
                    last_seen REAL NOT NULL,
                    state TEXT NOT NULL DEFAULT 'online'
                );
                CREATE TABLE IF NOT EXISTS job_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_job_events_job
                    ON job_events(job_id, event_id);
            """)

    @staticmethod
    def _row_job(row: sqlite3.Row) -> dict:
        return json.loads(row["payload_json"])

    def load_jobs(self) -> dict[str, dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM jobs ORDER BY queued_at, job_id"
            ).fetchall()
        jobs = [json.loads(row["payload_json"]) for row in rows]
        return {job["id"]: job for job in jobs}

    def save_job(self, job: dict, *, conn: sqlite3.Connection | None = None
                 ) -> None:
        owns_connection = conn is None
        if conn is None:
            conn = self._connect()
        now = time.time()
        conn.execute(
            """INSERT INTO jobs(job_id, payload_json, status, queued_at,
                                 updated_at)
               VALUES(?, ?, ?, ?, ?)
               ON CONFLICT(job_id) DO UPDATE SET
                 payload_json=excluded.payload_json,
                 status=excluded.status,
                 queued_at=excluded.queued_at,
                 updated_at=excluded.updated_at""",
            (job["id"], _json(job), job.get("status", "pending"),
             float(job.get("queued_at", job.get("started", now))), now),
        )
        if owns_connection:
            conn.commit()
            conn.close()

    def save_jobs(self, jobs: Iterable[dict]) -> None:
        with self._connect() as conn:
            for job in jobs:
                self.save_job(job, conn=conn)

    def append_event(self, job_id: str, event_type: str, payload: dict,
                     *, conn: sqlite3.Connection | None = None) -> int:
        owns_connection = conn is None
        if conn is None:
            conn = self._connect()
        cursor = conn.execute(
            """INSERT INTO job_events(job_id, event_type, payload_json,
                                      created_at) VALUES(?, ?, ?, ?)""",
            (job_id, event_type, _json(payload), time.time()),
        )
        event_id = int(cursor.lastrowid)
        if owns_connection:
            conn.commit()
            conn.close()
        return event_id

    def list_events(self, job_id: str, after_id: int = 0,
                    limit: int = 100) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT event_id, event_type, payload_json, created_at
                   FROM job_events WHERE job_id=? AND event_id>?
                   ORDER BY event_id LIMIT ?""",
                (job_id, max(0, int(after_id)), max(1, min(limit, 500))),
            ).fetchall()
        return [{
            "id": int(row["event_id"]),
            "type": row["event_type"],
            "data": json.loads(row["payload_json"]),
            "created_at": float(row["created_at"]),
        } for row in rows]

    def record_worker(self, worker_id: str, capabilities: dict,
                      state: str = "online") -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO workers(worker_id, capabilities_json,
                                       last_seen, state)
                   VALUES(?, ?, ?, ?)
                   ON CONFLICT(worker_id) DO UPDATE SET
                     capabilities_json=excluded.capabilities_json,
                     last_seen=excluded.last_seen,
                     state=excluded.state""",
                (worker_id, _json(capabilities), time.time(), state),
            )

    def get_worker(self, worker_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT capabilities_json, last_seen, state FROM workers
                   WHERE worker_id=?""", (worker_id,)
            ).fetchone()
        if row is None:
            return None
        return {
            "worker_id": worker_id,
            "capabilities": json.loads(row["capabilities_json"]),
            "last_seen": float(row["last_seen"]),
            "state": row["state"],
        }

    def lease_next(self, worker_id: str, capabilities: dict | None,
                   lease_seconds: int, last_owner: str | None,
                   *, require_capabilities: bool = False
                   ) -> tuple[dict | None, str | None, list[dict]]:
        """Atomically reclaim expired jobs and lease one compatible job."""
        now = time.time()
        reclaimed: list[dict] = []
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """SELECT payload_json FROM jobs
                   WHERE status IN ('pending', 'running')
                   ORDER BY queued_at, job_id"""
            ).fetchall()
            pending = []
            for row in rows:
                job = json.loads(row["payload_json"])
                if (job.get("status") == "running" and
                        job.get("exec") == "worker" and
                        float(job.get("lease_expires") or 0) <= now):
                    job["status"] = "pending"
                    job["retry_count"] = int(job.get("retry_count") or 0) + 1
                    job["retry_reason"] = "worker_lease_expired"
                    job.pop("worker_id", None)
                    job.pop("lease_expires", None)
                    self.save_job(job, conn=conn)
                    self.append_event(job["id"], "requeued", {
                        "reason": "worker_lease_expired",
                        "retry_count": job["retry_count"],
                    }, conn=conn)
                    reclaimed.append(job)
                if job.get("status") == "pending" and worker_can_run(
                        job, capabilities,
                        require_capabilities=require_capabilities):
                    pending.append(job)

            if not pending:
                conn.commit()
                return None, last_owner, reclaimed

            def owner(job: dict) -> str:
                return str(job.get("quota_payer_id") or
                           ((job.get("owner_ids") or [""])[0]) or
                           "anonymous")

            alternatives = [job for job in pending
                            if owner(job) != last_owner]
            chosen = alternatives[0] if alternatives else pending[0]
            chosen["status"] = "running"
            chosen["claimed_at"] = now
            chosen["worker_id"] = worker_id
            chosen["lease_expires"] = now + lease_seconds
            chosen["last_heartbeat"] = now
            chosen["progress_pct"] = max(
                3, int(chosen.get("progress_pct") or 0))
            chosen["stage_label"] = "计算节点已接单，正在准备运行环境"
            self.save_job(chosen, conn=conn)
            self.append_event(chosen["id"], "leased", {
                "worker_id": worker_id,
                "lease_seconds": lease_seconds,
            }, conn=conn)
            conn.commit()
        return chosen, owner(chosen), reclaimed
