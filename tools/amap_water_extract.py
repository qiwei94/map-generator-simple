"""从高德静态地图提取水体 polygon.

原理: 高德渲染水体为特定蓝色 → 图像颜色分割 → 矢量化 → GeoJSON polygon

用法:
    export AMAP_KEY=your_key
    python tools/amap_water_extract.py --bbox 29.53,106.43,29.63,106.55 --zoom 14
"""
import os
import sys
import argparse
import math
import time
from io import BytesIO
from pathlib import Path

import numpy as np
import requests
from PIL import Image

# Optional: for vectorization
try:
    from shapely.geometry import shape, Polygon, MultiPolygon
    from shapely.ops import unary_union
    import rasterio.features
    HAS_RASTERIO = True
except ImportError:
    HAS_RASTERIO = False


def _load_key():
    k = os.environ.get("AMAP_KEY", "")
    if k:
        return k
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("AMAP_KEY="):
                return line.split("=", 1)[1].strip()
    return ""

KEY = _load_key()
STATIC_MAP_URL = "https://restapi.amap.com/v3/staticmap"

# 高德静态地图水体颜色范围 (RGB)
# 高德默认底图水体大约是 #a2d4f0 ~ #72b4e0 范围
WATER_COLOR_RANGES = [
    # (R_min, R_max, G_min, G_max, B_min, B_max)
    (100, 200, 170, 230, 210, 255),   # 浅蓝色水体
    (60, 150, 140, 210, 190, 250),    # 中蓝色水体
]


def latlon_to_tile(lat, lon, zoom):
    """WGS84 → tile coordinates."""
    n = 2 ** zoom
    x = (lon + 180) / 360 * n
    y = (1 - math.log(math.tan(math.radians(lat)) + 1/math.cos(math.radians(lat))) / math.pi) / 2 * n
    return x, y


def tile_to_latlon(tx, ty, zoom):
    """Tile coordinates → WGS84."""
    n = 2 ** zoom
    lon = tx / n * 360 - 180
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))
    return lat, lon


def fetch_static_map(center_lon, center_lat, zoom, width=1024, height=1024):
    """Fetch one static map tile from Gaode."""
    params = {
        "key": KEY,
        "location": f"{center_lon},{center_lat}",
        "zoom": zoom,
        "size": f"{width}*{height}",
        "scale": 2,  # 2x resolution
    }
    r = requests.get(STATIC_MAP_URL, params=params)
    if r.status_code != 200:
        print(f"  HTTP {r.status_code} for ({center_lat:.4f}, {center_lon:.4f})")
        return None
    
    # Check if it's actually an image
    content_type = r.headers.get("Content-Type", "")
    if "image" not in content_type:
        print(f"  Non-image response: {content_type}")
        print(f"  Body: {r.text[:200]}")
        return None
    
    img = Image.open(BytesIO(r.content)).convert("RGB")
    return np.array(img)


