"""Load OSM data from city/attaraction cache as fallback when Overpass API is unavailable.

Uses `_cache_index.json` to find city entries near the query bbox, then loads
their associated raw Overpass JSON files. Parses OSM elements (nodes -> ways ->
relations) into shapely geometries, clips to the requested bbox, and returns
a GeoDataFrame matching what osm.py fetch functions return.

Because the SHA-1 hash filenames from historical queries don't match live
osmnx queries, this module parses the raw JSON directly instead of relying
on osmnx's cache lookup.

Integration:
    osm.py calls load_features() as fallback in _tile_cached_fetch()
    after Overpass API fails.
"""

import json
import logging
import os
import time
from typing import Dict, List, Optional, Tuple

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Polygon

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
def _get_cache_index_path():
    """获取缓存索引文件路径，支持跨平台和环境变量"""
    env_cache_dir = os.environ.get("MAP_GEN_CACHE_DIR")
    if env_cache_dir:
        return os.path.join(env_cache_dir, "_cache_index.json")
    
    # 平台相关默认路径
    if os.name == 'nt':  # Windows
        return "F:/map_gen_cache/_cache_index.json"
    else:  # macOS / Linux
        return os.path.expanduser("~/map_gen_cache/_cache_index.json")

CACHE_INDEX_PATH = _get_cache_index_path()


def _get_cache_file_roots():
    """获取缓存文件根目录，支持跨平台和环境变量"""
    env_cache_dir = os.environ.get("MAP_GEN_CACHE_DIR")
    if env_cache_dir:
        return {
            "city": os.path.join(env_cache_dir, "city/cache/osm"),
            "attaraction": os.path.join(env_cache_dir, "attaraction/cache/osm"),
            "project": os.path.join(env_cache_dir, "project_cache/osm"),
        }
    
    # 平台相关默认路径
    if os.name == 'nt':  # Windows
        return {
            "city": "F:/map_gen_cache/city/cache/osm",
            "attaraction": "F:/map_gen_cache/attaraction/cache/osm",
            "project": "F:/map_gen_cache/project_cache/osm",
        }
    else:  # macOS / Linux
        home_cache = os.path.expanduser("~/map_gen_cache")
        return {
            "city": os.path.join(home_cache, "city/cache/osm"),
            "attaraction": os.path.join(home_cache, "attaraction/cache/osm"),
            "project": os.path.join(home_cache, "project_cache/osm"),
        }

CACHE_FILE_ROOTS = _get_cache_file_roots()

# ---------------------------------------------------------------------------
# Tag type -> Overpass tag filter mapping
# Matches the filters used in osm.py fetch functions
# ---------------------------------------------------------------------------
TAG_FILTERS = {
    "building": {"building": True},
    "road": {"highway": True},
    "water": {
        "natural": "water",
        "waterway": True,
        "landuse": "reservoir",
        "water": True,
    },
    "vegetation": {
        "landuse": ["forest", "grass", "meadow", "village_green"],
        "natural": ["wood", "grassland", "scrub", "heath"],
    },
    "park": {
        "leisure": ["park", "garden", "nature_reserve"],
        "landuse": ["recreation_ground"],
    },
    "wetland": {
        "natural": ["wetland", "marsh", "swamp"],
    },
}

# Geometry types to keep per tag type
VALID_GEOM_TYPES = {
    "building": {"Polygon", "MultiPolygon"},
    "road": {"LineString", "MultiLineString"},
    "water": {"Polygon", "MultiPolygon", "LineString", "MultiLineString"},
    "vegetation": {"Polygon", "MultiPolygon"},
    "park": {"Polygon", "MultiPolygon"},
    "wetland": {"Polygon", "MultiPolygon"},
}

# Search radius (degrees) around query bbox for finding nearby city entries
CITY_SEARCH_RADIUS_DEG = 1.0

# ---------------------------------------------------------------------------
# Cache index loader
# ---------------------------------------------------------------------------
_cache_index: Optional[dict] = None


def _get_cache_index() -> dict:
    """Load and return the cache index (cached in memory)."""
    global _cache_index
    if _cache_index is not None:
        return _cache_index

    t0 = time.time()
    try:
        with open(CACHE_INDEX_PATH, "r", encoding="utf-8") as f:
            _cache_index = json.load(f)
        logger.info(
            "Loaded cache index: %d entries in %.1fs",
            len(_cache_index), time.time() - t0,
        )
    except Exception as e:
        logger.warning("Failed to load cache index: %s", e)
        _cache_index = {}
    return _cache_index


