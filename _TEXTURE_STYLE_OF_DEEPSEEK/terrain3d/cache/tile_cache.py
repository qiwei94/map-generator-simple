"""Tile-based spatial cache for OSM data.

New data is stored as coordinate-named GeoJSON tiles on a fixed 0.05° grid.
Legacy data (SHA-1 hash filenames from OSMnx) remains untouched.

Cache layout (new):
    {cache_dir}/tiles/{tag_type}/{south:.5f}_{west:.5f}_{north:.5f}_{east:.5f}.geojson

Example:
    F:/map_gen_cache/tiles/building/30.22000_120.12000_30.26000_120.16000.geojson

Resolution flow:
    1. Tile cache (coordinate key, self-indexing)  ← fastest
    2. OSMnx hash cache (SHA-1, exact URL match)   ← fast (for legacy data)
    3. Overpass API fetch                          ← slow
"""

import logging
import math
import os
from typing import List, Optional, Tuple

import geopandas as gpd

logger = logging.getLogger(__name__)

# Grid constants
TILE_SIZE_DEG = 0.05       # ~5.5 km at equator (matches effective chunk size)
PRECISION = 5               # decimal places for coordinate rounding in filenames


class TileCache:
    """Tile-based spatial cache for OSM features.

    Each tile is a GeoJSON file named by its exact coordinate bounds,
    making the cache self-indexing (no external index needed).
    """

    def __init__(self, cache_dir: str):
        """Initialize tile cache.

        Args:
            cache_dir: Base cache directory (e.g. from select_cache_path()).
                       Tiles are stored under {cache_dir}/tiles/.
        """
        self._base = os.path.join(cache_dir, "tiles")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, tag_type: str, south: float, west: float,
            north: float, east: float) -> Optional[gpd.GeoDataFrame]:
        """Load a single tile from cache by exact bounds.

        Returns:
            GeoDataFrame or None if not cached.
        """
        path = self._tile_path(tag_type, south, west, north, east)
        if os.path.isfile(path):
            try:
                gdf = gpd.read_file(path)
                logger.debug("Tile HIT  %s  [%d features]",
                             self._short_path(path), len(gdf))
                return gdf
            except Exception as exc:
                logger.warning("Tile read error (ignored): %s  %s",
                               self._short_path(path), exc)
        return None

    def get_bbox(self, tag_type: str, south: float, west: float,
                 north: float, east: float
                 ) -> Tuple[List[gpd.GeoDataFrame], List[Tuple[float, float, float, float]]]:
        """Get all cached tiles overlapping a bbox.

        Returns:
            (cached_tiles, missing_bounds)
            cached_tiles: list of GeoDataFrames from cache hits
            missing_bounds: list of (s, w, n, e) tuples for cache misses
        """
        tiles = self._decompose_bbox(south, west, north, east)
        hits = []
        misses = []
        for s, w, n, e in tiles:
            gdf = self.get(tag_type, s, w, n, e)
            if gdf is not None:
                hits.append(gdf)
            else:
                misses.append((s, w, n, e))
        return hits, misses

    def put(self, tag_type: str, south: float, west: float,
            north: float, east: float, gdf: gpd.GeoDataFrame) -> None:
        """Save a tile to cache.

        Skips if the GeoDataFrame is empty or the file already exists.
        """
        if gdf is None or gdf.empty:
            return

        path = self._tile_path(tag_type, south, west, north, east)
        if os.path.isfile(path):
            return  # already cached

        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            gdf.to_file(path, driver="GeoJSON")
            logger.debug("Tile SAVE %s  [%d features]",
                         self._short_path(path), len(gdf))
        except Exception as exc:
            logger.warning("Tile write error (skipped): %s  %s",
                           self._short_path(path), exc)

    def contains(self, tag_type: str, south: float, west: float,
                 north: float, east: float) -> bool:
        """Check if a complete bbox is already cached (all tiles present)."""
        _, misses = self.get_bbox(tag_type, south, west, north, east)
        return len(misses) == 0

    def list_tiles(self, tag_type: str) -> List[str]:
        """List all cached tile filenames for a tag type."""
        dirpath = os.path.join(self._base, tag_type)
        if not os.path.isdir(dirpath):
            return []
        return sorted(os.listdir(dirpath))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _snap_down(self, coord: float) -> float:
        """Snap coordinate down to the nearest tile grid boundary."""
        n = int(coord / TILE_SIZE_DEG)
        return round(n * TILE_SIZE_DEG, PRECISION)

    def _snap_up(self, coord: float) -> float:
        """Snap coordinate up to the nearest tile grid boundary."""
        n = int(coord / TILE_SIZE_DEG)
        if coord > n * TILE_SIZE_DEG:
            n += 1
        return round(n * TILE_SIZE_DEG, PRECISION)

    def _decompose_bbox(self, south: float, west: float,
                        north: float, east: float
                        ) -> List[Tuple[float, float, float, float]]:
        """Decompose a bbox into grid-aligned tile-sized pieces.

        All tile boundaries snap to the global TILE_SIZE_DEG grid,
        so overlapping queries share identical tile coordinates.
        """
        s0 = self._snap_down(south)
        w0 = self._snap_down(west)
        n0 = self._snap_up(north)
        e0 = self._snap_up(east)

        tiles = []
        lat = s0
        while lat < n0:
            lon = w0
            while lon < e0:
                tile_n = round(lat + TILE_SIZE_DEG, PRECISION)
                tile_e = round(lon + TILE_SIZE_DEG, PRECISION)
                # Clip to query bbox to avoid fetching extra data
                tiles.append((
                    max(lat, round(south, PRECISION)),
                    max(lon, round(west, PRECISION)),
                    min(tile_n, round(north, PRECISION)),
                    min(tile_e, round(east, PRECISION)),
                ))
                lon = tile_e
            lat = tile_n
        return tiles

    def _tile_path(self, tag_type: str, south: float, west: float,
                   north: float, east: float) -> str:
        """Generate absolute tile cache file path from bounds."""
        key = f"{south:.5f}_{west:.5f}_{north:.5f}_{east:.5f}.geojson"
        return os.path.join(self._base, tag_type, key)

    @staticmethod
    def _short_path(path: str) -> str:
        """Short human-readable representation of a cache path."""
        parts = path.split(os.sep)
        if len(parts) >= 3:
            return os.sep.join(parts[-3:])
        return path
