"""独立工具：高德水体提取 + OSM 对齐验证。

逐步验证：
1. 下载高德瓦片，提取水体 mask
2. 矢量化为 polygon（Mercator → WGS84）
3. 与 OSM 数据对比：位置、形状、宽度
4. 计算比例因子，输出缩放后的结果

用法:
    venv/bin/python tools/amap_align_test.py
"""
import math
import json
import sys
from io import BytesIO
from pathlib import Path

import numpy as np
import requests
from PIL import Image
from scipy import ndimage
from shapely.geometry import shape, LineString, Polygon, MultiPolygon, Point, mapping
from shapely.ops import unary_union
from pyproj import Transformer, CRS
import rasterio.features
from rasterio.transform import from_bounds
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


# === Config ===
BBOX_WGS84 = (29.4535, 106.4535, 29.6785, 106.7125)  # south, west, north, east
ZOOM = 12
TILE_SIZE = 1024
SCALE = 2
OSM_FILE = Path("tmp/osmium_water_29.4535_106.4535_29.6785_106.7125.geojson")
OUT_DIR = Path("output/amap_align_test")

# GCJ-02 constants
_A = 6378245.0
_EE = 0.00669342162296594


def _load_key():
    for p in [Path(".env"), Path(__file__).resolve().parent.parent / ".env"]:
        if p.exists():
            for line in p.read_text().splitlines():
                if line.startswith("AMAP_KEY="):
                    return line.split("=", 1)[1].strip()
    return ""


# === GCJ-02 ===
def _out_of_china(lon, lat):
    return not (72.004 <= lon <= 137.8347 and 0.8293 <= lat <= 55.8271)

def _transform_lat(x, y):
    ret = -100 + 2*x + 3*y + 0.2*y*y + 0.1*x*y + 0.2*math.sqrt(abs(x))
    ret += (20*math.sin(6*x*math.pi) + 20*math.sin(2*x*math.pi)) * 2/3
    ret += (20*math.sin(y*math.pi) + 40*math.sin(y/3*math.pi)) * 2/3
    ret += (160*math.sin(y/12*math.pi) + 320*math.sin(y*math.pi/30)) * 2/3
    return ret

def _transform_lon(x, y):
    ret = 300 + x + 2*y + 0.1*x*x + 0.1*x*y + 0.1*math.sqrt(abs(x))
    ret += (20*math.sin(6*x*math.pi) + 20*math.sin(2*x*math.pi)) * 2/3
    ret += (20*math.sin(x*math.pi) + 40*math.sin(x/3*math.pi)) * 2/3
    ret += (150*math.sin(x/12*math.pi) + 300*math.sin(x/30*math.pi)) * 2/3
    return ret

def wgs84_to_gcj02(lon, lat):
    if _out_of_china(lon, lat):
        return lon, lat
    dlat = _transform_lat(lon - 105, lat - 35)
    dlon = _transform_lon(lon - 105, lat - 35)
    radlat = lat / 180 * math.pi
    magic = math.sin(radlat)
    magic = 1 - _EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180) / ((_A * (1 - _EE)) / (magic * sqrtmagic) * math.pi)
    dlon = (dlon * 180) / (_A / sqrtmagic * math.cos(radlat) * math.pi)
    return lon + dlon, lat + dlat

def gcj02_to_wgs84(lon, lat):
    if _out_of_china(lon, lat):
        return lon, lat
    glon, glat = wgs84_to_gcj02(lon, lat)
    return lon - (glon - lon), lat - (glat - lat)


# === Step 1: 下载高德瓦片 ===
def fetch_tile(center_lon, center_lat, zoom):
    key = _load_key()
    if not key:
        print("ERROR: no AMAP_KEY in .env")
        sys.exit(1)
    url = "https://restapi.amap.com/v3/staticmap"
    params = {
        "key": key,
        "location": f"{center_lon},{center_lat}",
        "zoom": zoom,
        "size": f"{TILE_SIZE}*{TILE_SIZE}",
        "scale": SCALE,
    }
    print(f"  Fetching tile: center=({center_lon:.4f}, {center_lat:.4f}), zoom={zoom}")
    r = requests.get(url, params=params, timeout=15)
    if r.status_code != 200 or "image" not in r.headers.get("Content-Type", ""):
        print(f"  FAILED: status={r.status_code}")
        sys.exit(1)
    img = np.array(Image.open(BytesIO(r.content)).convert("RGB"))
    print(f"  Image: {img.shape[1]}×{img.shape[0]} px")
    return img


