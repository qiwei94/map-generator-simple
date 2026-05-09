#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""OSM缓存索引工具

解析缓存JSON文件，提取bbox范围和tags类型，建立索引供复用查询。

使用方法：
  python osm_cache_index.py --build    # 建立索引
  python osm_cache_index.py --query 30.22 120.12 30.26 120.16 water  # 查询匹配缓存
"""

import os
import sys
import json
import hashlib
import argparse
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# 缓存目录列表
CACHE_DIRS = [
    "F:/map_gen_cache/attaraction/cache/osm",
    "F:/map_gen_cache/project_cache/osm",
]

INDEX_FILE = "osm_cache_index.json"


def parse_cache_file(filepath: str) -> Optional[Dict]:
    """解析OSM缓存JSON文件，提取bbox和tags信息
    
    Returns:
        {
            "file": 文件路径,
            "bbox": (south, west, north, east),
            "tags": {"natural": "water", ...},
            "elements_count": 元素数量,
            "size_kb": 文件大小KB
        }
    """
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        elements = data.get('elements', [])
        if not elements:
            return None
        
        # 提取nodes的坐标范围
        nodes = [el for el in elements if el.get('type') == 'node' and 'lat' in el]
        if not nodes:
            return None
        
        lats = [n['lat'] for n in nodes]
        lons = [n['lon'] for n in nodes]
        
        bbox = (min(lats), min(lons), max(lats), max(lons))
        
        # 提取ways的tags信息
        ways = [el for el in elements if el.get('type') == 'way']
        tags_set = set()
        for way in ways:
            for k in way.get('tags', {}).keys():
                tags_set.add(k)
        
        return {
            "file": filepath,
            "bbox": bbox,
            "tags": list(tags_set),
            "elements_count": len(elements),
            "size_kb": os.path.getsize(filepath) / 1024
        }
    except Exception as e:
        print(f"Error parsing {filepath}: {e}")
        return None


def build_index() -> List[Dict]:
    """扫描所有缓存目录，建立索引"""
    index = []
    
    for cache_dir in CACHE_DIRS:
        if not os.path.isdir(cache_dir):
            continue
        
        print(f"Scanning {cache_dir}...")
        json_files = [f for f in os.listdir(cache_dir) if f.endswith('.json')]
        
        for i, filename in enumerate(json_files):
            filepath = os.path.join(cache_dir, filename)
            info = parse_cache_file(filepath)
            if info:
                index.append(info)
            
            if (i + 1) % 100 == 0:
                print(f"  Processed {i+1}/{len(json_files)} files...")
    
    return index


def find_matching_cache(index: List[Dict], 
                         south: float, west: float, 
                         north: float, east: float,
                         tag_type: str) -> List[Dict]:
    """查找包含查询bbox的缓存文件
    
    Args:
        index: 缓存索引
        south, west, north, east: 查询bbox
        tag_type: 查询类型 (water, highway, building等)
    
    Returns:
        匹配的缓存列表，按覆盖程度排序
    """
    matches = []
    
    for entry in index:
        cache_s, cache_w, cache_n, cache_e = entry['bbox']
        
        # 检查缓存bbox是否完全包含查询bbox
        if cache_s <= south and cache_w <= west and cache_n >= north and cache_e >= east:
            # 检查tags是否匹配
            if tag_type in entry['tags'] or tag_type.replace('way', '') in entry['tags']:
                # 计算覆盖比例
                cache_area = (cache_n - cache_s) * (cache_e - cache_w)
                query_area = (north - south) * (east - west)
                coverage = query_area / cache_area if cache_area > 0 else 0
                
                matches.append({
                    **entry,
                    "coverage": coverage,
                    "contains_query": True
                })
    
    # 按覆盖比例排序（最接近1的优先）
    matches.sort(key=lambda x: abs(x['coverage'] - 1.0))
    
    return matches


def load_index() -> Optional[List[Dict]]:
    """加载已有索引"""
    for cache_dir in CACHE_DIRS:
        index_path = os.path.join(cache_dir, INDEX_FILE)
        if os.path.isfile(index_path):
            with open(index_path, 'r', encoding='utf-8') as f:
                return json.load(f)
    return None


def save_index(index: List[Dict]):
    """保存索引到第一个有效目录"""
    for cache_dir in CACHE_DIRS:
        if os.path.isdir(cache_dir):
            index_path = os.path.join(cache_dir, INDEX_FILE)
            with open(index_path, 'w', encoding='utf-8') as f:
                json.dump(index, f, indent=2)
            print(f"Index saved to {index_path} ({len(index)} entries)")
            return


def main():
    parser = argparse.ArgumentParser(description="OSM Cache Index Tool")
    parser.add_argument('--build', action='store_true', help='Build cache index')
    parser.add_argument('--query', nargs=5, help='Query matching cache: south west north east tag_type')
    
    args = parser.parse_args()
    
    if args.build:
        print("Building OSM cache index...")
        index = build_index()
        save_index(index)
        print(f"Total {len(index)} cache files indexed")
        
        # 统计信息
        total_size = sum(e['size_kb'] for e in index)
        print(f"Total cache size: {total_size/1024:.1f} MB")
        
    elif args.query:
        south, west, north, east, tag_type = args.query
        south, west, north, east = float(south), float(west), float(north), float(east)
        
        index = load_index()
        if index is None:
            print("No index found. Run --build first.")
            return
        
        matches = find_matching_cache(index, south, west, north, east, tag_type)
        
        if matches:
            print(f"Found {len(matches)} matching caches for {tag_type}:")
            for m in matches[:10]:
                bbox = m['bbox']
                print(f"  {Path(m['file']).name}")
                print(f"    BBox: ({bbox[0]:.4f}, {bbox[1]:.4f}, {bbox[2]:.4f}, {bbox[3]:.4f})")
                print(f"    Coverage: {m['coverage']:.2%}, Size: {m['size_kb']:.1f}KB")
        else:
            print(f"No matching cache found for {tag_type} in ({south}, {west}, {north}, {east})")
    
    else:
        parser.print_help()


if __name__ == "__main__":
    main()