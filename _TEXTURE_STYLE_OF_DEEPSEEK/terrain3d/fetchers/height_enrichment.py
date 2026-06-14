"""Overture Maps height enrichment — spatial join OSM buildings with Overture AI heights.

Priority in the height estimation chain:
    1. OSM height tag
    2. OSM building:levels × 3.5m
    3. nDSM satellite-derived height
    4. **Overture Maps AI-estimated height** (this module)
    5. Area-based proxy fallback

Usage:
    from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.height_enrichment import (
        load_overture_heights,
    )

    overture_heights, overture_names = load_overture_heights(
        osm_gdf, bbox_wgs84=(30.13, 120.01, 30.36, 120.29))
"""

from __future__ import annotations

import logging
import os
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Default cache directory: project_root/data/height_cache/
_DEFAULT_CACHE_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "data", "height_cache"
)


def _find_overture_cache(
    bbox_wgs84: Tuple[float, float, float, float],
    cache_dir: str = _DEFAULT_CACHE_DIR,
) -> Optional[str]:
    """Find a cached Overture parquet file that covers the given bbox.

    Uses a simple filename convention: {name}.parquet
    For production, would check cache_manifest.json for bbox coverage.

    Returns path to parquet file, or None if no cache found.
    """
    if not os.path.isdir(cache_dir):
        return None

    # Strategy: find any parquet file in the cache dir
    # (For single-city use, there's typically one file per region)
    for fname in sorted(os.listdir(cache_dir)):
        if fname.endswith(".parquet"):
            path = os.path.join(cache_dir, fname)
            logger.info(f"Overture cache hit: {path}")
            return path

    return None


def _download_overture(
    bbox_wgs84: Tuple[float, float, float, float],
    cache_dir: str = _DEFAULT_CACHE_DIR,
    buffer_km: float = 2.0,
) -> Optional[str]:
    """Download Overture building data for the given bbox.

    Uses the overturemaps CLI tool.
    Returns path to downloaded parquet file, or None on failure.
    """
    import subprocess

    south, west, north, east = bbox_wgs84
    # Expand bbox by buffer_km (approx degrees)
    buf_deg = buffer_km / 111.0
    bbox_str = f"{west - buf_deg},{south - buf_deg},{east + buf_deg},{north + buf_deg}"

    os.makedirs(cache_dir, exist_ok=True)

    # Generate filename from bbox center
    center_lat = (south + north) / 2
    center_lon = (west + east) / 2
    fname = f"overture_{center_lat:.2f}_{center_lon:.2f}.parquet"
    output_path = os.path.join(cache_dir, fname)

    if os.path.exists(output_path):
        logger.info(f"Overture file already exists: {output_path}")
        return output_path

    # Find overturemaps CLI
    cli_path = "overturemaps"
    for p in [
        os.path.expanduser("~/Library/Python/3.9/bin/overturemaps"),
        "/usr/local/bin/overturemaps",
    ]:
        if os.path.exists(p):
            cli_path = p
            break

    logger.info(f"Downloading Overture buildings: bbox={bbox_str} → {output_path}")
    try:
        result = subprocess.run(
            [cli_path, "download",
             f"--bbox={bbox_str}",
             "--type=building",
             "-f", "geoparquet",
             "-o", output_path],
            capture_output=True, text=True, timeout=600,
        )
        if result.returncode == 0 and os.path.exists(output_path):
            size_mb = os.path.getsize(output_path) / (1024 * 1024)
            logger.info(f"Overture download OK: {size_mb:.1f} MB")
            return output_path
        else:
            logger.warning(f"Overture download failed: {result.stderr[:500]}")
            return None
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.warning(f"Overture download error: {e}")
        return None


