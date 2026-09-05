"""Fetch elevation data and build elevation grids.

Strategy (in order):
1. Local Copernicus DEM GLO-30 GeoTIFF in dem_cache/cop30/ (best quality, no network)
2. Local SRTM HGT in dem_cache/srtm/ (compatible with tools/manage_dem.py)
3. SRTM HGT downloaded on-demand into _CACHE_DIR (legacy fallback path)
4. Open Elevation API (batch queries when SRTM coverage is poor)
"""

import logging
import math
import os
import gzip
import hashlib
import importlib.util
import zipfile
import io
import time

import numpy as np
import requests
from scipy.ndimage import median_filter, gaussian_filter
from scipy.interpolate import griddata
from rich.progress import Progress, BarColumn, TextColumn, MofNCompleteColumn

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.config import CACHE_TTL_SECONDS, select_cache_path
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.utils import cache as cache_mgr

logger = logging.getLogger(__name__)


class ElevationDataError(RuntimeError):
    """Raised when a DEM grid exists syntactically but has no usable signal."""


def require_usable_elevation_grid(
    grid: np.ndarray,
    *,
    source: str,
    reject_all_zero: bool = True,
) -> np.ndarray:
    """Validate DEM identity before it may be cached or called ``ready``.

    Zero-valued cells are valid around coasts, and negative elevations are
    valid below sea level.  What is rejected is a grid with no finite samples
    or a grid whose *entire* finite signal is zero, the historical signature
    of an all-NaN source silently converted into a flat cache entry.
    """

    value = np.asarray(grid)
    if value.ndim != 2 or value.size == 0:
        raise ElevationDataError(
            f"{source} returned an invalid DEM shape {value.shape!r}")
    finite = value[np.isfinite(value)]
    if finite.size == 0:
        raise ElevationDataError(f"{source} returned no finite DEM samples")
    if reject_all_zero and np.all(finite == 0):
        raise ElevationDataError(
            f"{source} returned an all-zero DEM with no terrain signal")
    return value

# Project-local DEM cache (populated by tools/manage_dem.py).
# Keep separate from `_CACHE_DIR` (system cache) so users can ship a project
# with a known-good DEM snapshot.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
DEM_CACHE_DIR = os.path.join(_PROJECT_ROOT, "dem_cache")

# Local cache directory for downloaded HGT files (support multi-path)
def _get_srtm_cache_dir():
    """获取SRTM缓存目录（支持多路径）"""
    cache_base = select_cache_path(50)  # 预估SRTM缓存约50MB
    return os.path.join(cache_base, "srtm")

_CACHE_DIR = _get_srtm_cache_dir()

# Open Elevation API
_OPEN_ELEVATION_URL = "https://api.open-elevation.com/api/v1/lookup"
_OPEN_ELEVATION_BATCH_SIZE = 200  # max locations per request

# SRTM HGT tile mirrors.
# 顺序：第一个命中就停。多个端点是为了在某条网络路径偶发不通时自动绕道。
#
# 注意：
#   1. AWS 的 elevation-tiles-prod 桶只存在于 us-east-1，没有亚太副本（实测
#      ap-northeast-1 域名会 301 跳回 us-east-1）。两条 AWS URL 走的是同一
#      物理桶，只是 host header / DNS 解析路径不同 —— 但有时一条卡的时候
#      另一条还通。
#   2. 国内"稳定下载"真正的解法是预先本地化，看 tools/manage_dem.py 与
#      doc/data_and_performance.md。运行时尽量命中 cache/srtm/ 本地文件，
#      不走任何 HTTP。
_SRTM_URLS = [
    # 两条 URL 走同一桶，但 DNS / host header 不同；偶有一条卡的时候另一条通
    "https://elevation-tiles-prod.s3.amazonaws.com/skadi/{dir}/{filename}",
    "https://s3.amazonaws.com/elevation-tiles-prod/skadi/{dir}/{filename}",
]

_VOID = -32768
_HGT_SIZE_1 = 3601
_HGT_SIZE_3 = 1201


# ==================== Open Elevation API ====================

