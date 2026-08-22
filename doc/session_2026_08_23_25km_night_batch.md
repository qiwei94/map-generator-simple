# 2026-08-23 distributed 25 km showcase batch

This record captures the night batch state at 00:48 CST.  Generation and PBF
staging were still running when the record was written; a directory is not a
successful sample until `generate_showcase_samples.py` validates all four
top-down renders, physical area and non-zero feature evidence.

## Code and safety boundary

- Branch: `agent/distributed-25km-showcase`
- Starting baseline: `agent/coastal-water-mask-fix` at `3609bb8`
- `cloud-data` is used only as the existing PBF source; it receives no geometry
  work.
- `cloud-api` keeps the public Studio and worker roles; this batch does not use
  its compute or restart either service.
- Controller, Intel Mac and Windows WSL each run at concurrency one.
- Existing untracked Studio SQLite runtime files on the controller are
  preserved.

## 24-city partition

| Node | Initial batch | Automatic continuation |
| --- | --- | --- |
| Controller M1 | Chicago, New York | Paris, London, Rome |
| Windows WSL | Hangzhou | Tokyo, Milan, Singapore, Seoul, Sydney, Melbourne, Mexico City, Buenos Aires, Barcelona, Cape Town, Suzhou |
| Intel Mac | Shanghai | Beijing, Hong Kong, Berlin, Madrid, Guangzhou, Cairo |

All runtime outputs use isolated slugs such as
`showcase_chicago_25km`; the checked-in 15 km production gallery slugs are not
overwritten.  Suzhou was corrected from the Zhejiang extract to
`jiangsu-latest.osm.pbf` before this run.

## Data staging and gates

The cloud-data node has the 80-file PBF mirror.  The batch-specific exact byte
manifest is `data/showcase_pbf_manifest_20260822.json`.

- Controller receives Ile-de-France, Great Britain and Centro PBFs.
- Intel Mac receives Beijing, Hong Kong, Berlin, Madrid, Guangdong and Egypt;
  Shanghai was transferred first and matched cloud-data SHA-256.
- Windows receives eleven PBFs in `C:\Users\kiwi\pbf_stage`.  The WSL promoter
  waits for exact source sizes, copies each file through a `.incoming` name to
  WSL ext4, verifies the copied byte count and atomically promotes it.  Staging
  sources are retained.

Continuation batches wait up to 12 hours for every assigned PBF to match the
manifest and then wait for the active per-node advisory lock.  A partial PBF
cannot be consumed merely because its filename exists.

## Windows WSL lifetime

Detached Linux processes are reaped after the Windows OpenSSH command returns.
The one-time Task Scheduler entry `MapShowcase25Nightly-20260823` therefore
owns a PowerShell parent that waits for three foreground `wsl.exe` children:

1. Hangzhou generation;
2. host-staging to WSL-ext4 promotion;
3. the eleven-city continuation.

The task was read back as `Running` after SSH returned.  Its execution limit is
18 hours.  Host logs are under `C:\Users\kiwi\map_cluster_logs`; WSL batch
state is `/home/mapworker/map-generator-simple/tmp/showcase_batch_status.json`.

## Evidence at handoff time

- Controller Chicago: native ARM64 osmium 1.19.1; 1,702,652 raw building
  features, 503,705 road features and 2,963 water features entered the tile
  cache.  `baseline_topdown.png` and `baseline_height.png` exist; remaining
  styles are still rendering.
- Intel Shanghai: portable osmium fallback; 82,468 building features, 84,640
  road features and 2,812 water features entered the tile cache.
  `baseline_topdown.png` and `baseline_height.png` exist; remaining styles are
  still rendering.
- Windows Hangzhou: the 25 km bbox is active under the scheduled task and the
  continuation is waiting behind the input/lock gates.

The Overture CLI is unavailable on the two Mac runs.  Its optional download
attempt reports an error and the pipeline continues with real local OSM PBF
data; the non-zero evidence above proves this is not an empty-success path.

## Completion checks

For each city, the batch validator requires:

- measured profile area within 12 percent of 625 km²;
- non-zero combined building, road or water evidence;
- all four top-down PNGs at least 1600×1600;
- visually non-blank images;
- at least two non-identical style images;
- final `gallery_metadata.json` and `contact_sheet.png`.

Primary status files:

- controller: `tmp/showcase_batch_status.json`
- Intel Mac: `/Users/zhangqiwei/map-generator-simple/tmp/showcase_batch_status.json`
- Windows WSL: `/home/mapworker/map-generator-simple/tmp/showcase_batch_status.json`

The initial and continuation logs use the corresponding
`tmp/showcase_*_25km*.log` names, except that Windows host-owned foreground logs
are under `C:\Users\kiwi\map_cluster_logs`.
