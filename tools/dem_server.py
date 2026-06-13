#!/usr/bin/env python3
"""ECS DEM Tile Download Server

在 ECS 上启动 HTTP 服务，提供 SRTM/Copernicus 瓦片的远程下载能力。
ECS 从 AWS S3 下载瓦片，本地通过 HTTP（走 ECS 内网/公网）拉取，绕过国内到 AWS 的慢速链路。

用法（默认启动 HTTP 服务）：
    python tools/dem_server.py              # 启动服务，端口 8080
    python tools/dem_server.py --port 9090  # 指定端口
    python tools/dem_server.py --host 0.0.0.0 --port 8080

客户端命令（从本地操作远程 ECS）：
    python tools/dem_server.py client <url> status
    python tools/dem_server.py client <url> list
    python tools/dem_server.py client <url> tiles-info
    python tools/dem_server.py client <url> download --bbox 29.43 106.41 29.66 106.66
    python tools/dem_server.py client <url> download --preset chongqing
    python tools/dem_server.py client <url> sync --bbox 29.43 106.41 29.66 106.66 --out ./dem_cache
    python tools/dem_server.py client <url> sync --preset chongqing --out ./dem_cache

ECS 部署示例：
    # 把脚本传到 ECS
    scp tools/dem_server.py user@ecs:/path/to/project/tools/

    # 在 ECS 上后台启动
    nohup python tools/dem_server.py --host 0.0.0.0 --port 8080 > dem_server.log 2>&1 &

    # 从本地操作
    python tools/dem_server.py client http://<ecs-ip>:8080 download --preset chongqing
    python tools/dem_server.py client http://<ecs-ip>:8080 sync --preset chongqing --out ./dem_cache
"""

import argparse
import gzip
import json
import math
import os
import sys
import time
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from io import BytesIO

# ---------------------------------------------------------------------------
# 路径 & 预设区域（与 manage_dem.py 保持一致）
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEM_CACHE_DIR = PROJECT_ROOT / "dem_cache"

PRESETS = {
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
    "westlake":   {"bbox": (30.13, 120.01, 30.36, 120.29), "desc": "杭州西湖（测试）"},
}

SRTM_URLS = [
    "https://elevation-tiles-prod.s3.amazonaws.com/skadi/{dir}/{filename}",
    "https://s3.amazonaws.com/elevation-tiles-prod/skadi/{dir}/{filename}",
]

COP30_BASE = "https://copernicus-dem-30m.s3.amazonaws.com"


# ---------------------------------------------------------------------------
# Core functions（复用 manage_dem.py 的逻辑）
# ---------------------------------------------------------------------------


def _hgt_filename(lat: int, lon: int) -> str:
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"{ns}{abs(lat):02d}{ew}{abs(lon):03d}.hgt"


def _hgt_local_path(lat: int, lon: int) -> Path:
    ns = "N" if lat >= 0 else "S"
    return DEM_CACHE_DIR / "srtm" / f"{ns}{abs(lat):02d}" / _hgt_filename(lat, lon)


def _srtm_urls_for_tile(lat: int, lon: int) -> list[str]:
    fname = _hgt_filename(lat, lon)
    ns = "N" if lat >= 0 else "S"
    tile_dir = f"{ns}{abs(lat):02d}"
    return [tmpl.format(dir=tile_dir, filename=fname + ".gz") for tmpl in SRTM_URLS]


def _cop30_tile_id(lat: int, lon: int) -> str:
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"Copernicus_DSM_COG_10_{ns}{abs(lat):02d}_00_{ew}{abs(lon):03d}_00_DEM"


def _cop30_local_path(lat: int, lon: int) -> Path:
    tid = _cop30_tile_id(lat, lon)
    return DEM_CACHE_DIR / "cop30" / tid / f"{tid}.tif"


def tiles_for_bbox(south: float, west: float, north: float, east: float) -> list[tuple[int, int]]:
    lat_lo = int(math.floor(south))
    lat_hi = int(math.floor(north))
    lon_lo = int(math.floor(west))
    lon_hi = int(math.floor(east))
    return [
        (lat, lon)
        for lat in range(lat_lo, lat_hi + 1)
        for lon in range(lon_lo, lon_hi + 1)
    ]


def get_tile_path(tile_lat: int, tile_lon: int, source: str = "srtm") -> Path:
    if source == "srtm":
        return _hgt_local_path(tile_lat, tile_lon)
    else:
        return _cop30_local_path(tile_lat, tile_lon)


def compute_cache_stats() -> dict:
    stats = {"srtm": {"tiles": 0, "size_mb": 0.0}, "cop30": {"tiles": 0, "size_mb": 0.0}}
    for source in ("srtm", "cop30"):
        sub = DEM_CACHE_DIR / source
        if not sub.exists():
            continue
        files = [f for f in sub.rglob("*") if f.is_file()]
        stats[source]["tiles"] = len(files)
        stats[source]["size_mb"] = round(sum(f.stat().st_size for f in files) / 1024 / 1024, 1)
    return stats


