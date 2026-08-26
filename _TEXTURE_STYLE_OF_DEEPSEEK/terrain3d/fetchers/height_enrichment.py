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
_STORE_FILENAME = "building_heights.sqlite3"


def _open_height_store(cache_dir: str):
    from .building_height_store import BuildingHeightStore

    return BuildingHeightStore(os.path.join(cache_dir, _STORE_FILENAME))


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


def _extract_overture_candidates(overture_gdf):
    """Normalize Overture schema into the columns used by matching/storage."""
    import geopandas as gpd

    if overture_gdf is None or overture_gdf.empty:
        return gpd.GeoDataFrame(
            columns=["source_feature_id", "height", "num_floors", "name",
                     "geometry"],
            geometry="geometry", crs="EPSG:4326",
        )

    frame = overture_gdf.to_crs(epsg=4326) if (
        overture_gdf.crs and overture_gdf.crs.to_epsg() != 4326
    ) else overture_gdf.copy()

    heights = pd.to_numeric(
        frame["height"] if "height" in frame.columns else np.nan,
        errors="coerce",
    )
    floors = pd.to_numeric(
        frame["num_floors"] if "num_floors" in frame.columns else np.nan,
        errors="coerce",
    )
    if not isinstance(heights, pd.Series):
        heights = pd.Series(np.nan, index=frame.index, dtype=float)
    if not isinstance(floors, pd.Series):
        floors = pd.Series(np.nan, index=frame.index, dtype=float)
    fill = (heights.isna() | (heights <= 0)) & floors.notna() & (floors > 0)
    heights.loc[fill] = floors.loc[fill] * 3.5

    names = pd.Series(np.nan, index=frame.index, dtype=object)
    if "names" in frame.columns:
        try:
            names_col = frame["names"]
            if hasattr(names_col, "struct"):
                names = names_col.struct.field("primary")
            else:
                names = names_col.apply(
                    lambda value: value.get("primary", np.nan)
                    if isinstance(value, dict) else np.nan
                )
        except Exception:
            pass
    elif "name" in frame.columns:
        names = frame["name"]

    if "id" in frame.columns:
        feature_ids = frame["id"].astype(str)
    elif "source_feature_id" in frame.columns:
        feature_ids = frame["source_feature_id"].astype(str)
    else:
        # Geometry hashes are stable enough for legacy Parquet files that
        # predate retaining the Overture GERS id.
        import hashlib
        feature_ids = frame.geometry.apply(
            lambda geom: hashlib.sha256(bytes(geom.wkb)).hexdigest()
            if geom is not None and not geom.is_empty else ""
        )

    result = gpd.GeoDataFrame({
        "source_feature_id": feature_ids,
        "height": heights,
        "num_floors": floors,
        "name": names,
        "geometry": frame.geometry,
    }, geometry="geometry", crs="EPSG:4326")
    return result[
        result.geometry.notna()
        & ~result.geometry.is_empty
        & result["height"].notna()
        & (result["height"] > 0)
        & (result["height"] <= 2000)
    ].copy()


def _persist_overture_candidates(
    store, candidates, bbox_wgs84, *, raw_path: Optional[str] = None,
    source_release: str = "unknown",
) -> int:
    observations = (
        {
            "source_feature_id": row.source_feature_id,
            "height_m": row.height,
            "num_floors": row.num_floors,
            "height_kind": "overture_height",
            "name": row.name,
            "geometry": row.geometry,
        }
        for row in candidates.itertuples(index=False)
    )
    count = store.put_observations(
        observations, source="overture", source_release=source_release,
        source_url="https://overturemaps.org/",
    )
    # Empty responses are coverage too.  Registering them prevents a remote
    # provider from being queried again for the same contained bbox.
    store.register_coverage(
        "overture", bbox_wgs84, source_release=source_release,
        observation_count=count, raw_path=raw_path,
        metadata={"format": "geoparquet"},
    )
    return count