def extract_water_mask(img_array):
    """Extract water pixels from static map image using color segmentation."""
    r, g, b = img_array[:,:,0], img_array[:,:,1], img_array[:,:,2]
    
    mask = np.zeros(img_array.shape[:2], dtype=bool)
    for r_min, r_max, g_min, g_max, b_min, b_max in WATER_COLOR_RANGES:
        channel_mask = (
            (r >= r_min) & (r <= r_max) &
            (g >= g_min) & (g <= g_max) &
            (b >= b_min) & (b <= b_max)
        )
        mask |= channel_mask
    
    # Also detect by HSV: water is typically H=190-220, S>20%, V>50%
    from colorsys import rgb_to_hsv
    # Vectorized HSV conversion
    img_float = img_array.astype(np.float32) / 255.0
    r_f, g_f, b_f = img_float[:,:,0], img_float[:,:,1], img_float[:,:,2]
    
    cmax = np.maximum(np.maximum(r_f, g_f), b_f)
    cmin = np.minimum(np.minimum(r_f, g_f), b_f)
    delta = cmax - cmin
    
    # Hue (0-360)
    hue = np.zeros_like(cmax)
    mask_r = (cmax == r_f) & (delta > 0)
    mask_g = (cmax == g_f) & (delta > 0)
    mask_b = (cmax == b_f) & (delta > 0)
    hue[mask_r] = 60 * (((g_f[mask_r] - b_f[mask_r]) / delta[mask_r]) % 6)
    hue[mask_g] = 60 * ((b_f[mask_g] - r_f[mask_g]) / delta[mask_g] + 2)
    hue[mask_b] = 60 * ((r_f[mask_b] - g_f[mask_b]) / delta[mask_b] + 4)
    
    # Saturation (0-1)
    sat = np.where(cmax > 0, delta / cmax, 0)
    
    # Water in HSV: H≈190-230, S>0.15, V>0.5
    hsv_mask = (hue >= 180) & (hue <= 240) & (sat >= 0.12) & (cmax >= 0.45)
    
    mask |= hsv_mask
    
    # Morphological cleanup
    from scipy import ndimage
    # 1. Opening: remove small noise (icons, labels)
    mask = ndimage.binary_opening(mask, iterations=2)
    # 2. Closing with large kernel: bridge gaps (bridges ~30-50px at zoom14)
    mask = ndimage.binary_closing(mask, iterations=8)
    # 3. Fill internal holes (islands in river handled separately)
    mask = ndimage.binary_fill_holes(mask)
    # 4. Remove small connected components (< 500px = noise)
    labeled, n_labels = ndimage.label(mask)
    for i in range(1, n_labels + 1):
        if (labeled == i).sum() < 500:
            mask[labeled == i] = 0

    return mask.astype(np.uint8)


def pixel_to_lonlat(px, py, img_w, img_h, center_lon, center_lat, zoom):
    """Convert pixel position to WGS84 coordinates.
    
    Based on Web Mercator projection math for Gaode static maps.
    """
    # At zoom Z, one pixel covers: 
    # groundResolution = 156543.03392 * cos(lat) / 2^zoom  (meters/pixel)
    # But for static map with scale=2, actual pixels are 2x
    scale_factor = 2  # because we use scale=2
    
    # Gaode uses Web Mercator (EPSG:3857)
    # Convert center to pixel coordinates in the global tile system
    n = 2 ** zoom
    total_pixels = 256 * n * scale_factor
    
    # Center in pixel coordinates
    cx_px = (center_lon + 180) / 360 * total_pixels
    cy_px = (1 - math.log(math.tan(math.radians(center_lat)) + 
              1/math.cos(math.radians(center_lat))) / math.pi) / 2 * total_pixels
    
    # Target pixel in global coordinates
    gx = cx_px + (px - img_w/2)
    gy = cy_px + (py - img_h/2)
    
    # Global pixel back to lonlat
    lon = gx / total_pixels * 360 - 180
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * gy / total_pixels))))
    
    return lon, lat


def vectorize_mask(mask, center_lon, center_lat, zoom, img_w, img_h):
    """Convert binary mask to WGS84 polygons."""
    if not HAS_RASTERIO:
        print("  WARNING: rasterio not available, skipping vectorization")
        return []
    
    # Use rasterio.features to vectorize
    from rasterio.transform import from_bounds
    
    # Compute geographic bounds of the image
    lon_min, lat_max = pixel_to_lonlat(0, 0, img_w, img_h, center_lon, center_lat, zoom)
    lon_max, lat_min = pixel_to_lonlat(img_w, img_h, img_w, img_h, center_lon, center_lat, zoom)
    
    transform = from_bounds(lon_min, lat_min, lon_max, lat_max, img_w, img_h)
    
    polygons = []
    for geom_dict, value in rasterio.features.shapes(mask, transform=transform):
        if value == 1:
            poly = shape(geom_dict)
            if poly.is_valid and poly.area > 1e-8:  # filter tiny artifacts
                polygons.append(poly)
    
    return polygons


