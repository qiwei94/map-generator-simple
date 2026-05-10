"""快速测试 Tile Cache 是否工作"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import (
    set_pbf_file_path,
    fetch_buildings,
)

# 设置 PBF 文件
pbf_file = os.path.join(os.path.dirname(__file__), "pbf_cache", "zhejiang-latest.osm.pbf")
set_pbf_file_path(pbf_file)

print("第一次查询（应该从 PBF 读取，较慢）...")
buildings1 = fetch_buildings(south=30.2, west=120.1, north=30.3, east=120.2)
print(f"获取到 {len(buildings1)} 个建筑物\n")

print("第二次查询（应该从 Tile Cache 读取，很快）...")
buildings2 = fetch_buildings(south=30.2, west=120.1, north=30.3, east=120.2)
print(f"获取到 {len(buildings2)} 个建筑物\n")

print(f"两次结果相同: {len(buildings1) == len(buildings2)}")
