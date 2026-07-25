"""杭州建筑浮雕 — 西湖 + 钱塘江.

数据源：
- 建筑：Overture hangzhou_buildings.parquet（25.7万栋，2%有真实高度）
- 水体：OSM osmium export GeoJSON（含西湖、钱塘江）

策略：
- 有高度的 5153 栋：用真实值（mean=86m, max=310m）
- 无高度的 25.2 万栋：按 footprint 面积估算
  - <200m² → 6m（低层住宅）
  - 200-1000m² → 10m（多层）
  - 1000-5000m² → 15m（商业）
  - >5000m² → 20m（大型公共建筑）

用法：
    python -m relief_studio.run_hangzhou
"""

import os
import sys
import time

import numpy as np
import pandas as pd
import geopandas as gpd
from shapely import wkb
from shapely.geometry import Polygon

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from relief_studio.relief_map import build_relief_heightmap
from relief_studio.renderer import render_relief
from relief_studio.data_loader import load_water_osm, _latlon_to_utm_epsg


def _estimate_height(area_m2: float) -> float:
    """根据建筑面积估算高度."""
    if area_m2 < 200:
        return 6.0
    elif area_m2 < 1000:
        return 10.0
    elif area_m2 < 5000:
        return 15.0
    else:
        return 20.0


def _grand_canal_bridge_polygon():
    """大运河（江南运河）缺口桥接多边形（WGS84）.

    问题：OSM 与高德瓦片两个独立数据源在 lat 30.2627~30.2765 之间
    均缺失运河水面（约 1.5km），疑似城市箱涵/暗渠段或两者皆漏绘。
    为使运河在浮雕图中连续，依据两端实测河道断面手动桥接：
      - 南端：南侧宽阔水域北缘，河道中心 lon≈120.1525，宽 ~150m
      - 北端：北侧运河河道（lat 30.279 实测断面 lon[120.1564,120.1571]，
        中心 lon≈120.1567，宽 ~66m），桥接取 ~80m
      - 走向：略向东北（与运河实际走向一致）
      - 两端各向外延伸少许，与既有水面重叠保证无缝
    """
    return Polygon([
        (120.15172, 30.2618),  # 南端西岸
        (120.15328, 30.2618),  # 南端东岸（~150m 宽）
        (120.15712, 30.2772),  # 北端东岸
        (120.15628, 30.2772),  # 北端西岸（~80m 宽）
    ])


def main():
    t0 = time.time()
    print("=" * 60)
    print("  Relief Studio — Hangzhou")
    print("  West Lake + Qiantang River")
    print("=" * 60)

    # ── 配置 ──
    BBOX_WGS84 = (120.01, 30.13, 120.29, 30.36)  # min_lon, min_lat, max_lon, max_lat
    PARQUET_PATH = os.path.join(
        _project_root, "data", "height_cache", "hangzhou_buildings.parquet"
    )
    WATER_GEOJSON = os.path.join(
        _project_root, "tmp", "osmium_water_30.1300_120.0100_30.3600_120.2900.geojson"
    )
    OUTPUT_DIR = os.path.join(_project_root, "relief_studio", "output")

    # ── 1. 加载建筑 ──
    print("\n[1/4] Loading buildings from Overture...")
    df = pd.read_parquet(PARQUET_PATH)
    geoms = df["geometry"].apply(lambda b: wkb.loads(b, hex=False))
    gdf = gpd.GeoDataFrame(df, geometry=geoms, crs="EPSG:4326")

    # Bbox 裁剪
    from shapely.geometry import box
    bbox_poly = box(*BBOX_WGS84)
    gdf = gdf[gdf.intersects(bbox_poly)].copy()
    print(f"  buildings in bbox: {len(gdf)}")

    # UTM 投影
    utm_epsg = _latlon_to_utm_epsg(30.25, 120.15)
    gdf_utm = gdf.to_crs(epsg=utm_epsg)

    # ── 2. 高度填充 ──
    print("\n[2/4] Filling heights (real + estimated)...")
    has_real = gdf_utm["height"].notna() & (gdf_utm["height"] > 0)
    print(f"  real height: {has_real.sum()} ({has_real.mean()*100:.1f}%)")

    # 对无高度的建筑按面积估算
    areas = gdf_utm.geometry.area  # m² in UTM
    estimated = areas.apply(_estimate_height)
    gdf_utm["height"] = gdf_utm["height"].fillna(estimated)
    gdf_utm.loc[gdf_utm["height"] <= 0, "height"] = 6.0

    print(f"  height stats after fill:")
    print(f"    mean={gdf_utm['height'].mean():.1f}m, "
          f"median={gdf_utm['height'].median():.1f}m, "
          f"max={gdf_utm['height'].max():.1f}m")

    # ── 3. 加载水体 + 构建高度图 ──
    print("\n[3/4] Building relief heightmap...")
    water_gdf = load_water_osm(WATER_GEOJSON, BBOX_WGS84, utm_epsg)

    # 添加大运河桥接段（OSM 与高德数据在此处均缺失水面，需手动补全）
    canal_bridge = _grand_canal_bridge_polygon()
    canal_gdf = gpd.GeoDataFrame(geometry=[canal_bridge], crs="EPSG:4326")
    canal_gdf = canal_gdf.to_crs(epsg=utm_epsg)
    water_gdf = gpd.GeoDataFrame(
        pd.concat([water_gdf, canal_gdf], ignore_index=True),
        crs=f"EPSG:{utm_epsg}",
    )
    print(f"  [run_hangzhou] water with Grand Canal bridge: {len(water_gdf)} polygons")

    from pyproj import Transformer
    transformer = Transformer.from_crs("EPSG:4326", f"EPSG:{utm_epsg}", always_xy=True)
    min_x, min_y = transformer.transform(BBOX_WGS84[0], BBOX_WGS84[1])
    max_x, max_y = transformer.transform(BBOX_WGS84[2], BBOX_WGS84[3])
    bbox_utm = (min_x, min_y, max_x, max_y)

    relief_data = build_relief_heightmap(
        gdf_utm,
        water_gdf,
        bbox_utm,
        grid_size=2048,
        height_cap=350.0,
        height_floor=3.0,
    )

    # ── 4. 渲染 ──
    print("\n[4/4] Rendering...")

    # 风格 A：白底黑水（参考作品风格）
    render_relief(
        relief_data,
        os.path.join(OUTPUT_DIR, "hangzhou_mono_light.png"),
        city_name="Hangzhou",
        style="mono_light",
        z_exaggeration=4.0,      # 杭州建筑高度差异大，加强3D感
        light_azimuth=315.0,
        light_altitude=45.0,
        ao_strength=0.40,        # 稍强AO，让密集城区有层次
        edge_strength=0.25,
        grain_strength=0.02,
        height_gamma=0.45,       # 低gamma让高楼（310m）更突出
        output_size_px=3000,
    )

    # 风格 B：暗色
    render_relief(
        relief_data,
        os.path.join(OUTPUT_DIR, "hangzhou_mono_dark.png"),
        city_name="Hangzhou",
        style="mono_dark",
        z_exaggeration=5.0,
        light_azimuth=315.0,
        light_altitude=40.0,
        ao_strength=0.45,
        edge_strength=0.30,
        grain_strength=0.015,
        height_gamma=0.40,
        output_size_px=3000,
    )

    elapsed = time.time() - t0
    print(f"\n{'=' * 60}")
    print(f"  Done! ({elapsed:.1f}s)")
    print(f"  Output: {OUTPUT_DIR}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
