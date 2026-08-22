# Five-node resource plan

Updated: 2026-08-20

Implementation status was re-verified on 2026-08-22.  The detailed commands,
data inventory and acceptance evidence are in
`doc/session_2026_08_22_cluster_bootstrap.md`.

This document records the intended role of the available machines.  The goal
is not to make every host interchangeable: map generation is limited by a
mixture of memory, local-disk latency, single-core geometry work, data
availability, and network transfer.

## Current inventory

| Node | Measured / reported resources | Intended role |
| --- | --- | --- |
| Local MacBook Pro | Apple M1 Pro, 10 cores, 16 GB RAM, 460 GiB SSD, about 378 GiB free | Primary development, visual QA, and latency-sensitive render worker |
| Intel Mac | 6 cores, 16 GB RAM, 256 GB SSD | Secondary render worker, PNG batches, tests, and validation |
| Windows desktop | 6 cores, 32 GB RAM, 1 TB SSD, 4 TB HDD | High-memory render worker; hot cache on SSD; global cold PBF/DEM and artifact archive on HDD |
| `118.31.184.240` | 2 vCPU, 15 GiB RAM, 118 GiB root, about 15 GiB free | Public web/API, accounts, durable queue, artifact delivery, and emergency fallback worker |
| `8.136.0.235` | 2 vCPU, 1.8 GiB RAM; 316 GiB virtual disk but only a 135.8 GiB root partition | Data downloader/catalog, hot-cloud cache mirror, backup, and worker control plane; no heavy geometry |

The secondary cloud host currently has no mounted data filesystem.  `/data`
is an empty directory on the root filesystem.  The virtual disk was expanded
to 316 GiB, while the GPT and ext4 root partition still end at about 136 GiB.
The filesystem reports clean and the remaining space is contiguous, but a
provider snapshot should be taken before growing partition 3 and ext4 online.

## Work classes and placement

1. `preview_png`: use the Intel Mac first, then M1 Mac; allow two concurrent
   jobs only after memory telemetry proves it is safe.
2. `preview_3d_5km`: use M1 Mac first; Windows is the fallback.
3. `final_15km`: use M1 Mac or Windows, one geometry job per node initially.
4. `final_25km` and dense cities: prefer Windows because its 32 GB RAM provides
   the largest safety margin; M1 Mac is the second choice.
5. `download/index`: use `8.136.0.235` for cloud-visible data and Windows for
   the global archive.
6. `web/queue/auth`: keep on `118.31.184.240`; do not let a large render make
   the public API unavailable.

Measured New York evidence on 2026-08-20 supports this split.  On a cold local
frame the M1 Mac extracted the building layer in 8.6 seconds and prepared all
source GeoDataFrames in 123.4 seconds.  For the same cached full frame, the
2-vCPU cloud host took 46.6--47.6 seconds for one building-landmark preprocess
step that took 19.7 seconds on the Mac.  Cloud timings are kept as cloud
timings and are not used as a proxy for either Mac.

## Data layout

- Windows HDD: authoritative cold archive of global regional PBFs, DEM tiles,
  old outputs, and checksums.
- Windows SSD: currently requested PBFs, DEM tiles, and pipeline caches.
- Mac SSDs: bounded least-recently-used hot caches.  A worker downloads a
  missing immutable input before starting and keeps it for later users.
- Cloud secondary: a curated hot mirror and manifest, not the only copy of
  global data.
- Cloud primary: final artifacts and only the PBF/DEM set needed for fallback
  rendering.

Do not run the geometry pipeline directly against NFS or SMB over the public
network.  Stage immutable inputs on the worker's local SSD, compute locally,
then upload checksummed artifacts.  Cache keys must include PBF version,
geometry schema, bbox grid, and render parameters; caches remain shared across
users while tasks and quota remain user-owned.

## Scheduler changes required before all workers stay online

The current worker endpoint can lease a task without knowing whether that
worker has the required PBF.  Before enabling all machines continuously, each
worker must heartbeat a capability manifest containing:

- OS/architecture, logical cores, available RAM, and free SSD space;
- supported job classes and maximum concurrent jobs;
- installed PBF regions and DEM tile coverage, including checksums/versions;
- native osmium availability and renderer version;
- current load, cache-hit estimate, and last successful heartbeat.

The server should lease only compatible jobs and use a transactional SQLite
lease table (WAL mode is sufficient for this five-node fleet).  Jobs should be
described by an allow-listed schema instead of allowing the server to send an
arbitrary shell command to a personal computer.  Keep the existing lease,
heartbeat, retry, checksum, account fairness, and quota semantics.

Initial concurrency limits should be conservative:

- M1 Mac: one final job, or up to two light PNG jobs;
- Windows: up to two final jobs if measured memory remains safe;
- Intel Mac: one final job, or up to two light PNG jobs;
- primary cloud: API plus at most one fallback job when the public queue is
  otherwise idle;
- secondary cloud: no geometry jobs.

These limits should later be adjusted from peak RSS, wall time, cache hit rate,
and failure telemetry rather than CPU-count heuristics alone.

## Rollout order

1. Snapshot `8.136.0.235`, grow GPT partition 3 and ext4, then verify reboot,
   free space, and kernel logs.
2. Add worker capability reporting and compatible-job matching.
3. Put the Windows worker under WSL2 on the SSD; keep the 4 TB archive outside
   the active pipeline cache and stage files into WSL storage before work.
4. Install launch-managed workers on both Macs with per-node tokens and fixed
   concurrency limits.
5. Add a data manifest and checksum-based sync command; prewarm the most common
   15/25 km cities and their DEM tiles.
6. Move job leases from JSON to SQLite before enabling more than one permanent
   worker.
7. Add a fleet page showing queue depth, assigned node, progress stage, peak
   memory, cache hits, artifacts, and retry reason.

## Verified rollout status — 2026-08-22

- Windows WSL2 was bootstrapped from the verified `30d6733` repository, with
  the follow-up fixes recorded in the commit containing this document.  It has
  a Python 3.12 virtual
  environment, native osmium 1.16, a 24 GB memory / 8 GB swap allocation, and
  access to the existing F-drive DEM archive.  Non-slow tests and both draft
  and formal model generation pass.  It is ready for bounded manual compute,
  but is not registered as a permanent queue worker.
- The Intel Mac now has the same repository, a Python 3.9 virtual environment,
  the portable pyosmium backend, Zhejiang PBF and the West Lake DEM tile.
  Non-slow tests and a real portable road extraction pass.  Native osmium is
  still absent, so it remains a secondary worker.
- `cloud-api` remains the only active API/worker host.  No service restart or
  deployment was performed during the bootstrap.  Its root filesystem has
  only about 14 GiB free and must not become the global artifact archive.
- `cloud-data` remains storage-only.  Its 1.8 GiB memory is below the geometry
  pipeline requirement even though it holds the 80-PBF and DEM mirrors.
- Permanent multi-worker polling remains blocked on capability/PBF matching
  and transactional database leases.  Connectivity and a passing local render
  are not sufficient reasons to enable an unconstrained worker.