# ---------------------------------------------------------------------------
# 下载函数（使用 requests，失败时返回错误信息字符串）
# ---------------------------------------------------------------------------


def download_one_tile(lat: int, lon: int, source: str, force: bool = False) -> tuple[bool, str]:
    """下载单个瓦片。返回 (success, message)。"""
    try:
        import requests
    except ImportError:
        return False, "requests 未安装，请先执行: pip install requests"

    if source == "srtm":
        out_path = _hgt_local_path(lat, lon)
        if out_path.exists() and not force:
            return True, "已存在（跳过）"
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
                return True, f"{size_mb:.1f}MB ({took:.1f}s)"
            except Exception as e:
                last_err = repr(e)
                continue
        return False, str(last_err)

    else:  # cop30
        out_path = _cop30_local_path(lat, lon)
        if out_path.exists() and not force:
            return True, "已存在（跳过）"
        out_path.parent.mkdir(parents=True, exist_ok=True)

        import requests
        url = f"{COP30_BASE}/{_cop30_tile_id(lat, lon)}/{_cop30_tile_id(lat, lon)}.tif"
        try:
            t0 = time.time()
            with requests.get(url, timeout=120, stream=True) as resp:
                if resp.status_code == 404:
                    return False, "404（海面/未覆盖）"
                resp.raise_for_status()
                with out_path.open("wb") as f:
                    for chunk in resp.iter_content(chunk_size=1 << 20):
                        f.write(chunk)
            took = time.time() - t0
            size_mb = out_path.stat().st_size / 1024 / 1024
            return True, f"{size_mb:.1f}MB ({took:.1f}s)"
        except Exception as e:
            return False, repr(e)


def download_bbox(south: float, west: float, north: float, east: float,
                  source: str = "srtm", force: bool = False) -> list[dict]:
    """下载覆盖 bbox 的所有瓦片。返回结果列表。"""
    tiles = tiles_for_bbox(south, west, north, east)
    results = []
    for lat, lon in tiles:
        t0 = time.time()
        ok, msg = download_one_tile(lat, lon, source, force)
        results.append({
            "tile": _hgt_filename(lat, lon) if source == "srtm" else _cop30_tile_id(lat, lon),
            "lat": lat,
            "lon": lon,
            "success": ok,
            "message": msg,
            "time_s": round(time.time() - t0, 1),
        })
    return results


# ---------------------------------------------------------------------------
# HTTP Server
# ---------------------------------------------------------------------------

API_PREFIX = "/api"


