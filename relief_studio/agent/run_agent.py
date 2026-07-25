"""运行 Relief Agent：自动迭代优化芝加哥建筑浮雕.

前置：
    1. pip install langchain langchain-community langgraph dashscope
    2. 设置环境变量 DASHSCOPE_API_KEY（通义千问 API key）

用法：
    python -m relief_studio.agent.run_agent
"""

import os
import sys
import time

import pandas as pd
import geopandas as gpd
from shapely.geometry import Polygon

_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from relief_studio.data_loader import load_buildings_overture, load_water_osm
from relief_studio.agent.relief_agent import run_agent


def _lake_michigan_polygon():
    """密歇根湖简化多边形."""
    shoreline_points = [
        (-87.52, 41.70), (-87.57, 41.75), (-87.58, 41.80),
        (-87.60, 41.85), (-87.607, 41.88), (-87.62, 41.89),
        (-87.63, 41.91), (-87.65, 41.94), (-87.66, 41.97),
        (-87.67, 42.00), (-87.68, 42.05),
    ]
    east_edge = -87.40
    return Polygon(shoreline_points + [(east_edge, 42.05), (east_edge, 41.70)])


def main():
    t0 = time.time()

    # ── 检查 API Key ──
    api_key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not api_key:
        print("ERROR: 请设置环境变量 DASHSCOPE_API_KEY")
        print("  获取方式: https://dashscope.console.aliyun.com/apiKey")
        print("  设置: set DASHSCOPE_API_KEY=sk-xxxxx")
        sys.exit(1)

    # ── 配置 ──
    BBOX_WGS84 = (-87.77, 41.77, -87.47, 41.99)
    OVERTURE_PARQUET = os.path.join(
        _project_root, "data", "height_cache", "overture_41.88_-87.62.parquet"
    )
    WATER_GEOJSON = os.path.join(
        _project_root, "tmp", "osmium_water_41.7700_-87.7700_41.9900_-87.4700.geojson"
    )
    REFERENCE_IMAGE = r"C:\Users\kiwi\OneDrive\Desktop\city_demo\芝加哥\01芝加哥模型1.jpg"
    OUTPUT_DIR = os.path.join(_project_root, "relief_studio", "output", "agent")
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # ── 加载数据 ──
    print("[1/2] Loading data...")
    buildings_gdf, utm_epsg = load_buildings_overture(OVERTURE_PARQUET, BBOX_WGS84)
    water_gdf = load_water_osm(WATER_GEOJSON, BBOX_WGS84, utm_epsg)

    # 添加密歇根湖
    lake_gdf = gpd.GeoDataFrame(geometry=[_lake_michigan_polygon()], crs="EPSG:4326")
    lake_gdf = lake_gdf.to_crs(epsg=utm_epsg)
    water_gdf = gpd.GeoDataFrame(
        pd.concat([water_gdf, lake_gdf], ignore_index=True),
        crs=f"EPSG:{utm_epsg}",
    )

    # 计算 UTM bbox
    from pyproj import Transformer
    transformer = Transformer.from_crs("EPSG:4326", f"EPSG:{utm_epsg}", always_xy=True)
    min_x, min_y = transformer.transform(BBOX_WGS84[0], BBOX_WGS84[1])
    max_x, max_y = transformer.transform(BBOX_WGS84[2], BBOX_WGS84[3])
    bbox_utm = (min_x, min_y, max_x, max_y)

    # ── 运行 Agent ──
    print("\n[2/2] Running Relief Agent...")
    result = run_agent(
        city_name="chicago",
        buildings_gdf=buildings_gdf,
        water_gdf=water_gdf,
        bbox_utm=bbox_utm,
        utm_epsg=utm_epsg,
        reference_image_path=REFERENCE_IMAGE,
        output_dir=OUTPUT_DIR,
        max_iterations=4,
        target_score=7,
        bbox_wgs84=BBOX_WGS84,
    )

    # ── 输出结果 ──
    elapsed = time.time() - t0
    print(f"\n{'='*60}")
    print(f"  Agent 完成!")
    print(f"  迭代次数: {result['iterations']}")
    print(f"  最佳分数: {result['best_score']}")
    print(f"  耗时: {elapsed:.1f}s")
    print(f"  输出: {OUTPUT_DIR}")
    print(f"{'='*60}")

    # 打印迭代历史
    for h in result["history"]:
        scores = h["evaluation"].get("scores", {})
        print(f"  Iter {h['iteration']}: overall={scores.get('overall_aesthetic', '?')} "
              f"| texture={scores.get('building_texture', '?')} "
              f"| height={scores.get('height_variation', '?')}")


if __name__ == "__main__":
    main()
