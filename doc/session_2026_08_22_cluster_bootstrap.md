# 2026-08-22 cluster bootstrap and acceptance

This document records the verified state after preparing the five-node map
generation fleet.  It is an execution record, not permission to restart,
deploy, delete data, or enable permanent workers later.

## Code baseline

- Branch: `agent/coastal-water-mask-fix`
- Bootstrap input commit used on controller, Windows WSL2 and Intel Mac:
  `30d6733`.  The cold-start dependency/runtime fixes are in the commit that
  contains this document and must be synced before a permanent worker launch.
- Controller has untracked Studio runtime database files.  They were preserved
  and were not included in either remote repository.
- Windows WSL2 and Intel Mac repositories were created from a verified Git
  bundle, then pointed at the GitHub origin.  Both working trees were clean at
  final readback.

## Verified node state

| Node | Ready state | Local data | Intended use |
| --- | --- | --- | --- |
| Controller M1 Pro | Ready; 10 logical CPUs, 16 GB RAM | Zhejiang, Illinois and New York PBFs; local tile/pipeline caches | Source of truth, development, visual QA, burst rendering |
| Windows WSL2 | Ready; Python 3.12, 24 GB RAM, 8 GB swap, native osmium 1.16 | Zhejiang PBF; 231 HGT tiles exposed from F drive; hot cache on WSL/C drive | Primary bounded heavy compute |
| Intel Mac | Ready with limitations; Python 3.9, portable osmium | Zhejiang PBF; `N30E120.hgt`; portable smoke artifacts | Tests, PNG work, secondary compute |
| `cloud-api` | Online; Studio and worker active | 80 PBFs (32 GB), DEM (43 GB), tile cache (7 GB) | Public API, queue, artifact entry, low-priority fallback only |
| `cloud-data` | Online, services inactive, 1.8 GB RAM | 80 PBFs (32 GB), DEM (43 GB) | Download/catalog/backup only; no geometry compute |

The two cloud PBF manifests were previously verified identical.  The 80
regional extracts provide broad major-city coverage, not complete global
coverage.

## Windows WSL2 acceptance

Environment:

- Project: `/home/mapworker/map-generator-simple`
- Hot cache: `/home/mapworker/map-cache/hot`
- Existing SRTM archive: `/mnt/f/map_gen_cache/attaraction/cache/srtm`
- F-drive archive contains 231 HGT files and 470 existing NPY grids, about
  5.58 GiB total; its OSM directory was empty at inspection time.
- The repository-bundled Zhejiang PBF is 85 MiB.

Dependency installation used the Tsinghua mirror for ordinary wheels and the
official PyPI index only for `osmium`.  This raised observed large-wheel
throughput from roughly 65--80 KiB/s to 50--65 MiB/s.

Non-slow test command:

```bash
MAP_GEN_CACHE_DIR=/home/mapworker/map-cache/hot \
  .venv/bin/python -m pytest tests/ -m "not slow" --disable-warnings -q
```

Result:

```text
342 passed, 2 skipped, 11 deselected
```

Real 5 km draft command used native osmium, real Zhejiang PBF and the F-drive
DEM cache.  It completed in 68.0 seconds and produced:

- 266 raw water features;
- 9,731 raw building features;
- 6,097 raw road features;
- 3,923 post-preprocess road lines;
- a 1.88 MiB GLB and a 4.3 MiB PNG;
- a passing layer-grounding postcheck.

Formal 2 km generation completed in 191.7 seconds and produced:

- `full_wsl_westlake_2km_3mf_0822_2332.3mf` (4.35 MiB);
- `design_spec.json` (3.4 KiB);
- 452,220 watertight terrain faces, 11,368 building faces, 2,056 road faces;
- project validator: 12/12 rules passed, zero errors, zero warnings.

The cold formal run spent 146.1 seconds in Gaode water supplementation.  That
is the dominant measured latency here; the Windows hardware itself must not be
judged from Linux cloud timings.

## Intel Mac acceptance

Environment:

- Project: `/Users/zhangqiwei/map-generator-simple`
- Cache: `/Users/zhangqiwei/map-cache/hot`
- Python 3.9 virtual environment with all declared runtime/test dependencies
- Portable backend resolves to the active virtual-environment Python plus
  `tools/osmium_pyosmium.py`; no native osmium binary is installed.

Non-slow test result:

```text
342 passed, 2 skipped, 11 deselected
```

Real portable extraction used the same 2 km West Lake box:

```text
area extract: 49,363 elements
highway filter: 1,121 elements
GeoJSON export: 246 road features
```

This proves non-zero real extraction without native osmium.  It is
substantially slower than the native CLI and should remain a fallback until a
native binary is installed.

`N30E120.hgt` was copied from the existing cloud-data mirror through the
controller and verified at both ends with SHA-256:

```text
1a585aeafd5c336309680c9c16fe9d7d202129fa35778061077be14d92ff22bb
```

Known platform limitation: Apple's bundled Python 3.9 uses LibreSSL 2.8.3.
The requirements now select `urllib3<2` on old macOS/Python 3.9 workers.  This
node should still prefer offline PBF/DEM operation instead of runtime HTTP.

## Production version readback

No production files or services were changed.  `cloud-api` had both
`studio.service` and `worker.service` active and no recently modified session
jobs at the final inspection.  Root storage was 89% used with about 14 GiB
free.

Four key deployed files exactly matched the current baseline by SHA-256:

- `webapp/server.py`
- `tools/cloud_worker.py`
- `_TEXTURE_STYLE_OF_DEEPSEEK/terrain3d/fetchers/osmium_cli_fetcher.py`
- `webapp/static/app.js`

Two deployed files differ:

- `generate_city_legacy.py` is 11 insertions / 32 deletions behind the local
  version and lacks the `--preview-fast` path.
- `webapp/static/style.css` contains a 50-insertion / 19-deletion production
  showcase treatment not present in the baseline.

Therefore the production directory is a mixed deployed tree, not a clean Git
checkout.  Future deployment must first capture these two differences and
produce an explicit release commit; copying the repository over it would lose
production behavior.

## Queue expansion gate

Do not enable Windows and both Macs as unconstrained permanent workers yet.
The next implementation slice must provide:

1. worker capability heartbeats: job classes, RAM, native/portable osmium,
   installed PBF manifest, DEM coverage and renderer commit;
2. compatible-job leasing instead of leasing the global queue head;
3. SQLite WAL-backed transactional leases before multiple permanent workers;
4. allow-listed job specifications rather than arbitrary remote commands;
5. per-node concurrency of one until peak RSS and cache telemetry justify more;
6. checksummed input staging and artifact upload, while keeping hot compute
   paths on local SSDs.

Until those gates are complete, use Windows and Intel Mac for bounded manual
jobs only.  Keep `cloud-data` out of the geometry pool and keep large formal
renders away from the public API host.