def _find_nearby_cities(
    south: float, west: float, north: float, east: float
) -> List[Tuple[str, dict]]:
    """Find city entries in the cache index near the query bbox.

    Returns list of (city_key, city_data) tuples.
    """
    index = _get_cache_index()
    if not index:
        return []

    center_lat = (south + north) / 2
    center_lon = (west + east) / 2

    nearby = []
    for key, data in index.items():
        try:
            # Parse lat/lon from key like "Hangzhou|30.2741|120.1551"
            parts = key.split("|")
            if len(parts) >= 3:
                city_lat = float(parts[-2])
                city_lon = float(parts[-1])
            else:
                city_lat = float(data.get("lat", 0))
                city_lon = float(data.get("lon", 0))

            if (
                abs(city_lat - center_lat) <= CITY_SEARCH_RADIUS_DEG
                and abs(city_lon - center_lon) <= CITY_SEARCH_RADIUS_DEG
            ):
                nearby.append((key, data))
        except (ValueError, IndexError):
            continue

    return nearby


def _resolve_cache_path(relative_path: str) -> Optional[str]:
    """Resolve a relative cache path like 'city/xxx.json' to absolute path.

    The cache_index stores paths like:
        "city/0019fdccc9cbfdb5aa5bc306f9e86f3d568d4c6a.json"
        "attaraction/0e12e7a7a0bfa6def7244207291027f4bca1df34.json"

    The first segment is the root key in CACHE_FILE_ROOTS.
    """
    parts = relative_path.replace("\\", "/").split("/", 1)
    if len(parts) < 2:
        return None
    root_key, subpath = parts
    root = CACHE_FILE_ROOTS.get(root_key)
    if root is None:
        return None
    full_path = os.path.join(root, subpath)
    return full_path if os.path.isfile(full_path) else None


# ---------------------------------------------------------------------------
# Overpass JSON -> Geometry parsing
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Process-level cache: avoid re-parsing the same JSON file for multiple tiles
# ---------------------------------------------------------------------------
_parsed_file_cache: Dict[str, Tuple[Dict, List, List]] = {}


def _parse_osm_json(
    filepath: str,
) -> Tuple[Dict[int, Tuple[float, float]], List[dict], List[dict]]:
    """Parse Overpass JSON into nodes/ways/relations (cached per file)."""
    if filepath in _parsed_file_cache:
        return _parsed_file_cache[filepath]

    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    nodes: Dict[int, Tuple[float, float]] = {}
    ways: List[dict] = []
    relations: List[dict] = []

    for elem in data.get("elements", []):
        t = elem.get("type")
        if t == "node":
            nodes[elem["id"]] = (elem["lat"], elem["lon"])
        elif t == "way":
            ways.append({
                "id": elem["id"],
                "nodes": elem.get("nodes", []),
                "tags": elem.get("tags", {}),
            })
        elif t == "relation":
            relations.append({
                "id": elem["id"],
                "members": elem.get("members", []),
                "tags": elem.get("tags", {}),
            })

    result = (nodes, ways, relations)
    _parsed_file_cache[filepath] = result
    return result


def _way_to_geometry(
    nodes: Dict[int, Tuple[float, float]], way: dict
) -> Optional[object]:
    """Convert an OSM way to a shapely geometry.

    Returns LineString for open ways, Polygon for closed ways.
    Coordinates in (lon, lat) order (X, Y for shapely).
    """
    coords = []
    for node_id in way.get("nodes", []):
        if node_id in nodes:
            lat, lon = nodes[node_id]
            coords.append((lon, lat))

    if len(coords) < 2:
        return None

    if len(coords) >= 4 and coords[0] == coords[-1]:
        try:
            return Polygon(coords)
        except Exception:
            return LineString(coords)
    else:
        return LineString(coords)


def _relation_to_multipolygon(
    nodes: Dict[int, Tuple[float, float]],
    ways: List[dict],
    relation: dict,
) -> Optional[object]:
    """Convert a multipolygon relation to a (Multi)Polygon."""
    way_map = {w["id"]: w for w in ways}
    outer_rings = []
    inner_rings = []

    for member in relation.get("members", []):
        if member.get("type") != "way":
            continue
        way = way_map.get(member["ref"])
        if way is None:
            continue
        geom = _way_to_geometry(nodes, way)
        if geom is None:
            continue

        if member.get("role") == "inner":
            inner_rings.append(geom)
        else:
            outer_rings.append(geom)

    if not outer_rings:
        return None

    polygons = []
    for outer in outer_rings:
        # Skip non-polygon outer rings (open ways can't form polygons)
        if outer.geom_type not in ("Polygon", "MultiPolygon"):
            continue
        # Collect interior rings contained by this outer ring
        interiors = []
        for inner in inner_rings:
            if inner.geom_type not in ("Polygon", "MultiPolygon"):
                # Try to close LineString into Polygon
                try:
                    inner = Polygon(inner)
                except Exception:
                    continue
            if not inner.is_valid:
                inner = inner.buffer(0)
            if not inner.is_empty and outer.contains(inner):
                if inner.geom_type == "Polygon":
                    interiors.append(list(inner.exterior.coords))
        try:
            poly = Polygon(outer.exterior.coords, interiors)
            polygons.append(poly)
        except Exception:
            polygons.append(outer)

    if not polygons:
        return None
    if len(polygons) == 1:
        return polygons[0]
    return MultiPolygon(polygons)