def _fetch_elevations_api(lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
    """Fetch elevations from Open Elevation API in batches.

    Args:
        lats, lons: 1D arrays of coordinates

    Returns:
        1D array of elevation values (NaN for failures)
    """
    n = len(lats)
    elevations = np.full(n, np.nan, dtype=np.float64)

    batch_size = _OPEN_ELEVATION_BATCH_SIZE
    total_batches = (n + batch_size - 1) // batch_size

    with Progress(
        TextColumn("  {task.description}"),
        BarColumn(bar_width=30),
        MofNCompleteColumn(),
        transient=True,
    ) as progress:
        task = progress.add_task("Elevation batches", total=total_batches)
        for start in range(0, n, batch_size):
            end = min(start + batch_size, n)
            batch_lats = lats[start:end]
            batch_lons = lons[start:end]

            locations = [{"latitude": float(lat), "longitude": float(lon)}
                         for lat, lon in zip(batch_lats, batch_lons)]

            for attempt in range(3):
                try:
                    resp = requests.post(
                        _OPEN_ELEVATION_URL,
                        json={"locations": locations},
                        timeout=30
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        results = data.get("results", [])
                        for i, r in enumerate(results):
                            elev = r.get("elevation")
                            if elev is not None:
                                elevations[start + i] = float(elev)
                        break
                    else:
                        logger.debug(f"API returned {resp.status_code}, retrying...")
                        time.sleep(1 * (attempt + 1))
                except Exception as e:
                    logger.debug(f"API request failed (attempt {attempt+1}): {e}")
                    time.sleep(1 * (attempt + 1))

            # Rate limit
            time.sleep(0.1)
            progress.advance(task)

    return elevations


# ==================== SRTM HGT Tile Fallback ====================

_tile_cache = {}


def _tile_filename(lat: int, lon: int) -> str:
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"{ns}{abs(lat):02d}{ew}{abs(lon):03d}.hgt"


def _tile_dir(lat: int) -> str:
    ns = "N" if lat >= 0 else "S"
    return f"{ns}{abs(lat):02d}"


def _download_tile(lat: int, lon: int) -> str:
    """Download an SRTM HGT tile (or use a project-local one) and return its path."""
    filename = _tile_filename(lat, lon)

    # 0. Project-local cache populated by tools/manage_dem.py (offline-first)
    project_local = os.path.join(
        DEM_CACHE_DIR, "srtm", _tile_dir(lat), filename
    )
    if os.path.exists(project_local):
        return project_local

    # Historical downloads live in the repository-level ``cache/srtm``.
    # ``select_cache_path`` may now resolve to the package-local cache, so
    # ignoring this still-valid store caused an all-NaN grid followed by a
    # prohibitively large Open Elevation request even though the exact HGT
    # tile was already present on disk.
    legacy_project_local = os.path.join(
        _PROJECT_ROOT, "cache", "srtm", filename)
    if os.path.exists(legacy_project_local):
        return legacy_project_local

    os.makedirs(_CACHE_DIR, exist_ok=True)
    local_path = os.path.join(_CACHE_DIR, filename)

    if os.path.exists(local_path):
        return local_path

    tile_dir = _tile_dir(lat)

    for url_template in _SRTM_URLS:
        # Try .gz
        url = url_template.format(dir=tile_dir, filename=filename + ".gz")
        logger.info(f"Downloading SRTM tile: {url}")
        try:
            resp = requests.get(url, timeout=60)
            if resp.status_code == 200:
                data = gzip.decompress(resp.content)
                with open(local_path, "wb") as f:
                    f.write(data)
                return local_path
        except Exception as e:
            logger.debug(f"Failed: {e}")

        # Try raw .hgt
        url_raw = url_template.format(dir=tile_dir, filename=filename)
        try:
            resp = requests.get(url_raw, timeout=60)
            if resp.status_code == 200 and len(resp.content) > 1000:
                with open(local_path, "wb") as f:
                    f.write(resp.content)
                return local_path
        except Exception as e:
            logger.debug(f"Failed: {e}")

    return None


def _load_hgt(filepath: str) -> np.ndarray:
    """Load an HGT file into a numpy array."""
    filesize = os.path.getsize(filepath)

    if filesize == _HGT_SIZE_1 * _HGT_SIZE_1 * 2:
        size = _HGT_SIZE_1
    elif filesize == _HGT_SIZE_3 * _HGT_SIZE_3 * 2:
        size = _HGT_SIZE_3
    else:
        side = int(math.sqrt(filesize / 2))
        size = side if side * side * 2 == filesize else _HGT_SIZE_1

    with open(filepath, "rb") as f:
        data = f.read()

    grid = np.frombuffer(data, dtype=">i2").reshape((size, size)).astype(np.float64)
    grid[grid == _VOID] = np.nan
    return grid


def _get_tile(lat: int, lon: int) -> np.ndarray:
    key = (lat, lon)
    if key not in _tile_cache:
        filepath = _download_tile(lat, lon)
        _tile_cache[key] = _load_hgt(filepath) if filepath else None
    return _tile_cache[key]


def _sample_elevation_hgt(lat: float, lon: float) -> float:
    """Sample elevation from local HGT tiles."""
    tile_lat = int(math.floor(lat))
    tile_lon = int(math.floor(lon))

    tile = _get_tile(tile_lat, tile_lon)
    if tile is None:
        return np.nan

    size = tile.shape[0]
    row = (size - 1) - int(round((lat - tile_lat) * (size - 1)))
    col = int(round((lon - tile_lon) * (size - 1)))
    row = max(0, min(size - 1, row))
    col = max(0, min(size - 1, col))

    return tile[row, col]


def get_srtm_tiles_for_bbox(south: float, west: float, north: float,
                            east: float) -> list[tuple[int, int]]:
    """Return list of (tile_lat, tile_lon) SRTM 1°x1° tiles covering the bbox."""
    tile_lat_min = int(math.floor(south))
    tile_lat_max = int(math.floor(north))
    tile_lon_min = int(math.floor(west))
    tile_lon_max = int(math.floor(east))
    return [
        (tile_lat, tile_lon)
        for tile_lat in range(tile_lat_min, tile_lat_max + 1)
        for tile_lon in range(tile_lon_min, tile_lon_max + 1)
    ]


def download_srtm_tiles_for_bbox(south: float, west: float, north: float,
                                  east: float) -> list[str]:
    """Pre-download all SRTM tiles covering the bbox into cache/srtm/. Returns paths."""
    tiles = get_srtm_tiles_for_bbox(south, west, north, east)
    paths = []
    for tile_lat, tile_lon in tiles:
        path = _download_tile(tile_lat, tile_lon)
        if path:
            paths.append(path)
    return paths


# ==================== Copernicus DEM GLO-30 (local GeoTIFF) ====================


def _cop30_tile_path(lat: int, lon: int) -> str:
    """Path layout matches tools/manage_dem.py."""
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    tid = f"Copernicus_DSM_COG_10_{ns}{abs(lat):02d}_00_{ew}{abs(lon):03d}_00_DEM"
    return os.path.join(DEM_CACHE_DIR, "cop30", tid, f"{tid}.tif")


def _fetch_elevation_grid_from_cop30(south: float, west: float, north: float,
                                      east: float, rows: int, cols: int):
    """Sample a regular lat/lon grid from local Copernicus GLO-30 GeoTIFFs.

    Returns the grid on success; ``None`` if rasterio is unavailable or any
    required tile is missing. The caller falls back to SRTM in that case.
    """
    try:
        import rasterio
        from rasterio.windows import from_bounds
    except ImportError:
        logger.debug("rasterio not installed — skipping Copernicus path")
        return None

    tile_lat_min = int(math.floor(south))
    tile_lat_max = int(math.floor(north))
    tile_lon_min = int(math.floor(west))
    tile_lon_max = int(math.floor(east))

    tiles = []
    for tlat in range(tile_lat_min, tile_lat_max + 1):
        for tlon in range(tile_lon_min, tile_lon_max + 1):
            p = _cop30_tile_path(tlat, tlon)
            if not os.path.exists(p):
                # Need ALL covering tiles locally — partial coverage falls back
                logger.debug(f"Copernicus tile missing locally: {p}")
                return None
            tiles.append((tlat, tlon, p))

    logger.info(f"Reading Copernicus GLO-30 from {len(tiles)} local tile(s) in "
                f"{DEM_CACHE_DIR}/cop30/")

    lats = np.linspace(south, north, rows)
    lons = np.linspace(west, east, cols)
    lon_grid, lat_grid = np.meshgrid(lons, lats, indexing="xy")

    grid = np.full((rows, cols), np.nan, dtype=np.float64)

    for tlat, tlon, path in tiles:
        # Per-tile mask: points whose lat/lon falls inside this 1°×1° tile
        mask = ((lat_grid >= tlat) & (lat_grid < tlat + 1) &
                (lon_grid >= tlon) & (lon_grid < tlon + 1))
        if not np.any(mask):
            continue
        with rasterio.open(path) as src:
            xs = lon_grid[mask]
            ys = lat_grid[mask]
            samples = list(src.sample(
                np.column_stack([xs, ys]), indexes=1, masked=False
            ))
            vals = np.array([s[0] for s in samples], dtype=np.float64)
            nodata = src.nodata
            if nodata is not None:
                vals[vals == nodata] = np.nan
            grid[mask] = vals

    return grid


def _fetch_elevation_grid_from_srtm(south: float, west: float, north: float,
                                     east: float, rows: int, cols: int) -> np.ndarray:
    """Build full elevation grid from SRTM HGT tiles (vectorized, 1–4 tile downloads).

    Returns 2D array (rows x cols); NaN where no tile data.
    """
    lats = np.linspace(south, north, rows)
    lons = np.linspace(west, east, cols)
    lat_grid, lon_grid = np.meshgrid(lats, lons, indexing="ij")

    tile_lat_min = int(math.floor(south))
    tile_lat_max = int(math.floor(north))
    tile_lon_min = int(math.floor(west))
    tile_lon_max = int(math.floor(east))

    grid = np.full((rows, cols), np.nan, dtype=np.float64)

    for tile_lat in range(tile_lat_min, tile_lat_max + 1):
        for tile_lon in range(tile_lon_min, tile_lon_max + 1):
            tile = _get_tile(tile_lat, tile_lon)
            if tile is None:
                continue
            size = tile.shape[0]
            # Points inside this tile: [tile_lat, tile_lat+1) x [tile_lon, tile_lon+1)
            mask = (
                (lat_grid >= tile_lat) & (lat_grid < tile_lat + 1) &
                (lon_grid >= tile_lon) & (lon_grid < tile_lon + 1)
            )
            if not np.any(mask):
                continue
            # HGT: row 0 = north (tile_lat+1), row size-1 = south (tile_lat)
            row_idx = (tile_lat + 1 - lat_grid) * (size - 1)
            col_idx = (lon_grid - tile_lon) * (size - 1)
            row_idx = np.clip(np.round(row_idx).astype(int), 0, size - 1)
            col_idx = np.clip(np.round(col_idx).astype(int), 0, size - 1)
            grid[mask] = tile[row_idx[mask], col_idx[mask]]

    return grid


# ==================== Main Grid Fetcher ====================

ELEVATION_GRID_CACHE_VERSION = "physical-smoothing-v3"

# DEM denoising is a source-quality operation, not an art-direction knob.  A
# sigma expressed only in grid cells changes its real-world meaning whenever
# the requested resolution changes.  Keep the legacy cell value as an upper
# bound, but never blur more than this physical distance.
ELEVATION_SMOOTHING_MAX_METERS = 60.0


def _grid_spacing_m(south: float, west: float, north: float, east: float,
                    shape: tuple[int, int]) -> tuple[float, float]:
    """Approximate north/south and east/west spacing of a WGS84 grid."""
    rows, cols = shape
    mid_lat_rad = math.radians((south + north) * 0.5)
    lat_m = abs(north - south) * 111_320.0 / max(1, rows - 1)
    lon_m = (
        abs(east - west) * 111_320.0 * max(0.01, abs(math.cos(mid_lat_rad)))
        / max(1, cols - 1)
    )
    return float(lat_m), float(lon_m)


def _resolved_smoothing_sigma(
    requested_sigma_cells: float,
    *,
    south: float,
    west: float,
    north: float,
    east: float,
    shape: tuple[int, int],
) -> tuple[float, float, float]:
    """Resolve a stable, physically bounded Gaussian smoothing radius.

    Returns ``(sigma_cells, representative_cell_m, sigma_m)``.  The physical
    cap prevents a coarse cache grid from turning the legacy ``2.5`` setting
    into a 200+ metre low-pass filter.
    """
    requested = max(0.0, float(requested_sigma_cells))
    lat_m, lon_m = _grid_spacing_m(south, west, north, east, shape)
    positive = [value for value in (lat_m, lon_m) if value > 0]
    cell_m = float(sum(positive) / len(positive)) if positive else 0.0
    if requested <= 0.0 or cell_m <= 0.0:
        return 0.0, cell_m, 0.0
    sigma_cells = min(requested, ELEVATION_SMOOTHING_MAX_METERS / cell_m)
    # Preserve the historical guard against excessive kernels on tiny grids.
    sigma_cells = min(sigma_cells, (shape[0] + shape[1]) / 200.0)
    sigma_cells = max(0.0, float(sigma_cells))
    return sigma_cells, cell_m, sigma_cells * cell_m


def _smooth_elevation_grid(
    grid: np.ndarray,
    *,
    south: float,
    west: float,
    north: float,
    east: float,
    requested_sigma_cells: float,
) -> tuple[np.ndarray, dict]:
    """Apply the one allowed DEM smoothing pass and return its evidence."""
    sigma, cell_m, sigma_m = _resolved_smoothing_sigma(
        requested_sigma_cells,
        south=south,
        west=west,
        north=north,
        east=east,
        shape=grid.shape,
    )
    if sigma > 0.0:
        grid = gaussian_filter(grid, sigma=sigma, mode="nearest")
    evidence = {
        "requested_sigma_cells": float(requested_sigma_cells),
        "resolved_sigma_cells": float(sigma),
        "representative_cell_m": float(cell_m),
        "resolved_sigma_m": float(sigma_m),
        "max_sigma_m": float(ELEVATION_SMOOTHING_MAX_METERS),
        "passes": 1 if sigma > 0.0 else 0,
    }
    return grid, evidence

def _grid_cache_path(south: float, west: float, north: float, east: float,
                     resolution: int) -> str:
    """Generate cache file path for an elevation grid."""
    from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.config import (
        ELEVATION_SMOOTHING_SIGMA,
    )
    cache_base = select_cache_path(10)  # 预估网格缓存约10MB
    cache_dir = os.path.join(cache_base, "grids")
    os.makedirs(cache_dir, exist_ok=True)
    key = (
        f"{ELEVATION_GRID_CACHE_VERSION}_"
        f"s{float(ELEVATION_SMOOTHING_SIGMA):.3f}_"
        f"sm{float(ELEVATION_SMOOTHING_MAX_METERS):.1f}_"
        f"{south:.6f}_{west:.6f}_{north:.6f}_{east:.6f}_{resolution}"
    )
    return os.path.join(cache_dir, f"elev_{key}.npy")


def fetch_elevation_grid_from_file(filepath: str,
                                   south: float, west: float, north: float, east: float,
                                   rows: int, cols: int) -> np.ndarray:
    """Build elevation grid by sampling a local GeoTIFF (or other raster) DEM.

    Requires: pip install rasterio
    """
    try:
        import rasterio
        from rasterio.warp import transform as rasterio_transform
        from rasterio.crs import CRS as RasterioCRS
        from rasterio.transform import rowcol
    except ImportError as e:
        raise ImportError(
            "Using --elevation-file requires rasterio. Install with: pip install rasterio"
        ) from e

    lats = np.linspace(south, north, rows)
    lons = np.linspace(west, east, cols)
    lat_2d, lon_2d = np.meshgrid(lats, lons, indexing="ij")
    lats_flat = lat_2d.ravel()
    lons_flat = lon_2d.ravel()

    with rasterio.open(filepath) as src:
        wgs84 = RasterioCRS.from_epsg(4326)
        xs, ys = rasterio_transform(wgs84, src.crs, lons_flat, lats_flat)
        r, c = rowcol(src.transform, xs, ys)
        r = np.clip(np.asarray(r, dtype=np.intp), 0, src.height - 1)
        c = np.clip(np.asarray(c, dtype=np.intp), 0, src.width - 1)
        data = src.read(1)
        if data.dtype.kind in ("i", "u"):
            data = data.astype(np.float64)
        nodata = src.nodata
        if nodata is not None:
            data = np.where(data == nodata, np.nan, data)
        values = data[r, c].copy()

    grid = values.reshape((rows, cols))
    logger.info(f"Loaded elevation from {filepath}: {rows}x{cols}, "
                f"range {np.nanmin(grid):.0f}m - {np.nanmax(grid):.0f}m")
    return grid


def fetch_elevation_grid(south: float, west: float, north: float, east: float,
                         resolution: int = 256,
                         use_cache: bool = True,
                         ttl_seconds: int = None,
                         elevation_file: str = None) -> np.ndarray:
    """Fetch elevation data for a bounding box and return a regular grid.

    Uses cached grid if available and valid. If elevation_file is given, samples
    from that local raster (GeoTIFF etc.) instead. Otherwise tries SRTM HGT
    tiles first; falls back to Open Elevation API if needed.

    Args:
        south, west, north, east: WGS84 bounding box
        resolution: number of grid points along the longer axis
        use_cache: if False, always fetch fresh data
        ttl_seconds: cache TTL in seconds (None = use default from config)
        elevation_file: optional path to local GeoTIFF (or other raster) DEM

    Returns:
        2D numpy array of elevation values in meters, shape (rows, cols).
        Rows go from south to north, cols from west to east.
    """
    # Function-level import: allows runtime monkey-patch from auto-params
    from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.config import ELEVATION_SMOOTHING_SIGMA

    if ttl_seconds is None:
        ttl_seconds = CACHE_TTL_SECONDS

    lat_range = north - south
    lon_range = east - west
    if lat_range >= lon_range:
        rows = resolution
        cols = max(2, int(resolution * lon_range / lat_range))
    else:
        cols = resolution
        rows = max(2, int(resolution * lat_range / lon_range))

    # Cache check (skip when using local elevation file)
    if not elevation_file and use_cache:
        cache_path = _grid_cache_path(south, west, north, east, resolution)
        if cache_mgr.is_valid(cache_path, ttl_seconds):
            age_str = cache_mgr.format_age(cache_path)
            logger.info(f"Loading cached elevation grid ({age_str}): {cache_path}")
            try:
                grid = require_usable_elevation_grid(
                    np.load(cache_path), source=f"cached DEM {cache_path}")
            except (OSError, ValueError, ElevationDataError) as exc:
                logger.warning(
                    "Ignoring unusable cached elevation grid %s: %s",
                    cache_path, exc,
                )
            else:
                logger.info(f"Cached grid: {grid.shape[0]}x{grid.shape[1]}, "
                            f"Elevation: {np.nanmin(grid):.1f}m - {np.nanmax(grid):.1f}m")
                return grid

    logger.info(f"Fetching elevation grid {rows}x{cols} "
                f"({south:.4f},{west:.4f} -> {north:.4f},{east:.4f})")

    # Local DEM file (no network, fast)
    if elevation_file:
        grid = fetch_elevation_grid_from_file(
            elevation_file, south, west, north, east, rows, cols
        )
        grid = _fill_nodata(grid)
        grid, smoothing = _smooth_elevation_grid(
            grid,
            south=south, west=west, north=north, east=east,
            requested_sigma_cells=ELEVATION_SMOOTHING_SIGMA,
        )
        if smoothing["passes"]:
            logger.info(
                "Applied elevation smoothing (sigma=%.3f cells / %.1fm)",
                smoothing["resolved_sigma_cells"],
                smoothing["resolved_sigma_m"],
            )
        grid = require_usable_elevation_grid(
            grid, source=f"local DEM {elevation_file}")
        logger.info(f"Elevation range: {np.nanmin(grid):.1f}m - {np.nanmax(grid):.1f}m")
        return grid

    # Primary: local Copernicus GLO-30 GeoTIFF (highest quality, no network)
    grid = _fetch_elevation_grid_from_cop30(south, west, north, east, rows, cols)
    if grid is not None:
        nan_count = int(np.isnan(grid).sum())
        total = int(grid.size)
        if nan_count < total * 0.5:
            logger.info(
                f"Copernicus GLO-30 OK ({total - nan_count}/{total} points, "
                f"{nan_count} missing will be filled)"
            )
        else:
            grid = None  # too many NaN — fall through to SRTM

    # Secondary: SRTM HGT tiles (local first, then network download — fast)
    if grid is None:
        logger.info("Trying SRTM HGT tiles...")
        grid = _fetch_elevation_grid_from_srtm(south, west, north, east, rows, cols)
    nan_count = np.isnan(grid).sum()
    total = grid.size
    srtm_ok = nan_count < total * 0.5

    if srtm_ok:
        logger.info(f"SRTM coverage OK ({total - nan_count}/{total} points, "
                    f"{nan_count} missing will be filled)")
    else:
        # Fallback: Open Elevation API (many batch requests — slow)
        logger.warning(f"SRTM coverage low ({nan_count}/{total} NaN), "
                      "falling back to Open Elevation API...")
        flat_lats = np.linspace(south, north, rows)
        flat_lons = np.linspace(west, east, cols)
        lat_grid, lon_grid = np.meshgrid(flat_lats, flat_lons, indexing="ij")
        flat_lats = lat_grid.ravel()
        flat_lons = lon_grid.ravel()
        elevations = _fetch_elevations_api(flat_lats, flat_lons)
        nan_api = np.isnan(elevations).sum()
        if nan_api > len(elevations) * 0.5:
            # Fill remaining with SRTM per-point
            with Progress(
                TextColumn("  {task.description}"),
                BarColumn(bar_width=30),
                MofNCompleteColumn(),
                transient=True,
            ) as progress:
                task = progress.add_task("HGT fill", total=len(flat_lats))
                for i in range(len(flat_lats)):
                    if np.isnan(elevations[i]):
                        elevations[i] = _sample_elevation_hgt(flat_lats[i], flat_lons[i])
                    progress.advance(task)
        grid = elevations.reshape((rows, cols))

    # Fill missing data
    grid = _fill_nodata(grid)

    # Smooth elevation to reduce blocky appearance from coarse DEM/API data
    grid, smoothing = _smooth_elevation_grid(
        grid,
        south=south, west=west, north=north, east=east,
        requested_sigma_cells=ELEVATION_SMOOTHING_SIGMA,
    )
    if smoothing["passes"]:
        logger.info(
            "Applied elevation smoothing (sigma=%.3f cells / %.1fm)",
            smoothing["resolved_sigma_cells"],
            smoothing["resolved_sigma_m"],
        )

    grid = require_usable_elevation_grid(
        grid, source="fetched elevation grid")

    # Cache the result
    cache_path = _grid_cache_path(south, west, north, east, resolution)
    np.save(cache_path, grid)
    logger.info(f"Cached elevation grid to {cache_path}")

    logger.info(f"Elevation range: {np.nanmin(grid):.1f}m - {np.nanmax(grid):.1f}m")

    return grid


# ==================== Tile-level cache (Phase 2) ====================

# Legacy/default sample count for callers that use the private helpers
# directly.  Production tiled fetches derive the per-tile resolution from the
# requested full-grid resolution; this constant is no longer a fixed quality
# ceiling.
ELEV_TILE_RES = 61
ELEV_TILE_CACHE_VERSION = "resolution-source-v3"
ELEV_TILE_SAMPLING_VERSION = "regular-wgs84-nearest-v1"


def _requested_tile_resolution(
    resolution: int,
    *,
    ix0: int,
    iy0: int,
    ix1: int,
    iy1: int,
) -> int:
    """Return samples per tile so the stitched long axis meets the request."""
    requested = max(2, int(resolution))
    tiles_x = max(1, ix1 - ix0 + 1)
    tiles_y = max(1, iy1 - iy0 + 1)
    intervals_per_tile = int(math.ceil(
        (requested - 1) / max(tiles_x, tiles_y)))
    return max(2, intervals_per_tile + 1)


def _source_file_fingerprint(paths: list[str]) -> str:
    """Cheap cache identity that changes when the installed DEM changes."""
    digest = hashlib.sha256()
    for path in sorted(paths):
        stat = os.stat(path)
        digest.update(os.path.abspath(path).encode("utf-8"))
        digest.update(str(stat.st_size).encode("ascii"))
        digest.update(str(stat.st_mtime_ns).encode("ascii"))
    return digest.hexdigest()[:12]


def _preferred_tile_source_identity(
    south: float, west: float, north: float, east: float,
) -> str:
    """Resolve the local source identity before a raw tile cache lookup.

    This deliberately performs no download.  If a source is installed later,
    its identity changes and the previous lower-quality cache cannot mask it.
    """
    tile_lat_min = int(math.floor(south))
    tile_lat_max = int(math.floor(math.nextafter(north, south)))
    tile_lon_min = int(math.floor(west))
    tile_lon_max = int(math.floor(math.nextafter(east, west)))

    cop30_paths = [
        _cop30_tile_path(lat, lon)
        for lat in range(tile_lat_min, tile_lat_max + 1)
        for lon in range(tile_lon_min, tile_lon_max + 1)
    ]
    if (cop30_paths and all(os.path.exists(path) for path in cop30_paths)
            and importlib.util.find_spec("rasterio") is not None):
        return f"cop30-{_source_file_fingerprint(cop30_paths)}"

    srtm_paths = []
    for lat in range(tile_lat_min, tile_lat_max + 1):
        for lon in range(tile_lon_min, tile_lon_max + 1):
            filename = _tile_filename(lat, lon)
            candidates = (
                os.path.join(DEM_CACHE_DIR, "srtm", _tile_dir(lat), filename),
                os.path.join(_CACHE_DIR, filename),
            )
            existing = next((path for path in candidates
                             if os.path.exists(path)), None)
            if existing is not None:
                srtm_paths.append(existing)
    if srtm_paths:
        sizes = {os.path.getsize(path) for path in srtm_paths}
        if sizes == {_HGT_SIZE_1 * _HGT_SIZE_1 * 2}:
            family = "srtm1"
        elif sizes == {_HGT_SIZE_3 * _HGT_SIZE_3 * 2}:
            family = "srtm3"
        else:
            family = "srtm-mixed"
        return f"{family}-{_source_file_fingerprint(srtm_paths)}"
    return "srtm-auto"


def _elev_tile_path(ix: int, iy: int,
                    resolution: int = ELEV_TILE_RES,
                    source_identity: str = "auto",
                    step: float = 0.05) -> str:
    """高程瓦片缓存路径（与全框 grids 缓存同目录下的 tiles/ 子目录）。"""
    cache_base = select_cache_path(10)
    d = os.path.join(cache_base, "grids", "tiles")
    os.makedirs(d, exist_ok=True)
    # v1 could persist an all-zero fallback forever when DEM tiles were not
    # installed yet.  Include the source policy in the key so installing a
    # real local DEM cannot keep hitting those poisoned cache entries.
    return os.path.join(
        d,
        f"elevtile_{ELEV_TILE_CACHE_VERSION}_{ELEV_TILE_SAMPLING_VERSION}_"
        f"step{float(step):.6f}_{source_identity}_{ix}_{iy}_r{int(resolution)}.npy",
    )


def _compute_tile_elevation(ts: float, tw: float, tn: float, te: float,
                            resolution: int = ELEV_TILE_RES) -> np.ndarray:
    """单瓦片高程网格（row0=south, col0=west）：不做平滑，只补缺。"""
    grid = _fetch_elevation_grid_from_cop30(ts, tw, tn, te,
                                            resolution, resolution)
    if grid is None or np.isnan(grid).sum() > grid.size * 0.5:
        grid = _fetch_elevation_grid_from_srtm(ts, tw, tn, te,
                                               resolution, resolution)
    return require_usable_elevation_grid(
        _fill_nodata(grid), source=f"elevation tile {ts},{tw},{tn},{te}")


def _stitch_tile_grids(tiles: dict, ix0: int, iy0: int,
                       ix1: int, iy1: int) -> np.ndarray:
    """拼接瓦片网格为整框网格（row0=south）；相邻瓦片共享边界行/列，去重。"""
    strips = []
    for iy in range(iy0, iy1 + 1):
        parts = [tiles[(ix, iy)] for ix in range(ix0, ix1 + 1)]
        strip = parts[0] if len(parts) == 1 else np.hstack(
            [parts[0]] + [p[:, 1:] for p in parts[1:]])
        strips.append(strip)
    return strips[0] if len(strips) == 1 else np.vstack(
        [strips[0]] + [s[1:, :] for s in strips[1:]])


def fetch_elevation_grid_tiled(south: float, west: float, north: float, east: float,
                               resolution: int = 256,
                               step: float = None,
                               use_cache: bool = True,
                               return_evidence: bool = False):
    """瓦片级高程取数：按 0.05° 网格瓦片缓存，拼接后返回量化框整框网格。

    与 fetch_elevation_grid 相同的网格约定（row0=south, col0=west）；
    旧通用调用方（generate_city_legacy.py）拿到后再重采样裁剪到用户精确框。
    平滑在拼接后整框做，避免瓦片接缝。
    """
    from _TEXTURE_STYLE_OF_DEEPSEEK._tile_grid import (
        DEFAULT_TILE_STEP, snap_bbox, tile_range, tile_bbox)
    # Function-level import: allows runtime monkey-patch from auto-params
    from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.config import ELEVATION_SMOOTHING_SIGMA

    step = step or DEFAULT_TILE_STEP
    fs, fw, fn, fe = snap_bbox(south, west, north, east, step)
    ix0, iy0, ix1, iy1 = tile_range(fs, fw, fn, fe, step)
    tile_resolution = _requested_tile_resolution(
        resolution, ix0=ix0, iy0=iy0, ix1=ix1, iy1=iy1)

    tiles = {}
    source_identities = {}
    n_hit = 0
    for iy in range(iy0, iy1 + 1):
        for ix in range(ix0, ix1 + 1):
            ts, tw, tn, te = tile_bbox(ix, iy, step)
            source_identity = _preferred_tile_source_identity(ts, tw, tn, te)
            source_identities[(ix, iy)] = source_identity
            p = _elev_tile_path(
                ix, iy, tile_resolution, source_identity, step)
            if use_cache and os.path.exists(p):
                try:
                    cached = require_usable_elevation_grid(
                        np.load(p), source=f"cached elevation tile {p}")
                    if cached.shape != (tile_resolution, tile_resolution):
                        raise ElevationDataError(
                            f"cached elevation tile {p} has shape "
                            f"{cached.shape}, expected "
                            f"{(tile_resolution, tile_resolution)}")
                    tiles[(ix, iy)] = cached
                    n_hit += 1
                    continue
                except (OSError, ValueError, ElevationDataError) as exc:
                    logger.warning("Ignoring unusable elevation tile %s: %s", p, exc)
            g = _compute_tile_elevation(
                ts, tw, tn, te, tile_resolution)
            if g.shape != (tile_resolution, tile_resolution):
                raise ElevationDataError(
                    f"computed elevation tile {(ix, iy)} has shape {g.shape}, "
                    f"expected {(tile_resolution, tile_resolution)}")
            # 原子写；用文件句柄写避免 np.save 对字符串路径自动追加 .npy
            tmp_path = p + f".tmp{os.getpid()}"
            with open(tmp_path, 'wb') as fh:
                np.save(fh, g)
            os.replace(tmp_path, p)
            tiles[(ix, iy)] = g
    logger.info(
        "Elevation tiles: %d hit / %d total; %dx%d samples per tile",
        n_hit, len(tiles), tile_resolution, tile_resolution,
    )

    grid = _stitch_tile_grids(tiles, ix0, iy0, ix1, iy1)

    grid, smoothing = _smooth_elevation_grid(
        grid,
        south=fs, west=fw, north=fn, east=fe,
        requested_sigma_cells=ELEVATION_SMOOTHING_SIGMA,
    )
    if smoothing["passes"]:
        logger.info(
            "Applied stitched elevation smoothing once "
            "(sigma=%.3f cells / %.1fm)",
            smoothing["resolved_sigma_cells"],
            smoothing["resolved_sigma_m"],
        )

    grid = require_usable_elevation_grid(
        grid, source="stitched elevation tile grid")
    lat_spacing_m, lon_spacing_m = _grid_spacing_m(
        fs, fw, fn, fe, grid.shape)
    evidence = {
        "cache_version": ELEV_TILE_CACHE_VERSION,
        "sampling_version": ELEV_TILE_SAMPLING_VERSION,
        "requested_resolution": int(resolution),
        "tile_resolution": int(tile_resolution),
        "tile_count": int(len(tiles)),
        "cache_hits": int(n_hit),
        "snapped_bbox_wgs84": [float(fs), float(fw), float(fn), float(fe)],
        "stitched_shape": [int(grid.shape[0]), int(grid.shape[1])],
        "effective_spacing_m": {
            "latitude": float(lat_spacing_m),
            "longitude": float(lon_spacing_m),
        },
        "source_identities": sorted(set(source_identities.values())),
        "smoothing": smoothing,
    }
    return (grid, evidence) if return_evidence else grid


def _fill_nodata(grid: np.ndarray) -> np.ndarray:
    """Fill NaN values in elevation grid using interpolation and median filter."""
    grid = np.asarray(grid, dtype=np.float64).copy()
    grid[~np.isfinite(grid)] = np.nan
    nan_mask = np.isnan(grid)
    nan_count = nan_mask.sum()

    if nan_count == 0:
        return grid

    total = grid.size
    logger.info(f"Filling {nan_count}/{total} missing elevation values")

    if nan_count == total:
        raise ElevationDataError(
            "all elevation values are non-finite; refusing a flat zero DEM")

    rows, cols = grid.shape
    y_coords, x_coords = np.mgrid[0:rows, 0:cols]

    known_mask = ~nan_mask
    known_points = np.column_stack((y_coords[known_mask], x_coords[known_mask]))
    known_values = grid[known_mask]
    nan_points = np.column_stack((y_coords[nan_mask], x_coords[nan_mask]))

    if len(known_points) > 3:
        filled = griddata(known_points, known_values, nan_points,
                          method='linear', fill_value=np.nanmean(known_values))
        grid[nan_mask] = filled
    else:
        grid[nan_mask] = np.nanmean(known_values) if known_values.size > 0 else 0

    remaining_nan = np.isnan(grid)
    if remaining_nan.any():
        remaining_points = np.column_stack(
            (y_coords[remaining_nan], x_coords[remaining_nan]))
        filled2 = griddata(known_points, known_values, remaining_points,
                           method='nearest')
        grid[remaining_nan] = filled2

    if nan_count > total * 0.1:
        grid = median_filter(grid, size=3)

    return grid


def sample_elevation_at_points(lats: np.ndarray, lons: np.ndarray,
                               elevation_grid: np.ndarray,
                               south: float, west: float,
                               north: float, east: float) -> np.ndarray:
    """Sample elevation from grid at given lat/lon points via bilinear interpolation."""
    rows, cols = elevation_grid.shape

    row_frac = (lats - south) / (north - south) * (rows - 1)
    col_frac = (lons - west) / (east - west) * (cols - 1)

    row_frac = np.clip(row_frac, 0, rows - 1)
    col_frac = np.clip(col_frac, 0, cols - 1)

    r0 = np.floor(row_frac).astype(int)
    c0 = np.floor(col_frac).astype(int)
    r1 = np.minimum(r0 + 1, rows - 1)
    c1 = np.minimum(c0 + 1, cols - 1)

    dr = row_frac - r0
    dc = col_frac - c0

    v00 = elevation_grid[r0, c0]
    v01 = elevation_grid[r0, c1]
    v10 = elevation_grid[r1, c0]
    v11 = elevation_grid[r1, c1]

    result = (v00 * (1 - dr) * (1 - dc) +
              v01 * (1 - dr) * dc +
              v10 * dr * (1 - dc) +
              v11 * dr * dc)

    return result
