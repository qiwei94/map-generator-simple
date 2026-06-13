#!/usr/bin/env python3
"""一次性下载 DEM 瓦片到本地，让 pipeline 完全离线运行。

支持两种数据源（与 _TEXTURE_STYLE_OF_DEEPSEEK/terrain3d/fetchers/elevation.py 对接）：

  --source srtm   ：SRTM 1 弧秒（~30m）HGT 瓦片。  落到 dem_cache/srtm/<lat>/<file>.hgt
                    向后兼容：项目当前 elevation.py 默认就读这个路径。
  --source cop30  ：Copernicus DEM GLO-30 GeoTIFF。落到 dem_cache/cop30/<tile>/<file>.tif
                    需要 elevation.py 走 GeoTIFF 路径（见 P3 升级）。

按区域选下载范围：

    # 按预设地区（与 manage_pbf.py 同名）
    python tools/manage_dem.py download zhejiang --source srtm
    python tools/manage_dem.py download china    --source cop30

    # 按矩形 bbox（south west north east，WGS84 度）
    python tools/manage_dem.py bbox 30.13 120.01 30.36 120.29 --source srtm

    # 看本地缓存
    python tools/manage_dem.py info
    python tools/manage_dem.py list

数据源稳定性提示（国内）：
  - SRTM HGT 走的是 AWS us-east-1（elevation-tiles-prod）
  - Copernicus GLO-30 走 AWS S3（copernicus-dem-30m，eu-central-1）
  - 都不需要登录、不签名（--no-sign-request 等价的开放访问）
  - 真正"国内最稳"的源是 gscloud.cn — 网页端手动下载后丢进 dem_cache/srtm/

详见 doc/data_and_performance.md。
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time
from pathlib import Path
from typing import Iterable

try:
    import requests
except ImportError:
    print("❌ 需要 requests: pip install requests", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# 路径与预设区域
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEM_CACHE_DIR = PROJECT_ROOT / "dem_cache"

# 与 manage_pbf.py 的命名对齐，方便记忆。
# bbox 是地理边界（south, west, north, east），DEM 按 1°×1° 瓦片切，所以
# 边界向外取整一格。
PRESETS = {
    # 中国主要省份（含港澳） + 全国
    "zhejiang":   {"bbox": (27.0, 118.0, 31.5, 123.5), "desc": "浙江省"},
    "jiangsu":    {"bbox": (30.0, 116.0, 35.5, 122.5), "desc": "江苏省"},
    "shanghai":   {"bbox": (30.5, 120.5, 32.0, 122.5), "desc": "上海市"},
    "beijing":    {"bbox": (39.0, 115.0, 41.5, 117.5), "desc": "北京市"},
    "guangdong":  {"bbox": (20.0, 109.5, 25.5, 117.5), "desc": "广东省（含港澳）"},
    "sichuan":    {"bbox": (26.0, 97.0, 34.5, 109.0),  "desc": "四川省"},
    "chongqing":  {"bbox": (28.0, 105.0, 32.5, 110.5), "desc": "重庆市"},
    "hainan":     {"bbox": (18.0, 108.0, 20.5, 111.5), "desc": "海南省"},
    "yunnan":     {"bbox": (21.0, 97.0, 29.5, 106.5),  "desc": "云南省"},
    "xizang":     {"bbox": (26.0, 78.0, 36.5, 99.5),   "desc": "西藏自治区"},
    "china":      {"bbox": (18.0, 73.0, 54.0, 135.0),  "desc": "全国"},
    # 测试用的小范围（西湖 25km）
    "westlake":   {"bbox": (30.13, 120.01, 30.36, 120.29), "desc": "杭州西湖（测试）"},
}


# ---------------------------------------------------------------------------
# Source: SRTM HGT
# ---------------------------------------------------------------------------

# 与 elevation.py 中 _SRTM_URLS 顺序一致；如果第一个超时再试第二个
SRTM_URLS = [
    "https://elevation-tiles-prod.s3.amazonaws.com/skadi/{dir}/{filename}",
    "https://s3.amazonaws.com/elevation-tiles-prod/skadi/{dir}/{filename}",
]


def _hgt_filename(lat: int, lon: int) -> str:
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"{ns}{abs(lat):02d}{ew}{abs(lon):03d}.hgt"


def _hgt_local_path(lat: int, lon: int) -> Path:
    """与 elevation.py::_tile_dir/_tile_filename 路径布局一致。"""
    ns = "N" if lat >= 0 else "S"
    return DEM_CACHE_DIR / "srtm" / f"{ns}{abs(lat):02d}" / _hgt_filename(lat, lon)


def _srtm_urls_for_tile(lat: int, lon: int) -> list[str]:
    fname = _hgt_filename(lat, lon)
    ns = "N" if lat >= 0 else "S"
    tile_dir = f"{ns}{abs(lat):02d}"
    urls = []
    for tmpl in SRTM_URLS:
        urls.append(tmpl.format(dir=tile_dir, filename=fname + ".gz"))
    return urls


def _download_one_srtm(lat: int, lon: int, *, force: bool = False) -> bool:
    """下载并解压一个 SRTM 瓦片到 dem_cache/srtm/。"""
    import gzip

    out_path = _hgt_local_path(lat, lon)
    if out_path.exists() and not force:
        return True
    out_path.parent.mkdir(parents=True, exist_ok=True)

    last_err = None
    for url in _srtm_urls_for_tile(lat, lon):
        try:
            t0 = time.time()
            resp = requests.get(url, timeout=60)
            if resp.status_code != 200:
                last_err = f"HTTP {resp.status_code}"
                continue
            data = gzip.decompress(resp.content)
            out_path.write_bytes(data)
            took = time.time() - t0
            size_mb = len(data) / 1024 / 1024
            print(f"  ✅ {out_path.name}  {size_mb:.1f}MB  {took:.1f}s")
            return True
        except Exception as e:
            last_err = repr(e)
            continue

    print(f"  ❌ {_hgt_filename(lat, lon)}  ({last_err})")
    return False


# ---------------------------------------------------------------------------
# Source: Copernicus GLO-30 GeoTIFF
# ---------------------------------------------------------------------------

COP30_BASE = "https://copernicus-dem-30m.s3.amazonaws.com"


def _cop30_tile_id(lat: int, lon: int) -> str:
    """e.g. (30, 120) -> 'Copernicus_DSM_COG_10_N30_00_E120_00_DEM'."""
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"Copernicus_DSM_COG_10_{ns}{abs(lat):02d}_00_{ew}{abs(lon):03d}_00_DEM"


def _cop30_local_path(lat: int, lon: int) -> Path:
    tid = _cop30_tile_id(lat, lon)
    return DEM_CACHE_DIR / "cop30" / tid / f"{tid}.tif"


def _cop30_url(lat: int, lon: int) -> str:
    tid = _cop30_tile_id(lat, lon)
    return f"{COP30_BASE}/{tid}/{tid}.tif"


def _download_one_cop30(lat: int, lon: int, *, force: bool = False) -> bool:
    out_path = _cop30_local_path(lat, lon)
    if out_path.exists() and not force:
        return True
    out_path.parent.mkdir(parents=True, exist_ok=True)

    url = _cop30_url(lat, lon)
    try:
        t0 = time.time()
        with requests.get(url, timeout=120, stream=True) as resp:
            if resp.status_code == 404:
                # 海面 / 极区会缺瓦片，是正常情况
                print(f"  – {_cop30_tile_id(lat, lon)}  (404, 海面/未覆盖)")
                return False
            resp.raise_for_status()
            with out_path.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
        took = time.time() - t0
        size_mb = out_path.stat().st_size / 1024 / 1024
        print(f"  ✅ {out_path.name}  {size_mb:.1f}MB  {took:.1f}s")
        return True
    except Exception as e:
        print(f"  ❌ {_cop30_tile_id(lat, lon)}  ({e})")
        return False


# ---------------------------------------------------------------------------
# Tile enumeration
# ---------------------------------------------------------------------------


def tiles_for_bbox(south: float, west: float, north: float, east: float
                  ) -> list[tuple[int, int]]:
    """返回覆盖 bbox 的全部 1°×1° 瓦片整数坐标。"""
    lat_lo = int(math.floor(south))
    lat_hi = int(math.floor(north))
    lon_lo = int(math.floor(west))
    lon_hi = int(math.floor(east))
    return [
        (lat, lon)
        for lat in range(lat_lo, lat_hi + 1)
        for lon in range(lon_lo, lon_hi + 1)
    ]


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _format_bbox(b: tuple[float, float, float, float]) -> str:
    return f"S {b[0]:.2f}° W {b[1]:.2f}° → N {b[2]:.2f}° E {b[3]:.2f}°"


def _do_download(tiles: list[tuple[int, int]], source: str, force: bool) -> None:
    print(f"\n下载 {len(tiles)} 个瓦片到 {DEM_CACHE_DIR}/{source}/ ...\n")
    fn = _download_one_srtm if source == "srtm" else _download_one_cop30

    n_ok = 0
    n_skip = 0
    n_fail = 0
    t0 = time.time()
    for i, (lat, lon) in enumerate(tiles, 1):
        # Skip-cached path（不调下载函数，避免误打印）
        out = _hgt_local_path(lat, lon) if source == "srtm" else _cop30_local_path(lat, lon)
        if out.exists() and not force:
            n_skip += 1
            continue
        if fn(lat, lon, force=force):
            n_ok += 1
        else:
            n_fail += 1
        if i % 10 == 0:
            print(f"  [{i}/{len(tiles)}] kept={n_ok} skipped={n_skip} failed={n_fail}")

    print(
        f"\n完成。下载 {n_ok}，命中缓存 {n_skip}，失败 {n_fail}，用时 {time.time() - t0:.1f}s"
    )


def cmd_download(args: argparse.Namespace) -> None:
    if args.region not in PRESETS:
        print(f"❌ 未知区域: {args.region}")
        print(f"  可选: {', '.join(PRESETS.keys())}")
        sys.exit(2)
    bbox = PRESETS[args.region]["bbox"]
    desc = PRESETS[args.region]["desc"]
    tiles = tiles_for_bbox(*bbox)
    print(f"\n区域: {args.region} ({desc})")
    print(f"BBox: {_format_bbox(bbox)}")
    print(f"瓦片: {len(tiles)} 个")
    _do_download(tiles, args.source, args.force)


def cmd_bbox(args: argparse.Namespace) -> None:
    bbox = (args.south, args.west, args.north, args.east)
    if not (-90 <= bbox[0] < bbox[2] <= 90 and -180 <= bbox[1] < bbox[3] <= 180):
        print("❌ bbox 不合法（south < north, west < east，纬度 ±90，经度 ±180）")
        sys.exit(2)
    tiles = tiles_for_bbox(*bbox)
    print(f"\nBBox: {_format_bbox(bbox)}")
    print(f"瓦片: {len(tiles)} 个")
    _do_download(tiles, args.source, args.force)


def cmd_info(_: argparse.Namespace) -> None:
    print(f"\nDEM 缓存目录: {DEM_CACHE_DIR}")
    if not DEM_CACHE_DIR.exists():
        print("  目录不存在 — 还没下载过任何 DEM 数据。")
        return

    for src in ("srtm", "cop30"):
        sub = DEM_CACHE_DIR / src
        if not sub.exists():
            continue
        files = list(sub.rglob("*"))
        files = [f for f in files if f.is_file()]
        size_mb = sum(f.stat().st_size for f in files) / 1024 / 1024
        print(f"  [{src}]  {len(files)} 个文件，{size_mb:.1f} MB")


def cmd_list(_: argparse.Namespace) -> None:
    print("\n可用预设区域：\n")
    print(f"{'名称':<12} {'描述':<22} {'瓦片数':<8} {'BBox':<40}")
    print("-" * 90)
    for name, info in PRESETS.items():
        b = info["bbox"]
        n = len(tiles_for_bbox(*b))
        print(f"{name:<12} {info['desc']:<22} {n:<8} {_format_bbox(b)}")
    print(
        "\n用法:\n"
        "  python tools/manage_dem.py download <region> [--source srtm|cop30] [--force]\n"
        "  python tools/manage_dem.py bbox <south> <west> <north> <east> [--source ...] \n"
        "  python tools/manage_dem.py info\n"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="一次性下载 DEM 瓦片到 dem_cache/，配合 elevation.py 离线运行。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    common_source = dict(choices=["srtm", "cop30"], default="srtm",
                         help="数据源 (默认: srtm，HGT 1\"; 'cop30' = Copernicus GLO-30 GeoTIFF)")

    pd = sub.add_parser("download", help="按预设区域下载（推荐）")
    pd.add_argument("region", help=f"区域名 (one of: {', '.join(PRESETS.keys())})")
    pd.add_argument("--source", **common_source)
    pd.add_argument("--force", action="store_true", help="重下已存在的瓦片")
    pd.set_defaults(fn=cmd_download)

    pb = sub.add_parser("bbox", help="按矩形 bbox 下载")
    pb.add_argument("south", type=float)
    pb.add_argument("west", type=float)
    pb.add_argument("north", type=float)
    pb.add_argument("east", type=float)
    pb.add_argument("--source", **common_source)
    pb.add_argument("--force", action="store_true")
    pb.set_defaults(fn=cmd_bbox)

    pi = sub.add_parser("info", help="显示 dem_cache 目录使用情况")
    pi.set_defaults(fn=cmd_info)

    pl = sub.add_parser("list", help="列出所有预设区域")
    pl.set_defaults(fn=cmd_list)

    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