def _matches_tag_filter(tags: dict, tag_filter: dict) -> bool:
    """Check if OSM tags match a given filter (OR logic across conditions).

    Filter format examples:
        {"highway": True}           -> any highway tag value
        {"building": True}          -> any building tag value
        {"natural": "water"}        -> natural=water exactly
        {"landuse": ["forest","grass"]} -> landuse in list
    """
    for key, value in tag_filter.items():
        if key not in tags:
            continue
        if value is True:
            return True  # tag exists with any value
        if isinstance(value, (list, set, tuple)):
            if tags[key] in value:
                return True
        elif tags[key] == value:
            return True
    return False


def _clip_to_bbox(geom, bbox: Tuple[float, float, float, float]) -> Optional[object]:
    """Clip geometry to WGS84 bbox (south, west, north, east)."""
    if geom is None or geom.is_empty:
        return None
    from shapely import box

    bbox_poly = box(bbox[1], bbox[0], bbox[3], bbox[2])
    try:
        clipped = geom.intersection(bbox_poly)
        return clipped if not clipped.is_empty else None
    except Exception:
        return geom if geom.intersects(bbox_poly) else None


def _compute_bbox_from_nodes(
    nodes: Dict[int, Tuple[float, float]],
) -> Optional[Tuple[float, float, float, float]]:
    """Compute (south, west, north, east) from a node dict."""
    if not nodes:
        return None
    lats = [lat for lat, _ in nodes.values()]
    lons = [lon for _, lon in nodes.values()]
    return (min(lats), min(lons), max(lats), max(lons))


def _file_has_data_for_query(
    filepath: str,
    query_bbox: Tuple[float, float, float, float],
    tag_filter: dict,
) -> bool:
    """Quick check if a cache file has relevant data for the query."""
    try:
        nodes, ways, _ = _parse_osm_json(filepath)

        # Check bbox overlap
        file_bbox = _compute_bbox_from_nodes(nodes)
        if file_bbox is None:
            return False

        qs, qw, qn, qe = query_bbox
        fs, fw, fn, fe = file_bbox
        if not (fs < qn and fn > qs and fw < qe and fe > qw):
            return False

        # Check tag relevance
        return any(_matches_tag_filter(w.get("tags", {}), tag_filter) for w in ways)

    except Exception:
        return False


def _gather_features(
    tag_filter: dict,
    valid_types: set,
    query_bbox: Tuple[float, float, float, float],
    candidate_files: List[str],
) -> List[dict]:
    """Parse candidate files and collect features matching the filter + bbox.

    Uses _parsed_file_cache to avoid re-parsing files across calls.

    Returns:
        List of feature dicts with 'geometry' key.
    """
    all_features: List[dict] = []

    for rel_path in candidate_files:
        abs_path = _resolve_cache_path(rel_path)
        if abs_path is None:
            continue

        try:
            nodes, ways, relations = _parse_osm_json(abs_path)
        except Exception as e:
            logger.debug("Parse error %s: %s", os.path.basename(abs_path), e)
            continue

        # Quick bbox check
        file_bbox = _compute_bbox_from_nodes(nodes)
        if file_bbox is None:
            continue
        qs, qw, qn, qe = query_bbox
        fs, fw, fn, fe = file_bbox
        if not (fs < qn and fn > qs and fw < qe and fe > qw):
            continue

        # Check tag relevance
        has_relevant = any(
            _matches_tag_filter(w.get("tags", {}), tag_filter) for w in ways
        )
        if not has_relevant:
            continue

        # Extract matching features
        for way in ways:
            tags = way.get("tags", {})
            if not tags or not _matches_tag_filter(tags, tag_filter):
                continue
            geom = _way_to_geometry(nodes, way)
            if geom is None or geom.is_empty:
                continue
            geom = _clip_to_bbox(geom, query_bbox)
            if geom is None:
                continue
            if valid_types and geom.geom_type not in valid_types:
                continue
            feature = dict(tags)
            feature["geometry"] = geom
            all_features.append(feature)

        for relation in relations:
            tags = relation.get("tags", {})
            if not tags:
                continue
            if tags.get("type") != "multipolygon":
                continue
            if not _matches_tag_filter(tags, tag_filter):
                continue
            geom = _relation_to_multipolygon(nodes, ways, relation)
            if geom is None or geom.is_empty:
                continue
            geom = _clip_to_bbox(geom, query_bbox)
            if geom is None:
                continue
            if valid_types and geom.geom_type not in valid_types:
                continue
            feature = dict(tags)
            feature["geometry"] = geom
            all_features.append(feature)

    return all_features


