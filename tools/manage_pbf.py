#!/usr/bin/env python3
"""下载和管理 PBF 文件

从 Geofabrik 下载 OSM 数据到 pbf_cache 目录
"""

import os
import sys
import subprocess
from pathlib import Path

# PBF 文件配置
PBF_FILES = {
    "zhejiang": {
        "url": "https://download.geofabrik.de/asia/china/zhejiang-latest.osm.pbf",
        "description": "浙江省",
        "size": "~84 MB",
    },
    "jiangsu": {
        "url": "https://download.geofabrik.de/asia/china/jiangsu-latest.osm.pbf",
        "description": "江苏省",
        "size": "~72 MB",
    },
    "shanghai": {
        "url": "https://download.geofabrik.de/asia/china/shanghai-latest.osm.pbf",
        "description": "上海市",
        "size": "~24 MB",
    },
    "beijing": {
        "url": "https://download.geofabrik.de/asia/china/beijing-latest.osm.pbf",
        "description": "北京市",
        "size": "~34 MB",
    },
    "guangdong": {
        "url": "https://download.geofabrik.de/asia/china/guangdong-latest.osm.pbf",
        "description": "广东省（含港澳）",
        "size": "~156 MB",
    },
    "sichuan": {
        "url": "https://download.geofabrik.de/asia/china/sichuan-latest.osm.pbf",
        "description": "四川省",
        "size": "~97 MB",
    },
    "china": {
        "url": "https://download.geofabrik.de/asia/china-latest.osm.pbf",
        "description": "全国",
        "size": "~1.4 GB",
    },
}

PBF_CACHE_DIR = Path(__file__).parent / "pbf_cache"


def list_available():
    """列出可下载的 PBF 文件"""
    print("\n可用的 PBF 文件：\n")
    print(f"{'名称':<15} {'描述':<20} {'大小':<15} {'状态':<10}")
    print("-" * 60)
    
    for name, info in PBF_FILES.items():
        filename = f"{name}-latest.osm.pbf"
        filepath = PBF_CACHE_DIR / filename
        
        if filepath.exists():
            size = filepath.stat().st_size / 1024 / 1024
            status = f"✅ {size:.1f} MB"
        else:
            status = "❌ 未下载"
        
        print(f"{name:<15} {info['description']:<20} {info['size']:<15} {status}")
    
    print()


def download_pbf(name: str, use_aria2: bool = True):
    """下载指定的 PBF 文件"""
    if name not in PBF_FILES:
        print(f"❌ 未知的 PBF 文件: {name}")
        print(f"可用的选项: {', '.join(PBF_FILES.keys())}")
        return
    
    info = PBF_FILES[name]
    filename = f"{name}-latest.osm.pbf"
    filepath = PBF_CACHE_DIR / filename
    
    # 确保目录存在
    PBF_CACHE_DIR.mkdir(exist_ok=True)
    
    print(f"\n下载 {info['description']} PBF 文件...")
    print(f"URL: {info['url']}")
    print(f"目标: {filepath}")
    
    if filepath.exists():
        size = filepath.stat().st_size / 1024 / 1024
        print(f"⚠️  文件已存在 ({size:.1f} MB)")
        response = input("是否重新下载？(y/N): ")
        if response.lower() != 'y':
            print("取消下载")
            return
    
    # 使用 aria2 或 wget 下载
    if use_aria2:
        try:
            cmd = [
                "aria2c",
                "-x", "16", "-s", "16", "-k", "1M",
                "--continue=true",
                "--dir", str(PBF_CACHE_DIR),
                "--out", filename,
                info["url"]
            ]
            print("使用 aria2 多线程下载...")
            subprocess.run(cmd, check=True)
        except FileNotFoundError:
            print("aria2 未安装，使用 wget...")
            use_aria2 = False
    
    if not use_aria2:
        cmd = [
            "wget", "-c", "-O",
            str(filepath),
            info["url"]
        ]
        subprocess.run(cmd, check=True)
    
    if filepath.exists():
        size = filepath.stat().st_size / 1024 / 1024
        print(f"\n✅ 下载完成: {filename} ({size:.1f} MB)")
        print(f"\n使用方法:")
        print(f"  export OSM_PBF_FILE={filepath}")
    else:
        print("\n❌ 下载失败")


def main():
    """主函数"""
    if len(sys.argv) < 2:
        print("PBF 文件管理工具")
        print("\n用法:")
        print(f"  {sys.argv[0]} list              - 列出可用的 PBF 文件")
        print(f"  {sys.argv[0]} download <name>   - 下载指定的 PBF 文件")
        print(f"  {sys.argv[0]} info              - 显示 pbf_cache 目录信息")
        print()
        list_available()
        return
    
    command = sys.argv[1]
    
    if command == "list":
        list_available()
    
    elif command == "download":
        if len(sys.argv) < 3:
            print("❌ 请指定要下载的 PBF 文件名称")
            list_available()
            return
        
        name = sys.argv[2]
        use_aria2 = "--no-aria2" not in sys.argv
        download_pbf(name, use_aria2)
    
    elif command == "info":
        print("\nPBF Cache 目录信息")
        print("=" * 60)
        print(f"目录位置: {PBF_CACHE_DIR}")
        
        if PBF_CACHE_DIR.exists():
            pbf_files = list(PBF_CACHE_DIR.glob("*.pbf"))
            if pbf_files:
                total_size = sum(f.stat().st_size for f in pbf_files)
                print(f"文件数量: {len(pbf_files)}")
                print(f"总大小: {total_size / 1024 / 1024:.1f} MB")
                print("\n文件列表:")
                for f in pbf_files:
                    size = f.stat().st_size / 1024 / 1024
                    print(f"  - {f.name} ({size:.1f} MB)")
            else:
                print("暂无 PBF 文件")
        else:
            print("目录不存在")
        
        print()
    
    else:
        print(f"❌ 未知命令: {command}")
        main()


if __name__ == "__main__":
    main()
