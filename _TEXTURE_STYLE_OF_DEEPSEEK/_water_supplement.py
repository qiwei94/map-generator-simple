"""Water polygon supplementation: Gaode no-label tiles + Chamfer matching + adaptive buffer.

Priority: OSM polygon > Gaode extraction (shape-matched) > adaptive width buffer

Gaode tile service: scl=2&style=7 gives clean basemap without labels/text.
Water is bright blue, easily segmented. Resolution ~4.8m/px at zoom 14.

Used by _layer_preprocess.py (3MF pipeline) and tune_buildings_v2.py (PNG renderer).
"""
from __future__ import annotations

import json
import math
import time
from io import BytesIO
from pathlib import Path
from typing import List, Tuple, Optional

import numpy as np
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, shape, mapping
from shapely.ops import unary_union, nearest_points

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_AMAP_ZOOM = 14
_AMAP_TILE_PX = 512         # scl=2 returns 512x512 tiles
_MIN_SEGMENT_LEN = 200      # ignore uncovered segments shorter than this (m)
_MIN_POLYGON_AREA_M2 = 50000
_MAX_SUPPLEMENT_AREA_M2 = 500000  # reject Gaode polygons larger than 500k m² (0.5 km²)
_ADAPTIVE_MIN_HW = 40       # adaptive buffer 最小半宽 (m)——无参考时的河道默认
_ADAPTIVE_MAX_HW = 450      # 最大半宽上限 (m)——river default
_ADAPTIVE_DECAY_DIST = 15000
_ADAPTIVE_REF_MAX_DIST = 500  # 参考多边形必须贴近中心线（视为同一水体的延伸）

_WATERWAY_HW_CAPS = {
    "river":     (40, 450),
    "riverbank": (100, 500),
    "canal":     (12, 50),
    "stream":    (5, 20),
    "drain":     (3, 12),
    "ditch":     (2, 8),
}

# Chamfer matching search range (narrow: no-label tiles are well-calibrated)
_CHAMFER_SCALE_RANGE = (0.85, 1.15)
_CHAMFER_SCALE_STEPS = 30
_CHAMFER_ANGLE_RANGE = (-3, 3)  # degrees — GCJ correction handles most offset
_CHAMFER_ANGLE_STEP = 1
_CHAMFER_MAX_SCORE_M = 150.0    # if best score > this, match is unreliable


# ---------------------------------------------------------------------------
# Key loading
# ---------------------------------------------------------------------------

def _load_amap_key() -> str:
    """Load Gaode API key (needed only for static map fallback)."""
    import os
    k = os.environ.get("AMAP_KEY", "")
    if k:
        return k
    for env_path in [
        Path(__file__).resolve().parent.parent / ".env",
        Path.cwd() / ".env",
    ]:
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("AMAP_KEY="):
                    return line.split("=", 1)[1].strip()
    return ""


# ---------------------------------------------------------------------------
# GCJ-02 ↔ WGS84 coordinate conversion
# ---------------------------------------------------------------------------

_A = 6378245.0
_EE = 0.00669342162296594


def _out_of_china(lon: float, lat: float) -> bool:
    return not (72.004 <= lon <= 137.8347 and 0.8293 <= lat <= 55.8271)


def _transform_lat(x: float, y: float) -> float:
    ret = (-100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y +
           0.1 * x * y + 0.2 * math.sqrt(abs(x)))
    ret += (20.0 * math.sin(6.0 * x * math.pi) +
            20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) +
            40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) +
            320.0 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lon(x: float, y: float) -> float:
    ret = (300.0 + x + 2.0 * y + 0.1 * x * x +
           0.1 * x * y + 0.1 * math.sqrt(abs(x)))
    ret += (20.0 * math.sin(6.0 * x * math.pi) +
            20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi) +
            40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) +
            300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def _wgs84_to_gcj02(lon: float, lat: float) -> Tuple[float, float]:
    if _out_of_china(lon, lat):
        return lon, lat
    dlat = _transform_lat(lon - 105.0, lat - 35.0)
    dlon = _transform_lon(lon - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - _EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrtmagic) * math.pi)
    dlon = (dlon * 180.0) / (_A / sqrtmagic * math.cos(radlat) * math.pi)
    return lon + dlon, lat + dlat


