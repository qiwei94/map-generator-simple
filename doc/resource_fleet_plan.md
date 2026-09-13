# Five-node resource plan

Updated: 2026-09-05

## 实验与测试默认分工（用户要求，2026-09-05）

后续实验应充分利用全部设备的合适能力，不再默认把整城生成、A/B 与回归测试串行
堆在 controller 上。目标是缩短整轮实验时间，不是让每台机器无条件跑满。

| 节点 | 默认承担的独立工作 | 约束 |
|---|---|---|
| Windows / Ubuntu-24.04 WSL2 | 大范围城市、建筑聚合、正式 3MF；独立城市或 A/B 分支 | 优先使用本地 SSD 热缓存；并发数以 WSL 实际分配和实测峰值内存决定，不能按宿主 32 GB 直接估计 |
| controller M1 Pro | 开发、快速局部实验、视觉检查；有余量时承担另一份整城候选 | 不因本地代码最新就长期独占全部生成；代码分发需要单独核对授权 |
| Intel Mac | 回归测试、独立局部候选、PNG、导出后验证 | 先核对依赖与代码版本；有数据且资源足够时也可跑整城 |
| cloud-api | 线上服务和队列优先；空闲时做有界低优先级兼容性/产物检查 | 检查在线任务、内存和磁盘；不与用户生成抢资源，不为实验重启服务 |
| cloud-data | 数据清单、已有数据校验、缓存覆盖检查 | 不承担重几何；下载、归档或传输须在授权范围内，保留海外流量约束 |

### 每轮实验的执行规则

1. 先只读探测相关节点，检查身份、项目路径、commit/工作区差异、依赖版本、PBF/DEM
   内容摘要、当前进程及内存/磁盘；SSH 在线不等于已具备本轮计算条件。
2. 将独立城市、A/B 变体、单元回归、实际文件验证拆成任务矩阵，明确节点、输入、
   预期产物和完成标准，再并行执行。一个实验的 S0→S11 依赖顺序不变，不能跨节点
   偷跑依赖尚未完成的 Stage。
3. 算法 A/B 使用同一代码身份、数据、bbox、比例、打印 profile；只改变声明的变量。
   跨节点、不同冷/热缓存耗时不能直接作为算法加速结论。
4. 同节点也允许并行，但根据实际峰值 RSS、可用内存、swap、磁盘 I/O 和渲染器内部
   线程数设置预算；不能把“单个几何函数单线程”误解为“整台设备只能跑一个任务”。
5. 各任务使用独立 run/attempt 与输出目录；不覆盖旧图、不共写同一派生缓存。共享
   原始数据保持只读，跨机器结果记录 node、代码/数据摘要、时间、峰值 RSS 和验证结论。
6. 当前先采用有边界的人工分配/独立命令，不因该并行原则开启未经验证的永久多 worker
   抢单。安装、部署、重启、传输和删除仍遵守 `map-cluster-ops` 的授权与读回验证规则。

### 本次只读探测

五台节点均在线。controller / Intel Mac 均为 16 GiB，Windows 宿主约 32 GiB；
controller 可用约 276 GiB，Intel Mac 约 115 GiB，Windows C 盘约 80 GiB、F 盘约
2.49 TiB。cloud-api 根盘约 12.4 GiB 可用，API/worker 服务 active；cloud-data
约 1.8 GiB 内存，服务 inactive，仍仅作数据节点。

这次仅确认在线与基础资源并更新分工；未核对所有节点的新代码/数据一致性，未分发
代码、未启动新实验、未部署或重启。下次明确实验方案后再完成任务级 readiness 检查。

## 历史规划与引导记录

下方资源数字和部署状态属于 2026-08-20/22 的历史记录；尤其 `/data` 挂载、磁盘分区、
节点代码和永久 worker 状态不能据此假定仍然相同，实际操作前必须重新核对。

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
| Intel Mac | 4 cores / 8 threads, 16 GB RAM, 256 GB SSD, about 123 GiB free | Secondary render worker, PNG batches, tests, and validation |
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
