"""Fetch OpenStreetMap data: roads, buildings, water features."""

import logging
import os
import time
import functools
import geopandas as gpd
import pandas as pd
import osmnx as ox

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.config import CACHE_TTL_SECONDS, CACHE_BASE_DIR, get_water_high_detail, select_cache_path
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.utils import cache as cache_mgr
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.cache import TileCache

logger = logging.getLogger(__name__)

# =====================================================================
# 高可用配置
# =====================================================================

# 多镜像列表 (按优先级排列)
OVERPASS_ENDPOINTS = [
    "https://overpass.kumi.systems/api",      # 1. 社区镜像 (推荐,较稳定)
    "https://overpass.openstreetmap.fr/api",   # 2. OSM France
    "https://lz4.overpass-api.de/api",         # 3. Overpass 官方备用
    "https://overpass-api.de/api",             # 4. Overpass 官方主站
]

# 代理配置: 默认禁用系统代理，直接访问Overpass API
# 中国大陆直接访问OSM通常可行，系统代理可能导致连接问题
def _disable_system_proxy():
    """禁用系统代理，让osmnx直接访问Overpass API"""
    # 清除所有代理环境变量
    for var in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"]:
        if var in os.environ:
            del os.environ[var]
    logger.info("System proxy disabled for direct Overpass access")

_disable_system_proxy()

# =====================================================================
# 多路径OSM缓存配置
# =====================================================================

# OSM缓存路径列表（按优先级排列，第一个优先使用）
OSM_CACHE_PATHS = [
    "F:/map_gen_cache/attaraction/cache/osm",  # 主缓存目录（历史数据）
    "F:/map_gen_cache/project_cache/osm",      # 项目缓存目录
]

def _get_all_osm_cache_dirs():
    """获取所有有效的OSM缓存目录"""
    valid_dirs = []
    for d in OSM_CACHE_PATHS:
        if os.path.isdir(d):
            valid_dirs.append(d)
    return valid_dirs

def _find_cached_file_in_all_dirs(cache_key: str) -> str:
    """在所有缓存目录中查找缓存文件
    
    Args:
        cache_key: osmnx生成的缓存文件名（hash key）
    
    Returns:
        找到的缓存文件路径，如果都没找到返回None
    """
    for cache_dir in _get_all_osm_cache_dirs():
        cache_file = os.path.join(cache_dir, cache_key)
        if os.path.isfile(cache_file):
            logger.info(f"Found cached file in {cache_dir}: {cache_key}")
            return cache_file
    return None

def _ensure_cache_in_primary_dir(cache_key: str):
    """确保缓存文件在主目录中存在
    
    如果缓存文件在其他目录找到，复制到主目录
    """
    primary_dir = _get_osm_cache_dir()
    primary_file = os.path.join(primary_dir, cache_key)
    
    if os.path.isfile(primary_file):
        return  # 主目录已有
    
    # 在其他目录查找
    found_file = _find_cached_file_in_all_dirs(cache_key)
    if found_file and found_file != primary_file:
        # 复制到主目录
        import shutil
        os.makedirs(primary_dir, exist_ok=True)
        shutil.copy2(found_file, primary_file)
        logger.info(f"Copied cache from {found_file} to {primary_file}")

# OSM cache: support multi-path cache - select best cache dir
def _get_osm_cache_dir():
    """获取OSM缓存目录（优先使用缓存最多的目录）"""
    # 统计每个目录的缓存文件数量
    best_dir = None
    best_count = 0
    
    for d in OSM_CACHE_PATHS:
        if os.path.isdir(d):
            count = len([f for f in os.listdir(d) if f.endswith('.json')])
            if count > best_count:
                best_count = count
                best_dir = d
    
    # 如果没有找到有效目录，使用默认路径
    if best_dir is None:
        cache_base = select_cache_path(100)
        best_dir = os.path.join(cache_base, "osm")
        os.makedirs(best_dir, exist_ok=True)
    
    logger.info(f"Using OSM cache dir: {best_dir} ({best_count} cached files)")
    return best_dir

