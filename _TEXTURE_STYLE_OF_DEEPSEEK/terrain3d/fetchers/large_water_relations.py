"""Recover very large inland-water multipolygons before bbox clipping.

Regional PBF extracts commonly contain complete OSM relation dependencies,
but the normal city pipeline clips the PBF before filtering it.  That is fast
for ordinary features and wrong for relations such as Lake Michigan: clipping
first breaks the outer ring before it can be assembled.

This module builds one compact, versioned FlatGeobuf cache per regional PBF by
filtering water *relations* from the complete source file.  Individual city
requests then use the FlatGeobuf spatial index and clip only the intersecting
large polygons.  The path is deliberately native-osmium only; the bundled
Python compatibility backend remains a safe fallback for ordinary features
but is too slow for a whole-region relation scan.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Iterable, Optional, Sequence

import geopandas as gpd
import pandas as pd
from shapely.geometry import box
from shapely.ops import unary_union


logger = logging.getLogger(__name__)

CACHE_SCHEMA = "large_water_relations_v1"
MIN_SOURCE_AREA_M2 = 5_000_000.0
MIN_ADDED_AREA_M2 = 10_000.0
RELATION_FILTERS = (
    "r/natural=water",
    "r/water",
    "r/landuse=reservoir",
)


def native_osmium_binary(command: Sequence[str]) -> Optional[str]:
    """Return the native osmium executable, never the Python fallback."""
    if len(command) != 1:
        return None
    candidate = os.path.realpath(command[0])
    if os.path.basename(candidate) != "osmium":
        return None
    if not os.path.isfile(candidate) or not os.access(candidate, os.X_OK):
        return None
    return candidate


def _cache_paths(pbf_path: str, cache_root: Optional[str] = None):
    pbf = Path(pbf_path).resolve()
    stat = pbf.stat()
    fingerprint = f"{stat.st_size:x}-{stat.st_mtime_ns:x}"
    root = Path(cache_root) if cache_root else (
        Path(__file__).resolve().parents[3] / "cache" / "large_water_relations"
    )
    root.mkdir(parents=True, exist_ok=True)
    stem = f"{pbf.stem}-{fingerprint}-{CACHE_SCHEMA}"
    return root / f"{stem}.fgb", root / f"{stem}.json", root / f"{stem}.lock"


def _run(command: Sequence[str], timeout: int = 1200) -> None:
    result = subprocess.run(
        list(command), capture_output=True, timeout=timeout, check=False)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"command failed ({result.returncode}): {stderr}")


def _select_large_relations(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Keep tagged relation polygons large enough to need pre-clip recovery."""
    if gdf is None or len(gdf) == 0:
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

    result = gdf.copy()
    for column in ("@type", "@id", "natural", "water", "landuse", "name"):
        if column not in result.columns:
            result[column] = None
    tagged_water = (
        result["natural"].fillna("").eq("water")
        | result["water"].notna()
        | result["landuse"].fillna("").eq("reservoir")
    )
    result = result[
        result["@type"].fillna("").eq("relation")
        & tagged_water
        & result.geometry.geom_type.isin(("Polygon", "MultiPolygon"))
    ].copy()
    if result.empty:
        return gpd.GeoDataFrame(geometry=[], crs=gdf.crs or "EPSG:4326")

    invalid = ~result.geometry.is_valid
    if invalid.any():
        result.loc[invalid, "geometry"] = result.loc[invalid, "geometry"].buffer(0)
    area_m2 = result.to_crs("EPSG:6933").geometry.area
    result = result.loc[area_m2 >= MIN_SOURCE_AREA_M2].copy()
    if result.empty:
        return gpd.GeoDataFrame(geometry=[], crs=gdf.crs or "EPSG:4326")

    result["osm_type"] = "relation"
    result["osm_id"] = pd.to_numeric(result["@id"], errors="coerce").astype("Int64")
    result["source"] = "osm_relation_full"
    keep = [
        column for column in (
            "osm_type", "osm_id", "name", "natural", "water", "landuse",
            "source", "geometry",
        ) if column in result.columns
    ]
    return gpd.GeoDataFrame(result[keep], geometry="geometry", crs=result.crs)