class DEMServerHandler(BaseHTTPRequestHandler):

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, msg: str, status: int = 400):
        self._send_json({"error": msg}, status)

    def _send_file(self, filepath: Path):
        if not filepath.exists() or not filepath.is_file():
            self._send_error("文件不存在", 404)
            return
        size = filepath.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{filepath.name}"')
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        with filepath.open("rb") as f:
            self.wfile.write(f.read())

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def log_message(self, fmt, *args):
        # 日志加时间戳
        sys.stderr.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {fmt % args}\n")

    # ---- Routes ----

    def do_OPTIONS(self):
        """CORS preflight"""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")

        # GET /api/status
        if path == f"{API_PREFIX}/status":
            stats = compute_cache_stats()
            stats["cache_dir"] = str(DEM_CACHE_DIR)
            self._send_json(stats)

        # GET /api/list
        elif path == f"{API_PREFIX}/list":
            tiles = []
            srtm_dir = DEM_CACHE_DIR / "srtm"
            if srtm_dir.exists():
                for lat_dir in sorted(srtm_dir.iterdir()):
                    if lat_dir.is_dir():
                        for hgt in sorted(lat_dir.glob("*.hgt")):
                            tiles.append({
                                "tile": hgt.name,
                                "path": str(hgt.relative_to(DEM_CACHE_DIR)),
                                "size_mb": round(hgt.stat().st_size / 1024 / 1024, 2),
                            })
            self._send_json({"tiles": tiles, "count": len(tiles)})

        # GET /api/tiles-info
        elif path == f"{API_PREFIX}/tiles-info":
            info = []
            for name, p in PRESETS.items():
                b = p["bbox"]
                n = len(tiles_for_bbox(*b))
                info.append({"name": name, "desc": p["desc"], "bbox": list(b), "tile_count": n})
            self._send_json({"presets": info})

        # GET /api/files/<source>/<path>
        elif path.startswith(f"{API_PREFIX}/files/"):
            rel_path = path[len(f"{API_PREFIX}/files/"):]
            filepath = DEM_CACHE_DIR / rel_path
            self._send_file(filepath)

        else:
            self._send_error(f"未知路径: {self.path}", 404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")

        # POST /api/download
        if path == f"{API_PREFIX}/download":
            try:
                body = self._read_body()
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_error("请求体必须是 JSON")
                return

            source = body.get("source", "srtm")
            force = body.get("force", False)

            if "preset" in body:
                preset = body["preset"]
                if preset not in PRESETS:
                    self._send_error(f"未知预设: {preset}，可选: {list(PRESETS.keys())}")
                    return
                bbox = PRESETS[preset]["bbox"]
                south, west, north, east = bbox
                desc = PRESETS[preset]["desc"]
            elif "bbox" in body:
                b = body["bbox"]
                if len(b) != 4:
                    self._send_error("bbox 必须为 [south, west, north, east]")
                    return
                south, west, north, east = b
                desc = f"{south:.2f},{west:.2f} → {north:.2f},{east:.2f}"
            else:
                self._send_error("需要 preset 或 bbox 参数")
                return

            if source not in ("srtm", "cop30"):
                self._send_error("source 必须为 srtm 或 cop30")
                return

            # 下载瓦片（同步执行）
            results = download_bbox(south, west, north, east, source, force)

            self._send_json({
                "status": "completed",
                "source": source,
                "area": desc,
                "results": results,
                "total": len(results),
                "success": sum(1 for r in results if r["success"]),
                "failed": sum(1 for r in results if not r["success"]),
            })

        else:
            self._send_error(f"未知路径: {self.path}", 404)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


def cmd_client(url: str, args: list[str]):
    try:
        import requests as req
    except ImportError:
        print("❌ 客户端模式需要 requests: pip install requests")
        sys.exit(1)

    if not args:
        print("❌ 需要指定子命令: status | list | tiles-info | download | sync")
        sys.exit(1)

    sub = args[0]
    rest = args[1:]

    base_url = url.rstrip("/")

    if sub == "status":
        r = req.get(f"{base_url}/api/status", timeout=10)
        data = r.json()
        if "error" in data:
            print(f"❌ {data['error']}")
            return
        print(f"\nDEM 缓存状态: {data['cache_dir']}")
        for src in ("srtm", "cop30"):
            s = data.get(src, {})
            print(f"  [{src}]  {s.get('tiles', 0)} 个文件，{s.get('size_mb', 0)} MB")

    elif sub == "list":
        r = req.get(f"{base_url}/api/list", timeout=10)
        data = r.json()
        if "error" in data:
            print(f"❌ {data['error']}")
            return
        print(f"\n已缓存瓦片: {data['count']} 个")
        for t in data["tiles"]:
            print(f"  {t['tile']:14s}  {t['size_mb']:.1f} MB  ({t['path']})")

    elif sub == "tiles-info":
        r = req.get(f"{base_url}/api/tiles-info", timeout=10)
        data = r.json()
        if "error" in data:
            print(f"❌ {data['error']}")
            return
        print(f"{'名称':<12} {'描述':<22} {'瓦片数':<8} {'BBox':<40}")
        print("-" * 90)
        for p in data["presets"]:
            b = p["bbox"]
            bbox_str = f"S {b[0]:.2f}° W {b[1]:.2f}° → N {b[2]:.2f}° E {b[3]:.2f}°"
            print(f"{p['name']:<12} {p['desc']:<22} {p['tile_count']:<8} {bbox_str}")

    elif sub == "download":
        # 解析参数
        parser = argparse.ArgumentParser()
        parser.add_argument("--bbox", type=float, nargs=4, metavar=("S", "W", "N", "E"))
        parser.add_argument("--preset", type=str)
        parser.add_argument("--source", default="srtm", choices=["srtm", "cop30"])
        parser.add_argument("--force", action="store_true")
        opts, _ = parser.parse_known_args(rest)

        payload = {"source": opts.source, "force": opts.force}
        if opts.preset:
            payload["preset"] = opts.preset
        elif opts.bbox:
            payload["bbox"] = list(opts.bbox)
        else:
            print("❌ 需要 --bbox 或 --preset")
            return

        print(f"⏳ 正在触发下载...")
        r = req.post(f"{base_url}/api/download", json=payload, timeout=600)
        data = r.json()
        if "error" in data:
            print(f"❌ {data['error']}")
            return
        print(f"\n区域: {data['area']}")
        print(f"数据源: {data['source']}")
        print(f"总计: {data['total']} 瓦片，成功 {data['success']}，失败 {data['failed']}")
        for r in data.get("results", []):
            icon = "✅" if r["success"] else "❌"
            print(f"  {icon} {r['tile']:20s}  {r['message']}")

    elif sub == "sync":
        # download + 拉取文件
        parser = argparse.ArgumentParser()
        parser.add_argument("--bbox", type=float, nargs=4, metavar=("S", "W", "N", "E"))
        parser.add_argument("--preset", type=str)
        parser.add_argument("--source", default="srtm", choices=["srtm", "cop30"])
        parser.add_argument("--force", action="store_true")
        parser.add_argument("--out", type=str, default=str(DEM_CACHE_DIR),
                           help="本地缓存目录（默认 dem_cache）")
        opts, _ = parser.parse_known_args(rest)

        # 先触发下载
        payload = {"source": opts.source, "force": opts.force}
        if opts.preset:
            payload["preset"] = opts.preset
        elif opts.bbox:
            payload["bbox"] = list(opts.bbox)
        else:
            print("❌ 需要 --bbox 或 --preset")
            return

        print(f"⏳ [1/2] 触发下载...")
        r = req.post(f"{base_url}/api/download", json=payload, timeout=600)
        data = r.json()
        if "error" in data:
            print(f"❌ {data['error']}")
            return
        print(f"   成功 {data['success']} / {data['total']}，失败 {data['failed']}")

        # 确定需要拉取的瓦片文件名列表
        out_dir = Path(opts.out)
        out_dir.mkdir(parents=True, exist_ok=True)

        tiles_to_pull = []
        for r in data.get("results", []):
            if r["success"]:
                if opts.source == "srtm":
                    # 从 tile 名反推路径: N29E106.hgt → srtm/N29/N29E106.hgt
                    ns = r["tile"][0]
                    lat_str = r["tile"][1:3]
                    tile_path = f"srtm/{ns}{lat_str}/{r['tile']}"
                else:
                    tile_path = f"cop30/{r['tile']}/{r['tile']}.tif"
                tiles_to_pull.append(tile_path)

        if not tiles_to_pull:
            print("没有需要拉取的文件")
            return

        print(f"⏳ [2/2] 拉取 {len(tiles_to_pull)} 个文件到 {out_dir}...")
        for tpath in tiles_to_pull:
            local_file = out_dir / tpath
            local_file.parent.mkdir(parents=True, exist_ok=True)
            file_url = f"{base_url}/api/files/{tpath}"
            try:
                fr = req.get(file_url, timeout=120)
                if fr.status_code == 200:
                    local_file.write_bytes(fr.content)
                    size_mb = len(fr.content) / 1024 / 1024
                    print(f"  ✅ {tpath:40s}  {size_mb:.1f} MB")
                else:
                    print(f"  ❌ {tpath:40s}  HTTP {fr.status_code}")
            except Exception as e:
                print(f"  ❌ {tpath:40s}  {e}")

        print(f"\n完成。文件已同步到 {out_dir}")

    else:
        print(f"❌ 未知子命令: {sub}")
        print(f"  可选: status | list | tiles-info | download | sync")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="ECS DEM Tile Download Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0）")
    p.add_argument("--port", type=int, default=8080, help="监听端口（默认 8080）")
    p.add_argument("--daemon", action="store_true", help="后台运行（fork）")

    # client 模式
    p.add_argument("client", nargs="?", default=None,
                   help='客户端模式。用法: client <url> <command> [args...]')

    return p