_OSM_CACHE_DIR = _get_osm_cache_dir()

# Use Overpass API settings for reliability
ox.settings.use_cache = True
ox.settings.cache_folder = _OSM_CACHE_DIR
ox.settings.timeout = 30  # shorter timeout: city cache is primary fallback
# 设置默认镜像 (第一个)
ox.settings.overpass_url = OVERPASS_ENDPOINTS[0]

# =====================================================================
# 瓦片缓存 (Tile Cache) — 新数据使用坐标瓦片模式
# =====================================================================
# 存量数据仍由 OSMnx SHA-1 hash 缓存管理（不受影响）
# 瓦片缓存路径: {cache_base}/tiles/{tag_type}/{坐标}.geojson

_TILE_CACHE = TileCache(select_cache_path(0))


def get_tile_cache() -> TileCache:
    """获取全局 TileCache 实例，供外部使用（如索引重建）。"""
    return _TILE_CACHE


def _retry_with_fallback(func, max_retries_per_endpoint=2):
    """装饰器: 多镜像回退重试机制

    依次尝试所有镜像,每个镜像重试多次,直到成功为止。
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        last_exception = None

        for endpoint_idx, endpoint in enumerate(OVERPASS_ENDPOINTS):
            # 切换到当前镜像
            ox.settings.overpass_url = endpoint

            for attempt in range(max_retries_per_endpoint):
                try:
                    if len(OVERPASS_ENDPOINTS) > 1 or attempt > 0:
                        logger.info(
                            f"Overpass endpoint [{endpoint_idx+1}/{len(OVERPASS_ENDPOINTS)}]: "
                            f"{endpoint} (attempt {attempt+1})"
                        )
                    result = func(*args, **kwargs)
                    if endpoint_idx > 0 or attempt > 0:
                        logger.info(f"Successfully fetched from {endpoint}")
                    return result
                except Exception as e:
                    last_exception = e
                    logger.warning(
                        f"Overpass {endpoint} failed (attempt {attempt+1}/{max_retries_per_endpoint}): "
                        f"{type(e).__name__}: {str(e)[:100]}"
                    )
                    if attempt < max_retries_per_endpoint - 1:
                        time.sleep(1 * (attempt + 1))  # 指数退避

            logger.warning(f"All retries exhausted for {endpoint}, trying next endpoint...")

        # 所有镜像都失败了
        raise RuntimeError(
            f"All {len(OVERPASS_ENDPOINTS)} Overpass endpoints failed. "
            f"Last error: {last_exception}"
        )
    return wrapper


def _setup_cache(use_cache: bool, ttl_seconds: int):
    """Configure osmnx cache and prune expired files.

    Args:
        use_cache: if False, disable osmnx cache for this fetch
        ttl_seconds: prune cache files older than this
    """
    if not use_cache:
        ox.settings.use_cache = False
        return

    ox.settings.use_cache = True
    ox.settings.cache_folder = _OSM_CACHE_DIR
    os.makedirs(_OSM_CACHE_DIR, exist_ok=True)
    # Prune expired cache files only (keeps valid cache for reuse)
    if os.path.isdir(_OSM_CACHE_DIR):
        cache_mgr.clear_expired(_OSM_CACHE_DIR, ttl_seconds)


def _restore_cache():
    """Re-enable osmnx cache after a fetch."""
    ox.settings.use_cache = True


def _tile_cached_fetch(tag_type: str, fetch_fn, south: float, west: float,
                       north: float, east: float,
                       rate_limit: float = 0.5,
                       **kwargs) -> gpd.GeoDataFrame:
    """Grid-aligned tile fetching with tile cache.

    New data is fetched as grid-aligned tiles and saved to the tile cache.
    Legacy OSMnx SHA-1 hash cache remains active as a second-level cache.

    Resolution order:
        1. Tile cache (coordinate key, self-indexing)  ← fastest
        2. City cache loader (raw Overpass JSON files)  ← fast, no network
        3. OSMnx hash cache (SHA-1, exact URL match)   ← transparent
        4. Overpass API fetch                          ← slow, may be unavailable

    Args:
        tag_type: Cache subdirectory name (e.g. 'building', 'road', 'water')
        fetch_fn: Chunk fetch function (e.g. _fetch_buildings_chunk)
        south, west, north, east: WGS84 bounding box
        rate_limit: Seconds to wait between API calls
        **kwargs: Forwarded to fetch_fn

    Returns:
        Combined GeoDataFrame for the full bbox
    """
    import pdb;pdb.set_trace()
    tiles = _TILE_CACHE._decompose_bbox(south, west, north, east)

    if not tiles:
        logger.warning("No tiles produced for bbox (%.4f, %.4f, %.4f, %.4f)",
                       south, west, north, east)
        return gpd.GeoDataFrame()

    logger.info("Tile cache: %d tiles for bbox (%.4f, %.4f, %.4f, %.4f)",
                len(tiles), south, west, north, east)

    results = []
    n_hit = 0
    n_miss = 0

    # --- Bulk load from city cache for the full bbox — much faster than per-tile ---
    # If many tiles will miss, load everything once and distribute.
    misses = []
    for s, w, n, e in tiles:
        gdf = _TILE_CACHE.get(tag_type, s, w, n, e)
        if gdf is not None:
            n_hit += 1
            results.append(gdf)
        else:
            misses.append((s, w, n, e))

    # If there are misses, try bulk city cache load
    if misses:
        n_miss = len(misses)
        try:
            from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.city_cache_loader import (
                load_features,
            )
            from shapely import box

            bulk_gdf = load_features(tag_type, south, west, north, east)

            if not bulk_gdf.empty:
                # Distribute bulk data to each missing tile
                logger.info(
                    "Distributing %d features across %d tiles...",
                    len(bulk_gdf), len(misses),
                )
                for s, w, n, e in misses:
                    tile_poly = box(w, s, e, n)
                    tile_mask = bulk_gdf.intersects(tile_poly)
                    if tile_mask.any():
                        tile_gdf = bulk_gdf[tile_mask].copy()
                        # Clip geometries to tile bbox
                        tile_gdf["geometry"] = tile_gdf.geometry.intersection(
                            tile_poly
                        )
                        tile_gdf = tile_gdf[~tile_gdf.geometry.is_empty].copy()
                        _TILE_CACHE.put(tag_type, s, w, n, e, tile_gdf)
                        results.append(tile_gdf)
                        n_miss -= 1  # was resolved from bulk load
                logger.info(
                    "Bulk city cache: resolved %d/%d tiles",
                    len(misses) - n_miss, len(misses),
                )

        except Exception as cache_err:
            logger.debug("City cache bulk error: %s", cache_err)

        # For remaining misses, try per-tile from Overpass API
        for s, w, n, e in misses:
            # Check if this tile was already resolved
            gdf = _TILE_CACHE.get(tag_type, s, w, n, e)
            if gdf is not None:
                continue

            try:
                gdf = fetch_fn(south=s, west=w, north=n, east=e, **kwargs)
            except Exception:
                gdf = None

            if gdf is not None and not gdf.empty:
                _TILE_CACHE.put(tag_type, s, w, n, e, gdf)
                results.append(gdf)

            time.sleep(rate_limit)

    if n_miss > 0:
        logger.info("Tile cache: %d hit, %d miss, %d total",
                    n_hit, n_miss, len(tiles))

    if not results:
        return gpd.GeoDataFrame()

    return pd.concat(results, ignore_index=True)


def _make_bbox(south, west, north, east):
    """Create bbox tuple in osmnx 2.x format: (west, south, east, north)."""
    return (west, south, east, north)


def _chunked_bbox_fetch(fetch_fn, south, west, north, east,
                        max_area_deg2=0.05, **kwargs):
    """Split large bounding boxes into chunks to avoid Overpass timeouts."""
    lat_range = north - south
    lon_range = east - west
    area_deg2 = lat_range * lon_range

    if area_deg2 <= max_area_deg2:
        return fetch_fn(south=south, west=west, north=north, east=east, **kwargs)

    # Calculate chunk count
    n_chunks_lat = max(1, int(lat_range / (max_area_deg2 ** 0.5)) + 1)
    n_chunks_lon = max(1, int(lon_range / (max_area_deg2 ** 0.5)) + 1)

    lat_step = lat_range / n_chunks_lat
    lon_step = lon_range / n_chunks_lon

    logger.info(f"Splitting OSM query into {n_chunks_lat}x{n_chunks_lon} chunks")

    results = []
    for i in range(n_chunks_lat):
        for j in range(n_chunks_lon):
            chunk_s = south + i * lat_step
            chunk_n = south + (i + 1) * lat_step
            chunk_w = west + j * lon_step
            chunk_e = west + (j + 1) * lon_step

            try:
                result = fetch_fn(south=chunk_s, west=chunk_w,
                                  north=chunk_n, east=chunk_e, **kwargs)
                if result is not None and not result.empty:
                    results.append(result)
            except Exception as e:
                logger.warning(f"Chunk ({i},{j}) failed: {e}")

            time.sleep(0.5)  # rate limit

    if not results:
        return gpd.GeoDataFrame()

    return pd.concat(results, ignore_index=True)


@_retry_with_fallback
def _fetch_buildings_chunk(south, west, north, east):
    """Fetch buildings for a single bounding box chunk."""
    bbox = _make_bbox(south, west, north, east)
    try:
        gdf = ox.features_from_bbox(bbox=bbox, tags={"building": True})
    except Exception as e:
        logger.warning(f"Building fetch failed: {e}")
        return gpd.GeoDataFrame()

    if gdf.empty:
        return gdf

    # Extract height info
    gdf = gdf.copy()
    gdf["est_height"] = _estimate_building_heights(gdf)

    # Keep only polygons/multipolygons
    gdf = gdf[gdf.geometry.type.isin(["Polygon", "MultiPolygon"])].copy()

    return gdf


def _estimate_building_heights(gdf: gpd.GeoDataFrame) -> pd.Series:
    """Estimate building heights from OSM tags."""
    from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.config import BUILDING_DEFAULT_HEIGHT_M, BUILDING_LEVEL_HEIGHT_M

    # Reasonable max: tallest building in China is ~630m (Shanghai Tower)
    MAX_HEIGHT_M = 800

    heights = pd.Series(BUILDING_DEFAULT_HEIGHT_M, index=gdf.index)

    # Try 'height' tag first
    if "height" in gdf.columns:
        parsed = pd.to_numeric(
            gdf["height"].astype(str).str.replace(r"[^\d.]", "", regex=True),
            errors="coerce"
        )
        valid = parsed.notna() & (parsed > 0) & (parsed <= MAX_HEIGHT_M)
        heights[valid] = parsed[valid]

    # Fall back to building:levels
    if "building:levels" in gdf.columns:
        levels = pd.to_numeric(gdf["building:levels"], errors="coerce")
        valid_levels = levels.notna() & (levels > 0) & heights.eq(BUILDING_DEFAULT_HEIGHT_M)
        heights[valid_levels] = (levels[valid_levels] * BUILDING_LEVEL_HEIGHT_M).clip(upper=MAX_HEIGHT_M)

    return heights


def fetch_buildings(south: float, west: float, north: float, east: float,
                    min_area_m2: float = 0,
                    use_cache: bool = True,
                    ttl_seconds: int = None) -> gpd.GeoDataFrame:
    """Fetch building footprints from OpenStreetMap.

    Args:
        south, west, north, east: WGS84 bounding box
        min_area_m2: minimum building footprint area to include (for LOD)
        use_cache: if False, force re-fetch from Overpass API
        ttl_seconds: cache TTL in seconds (None = use default)

    Returns:
        GeoDataFrame with building polygons and 'est_height' column
    """
    if ttl_seconds is None:
        ttl_seconds = CACHE_TTL_SECONDS
    _setup_cache(use_cache, ttl_seconds)
    try:
        if use_cache:
            ttl_label = "permanent" if ttl_seconds < 0 else f"{ttl_seconds // 86400}d"
            logger.info("Fetching buildings from OSM (cache: %s, TTL: %s)", _OSM_CACHE_DIR, ttl_label)
        else:
            logger.info("Fetching buildings from OSM (cache disabled)")
        gdf = _tile_cached_fetch("building", _fetch_buildings_chunk, south, west, north, east)
    finally:
        _restore_cache()

    if gdf.empty:
        logger.info("No buildings found")
        return gdf

    logger.info(f"Fetched {len(gdf)} buildings")
    return gdf


@_retry_with_fallback
def _fetch_roads_chunk(south, west, north, east):
    """Fetch road network for a single bounding box chunk.

    Uses features_from_bbox with highway tags for reliable results.
    Falls back to graph_from_bbox if needed.
    """
    bbox = _make_bbox(south, west, north, east)

    # Primary: use features_from_bbox with highway tag (more reliable)
    try:
        gdf = ox.features_from_bbox(bbox=bbox, tags={"highway": True})
        if not gdf.empty:
            # Keep only line geometries (roads)
            valid = gdf.geometry.type.isin(
                ["LineString", "MultiLineString"])
            gdf = gdf[valid].copy()
            if not gdf.empty:
                return gdf
    except Exception as e:
        logger.debug(f"features_from_bbox for roads failed: {e}")

    # Fallback: use graph_from_bbox
    try:
        G = ox.graph_from_bbox(bbox=bbox, network_type="all", simplify=True)
        if G is not None and len(G.edges) > 0:
            edges = ox.graph_to_gdfs(G, nodes=False, edges=True)
            return edges
    except Exception as e:
        logger.debug(f"graph_from_bbox failed: {e}")

    return gpd.GeoDataFrame()


def fetch_roads(south: float, west: float, north: float, east: float,
                highway_filter: set = None,
                use_cache: bool = True,
                ttl_seconds: int = None) -> gpd.GeoDataFrame:
    """Fetch road network from OpenStreetMap.

    Args:
        south, west, north, east: WGS84 bounding box
        highway_filter: if set, only include these highway types
        use_cache: if False, force re-fetch from Overpass API
        ttl_seconds: cache TTL in seconds (None = use default)

    Returns:
        GeoDataFrame with road LineString geometries
    """
    import pdb;pdb.set_trace()
    if ttl_seconds is None:
        ttl_seconds = CACHE_TTL_SECONDS
    _setup_cache(use_cache, ttl_seconds)
    try:
        if use_cache:
            logger.info("Fetching roads from OSM (cache: %s)", _OSM_CACHE_DIR)
        else:
            logger.info("Fetching roads from OSM (cache disabled)")
        gdf = _tile_cached_fetch("road", _fetch_roads_chunk, south, west, north, east)
    finally:
        _restore_cache()

    if gdf.empty:
        logger.info("No roads found")
        return gdf

    # Apply highway filter
    if highway_filter and "highway" in gdf.columns:
        def matches_filter(hw):
            if isinstance(hw, list):
                return any(h in highway_filter for h in hw)
            return hw in highway_filter
        gdf = gdf[gdf["highway"].apply(matches_filter)].copy()

    logger.info(f"Fetched {len(gdf)} road segments")
    return gdf


@_retry_with_fallback
def _fetch_water_chunk(south, west, north, east):
    """Fetch water features for a single bounding box chunk.

    Uses a single combined Overpass query for all water-related tags
    instead of 4 sequential queries.
    """
    bbox = _make_bbox(south, west, north, east)

    # Combine all water tags into ONE query — osmnx sends a single
    # Overpass request with OR logic across tags.
    combined_tags = {
        "natural": "water",
        "waterway": True,
        "landuse": "reservoir",
        "water": True,
    }

    try:
        gdf = ox.features_from_bbox(bbox=bbox, tags=combined_tags)
        if gdf.empty:
            return gpd.GeoDataFrame()
        return gdf
    except Exception as e:
        logger.debug(f"Water fetch failed: {e}")
        return gpd.GeoDataFrame()


def fetch_water(south: float, west: float, north: float, east: float,
                use_cache: bool = True,
                ttl_seconds: int = None) -> gpd.GeoDataFrame:
    """Fetch water features (rivers, lakes, etc.) from OpenStreetMap.

    Args:
        south, west, north, east: WGS84 bounding box
        use_cache: if False, force re-fetch from Overpass API
        ttl_seconds: cache TTL in seconds (None = use default)

    Returns:
        GeoDataFrame with water polygons and linestrings
    """
    if ttl_seconds is None:
        ttl_seconds = CACHE_TTL_SECONDS
    _setup_cache(use_cache, ttl_seconds)
    try:
        if use_cache:
            logger.info("Fetching water from OSM (cache: %s)", _OSM_CACHE_DIR)
        else:
            logger.info("Fetching water from OSM (cache disabled)")
        gdf = _tile_cached_fetch("water", _fetch_water_chunk, south, west, north, east)
    finally:
        _restore_cache()

    if gdf.empty:
        logger.info("No water features found")
        return gdf

    # Filter to geometries we can use
    valid_types = {"Polygon", "MultiPolygon", "LineString", "MultiLineString"}
    gdf = gdf[gdf.geometry.type.isin(valid_types)].copy()

    # Simplify geometry to reduce vertex count — water features often
    # have very high detail (hundreds of vertices along a riverbank)
    # that is unnecessary for 3D rendering.
    lat_range = north - south
    lon_range = east - west
    area_deg2 = lat_range * lon_range
    # Tolerance: smaller when high-detail water (e.g. 三潭印月) to avoid damaging islands/holes.
    factor = 0.0001 if get_water_high_detail() else 0.0005
    tol = max(0.00001, area_deg2 * factor)
    gdf["geometry"] = gdf["geometry"].simplify(tol, preserve_topology=True)

    # Drop tiny features that won't be visible
    gdf = gdf[~gdf.geometry.is_empty].copy()

    logger.info(f"Fetched {len(gdf)} water features")
    return gdf


# ---------------------------------------------------------------------------
# Vegetation
# ---------------------------------------------------------------------------

@_retry_with_fallback
def _fetch_vegetation_chunk(south, west, north, east):
    """Fetch vegetation/green-space features for a single bbox chunk."""
    from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.config import VEGETATION_TAGS
    bbox = _make_bbox(south, west, north, east)

    try:
        gdf = ox.features_from_bbox(bbox=bbox, tags=VEGETATION_TAGS)
        if gdf.empty:
            return gpd.GeoDataFrame()
        # Keep only polygons (vegetation areas, not points/lines)
        gdf = gdf[gdf.geometry.type.isin(["Polygon", "MultiPolygon"])].copy()
        return gdf
    except Exception as e:
        logger.debug(f"Vegetation fetch failed: {e}")
        return gpd.GeoDataFrame()


def fetch_vegetation(south: float, west: float, north: float, east: float,
                     use_cache: bool = True,
                     ttl_seconds: int = None) -> gpd.GeoDataFrame:
    """Fetch vegetation/green-space features from OpenStreetMap.

    Args:
        south, west, north, east: WGS84 bounding box
        use_cache: if False, force re-fetch from Overpass API
        ttl_seconds: cache TTL in seconds (None = use default)

    Returns:
        GeoDataFrame with vegetation polygons
    """
    if ttl_seconds is None:
        ttl_seconds = CACHE_TTL_SECONDS
    _setup_cache(use_cache, ttl_seconds)
    try:
        if use_cache:
            logger.info("Fetching vegetation from OSM (cache: %s)", _OSM_CACHE_DIR)
        else:
            logger.info("Fetching vegetation from OSM (cache disabled)")
        gdf = _tile_cached_fetch("vegetation", _fetch_vegetation_chunk, south, west, north, east)
    finally:
        _restore_cache()

    if gdf.empty:
        logger.info("No vegetation features found")
        return gdf

    # Simplify geometry (same approach as water)
    lat_range = north - south
    lon_range = east - west
    area_deg2 = lat_range * lon_range
    tol = max(0.00001, area_deg2 * 0.0005)
    gdf["geometry"] = gdf["geometry"].simplify(tol, preserve_topology=True)
    gdf = gdf[~gdf.geometry.is_empty].copy()

    logger.info(f"Fetched {len(gdf)} vegetation features")
    return gdf


# ---------------------------------------------------------------------------
# Parks (公园) & Wetlands (湿地)
# ---------------------------------------------------------------------------

@_retry_with_fallback
def _fetch_parks_chunk(south, west, north, east):
    """Fetch park/garden/recreation features for a single bbox chunk."""
    from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.config import PARKS_TAGS
    bbox = _make_bbox(south, west, north, east)
    try:
        gdf = ox.features_from_bbox(bbox=bbox, tags=PARKS_TAGS)
        if gdf.empty:
            return gpd.GeoDataFrame()
        gdf = gdf[gdf.geometry.type.isin(["Polygon", "MultiPolygon"])].copy()
        return gdf
    except Exception as e:
        logger.debug(f"Parks fetch failed: {e}")
        return gpd.GeoDataFrame()


def fetch_parks(south: float, west: float, north: float, east: float,
                use_cache: bool = True, ttl_seconds: int = None) -> gpd.GeoDataFrame:
    """Fetch park/garden/nature_reserve/recreation_ground from OSM."""
    if ttl_seconds is None:
        ttl_seconds = CACHE_TTL_SECONDS
    _setup_cache(use_cache, ttl_seconds)
    try:
        gdf = _tile_cached_fetch("park", _fetch_parks_chunk, south, west, north, east)
    finally:
        _restore_cache()
    if gdf.empty:
        return gdf
    lat_range = north - south
    lon_range = east - west
    area_deg2 = lat_range * lon_range
    tol = max(0.00001, area_deg2 * 0.0005)
    gdf["geometry"] = gdf["geometry"].simplify(tol, preserve_topology=True)
    gdf = gdf[~gdf.geometry.is_empty].copy()
    logger.info(f"Fetched {len(gdf)} park features")
    return gdf


@_retry_with_fallback
def _fetch_wetlands_chunk(south, west, north, east):
    """Fetch wetland/marsh/swamp features for a single bbox chunk."""
    from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.config import WETLANDS_TAGS
    bbox = _make_bbox(south, west, north, east)
    try:
        gdf = ox.features_from_bbox(bbox=bbox, tags=WETLANDS_TAGS)
        if gdf.empty:
            return gpd.GeoDataFrame()
        gdf = gdf[gdf.geometry.type.isin(["Polygon", "MultiPolygon"])].copy()
        return gdf
    except Exception as e:
        logger.debug(f"Wetlands fetch failed: {e}")
        return gpd.GeoDataFrame()


def fetch_wetlands(south: float, west: float, north: float, east: float,
                   use_cache: bool = True, ttl_seconds: int = None) -> gpd.GeoDataFrame:
    """Fetch wetland/marsh/swamp from OSM."""
    if ttl_seconds is None:
        ttl_seconds = CACHE_TTL_SECONDS
    _setup_cache(use_cache, ttl_seconds)
    try:
        gdf = _tile_cached_fetch("wetland", _fetch_wetlands_chunk, south, west, north, east)
    finally:
        _restore_cache()
    if gdf.empty:
        return gdf
    lat_range = north - south
    lon_range = east - west
    area_deg2 = lat_range * lon_range
    tol = max(0.00001, area_deg2 * 0.0005)
    gdf["geometry"] = gdf["geometry"].simplify(tol, preserve_topology=True)
    gdf = gdf[~gdf.geometry.is_empty].copy()
    logger.info(f"Fetched {len(gdf)} wetland features")
    return gdf