def _build_cache(
    pbf_path: str,
    osmium_binary: str,
    data_path: Path,
    metadata_path: Path,
) -> None:
    with tempfile.TemporaryDirectory(
            prefix="large-water-", dir=str(data_path.parent)) as tmp_dir:
        relation_pbf = Path(tmp_dir) / "water_relations.pbf"
        raw_geojson = Path(tmp_dir) / "water_relations.geojson"
        _run([
            osmium_binary, "tags-filter", pbf_path, *RELATION_FILTERS,
            "-o", str(relation_pbf), "--overwrite",
        ])
        _run([
            osmium_binary, "export", str(relation_pbf),
            "--geometry-types=polygon", "--attributes=type,id",
            "-o", str(raw_geojson), "--overwrite",
        ])
        raw = gpd.read_file(
            raw_geojson,
            columns=["@type", "@id", "natural", "water", "landuse", "name"],
        )
        selected = _select_large_relations(raw)
        if not selected.empty:
            tmp_data = data_path.with_suffix(f".tmp{os.getpid()}.fgb")
            selected.to_file(tmp_data, driver="FlatGeobuf")
            os.replace(tmp_data, data_path)

        metadata = {
            "schema": CACHE_SCHEMA,
            "pbf": str(Path(pbf_path).resolve()),
            "pbf_size": Path(pbf_path).stat().st_size,
            "feature_count": len(selected),
            "min_source_area_m2": MIN_SOURCE_AREA_M2,
        }
        tmp_metadata = metadata_path.with_suffix(f".tmp{os.getpid()}.json")
        tmp_metadata.write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
        os.replace(tmp_metadata, metadata_path)


def fetch_large_water_relations(
    pbf_path: str,
    bbox_wgs84: tuple[float, float, float, float],
    osmium_command: Sequence[str],
    cache_root: Optional[str] = None,
) -> gpd.GeoDataFrame:
    """Return complete large-water relation polygons clipped to a city bbox."""
    empty = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
    osmium_binary = native_osmium_binary(osmium_command)
    if osmium_binary is None:
        logger.info("large-water relation cache skipped: native osmium unavailable")
        return empty

    data_path, metadata_path, lock_path = _cache_paths(pbf_path, cache_root)
    lock_file = open(lock_path, "a+")
    try:
        try:
            import fcntl
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        if not metadata_path.exists():
            logger.info("building large-water relation cache for %s", pbf_path)
            _build_cache(pbf_path, osmium_binary, data_path, metadata_path)
    except Exception as exc:
        logger.warning("large-water relation cache failed: %s", exc)
        return empty
    finally:
        try:
            lock_file.close()
        except Exception:
            pass

    if not data_path.exists():
        return empty
    south, west, north, east = bbox_wgs84
    try:
        result = gpd.read_file(data_path, bbox=(west, south, east, north))
    except Exception as exc:
        logger.warning("large-water relation cache read failed: %s", exc)
        return empty
    if result.empty:
        return empty

    frame = box(west, south, east, north)
    result = result.copy()
    result["geometry"] = result.geometry.apply(lambda geometry: geometry.intersection(frame))
    result = result[
        result.geometry.map(lambda geometry: geometry is not None and not geometry.is_empty)
    ].copy()
    return gpd.GeoDataFrame(result, geometry="geometry", crs=result.crs or "EPSG:4326")


def merge_large_water_relations(
    water_gdf: gpd.GeoDataFrame,
    relation_gdf: gpd.GeoDataFrame,
) -> gpd.GeoDataFrame:
    """Append only the area not already represented by ordinary water polygons."""
    if relation_gdf is None or len(relation_gdf) == 0:
        return water_gdf
    if water_gdf is None or len(water_gdf) == 0:
        return relation_gdf.copy()

    existing_polygons = water_gdf.geometry[
        water_gdf.geometry.geom_type.isin(("Polygon", "MultiPolygon"))
    ]
    existing = unary_union(list(existing_polygons)) if len(existing_polygons) else None
    supplement = relation_gdf.copy()
    if existing is not None and not existing.is_empty:
        supplement["geometry"] = supplement.geometry.apply(
            lambda geometry: geometry.difference(existing))
    supplement = supplement[
        supplement.geometry.map(
            lambda geometry: geometry is not None and not geometry.is_empty)
    ].copy()
    if supplement.empty:
        return water_gdf

    try:
        area_m2 = supplement.to_crs("EPSG:6933").geometry.area
        supplement = supplement.loc[area_m2 >= MIN_ADDED_AREA_M2].copy()
    except Exception:
        pass
    if supplement.empty:
        return water_gdf

    merged = pd.concat([water_gdf, supplement], ignore_index=True, sort=False)
    return gpd.GeoDataFrame(
        merged, geometry="geometry", crs=water_gdf.crs or relation_gdf.crs)