# === Step 2: 水体 mask 提取 ===
def extract_water_mask(img):
    img_f = img.astype(np.float32) / 255.0
    r, g, b = img_f[:,:,0], img_f[:,:,1], img_f[:,:,2]
    cmax = np.maximum(np.maximum(r, g), b)
    cmin = np.minimum(np.minimum(r, g), b)
    delta = cmax - cmin

    hue = np.zeros_like(cmax)
    m_r = (cmax == r) & (delta > 0)
    m_g = (cmax == g) & (delta > 0)
    m_b = (cmax == b) & (delta > 0)
    hue[m_r] = 60 * (((g[m_r] - b[m_r]) / delta[m_r]) % 6)
    hue[m_g] = 60 * ((b[m_g] - r[m_g]) / delta[m_g] + 2)
    hue[m_b] = 60 * ((r[m_b] - g[m_b]) / delta[m_b] + 4)
    sat = np.where(cmax > 0, delta / cmax, 0)

    mask = (hue >= 180) & (hue <= 240) & (sat >= 0.12) & (cmax >= 0.45)

    # RGB supplement
    ri, gi, bi = img[:,:,0], img[:,:,1], img[:,:,2]
    rgb_mask = (ri >= 60) & (ri <= 200) & (gi >= 140) & (gi <= 230) & (bi >= 190) & (bi <= 255)
    mask = mask | rgb_mask

    # Morphological: conservative closing (3 iterations = ~57m at zoom12)
    mask = ndimage.binary_opening(mask, iterations=2)
    mask = ndimage.binary_closing(mask, iterations=3)
    mask = ndimage.binary_fill_holes(mask)
    labeled, n = ndimage.label(mask)
    for i in range(1, n + 1):
        if (labeled == i).sum() < 500:
            mask[labeled == i] = 0

    pct = mask.sum() / mask.size * 100
    print(f"  Water mask: {pct:.1f}% pixels")
    return mask.astype(np.uint8)


# === Step 3: 矢量化 (Mercator → WGS84) ===
def vectorize_mask(mask, center_lon, center_lat, zoom, apply_gcj_correction=True):
    tile_h, tile_w = mask.shape
    mpp = 156543.03392 / (2 ** zoom) / SCALE

    # Tile bounds in Mercator
    cx_merc = center_lon * 20037508.34 / 180.0
    cy_merc = math.log(math.tan((90 + center_lat) * math.pi / 360.0)) / math.pi * 20037508.34
    half_x = tile_w / 2 * mpp
    half_y = tile_h / 2 * mpp

    merc_w = cx_merc - half_x
    merc_e = cx_merc + half_x
    merc_s = cy_merc - half_y
    merc_n = cy_merc + half_y

    transform = from_bounds(merc_w, merc_s, merc_e, merc_n, tile_w, tile_h)

    polys_merc = []
    for geom_dict, value in rasterio.features.shapes(mask, transform=transform):
        if value == 1:
            poly = shape(geom_dict)
            if poly.is_valid and poly.area > 50000:
                polys_merc.append(poly)

    # Mercator → WGS84
    def to_wgs84(x, y):
        lon = x * 180.0 / 20037508.34
        lat = math.atan(math.exp(y * math.pi / 20037508.34)) * 360.0 / math.pi - 90.0
        if apply_gcj_correction:
            return gcj02_to_wgs84(lon, lat)
        return lon, lat

    cos_lat = math.cos(math.radians(center_lat))
    min_area_deg2 = 50000 / (111320 * cos_lat * 110574)

    polys_wgs = []
    for poly in polys_merc:
        ext = [to_wgs84(x, y) for x, y in poly.exterior.coords]
        p = Polygon(ext)
        if p.is_valid and p.area >= min_area_deg2:
            polys_wgs.append(p)

    print(f"  Vectorized: {len(polys_wgs)} polygons (gcj_correction={apply_gcj_correction})")
    return polys_wgs


