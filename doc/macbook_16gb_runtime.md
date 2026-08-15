# 16 GB Mac runtime guide

This project can run on a 16 GB Apple Silicon Mac, but preview and final export
should be treated as separate workloads. Results from a Linux sandbox are not a
Mac performance benchmark.

## Recommended environment

Use one generation process at a time. A native Homebrew or conda installation
of `osmium` and GDAL is the fastest supported setup:

```bash
brew install osmium-tool gdal
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest
```

`fast-simplification` is declared in the Python dependencies and should remain
installed for final-resolution meshes.

## Portable OSM fallback

If the native `osmium` executable is absent, the fetcher automatically invokes
the repository helper with the active Python interpreter:

```text
<python> tools/osmium_pyosmium.py <subcommand> ...
```

The fallback requires the `osmium` Python package. Commands are passed to
`subprocess` as argument arrays; paths and tag expressions are never composed
into a shell command.

Confirm which backend is selected:

```bash
.venv/bin/python - <<'PY'
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osmium_cli_fetcher import OsmiumCLIFetcher
fetcher = OsmiumCLIFetcher()
print(fetcher.osmium_backend)
print(fetcher.osmium_command)
PY
```

`native` is preferred. `pyosmium` is the portable fallback. `unavailable`
means neither implementation can run.

## Acceptance checks

Do not treat a zero exit code as successful extraction. Validate the feature
count, especially for roads:

```bash
.venv/bin/python -m pytest tests/test_osmium_cli_fetcher.py -q
```

The portable integration test removes native `osmium` from `PATH`, extracts a
small PBF fixture, and requires at least one road in the resulting GeoJSON.

For a real model, start with a small bounding box. Record wall time, peak memory,
output size, triangle count, and per-layer feature counts. Keep other memory-heavy
applications closed during final export. A successful release check also requires
the project validator to report `0 errors / 0 warnings` for the generated 3MF.
