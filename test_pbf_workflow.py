"""测试 PBF 数据源工作流

使用浙江省 PBF 文件获取杭州地区的 OSM 数据
"""

import logging
import sys
import os

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# 添加项目路径
sys.path.insert(0, os.path.dirname(__file__))

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import (
    set_pbf_file_path,
    fetch_buildings,
    fetch_roads,
    fetch_water,
    fetch_vegetation,
)

# 杭州地区边界框
HANGZHOU_BBOX = {
    'south': 30.15,
    'west': 120.05,
    'north': 30.35,
    'east': 120.25,
}

def test_pbf_workflow():
    """测试 PBF 工作流"""
    
    # 1. 设置 PBF 文件路径
    pbf_file = os.path.join(os.path.dirname(__file__), "pbf_cache", "zhejiang-latest.osm.pbf")
    
    if not os.path.exists(pbf_file):
        print(f"❌ PBF 文件不存在: {pbf_file}")
        print("请先下载浙江省 PBF 文件:")
        print("  wget https://download.geofabrik.de/asia/china/zhejiang-latest.osm.pbf")
        return
    
    print(f"✅ 找到 PBF 文件: {pbf_file}")
    print(f"   文件大小: {os.path.getsize(pbf_file) / 1024 / 1024:.1f} MB")
    
    # 设置 PBF 数据源
    set_pbf_file_path(pbf_file)
    
    # 2. 测试获取建筑物数据
    print("\n" + "="*60)
    print("测试 1: 获取建筑物数据")
    print("="*60)
    try:
        buildings = fetch_buildings(
            south=HANGZHOU_BBOX['south'],
            west=HANGZHOU_BBOX['west'],
            north=HANGZHOU_BBOX['north'],
            east=HANGZHOU_BBOX['east'],
        )
        print(f"✅ 获取到 {len(buildings)} 个建筑物")
        if not buildings.empty:
            print(f"   列: {list(buildings.columns)}")
            print(f"   示例: {buildings.head(1)}")
    except Exception as e:
        print(f"❌ 建筑物获取失败: {e}")
        import traceback
        traceback.print_exc()
    
    # 3. 测试获取道路数据
    print("\n" + "="*60)
    print("测试 2: 获取道路数据")
    print("="*60)
    try:
        roads = fetch_roads(
            south=HANGZHOU_BBOX['south'],
            west=HANGZHOU_BBOX['west'],
            north=HANGZHOU_BBOX['north'],
            east=HANGZHOU_BBOX['east'],
        )
        print(f"✅ 获取到 {len(roads)} 条道路")
        if not roads.empty:
            print(f"   列: {list(roads.columns)}")
            print(f"   示例: {roads.head(1)}")
    except Exception as e:
        print(f"❌ 道路获取失败: {e}")
        import traceback
        traceback.print_exc()
    
    # 4. 测试获取水体数据
    print("\n" + "="*60)
    print("测试 3: 获取水体数据")
    print("="*60)
    try:
        water = fetch_water(
            south=HANGZHOU_BBOX['south'],
            west=HANGZHOU_BBOX['west'],
            north=HANGZHOU_BBOX['north'],
            east=HANGZHOU_BBOX['east'],
        )
        print(f"✅ 获取到 {len(water)} 个水体要素")
        if not water.empty:
            print(f"   列: {list(water.columns)}")
    except Exception as e:
        print(f"❌ 水体获取失败: {e}")
        import traceback
        traceback.print_exc()
    
    # 5. 测试获取绿地数据
    print("\n" + "="*60)
    print("测试 4: 获取绿地数据")
    print("="*60)
    try:
        vegetation = fetch_vegetation(
            south=HANGZHOU_BBOX['south'],
            west=HANGZHOU_BBOX['west'],
            north=HANGZHOU_BBOX['north'],
            east=HANGZHOU_BBOX['east'],
        )
        print(f"✅ 获取到 {len(vegetation)} 个绿地要素")
        if not vegetation.empty:
            print(f"   列: {list(vegetation.columns)}")
    except Exception as e:
        print(f"❌ 绿地获取失败: {e}")
        import traceback
        traceback.print_exc()
    
    # 6. 总结
    print("\n" + "="*60)
    print("测试总结")
    print("="*60)
    print(f"建筑物: {len(buildings) if 'buildings' in locals() else 0}")
    print(f"道路: {len(roads) if 'roads' in locals() else 0}")
    print(f"水体: {len(water) if 'water' in locals() else 0}")
    print(f"绿地: {len(vegetation) if 'vegetation' in locals() else 0}")
    print("\n✅ PBF 工作流测试完成！")


if __name__ == "__main__":
    test_pbf_workflow()
