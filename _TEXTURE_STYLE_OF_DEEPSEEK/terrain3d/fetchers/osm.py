"""Unified OSM data fetching from local PBF files using osmium CLI.

This module is the single entry point for all OSM data retrieval. It uses
the osmium CLI tool for fast PBF processing (10-20x faster than pyosmium).

Processing pipeline for each feature type:
    Step 1: Raw PBF extraction via osmium CLI (extract/tags-filter/export)
    Step 2: Detailed tag filtering (highway types, min area, etc.)
    Step 3: Bbox clipping (shapely intersection with query bbox)
    Step 4: Cleanup (remove empty/invalid, deduplicate, sort)

For water features (relations), a special pipeline is used:
    Step 1: osmium tags-filter on full PBF (r/ prefix for relations)
    Step 2: osmium export to GeoJSON
    Step 3: ogr2ogr -clipsrc for precise bbox clipping

Each step can optionally export to GeoPackage for QGIS verification.

Usage:
    from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import (
        set_pbf_file_path, fetch_buildings, fetch_roads,
        fetch_water, fetch_vegetation,
    )

    set_pbf_file_path("pbf_cache/zhejiang-latest.osm.pbf")
    water = fetch_water(30.13, 120.01, 30.36, 120.29)

    # With QGIS verification output:
    water = fetch_water(30.13, 120.01, 30.36, 120.29,
                        export_gpkg="output/debug/hangzhou.gpkg")
"""

import logging
import os
import time
from typing import Callable, Dict, Optional, Set, Tuple

import geopandas as gpd
import pandas as pd
from shapely.geometry import box
from shapely.ops import unary_union

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osmium_cli_fetcher import (
    get_cli_fetcher,
)

logger = logging.getLogger(__name__)

# ===========================================================================
# Global PBF file path
# ===========================================================================

_pbf_file_path: Optional[str] = None


