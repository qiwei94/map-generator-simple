# 高程数据获取与地形生成完整代码

## 一、高程数据获取 (`fetchers/elevation.py`)

### 数据来源

1. **SRTM HGT 瓦片**（主要数据源）
   - NASA 航天飞机雷达地形数据（Shuttle Radar Topography Mission）
   - 分辨率：90m（全球）/ 30m（部分地区）
   - 格式：big-endian 16-bit 整数，单文件 3601x3601 或 1201x1201 网格
   - 覆盖范围：每个瓦片 1°x1° 经纬度
   - 下载源：AWS S3 `elevation-tiles-prod`

2. **Open Elevation API**（备用数据源）
   - URL: `https://api.open-elevation.com/api/v1/lookup`
   - 批量查询：每批最多 200 个点
   - 适用场景：SRTM 覆盖率低时（如海洋区域）

3. **本地 GeoTIFF DEM**（可选）
   - 支持任意本地高程栅格文件
   - 需要 `rasterio` 库

### 完整代码

```python
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
    cache_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "cache", "grids")
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
```

### 核心算法说明

#### 1. SRTM HGT 瓦片采样
```python
# HGT 文件布局：row 0 = 北边，row size-1 = 南边
row_idx = (tile_lat + 1 - lat_grid) * (size - 1)
col_idx = (lon_grid - tile_lon) * (size - 1)
```
- 将经纬度网格映射到 HGT 瓦片的行列索引
- 支持跨瓦片采样（最多 4 个瓦片）

#### 2. 缺失值填充
```python
# 使用 scipy.interpolate.griddata 进行空间插值
filled = griddata(known_points, known_values, nan_points,
                  method='linear', fill_value=np.nanmean(known_values))
# 剩余缺失值使用最近邻插值
filled2 = griddata(known_points, known_values, remaining_points, method='nearest')
```

#### 3. 高程平滑
```python
# 高斯滤波减少块状效应
sigma = min(ELEVATION_SMOOTHING_SIGMA, (grid.shape[0] + grid.shape[1]) / 200.0)
grid = gaussian_filter(grid, sigma=sigma, mode="nearest")
```

#### 4. 双线性插值采样
```python
# 在任意点采样高程（用于建筑物、道路等地物的高度查询）
result = (v00 * (1 - dr) * (1 - dc) +
          v01 * (1 - dr) * dc +
          v10 * dr * (1 - dc) +
          v11 * dr * dc)
```

---

## 二、地形网格生成 (`processors/terrain.py`)

### 完整代码

