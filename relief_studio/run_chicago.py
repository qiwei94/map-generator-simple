"""芝加哥建筑浮雕 — 对标 Reso/lution Urban Series 28.

数据源：
- 建筑：Overture Maps parquet（91.2% 高度覆盖）
- 水体：OSM osmium export GeoJSON

用法：
    python -m relief_studio.run_chicago
"""

import os
import sys
import time

import pandas as pd
from shapely.geometry import Polygon
import geopandas as gpd

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from relief_studio.data_loader import load_buildings_overture, load_water_osm
from relief_studio.relief_map import build_relief_heightmap
from relief_studio.renderer import render_relief


def _lake_michigan_polygon():
    """密歇根湖简化多边形（WGS84）.

    基于实际海岸线关键点插值。
    """
    # 芝加哥段海岸线关键点 (lon, lat)，从南到北
    shoreline_points = [
        (-87.52, 41.70),  # Hyde Park 南岸
        (-87.57, 41.75),  # South Shore
        (-87.58, 41.80),  # Burnham Park
        (-87.60, 41.85),  # Museum Campus
        (-87.607, 41.88),  # Downtown (Navy Pier)
        (-87.62, 41.89),  # Ohio Street Beach
        (-87.63, 41.91),  # Oak Street Beach
        (-87.65, 41.94),  # Lincoln Park
        (-87.66, 41.97),  # Belmont
        (-87.67, 42.00),  # Hollywood
        (-87.68, 42.05),  # Rogers Park
    ]
    # 闭合多边形：海岸线 + 向东延伸到 bbox 外
    east_edge = -87.40  # bbox 东边界外
    poly_coords = shoreline_points + [
        (east_edge, 42.05),
        (east_edge, 41.70),
    ]
    return Polygon(poly_coords)


def main():
    t0 = time.time()
    print("=" * 60)
    print("  Relief Studio — Chicago")
    print("  对标: Reso/lution Urban Series 28")
    print("=" * 60)

    # ── 配置 ──
    # 芝加哥 bbox（与现有管线一致）
    BBOX_WGS84 = (-87.77, 41.77, -87.47, 41.99)  # min_lon, min_lat, max_lon, max_lat

    # 数据路径
    OVERTURE_PARQUET = os.path.join(
        _project_root, "data", "height_cache", "overture_41.88_-87.62.parquet"
    )
    WATER_GEOJSON = os.path.join(
        _project_root, "tmp", "osmium_water_41.7700_-87.7700_41.9900_-87.4700.geojson"
    )
    OUTPUT_DIR = os.path.join(_project_root, "relief_studio", "output")

    # ── 1. 加载数据 ──
    print("\n[1/3] Loading data...")
    buildings_gdf, utm_epsg = load_buildings_overture(OVERTURE_PARQUET, BBOX_WGS84)
    water_gdf = load_water_osm(WATER_GEOJSON, BBOX_WGS84, utm_epsg)

    # 添加密歇根湖（OSM 小水体提取抓不到大湖）
    lake_poly = _lake_michigan_polygon()
    lake_gdf = gpd.GeoDataFrame(geometry=[lake_poly], crs="EPSG:4326")
    lake_gdf = lake_gdf.to_crs(epsg=utm_epsg)
    water_gdf = gpd.GeoDataFrame(
        pd.concat([water_gdf, lake_gdf], ignore_index=True),
        crs=f"EPSG:{utm_epsg}",
    )
    print(f"  [run_chicago] water with Lake Michigan: {len(water_gdf)} polygons")

    # ── 2. 构建高度图 ──
    print("\n[2/3] Building relief heightmap...")
    # 计算 UTM bbox
    from shapely.geometry import box
    from pyproj import Transformer

    transformer = Transformer.from_crs("EPSG:4326", f"EPSG:{utm_epsg}", always_xy=True)
    min_x, min_y = transformer.transform(BBOX_WGS84[0], BBOX_WGS84[1])
    max_x, max_y = transformer.transform(BBOX_WGS84[2], BBOX_WGS84[3])
    bbox_utm = (min_x, min_y, max_x, max_y)

    relief_data = build_relief_heightmap(
        buildings_gdf,
        water_gdf,
        bbox_utm,
        grid_size=2048,
        height_cap=400.0,
        height_floor=2.0,
    )

    # ── 3. 渲染 ──
    print("\n[3/3] Rendering...")

    # 风格 A：参考作品风格（白底黑水）
    render_relief(
        relief_data,
        os.path.join(OUTPUT_DIR, "chicago_mono_light.png"),
        city_name="Chicago",
        style="mono_light",
        z_exaggeration=3.0,
        light_azimuth=315.0,
        light_altitude=45.0,
        ao_radius=4,
        ao_strength=0.35,
        edge_strength=0.25,
        grain_strength=0.025,
        height_gamma=0.55,
        output_size_px=3000,
    )

    # 风格 B：暗色戏剧性
    render_relief(
        relief_data,
        os.path.join(OUTPUT_DIR, "chicago_mono_dark.png"),
        city_name="Chicago",
        style="mono_dark",
        z_exaggeration=4.0,
        light_azimuth=315.0,
        light_altitude=40.0,
        ao_radius=5,
        ao_strength=0.45,
        edge_strength=0.30,
        grain_strength=0.02,
        height_gamma=0.5,
        output_size_px=3000,
    )

    elapsed = time.time() - t0
    print(f"\n{'=' * 60}")
    print(f"  Done! ({elapsed:.1f}s)")
    print(f"  Output: {OUTPUT_DIR}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
