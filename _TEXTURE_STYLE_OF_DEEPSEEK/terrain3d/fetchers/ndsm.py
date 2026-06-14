"""Normalized Digital Surface Model (nDSM) for building height estimation.

nDSM = DSM (surface elevation including buildings) - DEM (bare earth)
Gives approximate building heights from satellite-derived elevation data.

v1: Copernicus DEM GLO-30 (DSM) - SRTM (DEM proxy). Both already cached locally.
"""

import logging
import numpy as np
import pandas as pd
import shapely

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.config import (
    NDSM_MIN_HEIGHT_M,
    NDSM_MAX_HEIGHT_M,
    NDSM_SAMPLE_PERCENTILE,
)

logger = logging.getLogger(__name__)


def compute_ndsm_grid(south: float, west: float, north: float, east: float,
                      rows: int, cols: int):
    """Compute nDSM grid: Copernicus DSM - SRTM DEM.

    Returns 2D numpy array (rows x cols) of meters above ground,
    or None if either data source is unavailable.
    """
    from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.elevation import (
        _fetch_elevation_grid_from_cop30,
        _fetch_elevation_grid_from_srtm,
    )

    dsm_grid = _fetch_elevation_grid_from_cop30(south, west, north, east, rows, cols)
    if dsm_grid is None:
        logger.warning("nDSM: Copernicus DEM tiles not available locally")
        return None

    dem_grid = _fetch_elevation_grid_from_srtm(south, west, north, east, rows, cols)
    if dem_grid is None or np.all(np.isnan(dem_grid)):
        logger.warning("nDSM: SRTM tiles not available")
        return None

    ndsm = dsm_grid - dem_grid
    ndsm = np.nan_to_num(ndsm, nan=0.0)
    ndsm = np.clip(ndsm, 0, None)

    return ndsm


def sample_building_heights_from_ndsm(
    gdf,
    ndsm_grid: np.ndarray,
    south: float, west: float, north: float, east: float,
    min_height: float = None,
    max_height: float = None,
    percentile: float = None,
) -> pd.Series:
    """Sample building heights from nDSM grid using footprint percentile.

    Vectorized implementation — no iterrows().
    Uses shapely 2.0 vectorized contains() for bulk pixel-in-polygon tests,
    replacing the per-pixel Python loop with a single C-level call per building.

    Args:
        gdf: Buildings GeoDataFrame in WGS84 (EPSG:4326).
        ndsm_grid: 2D array from compute_ndsm_grid().
        south, west, north, east: WGS84 bbox matching ndsm_grid.
        min_height: Minimum plausible height (below = noise). Defaults to config.
        max_height: Maximum plausible height (above = artifact). Defaults to config.
        percentile: Percentile to take within footprint. Defaults to config.

    Returns:
        pd.Series indexed like gdf. NaN where sampling failed or value implausible.
    """
    if min_height is None:
        min_height = NDSM_MIN_HEIGHT_M
    if max_height is None:
        max_height = NDSM_MAX_HEIGHT_M
    if percentile is None:
        percentile = NDSM_SAMPLE_PERCENTILE

    heights = pd.Series(np.nan, index=gdf.index)
    geoms = gdf.geometry.values  # shapely 2.0 GeometryArray

    # ── 1. Filter valid geometries (vectorized) ──
    valid_mask = shapely.is_valid(geoms) & ~shapely.is_empty(geoms)
    if not valid_mask.any():
        logger.info("nDSM sampling: no valid building geometries")
        return heights

    valid_indices = np.where(valid_mask)[0]
    valid_geoms = geoms[valid_mask]

    # ── 2. Pre-compute pixel-center meshgrid (once, shared across all buildings) ──
    rows, cols = ndsm_grid.shape
    lat_res = (north - south) / rows
    lon_res = (east - west) / cols

    pixel_lats = south + (np.arange(rows) + 0.5) * lat_res
    pixel_lons = west + (np.arange(cols) + 0.5) * lon_res
    grid_lons, grid_lats = np.meshgrid(pixel_lons, pixel_lats)
    # grid_lons[r, c] / grid_lats[r, c] = center of pixel (r, c)

    # ── 3. Batch-compute grid bounding-box indices (numpy, no loop) ──
    # shapely.bounds → (N, 4) array: [xmin, ymin, xmax, ymax]
    bounds_arr = shapely.bounds(valid_geoms)

    c_min_all = np.clip((bounds_arr[:, 0] - west) / lon_res, 0, cols - 1).astype(int)
    r_min_all = np.clip((bounds_arr[:, 1] - south) / lat_res, 0, rows - 1).astype(int)
    c_max_all = np.clip((bounds_arr[:, 2] - west) / lon_res, 0, cols - 1).astype(int)
    r_max_all = np.clip((bounds_arr[:, 3] - south) / lat_res, 0, rows - 1).astype(int)

    # Drop buildings whose grid bbox is degenerate
    grid_valid = (r_min_all <= r_max_all) & (c_min_all <= c_max_all)
    active_indices = valid_indices[grid_valid]
    active_geoms = valid_geoms[grid_valid]
    r_mins, r_maxs = r_min_all[grid_valid], r_max_all[grid_valid]
    c_mins, c_maxs = c_min_all[grid_valid], c_max_all[grid_valid]

    if len(active_indices) == 0:
        logger.info("nDSM sampling: no buildings overlap the nDSM grid")
        return heights

    # ── 4. Pre-compute centroids for fallback (vectorized) ──
    centroids = shapely.centroid(active_geoms)
    centroid_coords = shapely.get_coordinates(centroids)
    cr_cent = np.clip(
        ((centroid_coords[:, 1] - south) / lat_res).astype(int), 0, rows - 1
    )
    cc_cent = np.clip(
        ((centroid_coords[:, 0] - west) / lon_res).astype(int), 0, cols - 1
    )

    # ── 5. Per-building sampling (vectorized pixel-in-polygon via shapely 2.0) ──
    n_sampled = 0
    n_valid = 0

    for i, geom_idx in enumerate(active_indices):
        geom = active_geoms[i]
        r_min, r_max = r_mins[i], r_maxs[i]
        c_min, c_max = c_mins[i], c_maxs[i]

        # Slice the pre-computed meshgrid to this building's bbox
        sub_lons = grid_lons[r_min:r_max + 1, c_min:c_max + 1]
        sub_lats = grid_lats[r_min:r_max + 1, c_min:c_max + 1]

        # Single C-level call: test all pixel centers against this polygon
        # shapely.contains broadcasts: scalar polygon vs array of points
        pixel_pts = shapely.points(sub_lons.ravel(), sub_lats.ravel())
        inside_mask = shapely.contains(geom, pixel_pts).reshape(sub_lons.shape)

        values = ndsm_grid[r_min:r_max + 1, c_min:c_max + 1][inside_mask]
        values = values[~np.isnan(values)]

        # Fallback: centroid sample when no pixel center falls inside polygon
        if len(values) == 0:
            val = ndsm_grid[cr_cent[i], cc_cent[i]]
            if np.isnan(val):
                continue
            values = np.array([val])

        n_sampled += 1
        val = float(np.percentile(values, percentile))

        if min_height <= val <= max_height:
            heights.iloc[geom_idx] = val
            n_valid += 1

    logger.info(
        "nDSM sampling: %d buildings sampled, %d valid heights (%.1f%%)",
        n_sampled, n_valid, n_valid / max(n_sampled, 1) * 100,
    )

    return heights