# === Step 4: 加载 OSM 数据 ===
def load_osm():
    with open(OSM_FILE) as f:
        data = json.load(f)
    polys, lines = [], []
    for feat in data["features"]:
        g = shape(feat["geometry"])
        if g.geom_type == "Polygon":
            polys.append(g)
        elif g.geom_type == "MultiPolygon":
            polys.extend(g.geoms)
        elif g.geom_type == "LineString":
            lines.append(g)
        elif g.geom_type == "MultiLineString":
            lines.extend(g.geoms)
    print(f"  OSM: {len(polys)} polygons, {len(lines)} LineStrings")
    return polys, lines


# === Step 5: 投影到 UTM 对比 ===
def project_to_utm(polys_wgs=None, lines_wgs=None):
    utm_crs = CRS.from_epsg(32648)
    tr = Transformer.from_crs("EPSG:4326", utm_crs, always_xy=True)
    ox, oy = tr.transform(BBOX_WGS84[1], BBOX_WGS84[0])  # west, south

    def proj_poly(g):
        ext = [(x-ox, y-oy) for x, y in [tr.transform(lon, lat) for lon, lat in g.exterior.coords]]
        return Polygon(ext)
    def proj_line(g):
        coords = [(x-ox, y-oy) for x, y in [tr.transform(lon, lat) for lon, lat in g.coords]]
        return LineString(coords)

    result_p = [proj_poly(p) for p in (polys_wgs or []) if p.is_valid]
    result_l = [proj_line(l) for l in (lines_wgs or [])]
    return result_p, result_l


# === Step 6: 宽度比较 + 比例计算 ===
def measure_width_ratio(osm_polys_utm, amap_polys_utm, osm_lines_utm):
    """在重叠区域的横截面上测量 OSM 和 Gaode 宽度。"""
    osm_union = unary_union([p for p in osm_polys_utm if p.is_valid])
    amap_union = unary_union([p for p in amap_polys_utm if p.is_valid])

    if osm_union.is_empty or amap_union.is_empty:
        return 1.0, []

    measurements = []
    for line in osm_lines_utm:
        if line.length < 800:
            continue
        for frac in np.linspace(0.1, 0.9, 8):
            pt = line.interpolate(frac, normalized=True)
            # Perpendicular direction
            d = 50
            p1 = line.interpolate(max(0, frac * line.length - d))
            p2 = line.interpolate(min(line.length, frac * line.length + d))
            dx, dy = p2.x - p1.x, p2.y - p1.y
            L = math.sqrt(dx*dx + dy*dy)
            if L < 1:
                continue
            nx, ny = -dy/L, dx/L

            cross = LineString([
                (pt.x + nx*3000, pt.y + ny*3000),
                (pt.x - nx*3000, pt.y - ny*3000),
            ])

            osm_w = osm_union.intersection(cross).length
            amap_w = amap_union.intersection(cross).length

            if osm_w > 150 and amap_w > 150:
                measurements.append({
                    "pt": (pt.x, pt.y),
                    "osm_w": osm_w,
                    "amap_w": amap_w,
                    "ratio": osm_w / amap_w,
                })

    if not measurements:
        return 1.0, measurements

    ratios = [m["ratio"] for m in measurements]
    median_ratio = float(np.median(ratios))
    return median_ratio, measurements