def set_pbf_file_path(path: str) -> None:
    """Set the global PBF file path for all fetch functions.

    Args:
        path: Path to .osm.pbf file. Must exist.

    Raises:
        FileNotFoundError: If the file does not exist.
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"PBF file not found: {path}")
    global _pbf_file_path
    _pbf_file_path = os.path.abspath(path)
    logger.info("PBF file set: %s (%.1f MB)",
                _pbf_file_path, os.path.getsize(path) / 1024 / 1024)


def _resolve_pbf_path() -> str:
    """Resolve the PBF file path from global, env, or default location.

    Resolution order:
        1. Global variable set via set_pbf_file_path()
        2. Environment variable OSM_PBF_FILE
        3. Scan pbf_cache/ directory for *.osm.pbf files
        4. Raise RuntimeError if none found

    Returns:
        Absolute path to PBF file.

    Raises:
        RuntimeError: If no PBF file can be resolved.
    """
    if _pbf_file_path and os.path.isfile(_pbf_file_path):
        return _pbf_file_path

    env_path = os.environ.get("OSM_PBF_FILE")
    if env_path and os.path.isfile(env_path):
        return os.path.abspath(env_path)

    # Scan pbf_cache/ directory
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    pbf_dir = os.path.join(project_root, "pbf_cache")
    if os.path.isdir(pbf_dir):
        for f in os.listdir(pbf_dir):
            if f.endswith(".osm.pbf"):
                path = os.path.join(pbf_dir, f)
                logger.info("Auto-detected PBF file: %s", path)
                return path

    raise RuntimeError(
        "No PBF file configured. Call set_pbf_file_path(), "
        "set OSM_PBF_FILE env var, or place a .osm.pbf file in pbf_cache/"
    )


# ===========================================================================
# Feature type configurations
# ===========================================================================

# Tag filters use OR logic across keys.
# A feature matches if ANY key-value pair in the filter matches.

FEATURE_CONFIGS: Dict[str, dict] = {
    "building": {
        "tag_filters": {"building": True},
        "valid_geom_types": {"Polygon", "MultiPolygon"},
        "step_names": ["raw", "filtered", "clipped", "final"],
        "extra_filter_fn": None,
    },
    "road": {
        "tag_filters": {"highway": True},
        "valid_geom_types": {"LineString", "MultiLineString"},
        "step_names": ["raw", "filtered", "clipped", "final"],
        "extra_filter_fn": None,  # set dynamically based on highway_filter param
    },
    "water": {
        "tag_filters": {
            "natural": "water",
            "waterway": True,
            "landuse": "reservoir",
            "water": True,
        },
        "valid_geom_types": {"Polygon", "MultiPolygon", "LineString", "MultiLineString"},
        "step_names": ["raw", "filtered", "clipped", "final"],
        "extra_filter_fn": None,
    },
    "vegetation": {
        "tag_filters": {
            "landuse": ["forest", "grass", "meadow", "village_green"],
            "natural": ["wood", "grassland", "scrub", "heath"],
        },
        "valid_geom_types": {"Polygon", "MultiPolygon"},
        "step_names": ["raw", "filtered", "clipped", "final"],
        "extra_filter_fn": None,
    },
}


# ===========================================================================
# GeoPackage export utility
# ===========================================================================

def export_step_to_gpkg(
    gdf: gpd.GeoDataFrame,
    gpkg_path: str,
    layer_name: str,
) -> None:
    """Export a GeoDataFrame to a GeoPackage layer for QGIS verification.

    Creates parent directories if needed. Appends as a new layer if the
    file already exists. Logs the layer name and feature count.

    Args:
        gdf: GeoDataFrame to export. Must have valid geometry and CRS.
        gpkg_path: Output GeoPackage file path.
        layer_name: Layer name inside the GeoPackage (e.g. "step1_raw_water").
    """
    if gdf is None or gdf.empty:
        logger.info("GPKG export skipped (empty): %s", layer_name)
        return

    if gdf.geometry is None or gdf.geometry.is_empty.all():
        logger.info("GPKG export skipped (no geometry): %s", layer_name)
        return

    try:
        parent = os.path.dirname(gpkg_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        # Check if layer already exists
        mode = "w"
        if os.path.isfile(gpkg_path):
            try:
                existing = gpd.read_file(gpkg_path, layer=layer_name)
                if existing is not None and len(existing) >= 0:
                    mode = "a"
            except Exception:
                mode = "w"

        gdf.to_file(gpkg_path, driver="GPKG", layer=layer_name, mode=mode)
        logger.info("GPKG export: %s [%d features]", layer_name, len(gdf))

    except Exception as e:
        logger.warning("GPKG export failed (%s): %s", layer_name, e)


# ===========================================================================
# OSMPipeline - 4-step processing engine
# ===========================================================================

class OSMPipeline:
    """4-step OSM data processing pipeline.

    Pipeline flow:
        1. Raw PBF extraction via osmium CLI (extract/tags-filter/export)
        2. Detailed tag/attribute filtering (highway types, min area, etc.)
        3. Bbox clipping (shapely intersection with exact query bbox)
        4. Cleanup (remove empty/invalid, deduplicate by osm_id, sort)

    For water features, a relation-first pipeline is used:
        1. osmium tags-filter on full PBF (r/ prefix for relations)
        2. osmium export to GeoJSON
        3. ogr2ogr -clipsrc for precise bbox clipping

    Each step can optionally export its output to GeoPackage.
    """

    def __init__(
        self,
        pbf_path: str,
        feature_type: str,
        bbox: Tuple[float, float, float, float],
        config: dict,
    ):
        """Initialize the pipeline.

        Args:
            pbf_path: Path to .osm.pbf file.
            feature_type: One of "building", "road", "water", "vegetation".
            bbox: (south, west, north, east) WGS84 bounding box.
            config: Feature configuration dict from FEATURE_CONFIGS.
        """
        self.pbf_path = pbf_path
        self.feature_type = feature_type
        self.bbox = bbox  # (south, west, north, east)
        self.config = config
        self._cli_fetcher = get_cli_fetcher()

    def run(self, export_gpkg: Optional[str] = None) -> gpd.GeoDataFrame:
        """Execute the full pipeline.

        Args:
            export_gpkg: If provided, export each step to this GeoPackage file.

        Returns:
            Final GeoDataFrame in EPSG:4326.
        """
        t0 = time.time()
        south, west, north, east = self.bbox
        logger.info("Pipeline [%s]: bbox=(%.4f, %.4f, %.4f, %.4f)",
                     self.feature_type, south, west, north, east)

        # Step 1: Raw extraction (使用 CLI 或 Python)
        gdf = self.step1_raw_extract()
        if export_gpkg:
            export_step_to_gpkg(gdf, export_gpkg,
                                f"step1_raw_{self.feature_type}")
        if gdf.empty:
            logger.info("Pipeline [%s]: empty after step 1, returning",
                         self.feature_type)
            return gdf

        # Step 2: Tag filtering
        gdf = self.step2_filter_tags(gdf)
        if export_gpkg:
            export_step_to_gpkg(gdf, export_gpkg,
                                f"step2_filtered_{self.feature_type}")
        if gdf.empty:
            logger.info("Pipeline [%s]: empty after step 2, returning",
                         self.feature_type)
            return gdf

        # Step 3: Bbox clipping
        gdf = self.step3_clip_bbox(gdf)
        if export_gpkg:
            export_step_to_gpkg(gdf, export_gpkg,
                                f"step3_clipped_{self.feature_type}")
        if gdf.empty:
            logger.info("Pipeline [%s]: empty after step 3, returning",
                         self.feature_type)
            return gdf

        # Step 4: Cleanup
        gdf = self.step4_cleanup(gdf)
        if export_gpkg:
            export_step_to_gpkg(gdf, export_gpkg,
                                f"step4_final_{self.feature_type}")

        elapsed = time.time() - t0
        logger.info("Pipeline [%s]: %d features in %.1fs",
                     self.feature_type, len(gdf), elapsed)
        # import pdb;pdb.set_trace()
        return gdf

    def step1_raw_extract(self) -> gpd.GeoDataFrame:
        """Step 1: Extract raw features from PBF using osmium CLI.

        For standard types (buildings, roads, vegetation):
            osmium extract → tags-filter → export

        For water (relations):
            osmium tags-filter (r/ prefix) → export → ogr2ogr -clipsrc
        """
        south, west, north, east = self.bbox
        logger.info("Step 1 [%s]: Raw PBF extraction via CLI...", self.feature_type)

        try:
            gdf = self._cli_fetcher.fetch_features(
                tag_type=self.feature_type,
                south=south, west=west, north=north, east=east,
                pbf_file=self.pbf_path,
            )
        except Exception as e:
            logger.error("Step 1 [%s]: extraction failed: %s",
                          self.feature_type, e)
            return gpd.GeoDataFrame()

        if gdf.empty:
            logger.info("Step 1 [%s]: no features found", self.feature_type)
        else:
            logger.info("Step 1 [%s]: %d raw features (CLI)",
                         self.feature_type, len(gdf))
        return gdf

    def step2_filter_tags(self, gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        """Step 2: Apply detailed tag and attribute filtering.

        Applies the extra_filter_fn from config if set. For buildings,
        applies min_area_m2 filter. For roads, filters by highway type.

        Args:
            gdf: GeoDataFrame from Step 1.

        Returns:
            Filtered GeoDataFrame.
        """
        logger.info("Step 2 [%s]: Tag filtering...", self.feature_type)

        extra_fn = self.config.get("extra_filter_fn")
        if extra_fn is not None:
            gdf = extra_fn(gdf)

        if gdf.empty:
            logger.info("Step 2 [%s]: no features after filtering",
                         self.feature_type)
        else:
            logger.info("Step 2 [%s]: %d features after filtering",
                         self.feature_type, len(gdf))
        return gdf

    def step3_clip_bbox(self, gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        """Step 3: Clip geometries to the exact query bounding box.

        Uses shapely intersection to clip each geometry to the bbox.
        Features that become empty after clipping are removed.

        Args:
            gdf: GeoDataFrame from Step 2.

        Returns:
            Clipped GeoDataFrame.
        """
        south, west, north, east = self.bbox
        logger.info("Step 3 [%s]: Bbox clipping...", self.feature_type)

        bbox_poly = box(west, south, east, north)

        def _clip_geom(geom):
            if geom is None or geom.is_empty:
                return None
            try:
                result = geom.intersection(bbox_poly)
                return result if not result.is_empty else None
            except Exception:
                return geom if geom.intersects(bbox_poly) else None

        gdf = gdf.copy()
        gdf["geometry"] = gdf.geometry.apply(_clip_geom)
        gdf = gdf[~gdf.geometry.is_empty & gdf.geometry.notna()].copy()

        logger.info("Step 3 [%s]: %d features after clipping",
                     self.feature_type, len(gdf))
        return gdf

    def step4_cleanup(self, gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        """Step 4: Final cleanup - repair, deduplicate, sort.

        - Repairs invalid geometries with buffer(0)
        - Removes empty geometries
        - Deduplicates by osm_id (keeps first occurrence)
        - Sorts by area (polygons) or length (lines) descending

        Args:
            gdf: GeoDataFrame from Step 3.

        Returns:
            Cleaned final GeoDataFrame.
        """
        logger.info("Step 4 [%s]: Cleanup...", self.feature_type)

        if gdf.empty:
            return gdf

        # Repair invalid geometries
        mask_invalid = ~gdf.geometry.is_valid
        if mask_invalid.any():
            count = mask_invalid.sum()
            logger.info("Step 4 [%s]: Repairing %d invalid geometries",
                         self.feature_type, count)
            gdf.loc[mask_invalid, "geometry"] = gdf.loc[mask_invalid, "geometry"].buffer(0)

        # Remove empty geometries
        gdf = gdf[~gdf.geometry.is_empty & gdf.geometry.notna()].copy()

        # Deduplicate by osm_id
        if "osm_id" in gdf.columns:
            before = len(gdf)
            gdf = gdf.drop_duplicates(subset="osm_id", keep="first")
            if len(gdf) < before:
                logger.info("Step 4 [%s]: Removed %d duplicates",
                             self.feature_type, before - len(gdf))

        # Sort by size (area for polygons, length for lines)
        # Project to UTM first to avoid GeoCRS warnings
        if not gdf.empty:
            import pyproj
            s, w, n, e = self.bbox
            center_lat = (s + n) / 2
            center_lon = (w + e) / 2
            zone = int((center_lon + 180) / 6) + 1
            utm_crs = pyproj.CRS.from_epsg(
                32600 + zone if center_lat >= 0 else 32700 + zone
            )
            projected = gdf.to_crs(utm_crs)

            is_polygon = gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon"])
            if is_polygon.any():
                gdf.loc[is_polygon, "_sort_key"] = projected.loc[is_polygon, "geometry"].area
            if (~is_polygon).any():
                gdf.loc[~is_polygon, "_sort_key"] = projected.loc[~is_polygon, "geometry"].length
            gdf = gdf.sort_values("_sort_key", ascending=False).drop(columns=["_sort_key"])

        logger.info("Step 4 [%s]: %d final features",
                     self.feature_type, len(gdf))
        return gdf


# ===========================================================================
# Tile cache integration helpers
# ===========================================================================

def _get_tile_cache():
    """Get the TileCache instance for final output caching.

    Returns:
        TileCache instance or None if cache setup fails.
    """
    try:
        from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.config import (
            select_cache_path,
        )
        from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.cache.tile_cache import (
            TileCache,
        )
        cache_dir = select_cache_path()
        return TileCache(cache_dir)
    except Exception as e:
        logger.debug("TileCache not available: %s", e)
        return None


def _check_tile_cache(
    tag_type: str, south: float, west: float, north: float, east: float
) -> Optional[gpd.GeoDataFrame]:
    """Check if all tiles are cached for the query bbox.

    Returns:
        Concatenated GeoDataFrame if all tiles are cached, None otherwise.
    """
    cache = _get_tile_cache()
    if cache is None:
        return None

    try:
        hits, misses = cache.get_bbox(tag_type, south, west, north, east)
        if not misses:
            if hits:
                gdf = pd.concat(hits, ignore_index=True)
                logger.info("Tile cache HIT [%s]: %d features", tag_type, len(gdf))
                return gdf
        else:
            logger.info("Tile cache MISS [%s]: %d tiles missing", tag_type, len(misses))
    except Exception as e:
        logger.debug("Tile cache check failed: %s", e)
    return None


def _save_tile_cache(
    tag_type: str, south: float, west: float, north: float, east: float,
    gdf: gpd.GeoDataFrame,
) -> None:
    """Save the final result to tile cache."""
    cache = _get_tile_cache()
    if cache is None:
        return

    try:
        cache.put(tag_type, south, west, north, east, gdf)
    except Exception as e:
        logger.debug("Tile cache save failed: %s", e)


# ===========================================================================
# Public API functions
# ===========================================================================

def fetch_buildings(
    south: float,
    west: float,
    north: float,
    east: float,
    min_area_m2: float = 0,
    export_gpkg: Optional[str] = None,
) -> gpd.GeoDataFrame:
    """Fetch building footprints from PBF file.

    Args:
        south, west, north, east: WGS84 bounding box.
        min_area_m2: Minimum footprint area in m² (features below are dropped).
        export_gpkg: If provided, export pipeline steps to this GeoPackage.

    Returns:
        GeoDataFrame in EPSG:4326 with building polygons.
        Includes 'est_height' column estimated from OSM tags.
    """
    pbf_path = _resolve_pbf_path()

    # Check tile cache
    cached = _check_tile_cache("building", south, west, north, east)
    if cached is not None:
        return cached

    # Build extra filter for min_area
    extra_fn = None
    if min_area_m2 > 0:
        def _filter_min_area(gdf):
            gdf = gdf[gdf.geometry.is_valid].copy()
            # Project to UTM for area calculation
            import pyproj
            center_lat = (south + north) / 2
            center_lon = (west + east) / 2
            zone = int((center_lon + 180) / 6) + 1
            utm_crs = pyproj.CRS.from_epsg(32600 + zone if center_lat >= 0 else 32700 + zone)
            projected = gdf.to_crs(utm_crs)
            projected["_area"] = projected.geometry.area
            filtered = projected[projected["_area"] >= min_area_m2].copy()
            return gdf.loc[filtered.index].copy() if not filtered.empty else gpd.GeoDataFrame()
        extra_fn = _filter_min_area

    config = {
        **FEATURE_CONFIGS["building"],
        "extra_filter_fn": extra_fn,
    }

    pipeline = OSMPipeline(pbf_path, "building", (south, west, north, east), config)
    result = pipeline.run(export_gpkg=export_gpkg)

    # Add est_height column
    if not result.empty:
        result["est_height"] = _estimate_building_heights(result)

    # Save to tile cache
    if not result.empty:
        _save_tile_cache("building", south, west, north, east, result)

    return result


def fetch_roads(
    south: float,
    west: float,
    north: float,
    east: float,
    highway_filter: Optional[Set[str]] = None,
    export_gpkg: Optional[str] = None,
) -> gpd.GeoDataFrame:
    """Fetch road network from PBF file.

    Args:
        south, west, north, east: WGS84 bounding box.
        highway_filter: Set of highway types to include. If None, includes all.
        export_gpkg: If provided, export pipeline steps to this GeoPackage.

    Returns:
        GeoDataFrame in EPSG:4326 with road lines.
    """
    pbf_path = _resolve_pbf_path()

    # Check tile cache
    cached = _check_tile_cache("road", south, west, north, east)
    if cached is not None:
        if highway_filter and "highway" in cached.columns:
            def _matches(hw):
                if isinstance(hw, list):
                    return any(h in highway_filter for h in hw)
                return hw in highway_filter
            cached = cached[cached["highway"].apply(_matches)].copy()
        return cached

    # Build extra filter for highway types
    extra_fn = None
    if highway_filter is not None:
        def _filter_highway(gdf):
            if "highway" not in gdf.columns:
                return gdf
            def _matches(hw):
                if isinstance(hw, list):
                    return any(h in highway_filter for h in hw)
                return hw in highway_filter
            return gdf[gdf["highway"].apply(_matches)].copy()
        extra_fn = _filter_highway

    config = {
        **FEATURE_CONFIGS["road"],
        "extra_filter_fn": extra_fn,
    }

    pipeline = OSMPipeline(pbf_path, "road", (south, west, north, east), config)
    result = pipeline.run(export_gpkg=export_gpkg)

    # Apply ROAD_MIN_LENGTH_M filter
    if not result.empty:
        from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.config import ROAD_MIN_LENGTH_M
        import pyproj
        center_lat = (south + north) / 2
        center_lon = (west + east) / 2
        zone = int((center_lon + 180) / 6) + 1
        utm_crs = pyproj.CRS.from_epsg(32600 + zone if center_lat >= 0 else 32700 + zone)
        projected = result.to_crs(utm_crs)
        projected["_length"] = projected.geometry.length
        keep = projected[projected["_length"] >= ROAD_MIN_LENGTH_M].index
        result = result.loc[keep].copy()

    # Save to tile cache
    if not result.empty:
        _save_tile_cache("road", south, west, north, east, result)

    return result


def fetch_water(
    south: float,
    west: float,
    north: float,
    east: float,
    export_gpkg: Optional[str] = None,
) -> gpd.GeoDataFrame:
    """Fetch water features (polygons and lines) from PBF file.

    Args:
        south, west, north, east: WGS84 bounding box.
        export_gpkg: If provided, export pipeline steps to this GeoPackage.

    Returns:
        GeoDataFrame in EPSG:4326 with water polygons and lines.
    """
    pbf_path = _resolve_pbf_path()

    # Check tile cache
    # cached = _check_tile_cache("water", south, west, north, east)
    # if cached is not None:
    #     return cached

    config = FEATURE_CONFIGS["water"]
    pipeline = OSMPipeline(pbf_path, "water", (south, west, north, east), config)
    result = pipeline.run(export_gpkg=export_gpkg)

    # Apply WATER_MIN_AREA_M2 filter for polygons
    if not result.empty:
        from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.config import WATER_MIN_AREA_M2
        import pyproj
        center_lat = (south + north) / 2
        center_lon = (west + east) / 2
        zone = int((center_lon + 180) / 6) + 1
        utm_crs = pyproj.CRS.from_epsg(32600 + zone if center_lat >= 0 else 32700 + zone)
        is_polygon = result.geometry.geom_type.isin(["Polygon", "MultiPolygon"])
        if is_polygon.any():
            projected = result[is_polygon].to_crs(utm_crs)
            projected["_area"] = projected.geometry.area
            keep_poly = projected[projected["_area"] >= WATER_MIN_AREA_M2].index
            result = pd.concat([result.loc[keep_poly], result[~is_polygon]]).copy()

    # Save to tile cache
    # if not result.empty:
    #     _save_tile_cache("water", south, west, north, east, result)

    return result


def fetch_vegetation(
    south: float,
    west: float,
    north: float,
    east: float,
    export_gpkg: Optional[str] = None,
) -> gpd.GeoDataFrame:
    """Fetch vegetation areas from PBF file.

    Args:
        south, west, north, east: WGS84 bounding box.
        export_gpkg: If provided, export pipeline steps to this GeoPackage.

    Returns:
        GeoDataFrame in EPSG:4326 with vegetation polygons.
    """
    pbf_path = _resolve_pbf_path()

    # Check tile cache
    cached = _check_tile_cache("vegetation", south, west, north, east)
    if cached is not None:
        return cached

    config = FEATURE_CONFIGS["vegetation"]
    pipeline = OSMPipeline(pbf_path, "vegetation", (south, west, north, east), config)
    result = pipeline.run(export_gpkg=export_gpkg)

    # Save to tile cache
    if not result.empty:
        _save_tile_cache("vegetation", south, west, north, east, result)

    return result


# ===========================================================================
# Internal utilities
# ===========================================================================

def _estimate_building_heights(gdf: gpd.GeoDataFrame) -> pd.Series:
    """Estimate building heights from OSM tags.

    Priority:
        1. height tag (direct value in meters)
        2. building:levels * BUILDING_LEVEL_HEIGHT_M
        3. BUILDING_DEFAULT_HEIGHT_M fallback

    Args:
        gdf: Building GeoDataFrame.

    Returns:
        Series of estimated heights in meters.
    """
    from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.config import (
        BUILDING_DEFAULT_HEIGHT_M,
        BUILDING_LEVEL_HEIGHT_M,
    )

    MAX_HEIGHT_M = 800
    heights = pd.Series(BUILDING_DEFAULT_HEIGHT_M, index=gdf.index)

    if "height" in gdf.columns:
        parsed = pd.to_numeric(
            gdf["height"].astype(str).str.replace(r"[^\d.]", "", regex=True),
            errors="coerce",
        )
        valid = parsed.notna() & (parsed > 0) & (parsed <= MAX_HEIGHT_M)
        heights[valid] = parsed[valid]

    if "building:levels" in gdf.columns:
        levels = pd.to_numeric(gdf["building:levels"], errors="coerce")
        valid_levels = (
            levels.notna() & (levels > 0)
            & heights.eq(BUILDING_DEFAULT_HEIGHT_M)
        )
        heights[valid_levels] = (
            levels[valid_levels] * BUILDING_LEVEL_HEIGHT_M
        ).clip(upper=MAX_HEIGHT_M)

    return heights