def main():
    # 如果第一个参数是 "client"，进入客户端模式
    if len(sys.argv) > 1 and sys.argv[1] == "client":
        if len(sys.argv) < 4:
            print("用法: python tools/dem_server.py client <url> <command> [args...]")
            print()
            print(__doc__)
            sys.exit(1)
        url = sys.argv[2]
        cmd_args = sys.argv[3:]
        cmd_client(url, cmd_args)
        return

    # 服务端模式
    parser = build_parser()
    args, _ = parser.parse_known_args()

    if args.daemon:
        pid = os.fork()
        if pid > 0:
            print(f"后台运行中，PID: {pid}")
            sys.exit(0)

    server = HTTPServer((args.host, args.port), DEMServerHandler)
    print(f"🚀 DEM Tile Server 已启动: http://{args.host}:{args.port}")
    print(f"   状态查询:  curl http://localhost:{args.port}/api/status")
    print(f"   下载瓦片:  curl -X POST http://localhost:{args.port}/api/download \\")
    print(f"               -H 'Content-Type: application/json' \\")
    print(f"               -d '{{\"preset\": \"chongqing\"}}'")
    print(f"   获取缓存:  curl http://localhost:{args.port}/api/files/srtm/N29/N29E106.hgt")
    print(f"\n   客户端模式:")
    print(f"   python tools/dem_server.py client http://localhost:{args.port} status")
    print(f"   python tools/dem_server.py client http://localhost:{args.port} sync --preset chongqing")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
        server.server_close()


if __name__ == "__main__":
    main()
