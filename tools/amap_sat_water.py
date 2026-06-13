"""卫星图水体提取工具 — 分析颜色空间 + 提取水体 mask。

高德 tile service style=6 提供纯卫星图（无标注、路网、桥梁）。
水体在卫星图中呈暗蓝绿色，与渲染地图的亮蓝色完全不同。

用法:
    venv/bin/python tools/amap_sat_water.py
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
from shapely.geometry import shape, Polygon, MultiPolygon, mapping
from shapely.ops import unary_union
from pyproj import Transformer, CRS
import rasterio.features
from rasterio.transform import from_bounds
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# === Config ===
BBOX_WGS84 = (29.4535, 106.4535, 29.6785, 106.7125)  # south, west, north, east
ZOOM = 14
OUT_DIR = Path("output/amap_align_test")
OSM_FILE = Path("tmp/osmium_water_29.4535_106.4535_29.6785_106.7125.geojson")

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


# === Tile math ===
def lon_to_tile_x(lon, zoom):
    return int((lon + 180) / 360 * (2 ** zoom))

def lat_to_tile_y(lat, zoom):
    lat_rad = math.radians(lat)
    return int((1 - math.log(math.tan(lat_rad) + 1/math.cos(lat_rad)) / math.pi) / 2 * (2 ** zoom))

def tile_to_lon(x, zoom):
    return x / (2 ** zoom) * 360 - 180

def tile_to_lat(y, zoom):
    n = math.pi - 2 * math.pi * y / (2 ** zoom)
    return math.degrees(math.atan(math.sinh(n)))


# === Step 1: Fetch satellite tiles ===
def fetch_satellite_grid(bbox, zoom):
    """Download satellite tiles (style=6) covering bbox."""
    s, w, n, e = bbox
    # Convert to GCJ-02 for Gaode tile service
    w_gcj, s_gcj = wgs84_to_gcj02(w, s)
    e_gcj, n_gcj = wgs84_to_gcj02(e, n)

    x_min = lon_to_tile_x(w_gcj, zoom)
    x_max = lon_to_tile_x(e_gcj, zoom)
    y_min = lat_to_tile_y(n_gcj, zoom)  # north = smaller y
    y_max = lat_to_tile_y(s_gcj, zoom)

    cols = x_max - x_min + 1
    rows = y_max - y_min + 1
    print(f"  Tile grid: {cols}×{rows} = {cols*rows} tiles at zoom {zoom}")

    mosaic = np.zeros((rows * 256, cols * 256, 3), dtype=np.uint8)
    servers = ["wprd01", "wprd02", "wprd03", "wprd04"]

    for iy, ty in enumerate(range(y_min, y_max + 1)):
        for ix, tx in enumerate(range(x_min, x_max + 1)):
            server = servers[(tx + ty) % 4]
            url = f"http://{server}.is.autonavi.com/appmaptile?x={tx}&y={ty}&z={zoom}&style=6"
            try:
                r = requests.get(url, timeout=10)
                if r.status_code == 200 and len(r.content) > 1000:
                    tile = np.array(Image.open(BytesIO(r.content)).convert("RGB"))
                    mosaic[iy*256:(iy+1)*256, ix*256:(ix+1)*256] = tile
            except Exception as ex:
                print(f"    WARN: tile ({tx},{ty}) failed: {ex}")

    # Compute geographic bounds of the tile grid (in GCJ-02 → convert back to WGS84)
    grid_w_gcj = tile_to_lon(x_min, zoom)
    grid_e_gcj = tile_to_lon(x_max + 1, zoom)
    grid_n_gcj = tile_to_lat(y_min, zoom)
    grid_s_gcj = tile_to_lat(y_max + 1, zoom)

    # These bounds are in GCJ-02 space (which is what the tile pixels represent)
    grid_bounds_gcj = (grid_s_gcj, grid_w_gcj, grid_n_gcj, grid_e_gcj)
    print(f"  Mosaic: {mosaic.shape[1]}×{mosaic.shape[0]} px")
    print(f"  GCJ bounds: S={grid_s_gcj:.4f} W={grid_w_gcj:.4f} N={grid_n_gcj:.4f} E={grid_e_gcj:.4f}")
    return mosaic, grid_bounds_gcj


# === Step 2: Color analysis ===
def analyze_colors(img, osm_mask=None):
    """Analyze color distribution in satellite image."""
    img_f = img.astype(np.float32) / 255.0
    r, g, b = img_f[:,:,0], img_f[:,:,1], img_f[:,:,2]

    # Compute HSV
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
    val = cmax

    # NDWI-like index: (G - NIR) / (G + NIR), approximate with (G - R) / (G + R)
    ndwi = np.where((g + r) > 0, (g - r) / (g + r), 0)

    print(f"  Image stats:")
    print(f"    Hue: mean={hue.mean():.1f}, median={np.median(hue):.1f}")
    print(f"    Sat: mean={sat.mean():.3f}, median={np.median(sat):.3f}")
    print(f"    Val: mean={val.mean():.3f}, median={np.median(val):.3f}")
    print(f"    NDWI: mean={ndwi.mean():.3f}, median={np.median(ndwi):.3f}")

    return hue, sat, val, ndwi


# === Step 3: Water extraction from satellite ===
def extract_water_satellite(img):
    """Extract water mask from satellite imagery.

    Satellite water characteristics (Chongqing rivers):
    - Dark teal/green-blue color
    - Hue 140-200 (cyan-blue range)
    - Moderate saturation (0.15-0.5)
    - Low-medium brightness (0.2-0.55)
    - High green relative to red (positive NDWI)
    """
    img_f = img.astype(np.float32) / 255.0
    r, g, b = img_f[:,:,0], img_f[:,:,1], img_f[:,:,2]

    # HSV
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
    val = cmax

    # Strategy: multi-condition intersection
    # Condition 1: Hue in teal-blue range (120-210)
    hue_mask = (hue >= 120) & (hue <= 210)

    # Condition 2: Not too bright (excludes bright buildings, clouds)
    bright_mask = (val >= 0.15) & (val <= 0.55)

    # Condition 3: Some saturation (excludes gray roads/buildings)
    sat_mask = sat >= 0.10

    # Condition 4: Green dominates over red (water signature)
    green_dom = g > r

    # Condition 5: Blue >= Green (blue-green water, not pure vegetation)
    blue_ge_green = b >= (g - 0.05)

    # Primary mask: all conditions
    mask = hue_mask & bright_mask & sat_mask & green_dom & blue_ge_green

    # Secondary: very dark teal water (some sections darker)
    dark_water = (val >= 0.10) & (val <= 0.30) & (hue >= 140) & (hue <= 200) & (sat >= 0.15)
    mask = mask | dark_water

    # Morphological cleanup
    mask = ndimage.binary_opening(mask, iterations=2)
    mask = ndimage.binary_closing(mask, iterations=4)
    mask = ndimage.binary_fill_holes(mask)

    # Remove small components
    labeled, n = ndimage.label(mask)
    for i in range(1, n + 1):
        if (labeled == i).sum() < 2000:
            mask[labeled == i] = 0

    pct = mask.sum() / mask.size * 100
    print(f"  Satellite water mask: {pct:.1f}% pixels")
    return mask.astype(np.uint8)


# === Step 4: Vectorize ===
def vectorize_satellite_mask(mask, grid_bounds_gcj, zoom):
    """Vectorize satellite mask. Tiles are in GCJ-02 projection."""
    h, w = mask.shape
    s_gcj, w_gcj, n_gcj, e_gcj = grid_bounds_gcj

    # Convert GCJ bounds to Web Mercator
    def lonlat_to_merc(lon, lat):
        x = lon * 20037508.34 / 180.0
        y = math.log(math.tan((90 + lat) * math.pi / 360.0)) / math.pi * 20037508.34
        return x, y

    merc_w, merc_s = lonlat_to_merc(w_gcj, s_gcj)
    merc_e, merc_n = lonlat_to_merc(e_gcj, n_gcj)

    transform = from_bounds(merc_w, merc_s, merc_e, merc_n, w, h)

    polys_merc = []
    for geom_dict, value in rasterio.features.shapes(mask, transform=transform):
        if value == 1:
            poly = shape(geom_dict)
            if poly.is_valid and poly.area > 10000:
                polys_merc.append(poly)

    # Mercator → WGS84 with GCJ-02 correction
    def merc_to_wgs84(x, y):
        lon = x * 180.0 / 20037508.34
        lat = math.atan(math.exp(y * math.pi / 20037508.34)) * 360.0 / math.pi - 90.0
        return gcj02_to_wgs84(lon, lat)

    polys_wgs = []
    for poly in polys_merc:
        ext = [merc_to_wgs84(x, y) for x, y in poly.exterior.coords]
        p = Polygon(ext)
        if p.is_valid and p.area > 1e-7:
            polys_wgs.append(p)

    print(f"  Vectorized: {len(polys_wgs)} polygons")
    return polys_wgs


# === Step 5: Load OSM & compare ===
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
    return polys, lines


def project_to_utm(polys_wgs=None, lines_wgs=None):
    utm_crs = CRS.from_epsg(32648)
    tr = Transformer.from_crs("EPSG:4326", utm_crs, always_xy=True)
    ox, oy = tr.transform(BBOX_WGS84[1], BBOX_WGS84[0])

    def proj_poly(g):
        ext = [(x-ox, y-oy) for x, y in [tr.transform(lon, lat) for lon, lat in g.exterior.coords]]
        return Polygon(ext)
    def proj_line(g):
        coords = [(x-ox, y-oy) for x, y in [tr.transform(lon, lat) for lon, lat in g.coords]]
        from shapely.geometry import LineString
        return LineString(coords)

    result_p = [proj_poly(p) for p in (polys_wgs or []) if p.is_valid]
    result_l = [proj_line(l) for l in (lines_wgs or [])]
    return result_p, result_l


# === Step 6: Width measurement ===
def measure_widths(osm_polys_utm, sat_polys_utm, osm_lines_utm):
    """Measure width ratio at cross-sections."""
    osm_union = unary_union([p for p in osm_polys_utm if p.is_valid])
    sat_union = unary_union([p for p in sat_polys_utm if p.is_valid])

    if osm_union.is_empty or sat_union.is_empty:
        return 1.0, []

    from shapely.geometry import LineString
    measurements = []
    for line in osm_lines_utm:
        if line.length < 500:
            continue
        for frac in np.linspace(0.1, 0.9, 10):
            pt = line.interpolate(frac, normalized=True)
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
            sat_w = sat_union.intersection(cross).length

            if osm_w > 100 and sat_w > 100:
                measurements.append({
                    "pt": (pt.x, pt.y),
                    "osm_w": osm_w,
                    "sat_w": sat_w,
                    "ratio": osm_w / sat_w,
                })

    if not measurements:
        return 1.0, measurements

    ratios = [m["ratio"] for m in measurements]
    return float(np.median(ratios)), measurements


# === Step 7: Visualization ===
def plot_comparison(img, mask, osm_polys_utm, osm_lines_utm, sat_polys_utm,
                    measurements, scale_ratio):
    fig, axes = plt.subplots(2, 3, figsize=(24, 16))

    # Panel 1: Satellite image
    ax = axes[0, 0]
    ax.imshow(img)
    ax.set_title("1. Satellite image (style=6, no labels)")
    ax.axis("off")

    # Panel 2: Water mask overlay
    ax = axes[0, 1]
    ax.imshow(img)
    mask_rgba = np.zeros((*mask.shape, 4), dtype=np.float32)
    mask_rgba[:,:,2] = mask.astype(np.float32)
    mask_rgba[:,:,3] = mask.astype(np.float32) * 0.4
    ax.imshow(mask_rgba)
    ax.set_title(f"2. Water mask ({mask.sum()/mask.size*100:.1f}% water)")
    ax.axis("off")

    # Panel 3: Color histogram of water vs non-water
    ax = axes[0, 2]
    img_f = img.astype(np.float32) / 255.0
    r, g, b = img_f[:,:,0], img_f[:,:,1], img_f[:,:,2]
    water_r = r[mask == 1].ravel()
    water_g = g[mask == 1].ravel()
    water_b = b[mask == 1].ravel()
    land_r = r[mask == 0].ravel()

    # Sample for histogram
    n_sample = min(50000, len(water_r))
    if n_sample > 0:
        idx = np.random.choice(len(water_r), n_sample, replace=False)
        ax.hist(water_r[idx], bins=50, alpha=0.5, color='r', label='Water R', density=True)
        ax.hist(water_g[idx], bins=50, alpha=0.5, color='g', label='Water G', density=True)
        ax.hist(water_b[idx], bins=50, alpha=0.5, color='b', label='Water B', density=True)
    ax.set_title("3. Water pixel RGB distribution")
    ax.legend()

    # Panel 4: UTM overlay - full
    ax = axes[1, 0]
    for p in sat_polys_utm:
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
    ax.set_title("4. UTM: Blue=Satellite, Red=OSM poly, Orange=OSM line")
    ax.grid(True, alpha=0.2)

    # Panel 5: Confluence zoom
    ax = axes[1, 1]
    tr = Transformer.from_crs("EPSG:4326", CRS.from_epsg(32648), always_xy=True)
    ox, oy = tr.transform(BBOX_WGS84[1], BBOX_WGS84[0])
    cx, cy = tr.transform(106.585, 29.563)
    cx -= ox; cy -= oy
    for p in sat_polys_utm:
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
    for m in measurements:
        ax.plot(m["pt"][0], m["pt"][1], "g+", markersize=8)
    ax.set_aspect("equal")
    ax.set_xlim(cx-5000, cx+5000)
    ax.set_ylim(cy-5000, cy+5000)
    ax.set_title(f"5. Confluence zoom (scale_ratio={scale_ratio:.3f})")
    ax.grid(True, alpha=0.2)

    # Panel 6: Width ratios
    ax = axes[1, 2]
    if measurements:
        ratios = [m["ratio"] for m in measurements]
        ax.hist(ratios, bins=20, color="steelblue", alpha=0.7)
        ax.axvline(np.median(ratios), color="red", linestyle="--",
                   label=f"median={np.median(ratios):.3f}")
        ax.set_xlabel("OSM width / Satellite width")
        ax.set_ylabel("Count")
        ax.legend()
    ax.set_title(f"6. Width ratio distribution (n={len(measurements)})")

    plt.tight_layout()
    out = OUT_DIR / "satellite_extraction_v2.png"
    plt.savefig(out, dpi=150)
    print(f"  Plot: {out}")


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("Step 1: Fetch satellite tiles (style=6, zoom=14)")
    print("=" * 60)
    img, grid_bounds_gcj = fetch_satellite_grid(BBOX_WGS84, ZOOM)
    Image.fromarray(img).save(OUT_DIR / "sat_mosaic_z14.png")

    print("\n" + "=" * 60)
    print("Step 2: Color analysis")
    print("=" * 60)
    analyze_colors(img)

    print("\n" + "=" * 60)
    print("Step 3: Extract water mask")
    print("=" * 60)
    mask = extract_water_satellite(img)
    Image.fromarray(mask * 255).save(OUT_DIR / "sat_water_mask_v2.png")

    print("\n" + "=" * 60)
    print("Step 4: Vectorize")
    print("=" * 60)
    sat_polys_wgs = vectorize_satellite_mask(mask, grid_bounds_gcj, ZOOM)

    # Save as GeoJSON
    features = []
    for p in sat_polys_wgs:
        features.append({"type": "Feature", "geometry": mapping(p), "properties": {}})
    geojson = {"type": "FeatureCollection", "features": features}
    out_geojson = OUT_DIR / "sat_water_polys.geojson"
    with open(out_geojson, "w") as f:
        json.dump(geojson, f)
    print(f"  Saved: {out_geojson}")

    print("\n" + "=" * 60)
    print("Step 5: Load OSM & project to UTM")
    print("=" * 60)
    osm_polys_wgs, osm_lines_wgs = load_osm()
    print(f"  OSM: {len(osm_polys_wgs)} polygons, {len(osm_lines_wgs)} lines")

    osm_polys_utm, osm_lines_utm = project_to_utm(osm_polys_wgs, osm_lines_wgs)
    sat_polys_utm, _ = project_to_utm(sat_polys_wgs)
    sat_polys_utm = [p for p in sat_polys_utm if p.is_valid and p.area > 20000]
    print(f"  OSM UTM: {len(osm_polys_utm)} polys, {len(osm_lines_utm)} lines")
    print(f"  Satellite UTM: {len(sat_polys_utm)} polys")

    print("\n" + "=" * 60)
    print("Step 6: Width measurement")
    print("=" * 60)
    scale_ratio, measurements = measure_widths(osm_polys_utm, sat_polys_utm, osm_lines_utm)
    print(f"  Measurements: {len(measurements)} cross-sections")
    if measurements:
        osm_ws = [m["osm_w"] for m in measurements]
        sat_ws = [m["sat_w"] for m in measurements]
        print(f"  OSM width: median={np.median(osm_ws):.0f}m, mean={np.mean(osm_ws):.0f}m")
        print(f"  Satellite width: median={np.median(sat_ws):.0f}m, mean={np.mean(sat_ws):.0f}m")
        print(f"  Scale ratio (OSM/Sat): {scale_ratio:.3f}")

    print("\n" + "=" * 60)
    print("Step 7: Visualization")
    print("=" * 60)
    plot_comparison(img, mask, osm_polys_utm, osm_lines_utm, sat_polys_utm,
                    measurements, scale_ratio)

    print("\n" + "=" * 60)
    print("DONE")
    print("=" * 60)


if __name__ == "__main__":
    main()
