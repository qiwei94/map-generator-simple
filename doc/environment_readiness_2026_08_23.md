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
- Native osmium 1.18.0 and aria2 1.37.0 were already installed under
  `/usr/local/bin`; non-interactive SSH omitted that directory from `PATH`.
  Commit `e68511c` now probes standard Homebrew paths, and the environment
  doctor reads both tools as available without shell activation.
- Portable pyosmium remains functional as the fallback.  The Shanghai 25 km
  run made before the PATH fix extracted 51,536 road lines through that
  backend, proving the fallback does not silently return zero.
- `fast-simplification` 0.1.12 was installed from the exact CPython 3.9
  x86_64 wheel.  The pre-install Shanghai run kept 1,811,896 terrain faces and
  spent 280.3 seconds in Manifold repair; later runs can decimate before repair.
- PyArrow 20.0.0 was installed from a controller-staged x86_64 wheel and
  `pip check` is clean.
- Latest readback: 19 OK, 2 warnings, 0 failures, 4 informational checks;
  the warnings are optional Overture enrichment and project-local DEM count.
- This node is suitable for tests and one bounded secondary render at a time.

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

`AMAP_KEY` remains optional.  The no-label tile supplement does not use that
API key, so a cache miss could previously issue hundreds of sequential live
HTTP requests.  `AMAP_WATER_AUTO_FETCH=0` now consumes existing Gaode cache
but skips live acquisition; cluster regressions use that mode and continue
with real OSM/coastline plus the adaptive river-width fallback.  Live tile
acquisition should be a separate prewarming operation with explicit evidence.

## Acceptance commands

```bash
.venv/bin/python tools/env_doctor.py
.venv/bin/python -m pytest -m "not slow"
```

The second command passed with 439 tests, 2 skips and 11 slow-test
deselections.  `pytest.ini` now restricts default discovery to `tests/`, so an
unscoped invocation no longer imports ad-hoc network diagnostics under
`tools/` or requires `AMAP_KEY` during collection.
