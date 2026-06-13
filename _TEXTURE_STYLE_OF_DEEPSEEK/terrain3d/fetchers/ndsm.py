"""Normalized Digital Surface Model (nDSM) for building height estimation.

nDSM = DSM (surface elevation including buildings) - DEM (bare earth)
Gives approximate building heights from satellite-derived elevation data.

v1: Copernicus DEM GLO-30 (DSM) - SRTM (DEM proxy). Both already cached locally.
"""

import logging
import numpy as np
import pandas as pd
from shapely.geometry import Point

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
    """Sample building heights from nDSM grid using footprint P90.

    For each building polygon, rasterizes the footprint onto the nDSM grid
    and takes the given percentile of covered pixel values.

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

    rows, cols = ndsm_grid.shape
    lat_res = (north - south) / rows
    lon_res = (east - west) / cols

    heights = pd.Series(np.nan, index=gdf.index)

    n_sampled = 0
    n_valid = 0

    for idx, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue

        bounds = geom.bounds  # (minx, miny, maxx, maxy) = (lon_min, lat_min, lon_max, lat_max)
        lon_min, lat_min, lon_max, lat_max = bounds

        # Grid cell indices for bounding box
        r_min = int((lat_min - south) / lat_res)
        r_max = int((lat_max - south) / lat_res)
        c_min = int((lon_min - west) / lon_res)
        c_max = int((lon_max - west) / lon_res)

        r_min = max(0, r_min)
        r_max = min(rows - 1, r_max)
        c_min = max(0, c_min)
        c_max = min(cols - 1, c_max)

        if r_min > r_max or c_min > c_max:
            continue

        # Collect pixel values within the building footprint
        values = []

        if r_min == r_max and c_min == c_max:
            # Single pixel — just take the value
            values.append(ndsm_grid[r_min, c_min])
        else:
            # Check which pixel centers fall inside the polygon
            for r in range(r_min, r_max + 1):
                lat_center = south + (r + 0.5) * lat_res
                for c in range(c_min, c_max + 1):
                    lon_center = west + (c + 0.5) * lon_res
                    if geom.contains(Point(lon_center, lat_center)):
                        values.append(ndsm_grid[r, c])

        if not values:
            # No pixel center inside polygon — sample at centroid
            centroid = geom.centroid
            cr = int((centroid.y - south) / lat_res)
            cc = int((centroid.x - west) / lon_res)
            cr = max(0, min(rows - 1, cr))
            cc = max(0, min(cols - 1, cc))
            values.append(ndsm_grid[cr, cc])

        n_sampled += 1
        val = float(np.percentile(values, percentile))

        if min_height <= val <= max_height:
            heights.at[idx] = val
            n_valid += 1

    logger.info(f"nDSM sampling: {n_sampled} buildings sampled, "
                f"{n_valid} valid heights ({n_valid/max(n_sampled,1)*100:.1f}%)")

    return heights
