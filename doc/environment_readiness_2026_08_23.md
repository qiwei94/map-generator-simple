# Runtime readiness audit — 2026-08-23

This is the readback after preparing the project runtimes.  It records real
capability evidence, not permission to deploy code or restart services.

## Controller Mac

- macOS 15.7.1, Apple Silicon, 16 GiB RAM, about 324 GiB free after setup.
- Project venv: Python 3.9.6; `pip check` clean.
- Native osmium 1.19.1 and portable pyosmium 4.3.1 are both available.
- Overture Maps CLI 1.0.1 is installed as an isolated Homebrew CLI.  The
  Python 3.9 project venv uses PyArrow 20.0.0, the final Arrow release line
  supporting Python 3.9.
- aria2 1.37.0 is available for PBF/DEM downloads.
- `ogr2ogr` is intentionally not installed: the active pipeline has an
  equivalent Shapely clipping fallback.
- `tools/env_doctor.py`: 20 OK, 0 warnings, 0 failures, 4 informational items.
- Real Overture smoke: a Chicago 100 m box downloaded 9 building rows and all
  9 geometries were non-empty when read by the project GeoPandas runtime.
- Real native-osmium smoke from `illinois-latest.osm.pbf`: 3,489 exported
  features, including 1,881 road lines.

## Windows WSL2 primary compute

- Ubuntu 24.04 WSL2; Python 3.12.3 project venv; `pip check` clean.
- Native osmium 1.16.0, Overture Maps CLI 1.0.1, PyArrow 25.0.1 and aria2
  1.37.0 are installed and read back.
- Overture dependencies were downloaded on the controller and installed from
  a bounded wheel cache after direct PyPI transfer stalled.
- Real Overture smoke used the same Chicago box: 9 rows, 9 non-empty
  geometries.
- Data at audit time: 13 local PBF files and 245 HGT files; no active gallery
  or city-generation process.

## Intel Mac secondary compute

- macOS 13.7.8, Intel, 16 GiB RAM; Python 3.9.6 project venv.
- Portable pyosmium backend is functional; native osmium remains absent.
- PyArrow 20.0.0 was installed from a controller-staged x86_64 wheel and
  `pip check` is clean.
- Data at audit time: 8 local PBF files and 10 DEM files; no active generator.
- This node is suitable for tests, cached/preprocessed work and bounded
  secondary renders.  Native-extraction performance must not be claimed for it.

## Cloud roles

- `cloud-data`: 80 PBF files and 1,491 DEM files; Studio and worker inactive.
  It remains a data/catalog node and deliberately does not receive a heavy
  rendering or Overture toolchain.
- `cloud-api`: Studio and worker active, with no worker child task at audit
  time.  The worker process is explicitly configured with native osmium
  1.19.1.  It has 80 PBF files and 1,492 DEM files.  Overture is not installed
  because the node has limited system-disk headroom and is not the primary
  renderer.
- No service was restarted during this audit.

## Remaining external capability

`AMAP_KEY` was unset in the controller shell and in the live cloud Studio and
worker processes.  High-grade water supplementation therefore remains an
optional capability that is not currently enabled.  The pipeline must report
that absence explicitly and continue with real OSM/coastline data; it must not
claim Gaode corroboration.

## Acceptance commands

```bash
.venv/bin/python tools/env_doctor.py
.venv/bin/python -m pytest tests -m "not slow"
```

The second command passed with 408 tests, 2 skips and 11 slow-test
deselections.  Running unscoped `pytest` still collects ad-hoc scripts under
`tools/`; the supported test target is explicitly `tests/`.