```python
"""Generate terrain mesh from elevation grid."""

import logging
import numpy as np
import trimesh

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.config import COLORS, get_area_class, TERRAIN_GRID, DECIMATION_TARGETS

logger = logging.getLogger(__name__)


def build_terrain_mesh(elevation_grid: np.ndarray,
                       width_m: float, height_m: float,
                       area_km2: float = 0) -> trimesh.Trimesh:
    """Build a 3D terrain mesh from a 2D elevation grid.

    Args:
        elevation_grid: 2D numpy array (rows, cols) of elevation in meters.
                       Rows: south->north, Cols: west->east.
        width_m: total width in meters (X axis)
        height_m: total height in meters (Y axis)
        area_km2: area for LOD decision

    Returns:
        trimesh.Trimesh with vertex colors encoding elevation
    """
    rows, cols = elevation_grid.shape
    logger.info(f"Building terrain mesh from {rows}x{cols} grid, "
                f"{width_m:.0f}m x {height_m:.0f}m")

    # Generate vertex positions
    # X: west->east (cols), Y: south->north (rows), Z: elevation
    x = np.linspace(-width_m / 2, width_m / 2, cols)
    y = np.linspace(-height_m / 2, height_m / 2, rows)
    xx, yy = np.meshgrid(x, y)

    vertices = np.column_stack([
        xx.ravel(),
        yy.ravel(),
        elevation_grid.ravel()
    ])

    # Generate triangle faces (two triangles per grid cell)
    faces = _generate_grid_faces(rows, cols)

    # Compute vertex colors based on elevation
    vertex_colors = _elevation_to_colors(elevation_grid.ravel())

    mesh = trimesh.Trimesh(
        vertices=vertices,
        faces=faces,
        vertex_colors=vertex_colors,
        process=False
    )

    # Decimate if needed — skip if fast_simplification unavailable (Python 3.9 compat)
    area_class = get_area_class(area_km2)
    target = DECIMATION_TARGETS.get(area_class)
    if target and len(mesh.faces) > target:
        logger.info(f"Decimating terrain from {len(mesh.faces)} to ~{target} faces")
        try:
            mesh = mesh.simplify_quadric_decimation(face_count=target)
        except (TypeError, ImportError) as e:
            logger.warning(f"Skipping decimation: {e}")
        # Re-apply colors after decimation (vertices may have changed)
        new_elevations = mesh.vertices[:, 2]
        mesh.visual.vertex_colors = _elevation_to_colors(new_elevations)

    logger.info(f"Terrain mesh: {len(mesh.vertices)} vertices, {len(mesh.faces)} faces")
    return mesh


def _generate_grid_faces(rows: int, cols: int) -> np.ndarray:
    """Generate triangle face indices for a regular grid."""
    faces = []
    for i in range(rows - 1):
        for j in range(cols - 1):
            # Top-left vertex index
            tl = i * cols + j
            tr = tl + 1
            bl = (i + 1) * cols + j
            br = bl + 1

            # Two triangles per quad
            faces.append([tl, bl, tr])
            faces.append([tr, bl, br])

    return np.array(faces, dtype=np.int64)


def _elevation_to_colors(elevations: np.ndarray) -> np.ndarray:
    """Map elevation values to green-brown gradient colors."""
    low_color = np.array(COLORS["terrain_low"], dtype=np.float64)
    high_color = np.array(COLORS["terrain_high"], dtype=np.float64)

    e_min = np.nanmin(elevations)
    e_max = np.nanmax(elevations)

    if e_max - e_min < 1e-3:
        # Flat terrain - use low color
        colors = np.tile(low_color, (len(elevations), 1)).astype(np.uint8)
        return colors

    # Normalize to [0, 1]
    t = (elevations - e_min) / (e_max - e_min)
    t = np.clip(t, 0, 1)

    # Compress gradient for subtle monochrome terrain look
    t = t * 0.4

    # Interpolate colors
    colors = np.outer(1 - t, low_color) + np.outer(t, high_color)
    colors = np.clip(colors, 0, 255).astype(np.uint8)

    return colors


def get_terrain_resolution(area_km2: float) -> int:
    """Get terrain grid resolution based on area size."""
    area_class = get_area_class(area_km2)
    return TERRAIN_GRID[area_class]


def carve_terrain_for_water(terrain_mesh: trimesh.Trimesh,
                            water_gdf) -> int:
    """Push terrain vertices inside water polygons below the water surface.

    SRTM elevation data includes noise over water bodies (radar reflects
    off water surface).  This function flattens the terrain inside water
    polygon areas to create visible river/lake basins.

    Modifies *terrain_mesh* in-place.  Returns the number of carved vertices.

    Args:
        terrain_mesh: open terrain surface mesh (Z-up, meters)
        water_gdf: GeoDataFrame of water features in local coordinates
                   (already projected and clipped)
    """
    from shapely.geometry import Polygon, MultiPolygon
    from shapely import Point as ShapelyPoint
    import shapely

    verts = terrain_mesh.vertices
    total_carved = 0
    min_poly_area = 500.0  # only carve polygons > 500 m2

    for _, row in water_gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue

        polygons = []
        if isinstance(geom, Polygon):
            polygons = [geom]
        elif isinstance(geom, MultiPolygon):
            polygons = list(geom.geoms)
        else:
            continue

        for poly in polygons:
            if poly.area < min_poly_area:
                continue

            # Compute water surface Z from boundary terrain heights
            ext = np.array(poly.exterior.coords)
            bz = sample_terrain_z(terrain_mesh, ext[:, 0], ext[:, 1])
            valid = ~np.isnan(bz)
            if valid.sum() < 3:
                continue
            water_z = float(np.nanpercentile(bz[valid], 25))

            # Fast bounds pre-filter
            bnd = poly.bounds  # (minx, miny, maxx, maxy)
            in_box = ((verts[:, 0] >= bnd[0]) & (verts[:, 0] <= bnd[2]) &
                      (verts[:, 1] >= bnd[1]) & (verts[:, 1] <= bnd[3]))
            candidates = np.where(in_box)[0]
            if len(candidates) == 0:
                continue

            # Vectorised point-in-polygon via shapely 2.x
            pts = shapely.points(verts[candidates, 0], verts[candidates, 1])
            inside = shapely.contains(poly, pts)

            carve_idx = candidates[inside]
            # Push down vertices that are above water surface
            above = verts[carve_idx, 2] > water_z - 1.0
            carve_idx = carve_idx[above]
            if len(carve_idx) == 0:
                continue

            verts[carve_idx, 2] = water_z - 1.0  # 1 m below water surface
            total_carved += len(carve_idx)

    if total_carved > 0:
        terrain_mesh.vertices = verts
        # Recolor carved vertices (use low-elevation color)
        new_colors = _elevation_to_colors(terrain_mesh.vertices[:, 2])
        terrain_mesh.visual.vertex_colors = new_colors
        logger.info(f"Carved terrain for water: {total_carved} vertices pushed down")

    return total_carved


def sample_terrain_z(mesh: trimesh.Trimesh, x: np.ndarray,
                     y: np.ndarray) -> np.ndarray:
    """Sample Z (elevation) values from terrain mesh at given X,Y positions.

    Uses cKDTree nearest-neighbor interpolation instead of ray casting,
    which is faster and avoids access violations on meshes with degenerate faces.
    """
    if len(x) == 0:
        return np.array([])

    from scipy.spatial import cKDTree

    tree = cKDTree(mesh.vertices[:, :2])
    k = min(8, len(mesh.vertices))
    dists, idxs = tree.query(np.column_stack([x, y]), k=k)
    if k == 1:
        idxs = idxs[:, np.newaxis]
    return mesh.vertices[idxs, 2].max(axis=1)


def build_terrain_with_base(terrain_mesh: trimesh.Trimesh,
                            base_thickness_m: float,
                            wall_color: tuple = None,
                            terrain_colors: dict = None) -> trimesh.Trimesh:
    """Build a watertight solid by adding walls and a flat bottom to the terrain.

    Args:
        terrain_mesh: open terrain surface mesh
        base_thickness_m: thickness of base below the lowest terrain point
        wall_color: RGBA tuple for walls and bottom (default: PRINT_COLORS["base_wall"])
        terrain_colors: dict with "terrain_low" and "terrain_high" keys for
                       re-coloring the terrain surface (None keeps current colors)

    Returns:
        Watertight trimesh with terrain top + walls + flat bottom
    """
    from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.mesh_repair import validate_and_repair_mesh

    if wall_color is None:
        from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.config import PRINT_COLORS
        wall_color = PRINT_COLORS["base_wall"]

    verts = terrain_mesh.vertices
    bottom_z = float(verts[:, 2].min()) - base_thickness_m

    # --- Find boundary edges (edges belonged to exactly one face) ---
    boundary_loop = _get_boundary_loop(terrain_mesh)
    if boundary_loop is None:
        logger.warning("Could not extract terrain boundary; returning surface only")
        return terrain_mesh

    n_top = len(verts)
    n_boundary = len(boundary_loop)

    # --- Build wall vertices and faces ---
    # For each boundary vertex, create a corresponding bottom vertex
    wall_bottom_verts = verts[boundary_loop].copy()
    wall_bottom_verts[:, 2] = bottom_z  # flatten to base level

    # Wall faces: quad strip connecting top boundary to bottom boundary
    # Bottom vertex indices start at n_top
    wall_faces = []
    for i in range(n_boundary):
        j = (i + 1) % n_boundary
        top_i = boundary_loop[i]
        top_j = boundary_loop[j]
        bot_i = n_top + i
        bot_j = n_top + j
        # Two triangles per quad (winding: outward)
        wall_faces.append([top_i, top_j, bot_j])
        wall_faces.append([top_i, bot_j, bot_i])

    wall_faces = np.array(wall_faces, dtype=np.int64)

    # --- Build bottom cap ---
    # Project boundary vertices to 2D (X, Y) at bottom_z, triangulate
    bottom_pts_2d = wall_bottom_verts[:, :2].astype(np.float64)

    from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.water import _earcut_triangulate

    # Try earcut first, filter degenerate faces
    bottom_faces = _earcut_triangulate(bottom_pts_2d, [len(bottom_pts_2d)])

    if bottom_faces is None or len(bottom_faces) == 0:
        # Fallback: Use constrained Delaunay-style triangulation
        # Create interior points to avoid radial pattern from single centroid
        import random
        
        # Compute bounding box of boundary points
        min_x, min_y = bottom_pts_2d.min(axis=0)
        max_x, max_y = bottom_pts_2d.max(axis=0)
        
        # Create a grid of interior points (avoid edges)
        n_boundary = len(bottom_pts_2d)
        interior_points = []
        interior_idx_map = []  # Track which indices are interior
        
        # Add a few interior points in a grid pattern
        grid_size = max(2, int(np.sqrt(n_boundary / 50)))  # Scale with boundary complexity
        for i in range(1, grid_size):
            for j in range(1, grid_size):
                x = min_x + (max_x - min_x) * i / grid_size
                y = min_y + (max_y - min_y) * j / grid_size
                pt = np.array([x, y])
                
                # Check if point is roughly inside the boundary (simple bounding circle check)
                center = bottom_pts_2d.mean(axis=0)
                radius = np.max(np.linalg.norm(bottom_pts_2d - center, axis=1))
                if np.linalg.norm(pt - center) < radius * 0.9:  # 90% of radius to stay inside
                    interior_points.append([x, y])
        
        # Combine boundary and interior points
        if interior_points:
            all_pts = np.vstack([bottom_pts_2d, np.array(interior_points)])
            n_total = len(all_pts)
            
            # Try earcut again with interior points
            # First, create a polygon with holes: outer boundary + inner "holes" that are actually interior regions
            bottom_faces = _earcut_triangulate(all_pts, [n_total])
            
            if bottom_faces is not None and len(bottom_faces) > 0:
                # Validate no degenerate faces
                v0_temp = wall_bottom_verts[bottom_faces[:, 0]]
                v1_temp = wall_bottom_verts[bottom_faces[:, 1]]
                v2_temp = wall_bottom_verts[bottom_faces[:, 2]]
                cross_temp = np.cross(v1_temp - v0_temp, v2_temp - v0_temp)
                areas_temp = np.sqrt(np.sum(cross_temp ** 2, axis=1)) * 0.5
                
                good = areas_temp > 1e-10
                if not np.all(good):
                    bottom_faces = bottom_faces[good]
                
                # If still valid, add interior vertices to wall_bottom_verts
                if len(bottom_faces) > 0:
                    interior_verts = np.array([[p[0], p[1], bottom_z] for p in interior_points])
                    wall_bottom_verts = np.vstack([wall_bottom_verts, interior_verts])
                    n_boundary = n_total  # Now includes all points
            else:
                # Final fallback: centroid fan triangulation
                centroid_2d = bottom_pts_2d.mean(axis=0)
                angles = np.arctan2(bottom_pts_2d[:, 1] - centroid_2d[1],
                                   bottom_pts_2d[:, 0] - centroid_2d[0])
                sorted_indices = np.argsort(angles)
                
                bottom_faces_list = []
                n_sorted = len(sorted_indices)
                for i in range(n_sorted):
                    j = (i + 1) % n_sorted
                    bottom_faces_list.append([n_boundary, sorted_indices[i], sorted_indices[j]])
                
                bottom_faces = np.array(bottom_faces_list, dtype=np.int64)
                centroid_3d = np.array([[centroid_2d[0], centroid_2d[1], bottom_z]])
                wall_bottom_verts = np.vstack([wall_bottom_verts, centroid_3d])
                n_boundary += 1
        else:
            # Simple case: too small for interior points, use centroid fan
            centroid_2d = bottom_pts_2d.mean(axis=0)
            angles = np.arctan2(bottom_pts_2d[:, 1] - centroid_2d[1],
                               bottom_pts_2d[:, 0] - centroid_2d[0])
            sorted_indices = np.argsort(angles)
            
            bottom_faces_list = []
            n_sorted = len(sorted_indices)
            for i in range(n_sorted):
                j = (i + 1) % n_sorted
                bottom_faces_list.append([n_boundary, sorted_indices[i], sorted_indices[j]])
            
            bottom_faces = np.array(bottom_faces_list, dtype=np.int64)
            centroid_3d = np.array([[centroid_2d[0], centroid_2d[1], bottom_z]])
            wall_bottom_verts = np.vstack([wall_bottom_verts, centroid_3d])
            n_boundary += 1
    else:
        # Check if earcut produced degenerate faces and filter them
        v0_temp = wall_bottom_verts[bottom_faces[:, 0]]
        v1_temp = wall_bottom_verts[bottom_faces[:, 1]]
        v2_temp = wall_bottom_verts[bottom_faces[:, 2]]
        cross_temp = np.cross(v1_temp - v0_temp, v2_temp - v0_temp)
        areas_temp = np.sqrt(np.sum(cross_temp ** 2, axis=1)) * 0.5

        good = areas_temp > 1e-10
        if not np.all(good):
            n_bad = int(np.sum(~good))
            logger.info(f"Bottom cap: removing {n_bad} degenerate faces from earcut")
            bottom_faces = bottom_faces[good]

    # Bottom face indices reference the wall_bottom_verts which start at n_top
    bottom_faces_offset = bottom_faces + n_top
    # Reverse winding so bottom faces point downward
    bottom_faces_offset = bottom_faces_offset[:, ::-1]

    # --- Combine everything ---
    all_verts = np.vstack([verts, wall_bottom_verts])
    all_faces = np.vstack([terrain_mesh.faces, wall_faces, bottom_faces_offset])

    # --- Colors ---
    # Re-color terrain surface if requested
    if terrain_colors is not None:
        top_colors = _elevation_to_colors_custom(
            verts[:, 2],
            terrain_colors["terrain_low"],
            terrain_colors["terrain_high"]
        )
    else:
        top_colors = np.array(terrain_mesh.visual.vertex_colors[:, :4])

    wall_color_arr = np.tile(wall_color, (n_boundary, 1)).astype(np.uint8)
    all_colors = np.vstack([top_colors, wall_color_arr])

    solid = trimesh.Trimesh(
        vertices=all_verts,
        faces=all_faces,
        vertex_colors=all_colors,
        process=False
    )
    solid.fix_normals()

    # Validate and repair to ensure watertight
    solid = validate_and_repair_mesh(solid, name="terrain_base",
                                      fix_watertight=True, fix_normals=True,
                                      fix_degenerate=True, fix_duplicates=True,
                                      fix_inverted=True)

    if not solid.is_watertight:
        logger.warning("Terrain base mesh is still not fully watertight after repair")
    else:
        logger.info("Terrain base mesh is watertight")

    logger.info(f"Terrain+base: {len(solid.vertices)} verts, {len(solid.faces)} faces")
    return solid


def _get_boundary_loop(mesh: trimesh.Trimesh):
    """Extract ordered boundary vertex loop from an open surface mesh.

    Returns:
        numpy array of UNIQUE vertex indices forming a closed boundary loop,
        or None if extraction fails.
    """
    # Find edges that appear in exactly one face (= boundary)
    edges = mesh.edges_sorted
    # Count edge occurrences
    edge_tuples = [tuple(e) for e in edges]
    from collections import Counter, defaultdict
    edge_counts = Counter(edge_tuples)
    boundary_edges = [e for e, c in edge_counts.items() if c == 1]

    if not boundary_edges:
        return None

    # Build adjacency for boundary vertices
    adj = defaultdict(list)
    for a, b in boundary_edges:
        adj[a].append(b)
        adj[b].append(a)

    # Walk ALL boundary loops
    visited_edges = set()
    all_boundary_loops = []

    for start_edge in boundary_edges:
        start = start_edge[0]

        # Check if this edge was already visited
        if tuple(sorted(start_edge)) in visited_edges:
            continue

        # Walk the boundary
        loop = []
        current = start
        prev = None

        while True:
            loop.append(current)
            neighbors = [n for n in adj[current] if n != prev]

            if not neighbors:
                break

            next_v = neighbors[0]
            edge = tuple(sorted([current, next_v]))

            if edge in visited_edges:
                break

            visited_edges.add(edge)
            prev = current
            current = next_v

            # Closed loop - back to start
            if current == start:
                break

        # Only keep loops with at least 3 unique vertices
        if len(loop) >= 3:
            all_boundary_loops.append(np.array(loop, dtype=np.int64))

    # Return the longest loop (main outer boundary)
    if all_boundary_loops:
        longest = max(all_boundary_loops, key=len)
        return longest

    return None


def _elevation_to_colors_custom(elevations: np.ndarray,
                                low_color: tuple,
                                high_color: tuple) -> np.ndarray:
    """Map elevation values to a custom color gradient."""
    low = np.array(low_color, dtype=np.float64)
    high = np.array(high_color, dtype=np.float64)

    e_min = np.nanmin(elevations)
    e_max = np.nanmax(elevations)

    if e_max - e_min < 1e-3:
        return np.tile(low, (len(elevations), 1)).astype(np.uint8)

    t = (elevations - e_min) / (e_max - e_min)
    t = np.clip(t, 0, 1)

    colors = np.outer(1 - t, low) + np.outer(t, high)
    return np.clip(colors, 0, 255).astype(np.uint8)
```