def load_overture_heights(
    osm_gdf,
    bbox_wgs84: Tuple[float, float, float, float],
    cache_dir: str = _DEFAULT_CACHE_DIR,
    auto_download: bool = True,
) -> Tuple[Optional[pd.Series], Optional[pd.Series]]:
    """Load Overture building heights and spatially join onto OSM buildings.

    Args:
        osm_gdf: OSM buildings GeoDataFrame (any CRS, will be reprojected).
        bbox_wgs84: (south, west, north, east) bounding box in WGS84.
        cache_dir: Directory for cached Overture parquet files.
        auto_download: If True, download Overture data when cache misses.

    Returns:
        (overture_heights, overture_names) — two pd.Series aligned to osm_gdf.index.
        overture_heights: AI-estimated heights in meters (NaN = no match).
        overture_names: Overture building names (NaN = no match).
        Returns (None, None) if Overture data is unavailable.
    """
    if osm_gdf is None or len(osm_gdf) == 0:
        return None, None

    # Step 1: Find or download cache
    parquet_path = _find_overture_cache(bbox_wgs84, cache_dir)
    if parquet_path is None and auto_download:
        parquet_path = _download_overture(bbox_wgs84, cache_dir)

    if parquet_path is None:
        logger.info("Overture height enrichment: no data available, skipping")
        return None, None

    # Step 2: Load Overture data (only needed columns)
    try:
        import geopandas as gpd
        overture_gdf = gpd.read_parquet(
            parquet_path,
            columns=["geometry", "height", "names"],
        )
    except Exception as e:
        logger.warning(f"Failed to read Overture parquet: {e}")
        return None, None

    if overture_gdf.empty:
        logger.info("Overture parquet is empty")
        return None, None

    logger.info(f"Overture: loaded {len(overture_gdf)} buildings from {parquet_path}")

    # Step 3: Extract height and name from Overture schema
    # Overture 'height' is a numeric column (meters)
    # Overture 'names' is a struct with 'primary' sub-field
    ov_heights = overture_gdf["height"] if "height" in overture_gdf.columns else pd.Series(np.nan, index=overture_gdf.index)

    # Extract names.primary from the names struct column
    ov_names = pd.Series(np.nan, index=overture_gdf.index)
    if "names" in overture_gdf.columns:
        try:
            names_col = overture_gdf["names"]
            if hasattr(names_col, "struct"):
                # GeoPandas struct accessor
                ov_names = names_col.struct.field("primary")
            else:
                # Fallback: try to extract from dict-like values
                ov_names = names_col.apply(
                    lambda x: x.get("primary", np.nan) if isinstance(x, dict) else np.nan
                )
        except Exception:
            pass  # names extraction is best-effort

    # Step 4: Ensure both GDFs are in WGS84 for spatial join
    osm_wgs84 = osm_gdf.to_crs(epsg=4326) if osm_gdf.crs and osm_gdf.crs.to_epsg() != 4326 else osm_gdf
    ov_wgs84 = overture_gdf.to_crs(epsg=4326) if overture_gdf.crs and overture_gdf.crs.to_epsg() != 4326 else overture_gdf

    # Assign temp columns for the join
    ov_wgs84 = ov_wgs84[["geometry"]].copy()
    ov_wgs84["_ov_height"] = ov_heights.values
    ov_wgs84["_ov_name"] = ov_names.values

    # Filter out rows without height (no point joining)
    ov_with_height = ov_wgs84[ov_wgs84["_ov_height"].notna() & (ov_wgs84["_ov_height"] > 0)]

    if ov_with_height.empty:
        logger.info("Overture: no buildings with valid height")
        return None, None

    # Step 5: Spatial join (intersects)
    try:
        joined = osm_wgs84[["geometry"]].sjoin(
            ov_with_height, how="left", predicate="intersects"
        )
    except Exception as e:
        logger.warning(f"Overture spatial join failed: {e}")
        return None, None

    # Handle duplicate matches (one OSM building intersects multiple Overture)
    # Keep the first match (largest overlap would be ideal but expensive)
    joined = joined[~joined.index.duplicated(keep="first")]

    # Step 6: Align result to original osm_gdf index
    result_heights = pd.Series(np.nan, index=osm_gdf.index)
    result_names = pd.Series(np.nan, index=osm_gdf.index)

    common_idx = joined.index.intersection(osm_gdf.index)
    if len(common_idx) > 0:
        result_heights.loc[common_idx] = joined.loc[common_idx, "_ov_height"].values
        result_names.loc[common_idx] = joined.loc[common_idx, "_ov_name"].values

    n_matched = result_heights.notna().sum()
    n_total = len(osm_gdf)
    coverage = n_matched / n_total * 100 if n_total > 0 else 0
    logger.info(
        f"Overture height enrichment: {n_matched}/{n_total} buildings matched "
        f"({coverage:.1f}% coverage)"
    )

    return result_heights, result_names
