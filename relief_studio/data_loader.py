"""数据加载器：Overture parquet（建筑+高度）+ OSM GeoJSON（水体）."""

import numpy as np
import pandas as pd
import geopandas as gpd
from shapely import wkb
from shapely.geometry import box
from pyproj import Transformer, CRS


def load_buildings_overture(
    parquet_path: str,
    bbox_wgs84: tuple[float, float, float, float],
    utm_epsg: int | None = None,
) -> tuple[gpd.GeoDataFrame, int]:
    """从 Overture parquet 加载建筑，裁剪到 bbox，投影到 UTM.

    Args:
        parquet_path: overture parquet 文件路径
        bbox_wgs84: (min_lon, min_lat, max_lon, max_lat)
        utm_epsg: 指定 UTM EPSG，None 则自动推断

    Returns:
        (buildings_gdf_utm, utm_epsg)
    """
    df = pd.read_parquet(parquet_path)

    # WKB → shapely geometry
    geoms = df["geometry"].apply(lambda b: wkb.loads(b, hex=False))
    gdf = gpd.GeoDataFrame(df, geometry=geoms, crs="EPSG:4326")

    # Bbox 裁剪
    min_lon, min_lat, max_lon, max_lat = bbox_wgs84
    bbox_poly = box(min_lon, min_lat, max_lon, max_lat)
    gdf = gdf[gdf.intersects(bbox_poly)].copy()

    # 只保留有高度的
    gdf = gdf[gdf["height"].notna() & (gdf["height"] > 0)].copy()

    # 自动 UTM
    if utm_epsg is None:
        center_lon = (min_lon + max_lon) / 2
        center_lat = (min_lat + max_lat) / 2
        utm_epsg = _latlon_to_utm_epsg(center_lat, center_lon)

    gdf_utm = gdf.to_crs(epsg=utm_epsg)

    print(f"  [data_loader] buildings: {len(gdf_utm)} (with height), utm={utm_epsg}")
    return gdf_utm, utm_epsg


def load_water_osm(
    geojson_path: str,
    bbox_wgs84: tuple[float, float, float, float],
    utm_epsg: int,
) -> gpd.GeoDataFrame:
    """从 OSM GeoJSON 加载水体多边形.

    Args:
        geojson_path: osmium water geojson 路径
        bbox_wgs84: (min_lon, min_lat, max_lon, max_lat)
        utm_epsg: 目标 UTM EPSG

    Returns:
        water_gdf_utm (仅 Polygon/MultiPolygon)
    """
    gdf = gpd.read_file(geojson_path)

    # 只保留面状水体
    gdf = gdf[gdf.geometry.type.isin(["Polygon", "MultiPolygon"])].copy()

    # Bbox 裁剪
    min_lon, min_lat, max_lon, max_lat = bbox_wgs84
    bbox_poly = box(min_lon, min_lat, max_lon, max_lat)
    gdf = gdf[gdf.intersects(bbox_poly)].copy()

    # 投影
    gdf_utm = gdf.to_crs(epsg=utm_epsg)

    print(f"  [data_loader] water polygons: {len(gdf_utm)}")
    return gdf_utm


def load_roads_osm(
    geojson_path: str,
    bbox_wgs84: tuple[float, float, float, float],
    utm_epsg: int,
    min_tier: int = 3,
) -> gpd.GeoDataFrame:
    """从 OSM GeoJSON 加载道路线.

    Args:
        geojson_path: osmium road geojson 路径
        bbox_wgs84: (min_lon, min_lat, max_lon, max_lat)
        utm_epsg: 目标 UTM EPSG
        min_tier: 最低道路等级 (1=motorway, 5=residential)

    Returns:
        roads_gdf_utm (LineString/MultiLineString)
    """
    gdf = gpd.read_file(geojson_path)

    # 只保留线状
    gdf = gdf[gdf.geometry.type.isin(["LineString", "MultiLineString"])].copy()

    # 道路等级过滤
    TIER_MAP = {
        "motorway": 1, "motorway_link": 1,
        "trunk": 1, "trunk_link": 1,
        "primary": 2, "primary_link": 2,
        "secondary": 3, "secondary_link": 3,
        "tertiary": 4, "tertiary_link": 4,
        "residential": 5, "unclassified": 5, "living_street": 5,
    }
    if "highway" in gdf.columns:
        gdf["tier"] = gdf["highway"].map(TIER_MAP).fillna(9)
        gdf = gdf[gdf["tier"] <= min_tier].copy()

    # Bbox 裁剪
    min_lon, min_lat, max_lon, max_lat = bbox_wgs84
    bbox_poly = box(min_lon, min_lat, max_lon, max_lat)
    gdf = gdf[gdf.intersects(bbox_poly)].copy()

    gdf_utm = gdf.to_crs(epsg=utm_epsg)
    print(f"  [data_loader] roads: {len(gdf_utm)} (tier<={min_tier})")
    return gdf_utm


def _latlon_to_utm_epsg(lat: float, lon: float) -> int:
    """根据经纬度推断 UTM EPSG."""
    zone = int((lon + 180) / 6) + 1
    if lat >= 0:
        return 32600 + zone
    else:
        return 32700 + zone