def _match_overture_candidates(osm_gdf, candidates):
    """Match footprints by maximum geometric overlap, never first-intersect."""
    import geopandas as gpd
    import shapely

    heights = pd.Series(np.nan, index=osm_gdf.index, dtype=float)
    names = pd.Series(np.nan, index=osm_gdf.index, dtype=object)
    if osm_gdf is None or osm_gdf.empty or candidates is None or candidates.empty:
        return heights, names

    osm = osm_gdf.to_crs(epsg=4326) if (
        osm_gdf.crs and osm_gdf.crs.to_epsg() != 4326
    ) else osm_gdf.copy()
    osm = osm[["geometry"]].reset_index(names="_original_index")
    osm["_osm_pos"] = np.arange(len(osm))
    right = candidates.to_crs(epsg=4326) if (
        candidates.crs and candidates.crs.to_epsg() != 4326
    ) else candidates.copy()
    right = right.reset_index(drop=True)
    right["_candidate_pos"] = np.arange(len(right))

    pairs = gpd.sjoin(
        osm[["geometry", "_osm_pos"]],
        right[["geometry", "_candidate_pos"]],
        how="inner", predicate="intersects",
    )
    if pairs.empty:
        return heights, names

    left_pos = pairs["_osm_pos"].to_numpy(dtype=int)
    right_pos = pairs["_candidate_pos"].to_numpy(dtype=int)
    left_geom = osm.geometry.iloc[left_pos].to_numpy()
    right_geom = right.geometry.iloc[right_pos].to_numpy()
    intersection_area = shapely.area(shapely.intersection(left_geom, right_geom))
    left_area = np.maximum(shapely.area(left_geom), 1e-18)
    right_area = np.maximum(shapely.area(right_geom), 1e-18)
    union_area = np.maximum(left_area + right_area - intersection_area, 1e-18)
    iou = intersection_area / union_area
    left_coverage = intersection_area / left_area
    right_coverage = intersection_area / right_area

    # Accept normal footprint agreement, or a contained building-part match.
    accepted = (
        (iou >= 0.30)
        | ((left_coverage >= 0.75) & (right_coverage >= 0.25))
        | ((right_coverage >= 0.75) & (left_coverage >= 0.25))
    )
    if not np.any(accepted):
        return heights, names

    scored = pd.DataFrame({
        "osm_pos": left_pos[accepted],
        "candidate_pos": right_pos[accepted],
        "score": np.maximum(iou[accepted], np.minimum(
            left_coverage[accepted], right_coverage[accepted])),
    })
    winners = scored.loc[scored.groupby("osm_pos")["score"].idxmax()]
    for match in winners.itertuples(index=False):
        original_index = osm.iloc[int(match.osm_pos)]["_original_index"]
        candidate = right.iloc[int(match.candidate_pos)]
        heights.loc[original_index] = float(candidate["height"])
        if pd.notna(candidate.get("name")):
            names.loc[original_index] = candidate["name"]
    return heights, names


