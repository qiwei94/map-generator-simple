# Remote worker channel and durable progress

## Production topology

The public API never opens a connection to a home or office machine.  Every
Mac/Windows worker initiates an outbound HTTPS connection to `cloud-api`:

1. `POST /api/worker/register` publishes a non-sensitive capability manifest.
2. `GET /api/worker/next` leases one compatible queued job.
3. `POST /api/worker/heartbeat` renews the lease and reports pipeline progress.
4. `POST /api/worker/upload` streams artifacts into `.part` files with SHA256.
5. `POST /api/worker/finish` verifies and atomically publishes the artifacts.

No worker port needs to be exposed.  Tailscale/WireGuard remains useful for
administration, but is not required by the queue protocol.  Worker requests use
`Authorization: Bearer ...`; query-string tokens are supported only during the
rolling migration and must not be used by new worker services.

Permanent workers read their credential from a mode-0600 token file through
`WORKER_TOKEN_FILE`/`--token-file`.  The API accepts node-bound SHA-256 digests
from `WORKER_TOKEN_HASH_FILE`; it does not need a copy of each node's plaintext
token.  The legacy shared `WORKER_TOKEN` remains valid during rolling migration.

When the public site has no trusted domain certificate, expose a worker-only
TLS listener and install only its public CA/server certificate on each worker.
Pass that path through `WORKER_CA_CERT` or `--ca-cert`.  Never use `verify=False`
and never transfer the TLS private key away from `cloud-api`.

## Durable state

`STUDIO_JOB_DB` defaults to `tmp/webapp_jobs/_jobs.sqlite3`.  SQLite runs in WAL
mode.  Worker selection is performed under `BEGIN IMMEDIATE`, so concurrent
workers cannot lease the same job.  The legacy `_jobs.json` file is kept as an
atomic rollback snapshot for one release and is migrated automatically when the
SQLite database is empty.

The database stores:

- complete job payload and status;
- worker capabilities and last registration time;
- durable queued/leased/progress/requeued/completed/failed events.

Set `WORKER_REQUIRE_CAPABILITIES=1` only after every permanent worker has been
updated and has registered successfully.  Matching currently checks job class,
minimum memory, and exact PBF filename.  DEM tile matching is supported by the
schema and can be enabled after all workers publish the same DEM manifest.

## Safe task schema

The server emits protocol version 1 jobs containing an allow-listed Python
entrypoint plus a string argument array.  Small parameter JSON files are sent as
inline files and materialized into a private temporary directory on the worker.
The worker rejects unknown entrypoints and never invokes a shell.  Do not add a
generic command or shell entrypoint to the allow-list.

## User-visible progress

Submission returns a job id immediately.  `/api/jobs/{id}` exposes:

- queue position and retry count;
- current pipeline stage and optional stage counter;
- monotonic overall percentage marked as an estimate;
- elapsed time and a broad remaining-time range;
- last worker heartbeat age and connection health;
- validation warnings and final artifacts.

`/api/jobs/{id}/events?after=<event_id>` provides reconnectable durable events.
The current browser uses short polling; a temporary network failure keeps the
job token and retries with bounded backoff instead of detaching from the task.

Overall percentage is derived from real pipeline log markers.  It is not an
exact CPU-work measurement.  Exact counters (for example fetched map tiles and
uploaded files) are shown separately when available.  ETA is always a range and
is recalibrated from elapsed time and completed stages.

## Initial node policy

- `windows-primary`: `full`, `draft`, and `styles`; one task; preferred for
  memory-heavy 25 km work when its PBF is present.
- `controller`: manual burst worker; one full task or two light PNG tasks.
- `intel-mac`: light preview/PNG work; one task.
- `cloud-api`: low-priority fallback worker and artifact publisher.
- `cloud-data`: data/catalog role only; never run geometry work there.

Keep PBF, DEM, and geometry caches on each worker's local SSD.  Never put the
pipeline hot path on WAN NFS/SMB.

## Verification

```bash
pytest -q tests/test_worker_protocol_v2.py tests/test_web_worker_queue.py
pytest -m "not slow"
```

Before restarting `studio` or a worker, confirm that no job is in `starting`,
`pending`, or `running`.  After a remote change, read back the service state,
worker registration, job lease, progress, artifact checksum, and final status.

## Rollout status — 2026-08-26

- `cloud-api` runs the SQLite/event-capable API.  Ninety-four historical jobs
  migrated from JSON and `journal_mode=wal` was verified.
- `local-primary` was restarted only after confirming zero active jobs and now
  registers its capability manifest (80 PBF files).
- A controller-only dry run completed the real API protocol end to end:
  submission, lease, progress event, two checksum uploads, and final status.
- Windows WSL is on `agent/durable-worker-progress-v16`; protocol tests pass and
  16 PBF files are visible.  Its permanent worker remains intentionally stopped
  until the worker-only TLS listener is explicitly approved and verified.
- The worker-only listener uses a dedicated nginx process and systemd unit
  ([`nginx-worker-tls-main.conf`](../deploy/nginx-worker-tls-main.conf),
  [`map-worker-tls.service`](../deploy/map-worker-tls.service)).  It must not
  include the distribution nginx configuration because the studio process owns
  port 80.  Only port 443 and `/api/worker/*` belong to this listener; every
  other path returns 404.
