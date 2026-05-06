"""
OSM 数据获取 - 代理配置示例

如果你有 VPN/代理,可以使用以下两种方式之一配置:

方式 1: 在终端设置环境变量 (推荐)
  export HTTP_PROXY=http://127.0.0.1:7890
  export HTTPS_PROXY=http://127.0.0.1:7890
  python tools/generate_water_hangzhou.py

方式 2: 在本文件中直接配置 (适合固定代理)
  取消下面的注释并修改为你的代理地址
"""

import os
import sys

# 确保项目根目录在 path 中
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# =====================================================================
# 方式 2: 在此配置代理 (取消注释并使用)
# =====================================================================
# 修改为你的代理地址
# os.environ["HTTP_PROXY"] = "http://127.0.0.1:7890"
# os.environ["HTTPS_PROXY"] = "http://127.0.0.1:7890"

# =====================================================================
# 测试多镜像切换
# =====================================================================
if __name__ == "__main__":
    from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import (
        OVERPASS_ENDPOINTS, _set_overpass_endpoint, fetch_water
    )

    print("=" * 60)
    print("  Overpass API 镜像列表")
    print("=" * 60)
    for i, endpoint in enumerate(OVERPASS_ENDPOINTS, 1):
        print(f"  {i}. {endpoint}")
    print()

    # 检查代理配置
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    if proxy:
        print(f"  代理: {proxy}")
    else:
        print("  代理: 未配置 (直接连接)")
    print()

    # 测试获取小区域数据
    print("  测试获取杭州西湖水域数据...")
    # 西湖大致范围
    water_gdf = fetch_water(
        south=30.22, west=120.12,
        north=30.26, east=120.16
    )

    if water_gdf is not None and not water_gdf.empty:
        print(f"  成功! 获取到 {len(water_gdf)} 个水域要素")
    else:
        print("  警告: 未获取到数据 (可能是网络问题或区域无水)")
