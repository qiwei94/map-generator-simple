"""Fetch elevation data and build elevation grids.

Strategy:
1. Primary: SRTM HGT tiles (1–4 tile downloads, then local sampling — fast)
2. Fallback: Open Elevation API (batch queries when SRTM has no coverage)
"""

import logging
import math
import os
import gzip
import zipfile
import io
import time

import numpy as np
import requests
from scipy.ndimage import median_filter, gaussian_filter
from scipy.interpolate import griddata
from rich.progress import Progress, BarColumn, TextColumn, MofNCompleteColumn

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.config import CACHE_TTL_SECONDS, ELEVATION_SMOOTHING_SIGMA, select_cache_path
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.utils import cache as cache_mgr

logger = logging.getLogger(__name__)

# Local cache directory for downloaded HGT files (support multi-path)
def _get_srtm_cache_dir():
    """获取SRTM缓存目录（支持多路径）"""
    cache_base = select_cache_path(50)  # 预估SRTM缓存约50MB
    return os.path.join(cache_base, "srtm")

_CACHE_DIR = _get_srtm_cache_dir()

# Open Elevation API
_OPEN_ELEVATION_URL = "https://api.open-elevation.com/api/v1/lookup"
_OPEN_ELEVATION_BATCH_SIZE = 200  # max locations per request

# SRTM HGT tile mirrors (fallback)
_SRTM_URLS = [
    "https://s3.amazonaws.com/elevation-tiles-prod/skadi/{dir}/{filename}",
    "https://elevation-tiles-prod.s3.amazonaws.com/skadi/{dir}/{filename}",
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
    """Download an SRTM HGT tile and return the local file path."""
    os.makedirs(_CACHE_DIR, exist_ok=True)

    filename = _tile_filename(lat, lon)
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

def _grid_cache_path(south: float, west: float, north: float, east: float,
                     resolution: int) -> str:
    """Generate cache file path for an elevation grid."""
    cache_base = select_cache_path(10)  # 预估网格缓存约10MB
    cache_dir = os.path.join(cache_base, "grids")
    os.makedirs(cache_dir, exist_ok=True)
    key = f"{south:.6f}_{west:.6f}_{north:.6f}_{east:.6f}_{resolution}"
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
            grid = np.load(cache_path)
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
        if ELEVATION_SMOOTHING_SIGMA > 0:
            sigma = min(ELEVATION_SMOOTHING_SIGMA, (grid.shape[0] + grid.shape[1]) / 200.0)
            grid = gaussian_filter(grid, sigma=sigma, mode="nearest")
        logger.info(f"Elevation range: {np.nanmin(grid):.1f}m - {np.nanmax(grid):.1f}m")
        return grid

    # Primary: SRTM HGT tiles (1–4 downloads, then local sampling — fast)
    logger.info("Trying SRTM HGT tiles (1–4 tile downloads)...")
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
    if ELEVATION_SMOOTHING_SIGMA > 0:
        sigma = min(ELEVATION_SMOOTHING_SIGMA, (grid.shape[0] + grid.shape[1]) / 200.0)
        grid = gaussian_filter(grid, sigma=sigma, mode="nearest")
        logger.info(f"Applied elevation smoothing (sigma={sigma:.2f})")

    # Cache the result
    cache_path = _grid_cache_path(south, west, north, east, resolution)
    np.save(cache_path, grid)
    logger.info(f"Cached elevation grid to {cache_path}")

    logger.info(f"Elevation range: {np.nanmin(grid):.1f}m - {np.nanmax(grid):.1f}m")

    return grid


def _fill_nodata(grid: np.ndarray) -> np.ndarray:
    """Fill NaN values in elevation grid using interpolation and median filter."""
    nan_mask = np.isnan(grid)
    nan_count = nan_mask.sum()

    if nan_count == 0:
        return grid

    total = grid.size
    logger.info(f"Filling {nan_count}/{total} missing elevation values")

    if nan_count == total:
        logger.warning("All elevation values are NaN, returning zeros")
        return np.zeros_like(grid)

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
