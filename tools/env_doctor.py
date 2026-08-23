#!/usr/bin/env python3
"""Read-only runtime readiness check for map-generator-simple workers."""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Dict, List, Optional


CORE_MODULES = {
    "numpy": "numpy",
    "trimesh": "trimesh",
    "shapely": "shapely",
    "geopandas": "geopandas",
    "pandas": "pandas",
    "pyproj": "pyproj",
    "scipy": "scipy",
    "mapbox_earcut": "mapbox-earcut",
    "rasterio": "rasterio",
    "osmium": "osmium",
    "manifold3d": "manifold3d",
    "pyarrow": "pyarrow",
    "PIL": "Pillow",
}

_STANDARD_EXECUTABLE_DIRS = (
    Path("/usr/local/bin"),
    Path("/opt/homebrew/bin"),
    Path("/usr/bin"),
)


def _version(distribution: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _executable_version(command: List[str]) -> Optional[str]:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    lines = (result.stdout or result.stderr).strip().splitlines()
    return lines[0] if lines else "available"


def _find_executable(name: str) -> Optional[str]:
    """Resolve tools installed outside a non-interactive worker's PATH."""

    sibling = Path(sys.executable).with_name(
        f"{name}.exe" if os.name == "nt" else name)
    candidates = [shutil.which(name), str(sibling)]
    candidates.extend(str(directory / name)
                      for directory in _STANDARD_EXECUTABLE_DIRS)
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(Path(candidate).resolve())
    return None


def _overture_cli() -> Optional[str]:
    override = os.environ.get("OVERTUREMAPS_BIN", "").strip()
    sibling = Path(sys.executable).with_name(
        "overturemaps.exe" if os.name == "nt" else "overturemaps"
    )
    for candidate in (override, str(sibling), shutil.which("overturemaps")):
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(Path(candidate).resolve())
    return None


def _fast_simplification_extension() -> Optional[str]:
    """Return the installed native extension without importing its wrapper.

    The project's Python 3.9 floor cannot import releases whose convenience
    wrapper uses ``X | None`` annotations, while the compiled ``_simplify``
    extension is still usable through the runtime's direct loader.  Checking
    only ``import fast_simplification`` would therefore report a false failure
    on the controller Mac.
    """

    try:
        distribution = importlib.metadata.distribution("fast-simplification")
    except importlib.metadata.PackageNotFoundError:
        return None
    for entry in distribution.files or ():
        name = str(entry).replace("\\", "/")
        if (name.startswith("fast_simplification/_simplify")
                and name.lower().endswith((".so", ".pyd", ".dylib"))):
            path = Path(distribution.locate_file(entry))
            if path.is_file():
                return str(path)
    return None


def _count_files(roots: List[Path], suffixes: tuple[str, ...]) -> int:
    seen = set()
    count = 0
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_dir():
            continue
        seen.add(resolved)
        for _dirpath, _dirnames, filenames in os.walk(resolved):
            count += sum(name.lower().endswith(suffixes) for name in filenames)
    return count


def inspect_environment(project_root: Path) -> Dict[str, object]:
    checks: List[Dict[str, str]] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    py_ok = sys.version_info >= (3, 9)
    add("python", "ok" if py_ok else "fail", sys.version.split()[0])

    for module, distribution in CORE_MODULES.items():
        try:
            importlib.import_module(module)
            add(f"python:{distribution}", "ok", _version(distribution))
        except Exception as exc:  # report binary loader failures as well as imports
            add(f"python:{distribution}", "fail", f"{type(exc).__name__}: {exc}")

    fast_simplification = _fast_simplification_extension()
    add(
        "python:fast-simplification",
        "ok" if fast_simplification else "fail",
        (_version("fast-simplification") if fast_simplification
         else "missing native extension; large terrain remains full-resolution"),
    )

    native_osmium = _find_executable("osmium")
    native_version = (
        _executable_version([native_osmium, "--version"])
        if native_osmium else None
    )
    portable = project_root / "tools" / "osmium_pyosmium.py"
    portable_ok = portable.is_file()
    if native_version:
        add("osmium:native", "ok", native_version)
    elif portable_ok:
        add("osmium:native", "warn", "missing; portable pyosmium fallback available")
    else:
        add("osmium:native", "fail", "native CLI and portable fallback are missing")

    overture = _overture_cli()
    overture_version = (
        _executable_version([overture, "--version"]) if overture else None
    )
    add(
        "overturemaps",
        "ok" if overture_version else "warn",
        overture_version or "missing; optional building-height enrichment disabled",
    )

    aria = _find_executable("aria2c")
    add(
        "aria2c",
        "ok" if aria else "warn",
        _executable_version([aria, "--version"]) if aria else "missing; downloads use slower fallback",
    )

    ogr = _find_executable("ogr2ogr")
    add(
        "ogr2ogr",
        "ok" if ogr else "info",
        _executable_version([ogr, "--version"]) if ogr else "missing; equivalent Shapely clipping is enabled",
    )

    cache_root = Path(os.environ.get("MAP_GEN_CACHE_DIR", project_root / "cache"))
    pbf_count = _count_files(
        [project_root / "pbf_cache", cache_root / "pbf_cache"],
        (".osm.pbf",),
    )
    dem_count = _count_files(
        [project_root / "dem_cache", project_root / "cache" / "srtm", cache_root],
        (".hgt", ".tif", ".tiff"),
    )
    add("data:pbf", "ok" if pbf_count else "warn", f"files={pbf_count}")
    add("data:dem", "ok" if dem_count else "warn", f"files={dem_count}")

    free_gib = shutil.disk_usage(project_root).free / (1024 ** 3)
    add("disk", "ok" if free_gib >= 10 else "fail", f"free_gib={free_gib:.1f}")

    for variable in ("AMAP_KEY", "ANTHROPIC_API_KEY", "VLM_API_KEY"):
        add(
            f"env:{variable}",
            "ok" if os.environ.get(variable) else "info",
            "set" if os.environ.get(variable) else "unset (optional)",
        )

    counts = {
        status: sum(check["status"] == status for check in checks)
        for status in ("ok", "warn", "fail", "info")
    }
    return {
        "project_root": str(project_root.resolve()),
        "python_executable": sys.executable,
        "checks": checks,
        "summary": counts,
        "ready": counts["fail"] == 0,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = inspect_environment(args.project_root)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for check in report["checks"]:
            print(f"[{check['status'].upper():4s}] {check['name']}: {check['detail']}")
        summary = report["summary"]
        print(
            "summary: "
            f"ok={summary['ok']} warn={summary['warn']} "
            f"fail={summary['fail']} info={summary['info']}"
        )
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
