"""测试高德 API 获取水体 polygon 边界.

用法:
    export AMAP_KEY=your_key_here
    python tools/test_amap_water.py
"""
import os
import sys
import json
import requests
import time

KEY = os.environ.get("AMAP_KEY", "")
if not KEY:
    print("ERROR: 请设置环境变量 AMAP_KEY")
    sys.exit(1)

BASE_V3 = "https://restapi.amap.com/v3"
BASE_V5 = "https://restapi.amap.com/v5"

# --- 测试区域: 重庆嘉陵江 (缺失 polygon 的那段) ---
# 大约坐标: 29.58, 106.50 (嘉陵江上游段)
TEST_LON, TEST_LAT = 106.50, 29.58
TEST_CITY = "重庆"


def test_poi_text_search():
    """方式1: POI 文字搜索水系类型."""
    print("\n=== 方式1: POI文字搜索 (v5/place/text) ===")
    url = f"{BASE_V5}/place/text"
    params = {
        "key": KEY,
        "keywords": "嘉陵江",
        "types": "190000",  # 水系大类
        "region": TEST_CITY,
        "show_fields": "business,polygon",
        "page_size": 5,
    }
    r = requests.get(url, params=params)
    data = r.json()
    print(f"  Status: {data.get('status')}, Info: {data.get('info')}")
    
    pois = data.get("pois", [])
    print(f"  Results: {len(pois)}")
    for poi in pois[:5]:
        name = poi.get("name", "")
        typecode = poi.get("typecode", "")
        location = poi.get("location", "")
        polygon = poi.get("polygon", "")
        poly_len = len(polygon) if polygon else 0
        print(f"    {name} | type={typecode} | loc={location} | polygon_chars={poly_len}")
        if polygon:
            # Show first 200 chars of polygon
            print(f"      polygon[:200] = {polygon[:200]}")
    return pois


def test_poi_around():
    """方式2: 周边搜索水体."""
    print("\n=== 方式2: POI周边搜索 (v5/place/around) ===")
    url = f"{BASE_V5}/place/around"
    params = {
        "key": KEY,
        "location": f"{TEST_LON},{TEST_LAT}",
        "types": "190000",
        "radius": 5000,
        "show_fields": "polygon",
        "page_size": 10,
    }
    r = requests.get(url, params=params)
    data = r.json()
    print(f"  Status: {data.get('status')}, Info: {data.get('info')}")
    
    pois = data.get("pois", [])
    print(f"  Results: {len(pois)}")
    for poi in pois[:10]:
        name = poi.get("name", "")
        typecode = poi.get("typecode", "")
        polygon = poi.get("polygon", "")
        poly_len = len(polygon) if polygon else 0
        print(f"    {name} | type={typecode} | polygon_chars={poly_len}")
        if polygon:
            print(f"      polygon[:200] = {polygon[:200]}")
    return pois


def test_poi_detail(poi_id):
    """方式3: POI 详情获取 polygon."""
    print(f"\n=== 方式3: POI详情 (v5/place/detail) id={poi_id} ===")
    url = f"{BASE_V5}/place/detail"
    params = {
        "key": KEY,
        "id": poi_id,
        "show_fields": "polygon,business",
    }
    r = requests.get(url, params=params)
    data = r.json()
    print(f"  Status: {data.get('status')}, Info: {data.get('info')}")
    
    pois = data.get("pois", [])
    if pois:
        poi = pois[0]
        polygon = poi.get("polygon", "")
        print(f"  Name: {poi.get('name')}")
        print(f"  Polygon chars: {len(polygon) if polygon else 0}")
        if polygon:
            print(f"  Polygon[:300] = {polygon[:300]}")
            # Count points
            points = polygon.replace("|", ";").split(";")
            print(f"  Polygon points: {len(points)}")
    return data


def test_polygon_search():
    """方式4: Polygon搜索 (在指定多边形范围内搜索水体)."""
    print("\n=== 方式4: Polygon范围搜索 (v5/place/polygon) ===")
    # 重庆嘉陵江缺失段的 bbox (大约)
    # 29.53,106.43 - 29.63,106.55
    polygon_str = "106.43,29.53|106.55,29.53|106.55,29.63|106.43,29.63|106.43,29.53"
    url = f"{BASE_V5}/place/polygon"
    params = {
        "key": KEY,
        "polygon": polygon_str,
        "types": "190000",
        "show_fields": "polygon",
        "page_size": 20,
    }
    r = requests.get(url, params=params)
    data = r.json()
    print(f"  Status: {data.get('status')}, Info: {data.get('info')}")
    
    pois = data.get("pois", [])
    print(f"  Results: {len(pois)}")
    for poi in pois[:10]:
        name = poi.get("name", "")
        typecode = poi.get("typecode", "")
        polygon = poi.get("polygon", "")
        poly_len = len(polygon) if polygon else 0
        print(f"    {name} | type={typecode} | polygon_chars={poly_len}")
        if polygon:
            print(f"      polygon[:200] = {polygon[:200]}")
    return pois


if __name__ == "__main__":
    print(f"高德 API Key: {KEY[:8]}...{KEY[-4:]}")
    print(f"测试区域: {TEST_CITY} 嘉陵江 ({TEST_LAT}, {TEST_LON})")
    
    # 逐个测试
    pois1 = test_poi_text_search()
    time.sleep(0.3)
    
    pois2 = test_poi_around()
    time.sleep(0.3)
    
    # 如果搜到了 POI，用第一个的 ID 查详情
    all_pois = pois1 + pois2
    if all_pois:
        first_id = all_pois[0].get("id", "")
        if first_id:
            time.sleep(0.3)
            test_poi_detail(first_id)
    
    time.sleep(0.3)
    test_polygon_search()
    
    print("\n\n=== 总结 ===")
    polygon_count = sum(1 for p in (pois1 + pois2) if p.get("polygon"))
    print(f"  带 polygon 的结果: {polygon_count}/{len(pois1 + pois2)}")
    if polygon_count > 0:
        print("  ✓ 高德能返回水体 polygon！可用于补全 OSM 缺失数据")
    else:
        print("  ✗ 未获取到 polygon，可能需要换 API 或升级权限")
