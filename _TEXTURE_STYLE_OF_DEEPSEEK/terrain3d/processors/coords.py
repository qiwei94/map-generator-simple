"""Coordinate projection utilities: WGS84 <-> UTM."""

import numpy as np
from pyproj import Transformer, CRS
import geopandas as gpd


def get_utm_zone(lon: float, lat: float) -> int:
    """Calculate UTM zone number from longitude/latitude."""
    zone = int((lon + 180) / 6) + 1
    return zone


def get_utm_crs(lon: float, lat: float) -> CRS:
    """Get the UTM CRS for a given longitude/latitude."""
    zone = get_utm_zone(lon, lat)
    hemisphere = "north" if lat >= 0 else "south"
    return CRS(f"+proj=utm +zone={zone} +{hemisphere} +datum=WGS84")


def create_transformers(center_lon: float, center_lat: float):
    """Create forward (WGS84->UTM) and inverse (UTM->WGS84) transformers."""
    utm_crs = get_utm_crs(center_lon, center_lat)
    wgs84 = CRS("EPSG:4326")
    forward = Transformer.from_crs(wgs84, utm_crs, always_xy=True)
    inverse = Transformer.from_crs(utm_crs, wgs84, always_xy=True)
    return forward, inverse, utm_crs


def bbox_to_utm(lat1: float, lon1: float, lat2: float, lon2: float):
    """Convert WGS84 bounding box to UTM coordinates.

    Returns:
        dict with keys:
            utm_bbox: (min_x, min_y, max_x, max_y) in meters
            origin: (origin_x, origin_y) center of bbox in UTM
            width_m: width in meters
            height_m: height in meters
            area_km2: area in square kilometers
            utm_crs: the UTM CRS object
            transformer: forward transformer (WGS84->UTM)
            inverse_transformer: inverse transformer (UTM->WGS84)
    """
    # Normalize bbox
    south = min(lat1, lat2)
    north = max(lat1, lat2)
    west = min(lon1, lon2)
    east = max(lon1, lon2)

    center_lon = (west + east) / 2
    center_lat = (south + north) / 2

    forward, inverse, utm_crs = create_transformers(center_lon, center_lat)

    # Transform corners
    min_x, min_y = forward.transform(west, south)
    max_x, max_y = forward.transform(east, north)

    # Compute origin (center)
    origin_x = (min_x + max_x) / 2
    origin_y = (min_y + max_y) / 2

    width_m = max_x - min_x
    height_m = max_y - min_y
    area_km2 = (width_m * height_m) / 1e6

    return {
        "utm_bbox": (min_x, min_y, max_x, max_y),
        "origin": (origin_x, origin_y),
        "width_m": width_m,
        "height_m": height_m,
        "area_km2": area_km2,
        "utm_crs": utm_crs,
        "transformer": forward,
        "inverse_transformer": inverse,
        "wgs84_bbox": (south, west, north, east),
    }


def latlon_to_local(lat: float, lon: float, origin: tuple, transformer) -> tuple:
    """Convert a single lat/lon to local coordinates relative to origin."""
    x, y = transformer.transform(lon, lat)
    return (x - origin[0], y - origin[1])


def latlon_grid_to_local(lats: np.ndarray, lons: np.ndarray,
                         origin: tuple, transformer) -> tuple:
    """Convert arrays of lat/lon to local coordinate arrays."""
    xs, ys = transformer.transform(lons, lats)
    return (xs - origin[0], ys - origin[1])


def project_geodataframe(gdf: gpd.GeoDataFrame, utm_crs: CRS,
                         origin: tuple, clip_bbox: tuple = None) -> gpd.GeoDataFrame:
    """Project a GeoDataFrame to local UTM coordinates centered at origin.

    Args:
        gdf: input GeoDataFrame in WGS84
        utm_crs: target UTM CRS
        origin: (origin_x, origin_y) in UTM meters
        clip_bbox: if provided, (min_x, min_y, max_x, max_y) in UTM
                   to clip geometries to the local extent
    """
    if gdf.empty:
        return gdf

    # Project to UTM
    gdf_utm = gdf.to_crs(utm_crs)

    # Translate to local origin
    from shapely.affinity import translate
    gdf_utm["geometry"] = gdf_utm["geometry"].apply(
        lambda geom: translate(geom, xoff=-origin[0], yoff=-origin[1])
    )

    # Clip to local bounding box
    if clip_bbox is not None:
        from shapely.geometry import box
        min_x, min_y, max_x, max_y = clip_bbox
        # Convert to local coords
        local_min_x = min_x - origin[0]
        local_min_y = min_y - origin[1]
        local_max_x = max_x - origin[0]
        local_max_y = max_y - origin[1]
        clip_box = box(local_min_x, local_min_y, local_max_x, local_max_y)

        gdf_utm = gdf_utm.copy()
        # Fix invalid geometries before intersection (buffer(0) repairs self-intersections)
        gdf_utm["geometry"] = gdf_utm["geometry"].apply(
            lambda geom: geom.buffer(0) if not geom.is_valid else geom
        )
        try:
            gdf_utm["geometry"] = gdf_utm["geometry"].intersection(clip_box)
        except Exception:
            # Fallback: intersect one-by-one, skipping problematic geometries
            safe_geoms = []
            for geom in gdf_utm["geometry"]:
                try:
                    repaired = geom.buffer(0) if not geom.is_valid else geom
                    result = repaired.intersection(clip_box)
                    safe_geoms.append(result)
                except Exception:
                    safe_geoms.append(None)
            from shapely.geometry import GeometryCollection
            gdf_utm["geometry"] = [g if g is not None else GeometryCollection() for g in safe_geoms]
        # Remove empty geometries
        gdf_utm = gdf_utm[~gdf_utm.geometry.is_empty].copy()

    return gdf_utm