def persist_osm_height_tags(
    buildings_gdf,
    bbox_wgs84: Tuple[float, float, float, float],
    *,
    cache_dir: str = _DEFAULT_CACHE_DIR,
) -> int:
    """Retain explicit OSM height/floor observations independently of PBFs."""
    if buildings_gdf is None or buildings_gdf.empty:
        return 0
    frame = buildings_gdf.to_crs(epsg=4326) if (
        buildings_gdf.crs and buildings_gdf.crs.to_epsg() != 4326
    ) else buildings_gdf
    direct = pd.to_numeric(
        frame["height"].astype(str).str.replace(r"[^\d.]", "", regex=True)
        if "height" in frame.columns else np.nan,
        errors="coerce",
    )
    levels = pd.to_numeric(
        frame["building:levels"] if "building:levels" in frame.columns
        else np.nan,
        errors="coerce",
    )
    if not isinstance(direct, pd.Series):
        direct = pd.Series(np.nan, index=frame.index, dtype=float)
    if not isinstance(levels, pd.Series):
        levels = pd.Series(np.nan, index=frame.index, dtype=float)

    observations = []
    for idx, row in frame.iterrows():
        height = direct.loc[idx]
        kind = "osm_height"
        if pd.isna(height) or height <= 0:
            floor_count = levels.loc[idx]
            if pd.isna(floor_count) or floor_count <= 0:
                continue
            height = float(floor_count) * 3.5
            kind = "osm_levels"
        if not (0 < float(height) <= 2000):
            continue
        osm_type = row.get("osm_type", "feature")
        osm_id = row.get("osm_id", idx)
        observations.append({
            "source_feature_id": f"{osm_type}/{osm_id}",
            "height_m": float(height),
            "num_floors": levels.loc[idx] if pd.notna(levels.loc[idx]) else None,
            "height_kind": kind,
            "name": row.get("name"),
            "geometry": row.geometry,
            "metadata": {"wikidata": row.get("wikidata")},
        })

    store = _open_height_store(cache_dir)
    count = store.put_observations(
        observations, source="osm", source_release="live-pbf",
        source_url="https://www.openstreetmap.org/",
    )
    store.register_coverage(
        "osm", bbox_wgs84, source_release="live-pbf",
        observation_count=count,
        metadata={"explicit_height_or_levels_only": True},
    )
    return count


def load_overture_heights(
    osm_gdf,
    bbox_wgs84: Tuple[float, float, float, float],
    cache_dir: str = _DEFAULT_CACHE_DIR,
    auto_download: bool = False,
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
    if env_auto_download:
        auto_download = env_auto_download.lower() in {"1", "true", "yes", "on"}

    os.makedirs(cache_dir, exist_ok=True)
    store = _open_height_store(cache_dir)

    # Step 1: Prefer normalized, spatially indexed observations.  A coverage
    # row proves that the requested bbox was imported in full, including the
    # legitimate case where the provider returned zero usable heights.
    if store.covers_bbox("overture", bbox_wgs84):
        candidates = store.query_bbox("overture", bbox_wgs84)
        heights, names = _match_overture_candidates(osm_gdf, candidates)
        logger.info(
            "Overture persistent cache hit: %d candidates, %d matches",
            len(candidates), int(heights.notna().sum()),
        )
        return heights, names

    # Step 2: Fall back to retained raw GeoParquet, and only use the network
    # when the caller explicitly opted in.
    parquet_path = _find_overture_cache(bbox_wgs84, cache_dir)
    if parquet_path is None and auto_download:
        parquet_path = _download_overture(bbox_wgs84, cache_dir)

    if parquet_path is None:
        logger.info("Overture height enrichment: no data available, skipping")
        return None, None

    # Step 3: Load and normalize Overture data.  Keep the GERS id when the
    # release supplies it; legacy files fall back to stable geometry hashes.
    try:
        import geopandas as gpd
        try:
            overture_gdf = gpd.read_parquet(
                parquet_path,
                columns=["id", "geometry", "height", "num_floors", "names"],
            )
        except (KeyError, ValueError):
            overture_gdf = gpd.read_parquet(parquet_path)
    except Exception as e:
        logger.warning(f"Failed to read Overture parquet: {e}")
        return None, None

    logger.info(f"Overture: loaded {len(overture_gdf)} buildings from {parquet_path}")
    candidates = _extract_overture_candidates(overture_gdf)
    source_release = os.environ.get("OVERTURE_RELEASE", "unknown").strip() or "unknown"
    _persist_overture_candidates(
        store, candidates, bbox_wgs84, raw_path=parquet_path,
        source_release=source_release,
    )
    result_heights, result_names = _match_overture_candidates(
        osm_gdf, candidates)

    n_matched = result_heights.notna().sum()
    n_total = len(osm_gdf)
    coverage = n_matched / n_total * 100 if n_total > 0 else 0
    logger.info(
        "Overture height enrichment: %d/%d buildings matched (%.1f%%); "
        "raw and normalized data retained",
        n_matched, n_total, coverage,
    )

    return result_heights, result_names