# === Step 7: 可视化 ===
def plot_results(osm_polys_utm, osm_lines_utm, amap_polys_utm, amap_scaled_utm,
                 measurements, scale_ratio):
    fig, axes = plt.subplots(2, 2, figsize=(20, 20))

    # Panel 1: Raw overlay (full)
    ax = axes[0, 0]
    for p in amap_polys_utm:
        if p.is_valid:
            try:
                x, y = p.exterior.xy
                ax.fill(x, y, color="#a0d4f0", alpha=0.5)
                ax.plot(x, y, "b-", linewidth=0.5)
            except: pass
    for p in osm_polys_utm:
        if p.is_valid:
            x, y = p.exterior.xy
            ax.fill(x, y, color="#e05050", alpha=0.4)
            ax.plot(x, y, "r-", linewidth=0.8)
    for l in osm_lines_utm:
        x, y = l.xy
        ax.plot(x, y, "orange", linewidth=1, linestyle="--", alpha=0.7)
    ax.set_aspect("equal")
    ax.set_xlim(-500, 26000)
    ax.set_ylim(-500, 26000)
    ax.set_title("1. Raw: Blue=Gaode, Red=OSM poly, Orange=OSM line")
    ax.grid(True, alpha=0.2)

    # Panel 2: Zoom confluence - raw
    ax = axes[0, 1]
    tr = Transformer.from_crs("EPSG:4326", CRS.from_epsg(32648), always_xy=True)
    ox, oy = tr.transform(BBOX_WGS84[1], BBOX_WGS84[0])
    cx, cy = tr.transform(106.585, 29.563)
    cx -= ox; cy -= oy
    for p in amap_polys_utm:
        if p.is_valid:
            try:
                x, y = p.exterior.xy
                ax.fill(x, y, color="#a0d4f0", alpha=0.5)
                ax.plot(x, y, "b-", linewidth=0.8)
            except: pass
    for p in osm_polys_utm:
        if p.is_valid:
            x, y = p.exterior.xy
            ax.fill(x, y, color="#e05050", alpha=0.4)
            ax.plot(x, y, "r-", linewidth=0.8)
    for l in osm_lines_utm:
        x, y = l.xy
        ax.plot(x, y, "orange", linewidth=1.5, linestyle="--")
    ax.set_aspect("equal")
    ax.set_xlim(cx-5000, cx+5000)
    ax.set_ylim(cy-5000, cy+5000)
    ax.set_title(f"2. Confluence zoom - RAW (scale_ratio={scale_ratio:.2f})")
    ax.grid(True, alpha=0.2)

    # Panel 3: After scaling - full
    ax = axes[1, 0]
    for p in amap_scaled_utm:
        if p.is_valid:
            try:
                x, y = p.exterior.xy
                ax.fill(x, y, color="#a0d4f0", alpha=0.5)
                ax.plot(x, y, "b-", linewidth=0.5)
            except: pass
    for p in osm_polys_utm:
        if p.is_valid:
            x, y = p.exterior.xy
            ax.fill(x, y, color="#e05050", alpha=0.4)
            ax.plot(x, y, "r-", linewidth=0.8)
    for l in osm_lines_utm:
        x, y = l.xy
        ax.plot(x, y, "orange", linewidth=1, linestyle="--", alpha=0.7)
    ax.set_aspect("equal")
    ax.set_xlim(-500, 26000)
    ax.set_ylim(-500, 26000)
    ax.set_title(f"3. After scale correction: Blue=Gaode scaled")
    ax.grid(True, alpha=0.2)

    # Panel 4: Zoom confluence - scaled
    ax = axes[1, 1]
    for p in amap_scaled_utm:
        if p.is_valid:
            try:
                x, y = p.exterior.xy
                ax.fill(x, y, color="#a0d4f0", alpha=0.5)
                ax.plot(x, y, "b-", linewidth=0.8)
            except: pass
    for p in osm_polys_utm:
        if p.is_valid:
            x, y = p.exterior.xy
            ax.fill(x, y, color="#e05050", alpha=0.4)
            ax.plot(x, y, "r-", linewidth=0.8)
    for l in osm_lines_utm:
        x, y = l.xy
        ax.plot(x, y, "orange", linewidth=1.5, linestyle="--")
    # Mark measurement points
    for m in measurements:
        ax.plot(m["pt"][0], m["pt"][1], "g+", markersize=8)
    ax.set_aspect("equal")
    ax.set_xlim(cx-5000, cx+5000)
    ax.set_ylim(cy-5000, cy+5000)
    ax.set_title(f"4. Confluence zoom - SCALED (+: measurement points)")
    ax.grid(True, alpha=0.2)

    plt.tight_layout()
    out = OUT_DIR / "alignment_result.png"
    plt.savefig(out, dpi=150)
    print(f"  Plot: {out}")


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    s, w, n, e = BBOX_WGS84
    center_lon = (w + e) / 2
    center_lat = (s + n) / 2

    print("=" * 60)
    print("Step 1: 下载高德瓦片")
    print("=" * 60)
    img = fetch_tile(center_lon, center_lat, ZOOM)
    Image.fromarray(img).save(OUT_DIR / "tile_raw.png")

    print("\n" + "=" * 60)
    print("Step 2: 提取水体 mask")
    print("=" * 60)
    mask = extract_water_mask(img)
    Image.fromarray(mask * 255).save(OUT_DIR / "water_mask.png")

    print("\n" + "=" * 60)
    print("Step 3: 矢量化 (with GCJ-02 correction)")
    print("=" * 60)
    amap_polys_wgs = vectorize_mask(mask, center_lon, center_lat, ZOOM,
                                     apply_gcj_correction=True)

    print("\n" + "=" * 60)
    print("Step 4: 加载 OSM 数据")
    print("=" * 60)
    osm_polys_wgs, osm_lines_wgs = load_osm()

    print("\n" + "=" * 60)
    print("Step 5: 投影到 UTM")
    print("=" * 60)
    osm_polys_utm, osm_lines_utm = project_to_utm(osm_polys_wgs, osm_lines_wgs)
    amap_polys_utm, _ = project_to_utm(amap_polys_wgs)
    amap_polys_utm = [p for p in amap_polys_utm if p.is_valid and p.area > 50000]
    print(f"  OSM UTM: {len(osm_polys_utm)} polys, {len(osm_lines_utm)} lines")
    print(f"  Gaode UTM: {len(amap_polys_utm)} polys")

    print("\n" + "=" * 60)
    print("Step 6: 宽度比较 + 比例计算")
    print("=" * 60)
    scale_ratio, measurements = measure_width_ratio(
        osm_polys_utm, amap_polys_utm, osm_lines_utm)
    print(f"  Measurements: {len(measurements)} cross-sections")
    if measurements:
        osm_ws = [m["osm_w"] for m in measurements]
        amap_ws = [m["amap_w"] for m in measurements]
        print(f"  OSM width: median={np.median(osm_ws):.0f}m, "
              f"mean={np.mean(osm_ws):.0f}m")
        print(f"  Gaode width: median={np.median(amap_ws):.0f}m, "
              f"mean={np.mean(amap_ws):.0f}m")
        print(f"  Scale ratio (OSM/Gaode): {scale_ratio:.3f}")
        print(f"  → Gaode needs to shrink by factor {scale_ratio:.3f}")

        # Compute negative buffer distance
        median_amap_w = np.median(amap_ws)
        target_w = median_amap_w * scale_ratio
        shrink_per_side = (median_amap_w - target_w) / 2
        print(f"  → Negative buffer: {shrink_per_side:.0f}m per side")
    else:
        shrink_per_side = 0
        print("  No overlap measurements — cannot compute scale")

    print("\n" + "=" * 60)
    print("Step 7: 应用缩放 (negative buffer)")
    print("=" * 60)
    if shrink_per_side > 10:
        amap_scaled_utm = []
        for p in amap_polys_utm:
            shrunk = p.buffer(-shrink_per_side)
            if shrunk.is_empty:
                continue
            if isinstance(shrunk, Polygon) and shrunk.area > 50000:
                amap_scaled_utm.append(shrunk)
            elif isinstance(shrunk, MultiPolygon):
                for part in shrunk.geoms:
                    if part.area > 50000:
                        amap_scaled_utm.append(part)
        print(f"  Scaled: {len(amap_polys_utm)} → {len(amap_scaled_utm)} polygons "
              f"(buffer={-shrink_per_side:.0f}m)")
    else:
        amap_scaled_utm = amap_polys_utm
        print(f"  No shrink needed (shrink={shrink_per_side:.0f}m)")

    # Verify: re-measure after scaling
    if amap_scaled_utm and measurements:
        ratio2, meas2 = measure_width_ratio(
            osm_polys_utm, amap_scaled_utm, osm_lines_utm)
        if meas2:
            print(f"  After scaling — ratio: {ratio2:.3f} "
                  f"(target=1.0, {len(meas2)} points)")

    print("\n" + "=" * 60)
    print("Step 8: 生成对比图")
    print("=" * 60)
    plot_results(osm_polys_utm, osm_lines_utm, amap_polys_utm, amap_scaled_utm,
                 measurements, scale_ratio)

    print("\n" + "=" * 60)
    print("DONE")
    print("=" * 60)
    print(f"  Output: {OUT_DIR}/")
    print(f"  - tile_raw.png (高德原始瓦片)")
    print(f"  - water_mask.png (水体 mask)")
    print(f"  - alignment_result.png (4-panel 对比图)")


if __name__ == "__main__":
    main()