### 核心算法说明

#### 1. 规则网格转三角面
```python
# 每个网格单元生成2个三角形
# 顶点索引：
# tl -- tr
# |     |
# bl -- br

faces.append([tl, bl, tr])  # 左下三角形
faces.append([tr, bl, br])  # 右上三角形
```

#### 2. 高程到颜色映射
```python
# 归一化高程到 [0, 1]
t = (elevations - e_min) / (e_max - e_min)
t = np.clip(t, 0, 1)

# 压缩梯度（地形颜色变化更 subtle）
t = t * 0.4

# 线性插值
colors = np.outer(1 - t, low_color) + np.outer(t, high_color)
```

#### 3. 地形镂空（Carve Water Basin）
```python
# 计算水面高程（取边界地形的 25 百分位）
water_z = float(np.nanpercentile(bz[valid], 25))

# 将水面内的地形顶点推到水面以下 1 米
verts[carve_idx, 2] = water_z - 1.0
```

#### 4. 封闭网格生成（Terrain + Base）
```python
# 1. 提取地形边界（只属于一个面的边）
boundary_edges = [e for e, c in edge_counts.items() if c == 1]

# 2. 生成墙壁（边界顶点到底面的垂直面）
wall_faces.append([top_i, top_j, bot_j])
wall_faces.append([top_i, bot_j, bot_i])

# 3. 底面三角化（EarCut 算法 + 退化面过滤）
bottom_faces = _earcut_triangulate(bottom_pts_2d, [len(bottom_pts_2d)])

# 4. 组合所有网格
all_verts = np.vstack([verts, wall_bottom_verts])
all_faces = np.vstack([terrain_mesh.faces, wall_faces, bottom_faces_offset])
```

