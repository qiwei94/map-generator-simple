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
import shutil
import sys
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Default cache directory: project_root/data/height_cache/
_DEFAULT_CACHE_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "data", "height_cache"
)


def _is_valid_parquet_file(path: str) -> bool:
    """Cheaply reject truncated downloads without loading PyArrow."""

    try:
        if os.path.getsize(path) < 12:
            return False
        with open(path, "rb") as stream:
            header = stream.read(4)
            stream.seek(-4, os.SEEK_END)
            footer = stream.read(4)
        return header == b"PAR1" and footer == b"PAR1"
    except OSError:
        return False


def _resolve_overture_cli() -> Optional[str]:
    """Return an executable Overture CLI without assuming an activated venv."""
    override = os.environ.get("OVERTUREMAPS_BIN", "").strip()
    candidates = []
    if override:
        candidates.append(override)

    python_dir = os.path.dirname(sys.executable)
    candidates.extend([
        os.path.join(python_dir, "overturemaps"),
        os.path.join(python_dir, "overturemaps.exe"),
        shutil.which("overturemaps"),
        os.path.expanduser("~/Library/Python/3.9/bin/overturemaps"),
        "/opt/homebrew/bin/overturemaps",
        "/usr/local/bin/overturemaps",
    ])
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return os.path.realpath(candidate)
    return None


def _find_overture_cache(
    bbox_wgs84: Tuple[float, float, float, float],
    cache_dir: str = _DEFAULT_CACHE_DIR,
) -> Optional[str]:
    """Find a cached Overture parquet file that covers the given bbox.

    Matches by parsing overture_{lat:.2f}_{lon:.2f}.parquet filenames
    and checking if the center coordinates fall within the query bbox.
    Unidentified legacy files are never reused across regions: doing so can
    suppress the correct download and silently attach the wrong city dataset.

    Returns path to parquet file, or None if no cache found.
    """
    if not os.path.isdir(cache_dir):
        return None

    south, west, north, east = bbox_wgs84
    all_parquets = sorted(os.listdir(cache_dir))

    # Prefer files matching the bbox by coordinates in filename
    for fname in all_parquets:
        if fname.startswith("overture_") and fname.endswith(".parquet"):
            parts = fname.replace("overture_", "").replace(".parquet", "").split("_")
            if len(parts) >= 2:
                try:
                    lat = float(parts[0])
                    lon = float(parts[1])
                    if south <= lat <= north and west <= lon <= east:
                        path = os.path.join(cache_dir, fname)
                        if _is_valid_parquet_file(path):
                            logger.info(
                                f"Overture cache hit (bbox match): {fname}")
                            return path
                        logger.warning(
                            "Ignoring truncated Overture cache: %s", path)
                except ValueError:
                    continue

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

    if _is_valid_parquet_file(output_path):
        logger.info(f"Overture file already exists: {output_path}")
        return output_path

    cli_path = _resolve_overture_cli()
    if cli_path is None:
        logger.warning(
            "Overture height enrichment unavailable: install the overturemaps "
            "CLI or set OVERTUREMAPS_BIN"
        )
        return None

    logger.info(f"Downloading Overture buildings: bbox={bbox_str} → {output_path}")
    tmp_path = output_path + f".tmp{os.getpid()}"
    try:
        result = subprocess.run(
            [cli_path, "download",
             f"--bbox={bbox_str}",
             "--type=building",
             "-f", "geoparquet",
             "-o", tmp_path],
            capture_output=True, text=True, timeout=600,
        )
        if result.returncode == 0 and _is_valid_parquet_file(tmp_path):
            os.replace(tmp_path, output_path)
            size_mb = os.path.getsize(output_path) / (1024 * 1024)
            logger.info(f"Overture download OK: {size_mb:.1f} MB")
            return output_path
        else:
            logger.warning(f"Overture download failed: {result.stderr[:500]}")
            return None
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.warning(f"Overture download error: {e}")
        return None
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


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

    env_auto_download = os.environ.get("OVERTURE_AUTO_DOWNLOAD", "").strip()
    if env_auto_download.lower() in {"0", "false", "no", "off"}:
        auto_download = False

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
            columns=["geometry", "height", "num_floors", "names"],
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
    # Overture 'num_floors' → height = num_floors * 3.5 (fallback when height is null)
    ov_heights_raw = overture_gdf["height"] if "height" in overture_gdf.columns else pd.Series(np.nan, index=overture_gdf.index)
    ov_heights = ov_heights_raw.copy()
    if "num_floors" in overture_gdf.columns:
        ov_floors = overture_gdf["num_floors"]
        has_floors = ov_floors.notna() & (ov_floors > 0)
        no_height = ov_heights.isna() | (ov_heights <= 0)
        fill = has_floors & no_height
        ov_heights[fill] = ov_floors[fill] * 3.5

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
    result_names = pd.Series(np.nan, index=osm_gdf.index, dtype=object)

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
