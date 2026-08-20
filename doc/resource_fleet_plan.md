# Five-node resource plan

Updated: 2026-08-20

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

