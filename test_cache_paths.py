#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Test cache path configuration"""

import os
import sys

_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.config import (
    get_cache_paths,
    select_cache_path,
    CACHE_PATHS,
)

def test_cache_paths():
    """Test cache path configuration"""
    print("="*60)
    print("Cache Path Test")
    print("="*60)

    # 1. Show configured cache paths
    print("\n[Configured Cache Paths]")
    for i, path in enumerate(CACHE_PATHS):
        exists = os.path.exists(path)
        status = "[OK]" if exists else "[MISSING]"
        print(f"  {i+1}. {path} {status}")

    # 2. Show valid cache paths
    print("\n[Valid Cache Paths]")
    valid_paths = get_cache_paths()
    for i, path in enumerate(valid_paths):
        print(f"  {i+1}. {path}")

    # 3. Test select_cache_path()
    print("\n[select_cache_path() Test]")
    for size_mb in [10, 50, 100]:
        path = select_cache_path(size_mb)
        print(f"  Data size {size_mb}MB -> {path}")

    # 4. Check cache subdirectories
    print("\n[Cache Subdirectories]")
    cache_base = select_cache_path(0)
    subdirs = ["grids", "osm", "srtm"]

    for subdir in subdirs:
        subdir_path = os.path.join(cache_base, subdir)
        if os.path.exists(subdir_path):
            files = os.listdir(subdir_path)
            print(f"  {subdir}/: [OK] {len(files)} files")

            if files:
                print(f"    Sample files:")
                for f in files[:3]:
                    file_path = os.path.join(subdir_path, f)
                    size = os.path.getsize(file_path)
                    print(f"      - {f} ({size/1024:.1f} KB)")
        else:
            print(f"  {subdir}/: [MISSING]")

    print("\n" + "="*60)
    print("Test Complete!")
    print("="*60)

if __name__ == "__main__":
    test_cache_paths()