def _gcj02_to_wgs84(lon: float, lat: float) -> Tuple[float, float]:
    if _out_of_china(lon, lat):
        return lon, lat
    glon, glat = _wgs84_to_gcj02(lon, lat)
    return lon - (glon - lon), lat - (glat - lat)


# ---------------------------------------------------------------------------
# Tile math
# ---------------------------------------------------------------------------

def _lon_to_tile_x(lon: float, zoom: int) -> int:
    return int((lon + 180) / 360 * (2 ** zoom))


def _lat_to_tile_y(lat: float, zoom: int) -> int:
    lat_rad = math.radians(lat)
    return int((1 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2 * (2 ** zoom))


def _tile_to_lon(x: int, zoom: int) -> float:
    return x / (2 ** zoom) * 360 - 180


def _tile_to_lat(y: int, zoom: int) -> float:
    n = math.pi - 2 * math.pi * y / (2 ** zoom)
    return math.degrees(math.atan(math.sinh(n)))


# ---------------------------------------------------------------------------
# Gaode no-label tile extraction
# ---------------------------------------------------------------------------

def _cache_path(bbox_wgs84: Tuple[float, float, float, float], zoom: int) -> Path:
    s, w, n, e = bbox_wgs84
    name = f"{s:.4f}_{w:.4f}_{n:.4f}_{e:.4f}_z{zoom}_nolabel.geojson"
    cache_dir = Path(__file__).resolve().parent.parent / "cache" / "amap_water"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / name


def _fetch_nolabel_tiles(bbox_wgs84: Tuple[float, float, float, float], zoom: int):
    """Fetch no-label basemap tiles (scl=2, style=7) covering bbox.

    Returns (mosaic_rgb, grid_bounds_gcj) or (None, None) on failure.
    grid_bounds_gcj = (south, west, north, east) in GCJ-02 frame.
    """
    import requests
    from PIL import Image

    s, w, n, e = bbox_wgs84
    w_gcj, s_gcj = _wgs84_to_gcj02(w, s)
    e_gcj, n_gcj = _wgs84_to_gcj02(e, n)

    x_min = _lon_to_tile_x(w_gcj, zoom)
    x_max = _lon_to_tile_x(e_gcj, zoom)
    y_min = _lat_to_tile_y(n_gcj, zoom)
    y_max = _lat_to_tile_y(s_gcj, zoom)

    cols = x_max - x_min + 1
    rows = y_max - y_min + 1
    total_tiles = cols * rows

    if total_tiles > 400:
        print(f"  [amap] too many tiles ({total_tiles}), reducing zoom")
        return _fetch_nolabel_tiles(bbox_wgs84, zoom - 1)

    mosaic = np.zeros((rows * _AMAP_TILE_PX, cols * _AMAP_TILE_PX, 3), dtype=np.uint8)
    servers = ["wprd01", "wprd02", "wprd03", "wprd04"]
    fetched = 0

    for iy, ty in enumerate(range(y_min, y_max + 1)):
        for ix, tx in enumerate(range(x_min, x_max + 1)):
            server = servers[(tx + ty) % 4]
            url = (f"http://{server}.is.autonavi.com/appmaptile"
                   f"?lang=zh_cn&size=1&scl=2&style=7&x={tx}&y={ty}&z={zoom}")
            try:
                r = requests.get(url, timeout=10)
                if r.status_code == 200 and len(r.content) > 1000:
                    tile = np.array(Image.open(BytesIO(r.content)).convert("RGB"))
                    if tile.shape[0] == _AMAP_TILE_PX and tile.shape[1] == _AMAP_TILE_PX:
                        mosaic[iy * _AMAP_TILE_PX:(iy + 1) * _AMAP_TILE_PX,
                               ix * _AMAP_TILE_PX:(ix + 1) * _AMAP_TILE_PX] = tile
                        fetched += 1
            except Exception:
                pass

    if fetched < total_tiles * 0.8:
        print(f"  [amap] only fetched {fetched}/{total_tiles} tiles")
        if fetched == 0:
            return None, None

    grid_bounds_gcj = (
        _tile_to_lat(y_max + 1, zoom),  # south
        _tile_to_lon(x_min, zoom),       # west
        _tile_to_lat(y_min, zoom),       # north
        _tile_to_lon(x_max + 1, zoom),   # east
    )

    print(f"  [amap] fetched {fetched}/{total_tiles} tiles "
          f"({mosaic.shape[1]}×{mosaic.shape[0]} px)")
    return mosaic, grid_bounds_gcj


def _extract_water_mask(img: np.ndarray) -> np.ndarray:
    """HSV + RGB color segmentation for water pixels in no-label basemap."""
    from scipy import ndimage

    img_f = img.astype(np.float32) / 255.0
    r, g, b = img_f[:, :, 0], img_f[:, :, 1], img_f[:, :, 2]

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

    # Water: bright blue in basemap
    mask = (hue >= 180) & (hue <= 240) & (sat >= 0.08) & (cmax >= 0.45)

    # RGB supplement for lighter/edge water pixels
    ri, gi, bi = img[:, :, 0], img[:, :, 1], img[:, :, 2]
    rgb_mask = (bi >= 190) & (gi >= 140) & (gi <= 230) & (ri >= 60) & (ri <= 200) & (bi > ri)
    mask = mask | rgb_mask

    # Morphological cleanup
    mask = ndimage.binary_opening(mask, iterations=2)
    mask = ndimage.binary_closing(mask, iterations=2)
    mask = ndimage.binary_fill_holes(mask)

    labeled, n = ndimage.label(mask)
    for i in range(1, n + 1):
        if (labeled == i).sum() < 1000:
            mask[labeled == i] = 0

    return mask.astype(np.uint8)


def _vectorize_mask(
    mask: np.ndarray,
    grid_bounds_gcj: Tuple[float, float, float, float],
) -> List[Polygon]:
    """Vectorize mask to WGS84 polygons. Tiles are in GCJ-02 projection."""
    try:
        import rasterio.features
        from rasterio.transform import from_bounds
    except ImportError:
        return []

    h, w = mask.shape
    s_gcj, w_gcj, n_gcj, e_gcj = grid_bounds_gcj

    # GCJ bounds → Web Mercator
    def _lonlat_to_merc(lon, lat):
        x = lon * 20037508.34 / 180.0
        y = math.log(math.tan((90 + lat) * math.pi / 360.0)) / math.pi * 20037508.34
        return x, y

    merc_w, merc_s = _lonlat_to_merc(w_gcj, s_gcj)
    merc_e, merc_n = _lonlat_to_merc(e_gcj, n_gcj)

    transform = from_bounds(merc_w, merc_s, merc_e, merc_n, w, h)

    polys_merc = []
    for geom_dict, value in rasterio.features.shapes(mask, transform=transform):
        if value == 1:
            poly = shape(geom_dict)
            if poly.is_valid and poly.area > 5000:
                polys_merc.append(poly)

    # Mercator → WGS84 with GCJ-02 correction
    def _merc_to_wgs84(x, y):
        lon = x * 180.0 / 20037508.34
        lat = (math.atan(math.exp(y * math.pi / 20037508.34))
               * 360.0 / math.pi - 90.0)
        return _gcj02_to_wgs84(lon, lat)

    polys_wgs = []
    for poly in polys_merc:
        ext = [_merc_to_wgs84(x, y) for x, y in poly.exterior.coords]
        p = Polygon(ext)
        if p.is_valid and p.area > 1e-7:
            polys_wgs.append(p)

    return polys_wgs


def _fetch_amap_water(
    bbox_wgs84: Tuple[float, float, float, float],
    zoom: int = 0,
) -> List[Polygon]:
    """Fetch water polygons from Gaode no-label tile service (WGS84 coords).

    Uses scl=2&style=7 tiles: clean basemap without labels/text.
    Results are disk-cached.
    """
    if zoom <= 0:
        zoom = _AMAP_ZOOM

    # Overseas bbox → skip (GCJ-02 only applies within China)
    s, w, n, e = bbox_wgs84
    center_lon, center_lat = (w + e) / 2, (s + n) / 2
    if _out_of_china(center_lon, center_lat):
        return []

    cache_file = _cache_path(bbox_wgs84, zoom)

    # Check cache
    if cache_file.exists():
        try:
            with open(cache_file) as f:
                fc = json.load(f)
            polys = []
            for feat in fc.get("features", []):
                g = shape(feat["geometry"])
                if isinstance(g, MultiPolygon):
                    polys.extend(g.geoms)
                elif isinstance(g, Polygon):
                    polys.append(g)
            print(f"  [amap] cache hit: {len(polys)} polygons from {cache_file.name}")
            return polys
        except Exception:
            pass

    # Fetch tile grid
    print(f"  [amap] fetching no-label tiles at zoom {zoom}...")
    mosaic, grid_bounds = _fetch_nolabel_tiles(bbox_wgs84, zoom)
    if mosaic is None:
        print("  [amap] tile fetch failed")
        return []

    # Extract water mask
    mask = _extract_water_mask(mosaic)
    if mask.sum() == 0:
        print("  [amap] no water detected")
        return []

    water_pct = mask.sum() / mask.size * 100

    # Vectorize
    polygons = _vectorize_mask(mask, grid_bounds)

    # Save cache
    if polygons:
        features = [{"type": "Feature", "geometry": mapping(p), "properties": {}}
                    for p in polygons]
        fc = {
            "type": "FeatureCollection",
            "features": features,
            "properties": {"source": "amap_nolabel_tiles", "zoom": zoom},
        }
        with open(cache_file, "w") as f:
            json.dump(fc, f)

    print(f"  [amap] extracted {len(polygons)} polygons ({water_pct:.1f}% water, zoom={zoom})")
    return polygons


# ---------------------------------------------------------------------------
# Chamfer distance matching (scale + angle estimation)
# ---------------------------------------------------------------------------

def _chamfer_match(
    osm_polys_utm: List[Polygon],
    amap_polys_utm: List[Polygon],
    resolution: float = 10.0,
) -> Tuple[float, float, float]:
    """Chamfer distance matching: find optimal scale/angle for Gaode→OSM alignment.

    Only uses polygons that overlap between datasets (avoids confusion from
    coverage differences). Searches narrow scale × angle range.

    Args:
        osm_polys_utm: OSM polygons in UTM local coords
        amap_polys_utm: Gaode polygons in UTM local coords
        resolution: meters per pixel for rasterization

    Returns:
        (scale, angle_deg, chamfer_score_meters)
    """
    try:
        import cv2
    except ImportError:
        return 1.0, 0.0, float("inf")

    if not osm_polys_utm or not amap_polys_utm:
        return 1.0, 0.0, float("inf")

    # Only use polygons that overlap between datasets
    osm_union = unary_union([p for p in osm_polys_utm if p.is_valid])
    osm_filtered = [p for p in osm_polys_utm if p.is_valid and p.area > 50000]
    amap_filtered = [p for p in amap_polys_utm
                     if p.is_valid and p.area > 50000
                     and p.intersects(osm_union.buffer(500))]

    if not osm_filtered or not amap_filtered:
        return 1.0, 0.0, float("inf")

    # Compute bounding box of overlapping polygons
    all_polys = osm_filtered + amap_filtered
    all_bounds = [p.bounds for p in all_polys if p.is_valid]
    if not all_bounds:
        return 1.0, 0.0, float("inf")

    min_x = min(b[0] for b in all_bounds)
    min_y = min(b[1] for b in all_bounds)
    max_x = max(b[2] for b in all_bounds)
    max_y = max(b[3] for b in all_bounds)

    # Add padding for scale > 1
    pad = max(max_x - min_x, max_y - min_y) * 0.2
    min_x -= pad
    min_y -= pad
    max_x += pad
    max_y += pad

    w_px = int((max_x - min_x) / resolution) + 1
    h_px = int((max_y - min_y) / resolution) + 1

    if w_px > 4000 or h_px > 4000:
        resolution = max(max_x - min_x, max_y - min_y) / 3000
        w_px = int((max_x - min_x) / resolution) + 1
        h_px = int((max_y - min_y) / resolution) + 1

    # Rasterize polygon boundaries to binary images
    def _rasterize_edges(polys):
        img = np.zeros((h_px, w_px), dtype=np.uint8)
        for p in polys:
            if not p.is_valid:
                continue
            coords = list(p.exterior.coords)
            pts = np.array([
                [int((x - min_x) / resolution),
                 int((max_y - y) / resolution)]  # flip Y
                for x, y in coords
            ], dtype=np.int32)
            cv2.polylines(img, [pts], isClosed=True, color=255, thickness=1)
        return img

    osm_edge_img = _rasterize_edges(osm_filtered)
    amap_edge_img = _rasterize_edges(amap_filtered)

    # Distance transform of OSM edges (target)
    dist_osm = cv2.distanceTransform(255 - osm_edge_img, cv2.DIST_L2, 3)

    # Extract Gaode edge points
    amap_pts = np.column_stack(np.where(amap_edge_img > 0))  # (row, col)
    if len(amap_pts) < 100:
        return 1.0, 0.0, float("inf")

    # Subsample for speed
    if len(amap_pts) > 5000:
        idx = np.random.default_rng(42).choice(len(amap_pts), 5000, replace=False)
        amap_pts = amap_pts[idx]

    center = amap_pts.mean(axis=0)

    # Search over scale × angle
    best_score = float("inf")
    best_scale = 1.0
    best_angle = 0.0

    scales = np.linspace(_CHAMFER_SCALE_RANGE[0], _CHAMFER_SCALE_RANGE[1],
                         _CHAMFER_SCALE_STEPS)
    angles = range(_CHAMFER_ANGLE_RANGE[0], _CHAMFER_ANGLE_RANGE[1] + 1,
                   _CHAMFER_ANGLE_STEP)

    for scale in scales:
        for angle in angles:
            theta = np.deg2rad(angle)
            R = np.array([
                [np.cos(theta), -np.sin(theta)],
                [np.sin(theta), np.cos(theta)]
            ])

            pts_centered = amap_pts - center
            pts_transformed = scale * (pts_centered @ R.T)
            pts_transformed = (pts_transformed + center).astype(int)

            valid = (
                (pts_transformed[:, 0] >= 0) &
                (pts_transformed[:, 0] < h_px) &
                (pts_transformed[:, 1] >= 0) &
                (pts_transformed[:, 1] < w_px)
            )

            pts_valid = pts_transformed[valid]
            if len(pts_valid) < 50:
                continue

            score = np.mean(dist_osm[pts_valid[:, 0], pts_valid[:, 1]])

            if score < best_score:
                best_score = score
                best_scale = scale
                best_angle = angle

    # Convert pixel-space score to meters
    best_score_m = best_score * resolution

    # Quality gate: if score too high, match is unreliable → return identity
    if best_score_m > _CHAMFER_MAX_SCORE_M:
        print(f"  [chamfer] score={best_score_m:.1f}m > {_CHAMFER_MAX_SCORE_M}m "
              f"(unreliable) → using identity")
        return 1.0, 0.0, best_score_m

    print(f"  [chamfer] best: scale={best_scale:.3f}, angle={best_angle}°, "
          f"score={best_score_m:.1f}m")
    return best_scale, float(best_angle), best_score_m


def _apply_chamfer_transform(
    polys_utm: List[Polygon],
    scale: float,
    angle_deg: float,
) -> List[Polygon]:
    """Apply scale + rotation around centroid of polygon set."""
    if abs(scale - 1.0) < 0.01 and abs(angle_deg) < 0.5:
        return polys_utm

    from shapely import affinity

    # Compute centroid of the whole set
    union = unary_union(polys_utm)
    if union.is_empty:
        return polys_utm
    cx, cy = union.centroid.x, union.centroid.y

    result = []
    for p in polys_utm:
        if not p.is_valid:
            continue
        transformed = affinity.scale(p, xfact=scale, yfact=scale, origin=(cx, cy))
        if abs(angle_deg) >= 0.5:
            transformed = affinity.rotate(transformed, angle_deg, origin=(cx, cy))
        if transformed.is_valid and not transformed.is_empty:
            result.append(transformed)
    return result


# ---------------------------------------------------------------------------
# Adaptive buffer for uncovered segments
# ---------------------------------------------------------------------------

def _estimate_polygon_width(poly: Polygon) -> float:
    if poly.length == 0:
        return 0
    return 2 * poly.area / poly.length


def _adaptive_buffer_segments(
    segments: List[Tuple[LineString, str]],
    wl_union,
    wl_list: List[Polygon],
) -> List[Polygon]:
    """Adaptive-width buffer for uncovered LineString segments.

    Args:
        segments: List of (LineString, waterway_type) tuples
    """
    if not segments or not wl_list:
        return []

    results = []
    for seg, wtype in segments:
        if seg.length < _MIN_SEGMENT_LEN:
            continue

        min_hw, max_hw = _WATERWAY_HW_CAPS.get(wtype, (_ADAPTIVE_MIN_HW, _ADAPTIVE_MAX_HW))

        best_dist, best_poly = float('inf'), None
        for p in wl_list:
            d = p.distance(seg)
            if d < best_dist:
                best_dist, best_poly = d, p

        if best_poly is None:
            half_w = min_hw
        else:
            est_w = _estimate_polygon_width(best_poly)
            direct_w = _cross_section_width(wl_union, seg)
            # 截面测量易被穿过邻接多边形内部的弦污染（延伸接触点曾
            # 测出 1600m）；只在河带上限内采纳，否则用多边形自身估宽
            if 0 < direct_w <= max_hw:
                ref_w = max(est_w, direct_w)
            else:
                ref_w = est_w

            # 参考只在“贴近 + 自身是河带状”时可信：中心线作为该水体
            # 的延伸继承其宽度。远处的大湖（西湖估宽 ~850m）会把城市
            # 河道半宽顶到上限 → 几百米宽藍带（历史 bug）。
            if best_dist <= _ADAPTIVE_REF_MAX_DIST and ref_w <= max_hw:
                decay = max(0.5, 1.0 - best_dist / _ADAPTIVE_DECAY_DIST)
                half_w = ref_w / 2 * decay
            else:
                half_w = min_hw

        half_w = max(half_w, min_hw)
        half_w = min(half_w, max_hw)

        d_start = wl_union.distance(Point(seg.coords[0]))
        d_end = wl_union.distance(Point(seg.coords[-1]))
        if d_start < d_end:
            start_hw, end_hw = half_w, half_w * 0.7
        else:
            start_hw, end_hw = half_w * 0.7, half_w

        n_pts = max(12, int(seg.length / 200))
        distances = np.linspace(0, seg.length, n_pts)
        points = [seg.interpolate(d) for d in distances]
        widths = np.linspace(start_hw, end_hw, n_pts)

        circles = [Point(p.x, p.y).buffer(w) for p, w in zip(points, widths)]
        parts = [unary_union([circles[i], circles[i + 1]]).convex_hull
                 for i in range(len(circles) - 1)]
        result = unary_union(parts)
        if not result.is_empty:
            result = result.simplify(half_w * 0.08)
            if isinstance(result, Polygon):
                results.append(result)
            elif isinstance(result, MultiPolygon):
                results.extend(result.geoms)

    return results


def _cross_section_width(polygon_union, segment, n_slices: int = 10) -> float:
    if polygon_union.is_empty:
        return 0
    widths = []
    for d in np.linspace(0, segment.length, n_slices):
        pt = segment.interpolate(d)
        px, py = pt.x, pt.y
        best_w = 0
        for angle_deg in range(0, 180, 15):
            ang = math.radians(angle_deg)
            dx, dy = math.cos(ang) * 2000, math.sin(ang) * 2000
            cross = LineString([(px - dx, py - dy), (px + dx, py + dy)])
            inter = polygon_union.intersection(cross)
            if not inter.is_empty and inter.length > best_w:
                best_w = inter.length
        if best_w > 0:
            widths.append(best_w)
    return float(np.median(widths)) if widths else 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def supplement_wl_coverage(
    wl_polygons: List[Polygon],
    wl_lines: List[Tuple[LineString, str]],
    bbox_wgs84: Tuple[float, float, float, float],
    utm_crs=None,
    origin: Optional[Tuple[float, float]] = None,
) -> List[Polygon]:
    """Supplement WL polygon coverage using Gaode + adaptive buffer.

    Args:
        wl_polygons: Existing WL polygons (UTM local coords)
        wl_lines: WL LineStrings + waterway type, UTM coords
        bbox_wgs84: (south, west, north, east) in WGS84
        utm_crs: pyproj CRS for UTM projection
        origin: UTM origin (easting, northing) for local coords

    Returns:
        Enhanced WL polygon list (original + supplemented)
    """
    if not wl_lines:
        return wl_polygons

    can_fetch_amap = (bbox_wgs84 is not None and utm_crs is not None
                      and origin is not None)

    all_lines = [line for line, _ in wl_lines]
    wl_union = unary_union(wl_polygons) if wl_polygons else Polygon()
    poly_coverage = wl_union.buffer(30) if not wl_union.is_empty else Polygon()

    # Step 1: find uncovered segments (carry waterway type)
    uncovered = []
    for line, wtype in wl_lines:
        diff = line.difference(poly_coverage)
        if diff.is_empty:
            continue
        if isinstance(diff, LineString):
            segs = [diff]
        elif hasattr(diff, 'geoms'):
            segs = [g for g in diff.geoms if isinstance(g, LineString)]
        else:
            continue
        for seg in segs:
            if seg.length >= _MIN_SEGMENT_LEN:
                uncovered.append((seg, wtype))

    if not uncovered:
        return wl_polygons

    total_uncovered_km = sum(s.length for s, _ in uncovered) / 1000
    print(f"  [water_supplement] {len(uncovered)} uncovered segments "
          f"({total_uncovered_km:.1f} km)")

    result_polys = list(wl_polygons)

    # Step 2: Gaode supplement — fetch shape, Chamfer-match to OSM, apply transform
    amap_polys_utm = []
    if not can_fetch_amap:
        print("  [water_supplement] skip Gaode (missing bbox_wgs84/utm_crs/origin)")
    else:
        try:
            amap_polys_wgs84 = _fetch_amap_water(bbox_wgs84)
            if amap_polys_wgs84 and utm_crs and origin:
                amap_raw = _project_to_utm(amap_polys_wgs84, utm_crs, origin)
                if amap_raw:
                    osm_valid = [p for p in wl_polygons if p.is_valid and p.area > 20000]
                    amap_valid = [p for p in amap_raw if p.is_valid and p.area > 20000]

                    scale, angle, score = _chamfer_match(osm_valid, amap_valid)

                    if abs(scale - 1.0) > 0.02 or abs(angle) > 0.5:
                        amap_matched = _apply_chamfer_transform(amap_raw, scale, angle)
                        print(f"  [water_supplement] Chamfer correction: "
                              f"scale={scale:.3f}, angle={angle:.1f}°")
                    else:
                        amap_matched = amap_raw
                        print(f"  [water_supplement] Gaode already aligned "
                              f"(scale={scale:.3f}, score={score:.1f}m)")

                    added_count = 0
                    skipped_area = 0
                    skipped_overlap = 0
                    for ap in amap_matched:
                        if not ap.is_valid or ap.area < _MIN_POLYGON_AREA_M2:
                            continue
                        if ap.area > _MAX_SUPPLEMENT_AREA_M2:
                            skipped_area += 1
                            continue
                        # Reject if OSM overlap ratio is too low (likely false detection)
                        osm_overlap = ap.intersection(wl_union).area if not wl_union.is_empty else 0
                        if ap.area > 0 and osm_overlap / ap.area < 0.15:
                            skipped_overlap += 1
                            continue
                        diff = ap.difference(poly_coverage)
                        if diff.is_empty:
                            continue
                        if isinstance(diff, Polygon) and diff.area > _MIN_POLYGON_AREA_M2:
                            if diff.area <= _MAX_SUPPLEMENT_AREA_M2:
                                amap_polys_utm.append(diff)
                                added_count += 1
                        elif isinstance(diff, MultiPolygon):
                            for part in diff.geoms:
                                if (part.area > _MIN_POLYGON_AREA_M2
                                        and part.area <= _MAX_SUPPLEMENT_AREA_M2):
                                    amap_polys_utm.append(part)
                                    added_count += 1
                    if skipped_area or skipped_overlap:
                        print(f"  [water_supplement] Gaode filtered: "
                              f"{skipped_area} too large, "
                              f"{skipped_overlap} low OSM overlap")

                    if amap_polys_utm:
                        result_polys.extend(amap_polys_utm)
                        print(f"  [water_supplement] +{added_count} Gaode supplement polygons")
        except Exception as e:
            print(f"  [water_supplement] Gaode failed: {e}")

    # Step 3: recalculate uncovered after Gaode
    updated_union = unary_union(result_polys) if result_polys else Polygon()
    updated_coverage = (updated_union.buffer(30)
                        if not updated_union.is_empty else Polygon())

    still_uncovered = []
    for seg, wtype in uncovered:
        diff = seg.difference(updated_coverage)
        if diff.is_empty:
            continue
        if isinstance(diff, LineString):
            if diff.length >= _MIN_SEGMENT_LEN:
                still_uncovered.append((diff, wtype))
        elif hasattr(diff, 'geoms'):
            for g in diff.geoms:
                if isinstance(g, LineString) and g.length >= _MIN_SEGMENT_LEN:
                    still_uncovered.append((g, wtype))

    # Step 4: adaptive buffer for remaining
    if still_uncovered:
        adaptive = _adaptive_buffer_segments(
            still_uncovered, updated_union, result_polys)
        if adaptive:
            result_polys.extend(adaptive)
            print(f"  [water_supplement] +{len(adaptive)} adaptive-buffer polygons")

    if len(result_polys) > len(wl_polygons):
        added = len(result_polys) - len(wl_polygons)
        print(f"  [water_supplement] total: {len(wl_polygons)} → "
              f"{len(result_polys)} (+{added})")

    return result_polys


def _project_to_utm(
    polys_wgs84: List[Polygon],
    utm_crs,
    origin: Tuple[float, float],
) -> List[Polygon]:
    """Project WGS84 polygons to local UTM coordinates."""
    try:
        from pyproj import Transformer
        transformer = Transformer.from_crs("EPSG:4326", utm_crs, always_xy=True)
    except Exception:
        return []

    ox, oy = origin
    result = []
    for poly in polys_wgs84:
        try:
            coords = list(poly.exterior.coords)
            utm_coords = []
            for lon, lat in coords:
                x, y = transformer.transform(lon, lat)
                utm_coords.append((x - ox, y - oy))
            projected = Polygon(utm_coords)
            if projected.is_valid and projected.area > _MIN_POLYGON_AREA_M2:
                result.append(projected)
        except Exception:
            continue
    return result