def main():
    parser = argparse.ArgumentParser(description="从高德静态地图提取水体 polygon")
    parser.add_argument("--bbox", type=str, required=True,
                        help="lat_min,lon_min,lat_max,lon_max (WGS84)")
    parser.add_argument("--zoom", type=int, default=14,
                        help="Zoom level (14-16 recommended)")
    parser.add_argument("--output", type=str, default="output/amap_water",
                        help="Output directory")
    parser.add_argument("--debug", action="store_true",
                        help="Save intermediate images")
    args = parser.parse_args()
    
    if not KEY:
        print("ERROR: 设置环境变量 AMAP_KEY")
        sys.exit(1)
    
    # Parse bbox
    parts = [float(x) for x in args.bbox.split(",")]
    lat_min, lon_min, lat_max, lon_max = parts
    
    print(f"区域: ({lat_min:.4f},{lon_min:.4f}) → ({lat_max:.4f},{lon_max:.4f})")
    print(f"Zoom: {args.zoom}")
    
    # Calculate tile coverage
    # At zoom 14, one 1024x1024 tile (scale=2 → 2048px) covers roughly:
    # ~0.04° lat × ~0.05° lon (varies with latitude)
    
    # We'll use overlapping tiles to cover the bbox
    # Ground resolution at this latitude:
    cos_lat = math.cos(math.radians((lat_min + lat_max) / 2))
    meters_per_pixel = 156543.03392 * cos_lat / (2 ** args.zoom) / 2  # /2 for scale=2
    
    img_size = 1024  # request size (will get 2048 with scale=2)
    actual_px = img_size * 2  # actual pixels received
    
    # Coverage per tile in degrees
    deg_per_tile_lon = meters_per_pixel * actual_px / (111320 * cos_lat)
    deg_per_tile_lat = meters_per_pixel * actual_px / 110574
    
    print(f"  每瓦片覆盖: {deg_per_tile_lat:.4f}° lat × {deg_per_tile_lon:.4f}° lon")
    print(f"  地面分辨率: {meters_per_pixel:.1f} m/px")
    
    # Grid of tile centers (30% overlap to ensure seam coverage)
    step_lon = deg_per_tile_lon * 0.7
    step_lat = deg_per_tile_lat * 0.7
    n_cols = max(1, math.ceil((lon_max - lon_min) / step_lon))
    n_rows = max(1, math.ceil((lat_max - lat_min) / step_lat))

    print(f"  瓦片网格: {n_rows} rows × {n_cols} cols = {n_rows * n_cols} tiles (30% overlap)")

    # Output directory
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    # --- Strategy: stitch tiles into one composite, process as single image ---
    # Compute composite image dimensions in pixels
    # Each pixel = meters_per_pixel meters
    total_lon_range = lon_max - lon_min
    total_lat_range = lat_max - lat_min
    composite_w = int(total_lon_range * 111320 * cos_lat / meters_per_pixel)
    composite_h = int(total_lat_range * 110574 / meters_per_pixel)
    print(f"  合成图尺寸: {composite_w} × {composite_h} px")

    composite = np.zeros((composite_h, composite_w, 3), dtype=np.uint8)
    coverage = np.zeros((composite_h, composite_w), dtype=np.uint8)

    for row in range(n_rows):
        for col in range(n_cols):
            lat = lat_min + (row + 0.5) * (lat_max - lat_min) / n_rows
            lon = lon_min + (col + 0.5) * (lon_max - lon_min) / n_cols

            print(f"  Fetching tile ({row},{col}): ({lat:.4f}, {lon:.4f})...", end="", flush=True)
            img = fetch_static_map(lon, lat, args.zoom, img_size, img_size)

            if img is None:
                print(" FAILED")
                continue

            tile_h, tile_w = img.shape[:2]
            print(f" {tile_w}x{tile_h}px", end="")

            if args.debug:
                Image.fromarray(img).save(out_dir / f"tile_{row}_{col}.png")

            # Place tile into composite at correct position
            # Tile center in composite pixel coordinates
            cx_px = int((lon - lon_min) / total_lon_range * composite_w)
            cy_px = int((lat_max - lat) / total_lat_range * composite_h)  # Y inverted

            x0 = cx_px - tile_w // 2
            y0 = cy_px - tile_h // 2

            # Clip to composite bounds
            src_x0 = max(0, -x0)
            src_y0 = max(0, -y0)
            dst_x0 = max(0, x0)
            dst_y0 = max(0, y0)
            src_x1 = min(tile_w, composite_w - x0)
            src_y1 = min(tile_h, composite_h - y0)
            dst_x1 = min(composite_w, x0 + tile_w)
            dst_y1 = min(composite_h, y0 + tile_h)

            if dst_x1 > dst_x0 and dst_y1 > dst_y0:
                composite[dst_y0:dst_y1, dst_x0:dst_x1] = img[src_y0:src_y1, src_x0:src_x1]
                coverage[dst_y0:dst_y1, dst_x0:dst_x1] = 1

            print(f" → placed")
            time.sleep(0.2)

    if coverage.sum() == 0:
        print("No tiles fetched successfully!")
        sys.exit(1)

    # Process composite as single image
    print(f"\n处理合成图 ({composite_w}×{composite_h})...")
    mask = extract_water_mask(composite)
    # Zero out areas not covered by any tile
    mask[coverage == 0] = 0
    water_pct = mask.sum() / coverage.sum() * 100
    print(f"  水体占比: {water_pct:.1f}%")

    if args.debug:
        Image.fromarray(composite).save(out_dir / "composite.png")
        Image.fromarray(mask * 255).save(out_dir / "mask_composite.png")

    # Vectorize composite mask
    print("矢量化...")
    from rasterio.transform import from_bounds
    transform = from_bounds(lon_min, lat_min, lon_max, lat_max, composite_w, composite_h)

    all_polygons = []
    for geom_dict, value in rasterio.features.shapes(mask, transform=transform):
        if value == 1:
            poly = shape(geom_dict)
            if poly.is_valid and poly.area > 1e-7:
                all_polygons.append(poly)

    # Filter: keep only polygons > min_area (remove small ponds/noise)
    # At this scale, 1e-6 deg² ≈ 12000 m² (roughly 110m×110m)
    min_area_deg2 = 5e-6  # ~60000m² — keep rivers/lakes, drop small ponds
    large_polys = [p for p in all_polygons if p.area >= min_area_deg2]
    small_polys = [p for p in all_polygons if p.area < min_area_deg2]
    print(f"  总 polygon: {len(all_polygons)}, 保留(≥{min_area_deg2:.1e}deg²): {len(large_polys)}, 过滤: {len(small_polys)}")

    if large_polys:
        combined = unary_union(large_polys)
        if isinstance(combined, Polygon):
            combined = MultiPolygon([combined])

        n_parts = len(combined.geoms) if isinstance(combined, MultiPolygon) else 1
        print(f"  合并结果: {n_parts} polygons, area={combined.area:.6f} deg²")

        # Save GeoJSON
        import json
        from shapely.geometry import mapping
        geojson = {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "geometry": mapping(combined),
                "properties": {"source": "amap_static_map", "zoom": args.zoom}
            }]
        }
        geojson_path = out_dir / "water_polygons.geojson"
        with open(geojson_path, "w") as f:
            json.dump(geojson, f)
        print(f"  保存: {geojson_path}")

        # Visualization
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(1, 1, figsize=(10, 10))
        if isinstance(combined, MultiPolygon):
            for p in combined.geoms:
                x, y = p.exterior.xy
                ax.fill(x, y, color="#0e74a8", alpha=0.7)
        else:
            x, y = combined.exterior.xy
            ax.fill(x, y, color="#0e74a8", alpha=0.7)
        ax.set_aspect("equal")
        ax.set_title(f"Amap water extraction (zoom={args.zoom}, stitched)")
        ax.set_xlabel("Longitude")
        ax.set_ylabel("Latitude")
        plt.tight_layout()
        fig_path = out_dir / "water_extracted.png"
        plt.savefig(fig_path, dpi=150)
        print(f"  可视化: {fig_path}")
    else:
        print("未提取到水体 polygon")


if __name__ == "__main__":
    main()