# ---------------------------------------------------------------------------
# Main public API
# ---------------------------------------------------------------------------

def load_features(
    tag_type: str,
    south: float,
    west: float,
    north: float,
    east: float,
) -> gpd.GeoDataFrame:
    """Load OSM features for a given tag type and bbox from city cache.

    Uses _cache_index.json to find cache files near the query area,
    parses raw Overpass JSON, clips to bbox, returns GeoDataFrame.

    Args:
        tag_type: One of "building", "road", "water", "vegetation", "park", "wetland"
        south, west, north, east: WGS84 bounding box

    Returns:
        GeoDataFrame (WGS84) with features, or empty if no data.
    """
    t0 = time.time()

    if tag_type not in TAG_FILTERS:
        logger.warning("Unknown tag_type '%s', skipping city cache", tag_type)
        return gpd.GeoDataFrame()

    tag_filter = TAG_FILTERS[tag_type]
    query_bbox = (south, west, north, east)

    # Find nearby cities in the cache index
    nearby = _find_nearby_cities(south, west, north, east)
    if not nearby:
        logger.info(
            "City cache [%s]: no nearby cities for (%.2f, %.2f, %.2f, %.2f)",
            tag_type, south, west, north, east,
        )
        return gpd.GeoDataFrame()

    logger.info(
        "City cache [%s]: %d nearby cities, gathering files...",
        tag_type, len(nearby),
    )

    # Collect all cache files from nearby cities
    candidate_files = []
    for _city_key, city_data in nearby:
        cache_files = city_data.get("cache_files", {}).get("osm", [])
        candidate_files.extend(cache_files)

    candidate_files = list(set(candidate_files))  # deduplicate
    logger.info(
        "City cache [%s]: %d candidate files", tag_type, len(candidate_files),
    )

    # Gather features from matching files (uses process-level parse cache)
    valid_types = VALID_GEOM_TYPES.get(tag_type)
    all_features = _gather_features(tag_filter, valid_types, query_bbox, candidate_files)

    if not all_features:
        logger.info(
            "City cache [%s]: no features for bbox (%.2f, %.2f, %.2f, %.2f)",
            tag_type, south, west, north, east,
        )
        return gpd.GeoDataFrame()

    gdf = gpd.GeoDataFrame(all_features, geometry="geometry", crs="EPSG:4326")

    logger.info(
        "City cache [%s]: %d features in %.1fs",
        tag_type, len(gdf), time.time() - t0,
    )
    return gdf


def load_buildings(
    south: float,
    west: float,
    north: float,
    east: float,
    min_area_m2: float = 0,
) -> gpd.GeoDataFrame:
    """Load building footprints from city cache.

    Adds est_height column matching osmnx output format.
    """
    gdf = load_features("building", south, west, north, east)
    if gdf.empty:
        return gdf

    gdf["est_height"] = _estimate_building_heights(gdf)

    if min_area_m2 > 0:
        gdf["_area"] = gdf.geometry.area
        gdf = gdf[gdf["_area"] >= min_area_m2].copy()
        gdf = gdf.drop(columns=["_area"])

    return gdf


def load_roads(
    south: float,
    west: float,
    north: float,
    east: float,
    highway_filter: set = None,
) -> gpd.GeoDataFrame:
    """Load road network from city cache."""
    gdf = load_features("road", south, west, north, east)
    if gdf.empty:
        return gdf

    if highway_filter and "highway" in gdf.columns:
        def _matches(hw):
            if isinstance(hw, list):
                return any(h in highway_filter for h in hw)
            return hw in highway_filter
        gdf = gdf[gdf["highway"].apply(_matches)].copy()

    return gdf


def load_water(
    south: float,
    west: float,
    north: float,
    east: float,
) -> gpd.GeoDataFrame:
    """Load water features from city cache (with simplification)."""
    gdf = load_features("water", south, west, north, east)
    if gdf.empty:
        return gdf

    # Simplify geometry (same approach as osm.py fetch_water)
    lat_range = north - south
    lon_range = east - west
    area_deg2 = lat_range * lon_range
    tol = max(0.00001, area_deg2 * 0.0005)
    gdf["geometry"] = gdf["geometry"].simplify(tol, preserve_topology=True)
    gdf = gdf[~gdf.geometry.is_empty].copy()

    return gdf


def _estimate_building_heights(gdf: gpd.GeoDataFrame) -> pd.Series:
    """Estimate building heights from OSM tags (matching osm.py logic)."""
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