#### 5. Z 值采样（cKDTree 最近邻）
```python
# 使用 KD 树快速查找最近 K 个顶点
tree = cKDTree(mesh.vertices[:, :2])
dists, idxs = tree.query(np.column_stack([x, y]), k=k)

# 取最大值（避免采样到空洞）
return mesh.vertices[idxs, 2].max(axis=1)
```

---

## 三、数据流总览

```
┌─────────────────┐
│  输入：bbox      │
│  (south,west,   │
│   north,east)   │
└────────┬────────┘
         │
         ▼
┌─────────────────────────────────┐
│  fetch_elevation_grid()         │
│  ┌──────────────────────────┐   │
│  │ 1. 检查缓存               │   │
│  │ 2. SRTM HGT 瓦片下载      │   │
│  │    (1-4 个瓦片)           │   │
│  │ 3. 采样生成高程网格       │   │
│  │ 4. 缺失值插值填充         │   │
│  │ 5. 高斯平滑              │   │
│  │ 6. 缓存结果              │   │
│  └──────────────────────────┘   │
└────────┬────────────────────────┘
         │
         ▼
┌─────────────────────────────────┐
│  输出：elevation_grid           │
│  形状：(rows, cols)             │
│  单位：米                        │
│  NaN 已填充                     │
└────────┬────────────────────────┘
         │
         ▼
┌─────────────────────────────────┐
│  build_terrain_mesh()           │
│  ┌──────────────────────────┐   │
│  │ 1. 生成顶点 (X,Y,Z)       │   │
│  │ 2. 生成三角面 (2/cell)    │   │
│  │ 3. 高程映射颜色           │   │
│  │ 4. 网格简化 (可选)        │   │
│  └──────────────────────────┘   │
└────────┬────────────────────────┘
         │
         ▼
┌─────────────────────────────────┐
│  build_terrain_with_base()      │
│  ┌──────────────────────────┐   │
│  │ 1. 提取地形边界           │   │
│  │ 2. 生成墙壁              │   │
│  │ 3. 底面三角化 (EarCut)    │   │
│  │ 4. 网格修复 (watertight)  │   │
│  └──────────────────────────┘   │
└────────┬────────────────────────┘
         │
         ▼
┌─────────────────┐
│  输出：地形网格  │
│  (封闭、水密)    │
│  带顶点颜色      │
└─────────────────┘
```

## 四、关键配置参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `ELEVATION_SMOOTHING_SIGMA` | 1.0 | 高斯平滑标准差 |
| `CACHE_TTL_SECONDS` | 86400 | 缓存过期时间（24小时） |
| `_OPEN_ELEVATION_BATCH_SIZE` | 200 | API 批量查询大小 |
| `TERRAIN_GRID` | 按面积分类 | 地形网格分辨率 |
| `DECIMATION_TARGETS` | 按面积分类 | 网格简化目标面数 |

## 五、性能优化要点

1. **缓存机制**：高程网格缓存为 `.npy` 文件，避免重复下载
2. **向量采样**：SRTM 采样使用 NumPy 向量化操作，避免逐点循环
3. **cKDTree 采样**：Z 值采样使用 KD 树，复杂度 O(log n)
4. **网格简化**：大面积区域使用 quadric decimation 降面
5. **进度条**：批量 API 请求和 HGT 下载显示进度
