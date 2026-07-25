"""标准地图获取：以高德底图作为地理对齐的"标准"，用于保真度检查.

"生成之后跟标准地图比较" —— 手动逐个修复数据缺口不可扩展，正确做法是
生成后自动与地理对齐的标准地图对比，检测缺失/断裂的水体等要素。

标准地图来源（按优先级）：
1. 高德无标注底图瓦片（scl=2&style=7）：最完整，含道路/水系/绿地，需联网
2. 本地缓存的高德水体矢量（cache/amap_water/）：渲染为蓝水白底，无需联网

注意：高德瓦片为 GCJ-02 坐标，与 WGS84 生成的浮雕图存在约 500m 整体偏移，
对"某水体是否存在"的视觉比对影响可忽略（相对 27km 边长 <2%）。
"""

import json
import os
from pathlib import Path

import numpy as np

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _to_swn_e(bbox_wgs84):
    """relief_studio bbox (min_lon, min_lat, max_lon, max_lat) → (south, west, north, east)."""
    w, s, e, n = bbox_wgs84
    return (s, w, n, e)


def get_standard_map(bbox_wgs84, output_path, zoom=13):
    """获取 bbox 区域的标准地图，保存为 PNG.

    Args:
        bbox_wgs84: (min_lon, min_lat, max_lon, max_lat)
        output_path: 标准地图 PNG 保存路径
        zoom: 瓦片缩放级别（13 ≈ 城市全景，瓦片数少）

    Returns:
        (path or None, source) — source ∈ {"amap_tiles", "amap_water_cache", None}
    """
    swn_e = _to_swn_e(bbox_wgs84)

    # 方案 A：高德底图瓦片拼接（最完整）
    try:
        from _TEXTURE_STYLE_OF_DEEPSEEK._water_supplement import _fetch_nolabel_tiles
        from PIL import Image

        mosaic, _bounds = _fetch_nolabel_tiles(swn_e, zoom)
        if mosaic is not None and mosaic.sum() > 0:
            Image.fromarray(mosaic).save(output_path)
            print(f"  [standard_map] saved AMap tiles mosaic → {output_path}")
            return output_path, "amap_tiles"
    except Exception as e:
        print(f"  [standard_map] tile fetch failed: {e}")

    # 方案 B：渲染本地缓存的高德水体矢量（无需网络）
    try:
        cache_path = _amap_water_cache_path(swn_e)
        if cache_path is not None and cache_path.exists():
            _render_water_cache(cache_path, bbox_wgs84, output_path)
            print(f"  [standard_map] rendered water cache → {output_path}")
            return output_path, "amap_water_cache"
    except Exception as e:
        print(f"  [standard_map] water cache render failed: {e}")

    print("  [standard_map] no standard map available")
    return None, None


def _amap_water_cache_path(swn_e, zoom=14):
    """与 _water_supplement._cache_path 相同的命名规则，定位本地缓存."""
    s, w, n, e = swn_e
    name = f"{s:.4f}_{w:.4f}_{n:.4f}_{e:.4f}_z{zoom}_nolabel.geojson"
    return Path(_project_root) / "cache" / "amap_water" / name


def _render_water_cache(cache_path, bbox_wgs84, output_path, size_px=1600):
    """把缓存的高德水体矢量渲染为蓝水白底图（与 bbox 地理对齐，上北下南）."""
    import rasterio.features
    from rasterio.transform import from_bounds
    from shapely.geometry import shape, box as shbox
    from PIL import Image

    with open(cache_path) as f:
        fc = json.load(f)
    polys = [shape(feat["geometry"]) for feat in fc.get("features", [])]

    w, s, e, n = bbox_wgs84
    bbox_poly = shbox(w, s, e, n)
    shapes = [(p, 1) for p in polys if p.is_valid and p.intersects(bbox_poly)]

    transform = from_bounds(w, s, e, n, size_px, size_px)
    mask = rasterio.features.rasterize(
        shapes,
        out_shape=(size_px, size_px),
        transform=transform,
        fill=0,
        dtype="uint8",
    )

    # 蓝水白底（近似高德水体配色）
    img = np.full((size_px, size_px, 3), 255, dtype=np.uint8)
    img[mask == 1] = [122, 175, 236]
    Image.fromarray(img).save(output_path)


# ---------------------------------------------------------------------------
# 程序化保真度检查（离线，无需 API key）
# ---------------------------------------------------------------------------

def _utm_epsg(bbox_wgs84):
    """由 bbox 中心经纬度推算 UTM EPSG（北半球 326xx / 南半球 327xx）."""
    w, s, e, n = bbox_wgs84
    zone = int(((w + e) / 2 + 180) / 6) + 1
    return (32600 if (s + n) / 2 >= 0 else 32700) + zone


def _load_water_geoms(geojson_path):
    """从 GeoJSON 读出水体 Polygon 与 LineString（WGS84）."""
    from shapely.geometry import shape

    with open(geojson_path, encoding="utf-8") as f:
        fc = json.load(f)
    polys, lines = [], []
    for feat in fc.get("features", []):
        gtype = feat.get("geometry", {}).get("type")
        if gtype in ("Polygon", "MultiPolygon"):
            polys.append(shape(feat["geometry"]))
        elif gtype in ("LineString", "MultiLineString"):
            lines.append(shape(feat["geometry"]))
    return polys, lines


def check_water_fidelity(
    bbox_wgs84,
    osm_water_geojson,
    viz_path=None,
    min_area_m2=8000,
    circularity_thresh=0.15,
    inset_deg=0.02,
    osm_buffer_m=15,
    line_buffer_m=30,
):
    """程序化保真度检查：高德水体 − OSM水体 = OSM 缺失的水体。

    离线、无需 API key，用几何差集直接给出每个缺失水体的位置/面积/圆形度。
    作为 AI 视觉检查（check_against_standard_map）的补充：AI 凭视觉语境判读，
    本函数给确定性几何证据，两者交叉验证更可靠。

    关键处理（缺一会产生大量伪缺口）：
    - 高德缓存已是 WGS84（_vectorize_mask 内部已做 gcj02→wgs84），勿重复转换
    - OSM 线状水体（waterway 线）buffer 后并入，避免把线状河道误判为缺失
    - 分析区向内收缩 inset_deg，去掉 bbox 边界瓦片碎屑伪影
    - 圆形度过滤分离真实水体（紧凑）与高德掌膜过度提取噪声（细长）

    Args:
        bbox_wgs84: (min_lon, min_lat, max_lon, max_lat)
        osm_water_geojson: OSM 水体 GeoJSON 路径（Polygon + LineString）
        viz_path: 可选，输出诊断图 PNG（灰=OSM水，红=缺失水）
        min_area_m2: 只报告大于此面积的缺口
        circularity_thresh: 圆形度阈值（4π·面积/周长²），≥阈值为紧凑候选水体
        inset_deg: 分析区内缩度数（~0.02 ≈ 2km）去边界伪影
        osm_buffer_m: OSM 水体 union 外扩缓冲（吸收边界对齐误差）
        line_buffer_m: OSM 线状水体 buffer 半宽

    Returns:
        dict: {
            "n_compact", "n_noise",
            "compact_total_m2", "noise_total_m2",
            "compact_gaps": [{lat,lon,area_m2,circularity}, ...] 按面积降序,
            "noise_gaps":   [...],
            "viz_path": str or None,
        }
    """
    import geopandas as gpd
    from shapely.geometry import box as shp_box
    from shapely.ops import unary_union

    utm = _utm_epsg(bbox_wgs84)

    def to_utm(geom):
        return gpd.GeoSeries([geom], crs="EPSG:4326").to_crs(epsg=utm).iloc[0]

    # ---- OSM 水体（多边形 + buffer 后的线）----
    osm_polys, osm_lines = _load_water_geoms(osm_water_geojson)
    if osm_lines:
        lines_utm = gpd.GeoSeries(osm_lines, crs="EPSG:4326").to_crs(epsg=utm)
        osm_water_utm = unary_union(
            [to_utm(p) for p in osm_polys] + list(lines_utm.buffer(line_buffer_m))
        )
    else:
        osm_water_utm = unary_union([to_utm(p) for p in osm_polys]) if osm_polys \
            else shp_box(0, 0, 0, 0)

    # ---- 高德水体（缓存已是 WGS84）----
    swn_e = _to_swn_e(bbox_wgs84)
    amap_path = _amap_water_cache_path(swn_e)
    if amap_path is None or not amap_path.exists():
        return {"n_compact": 0, "n_noise": 0, "compact_total_m2": 0,
                "noise_total_m2": 0, "compact_gaps": [], "noise_gaps": [],
                "viz_path": None, "error": f"AMap water cache not found: {amap_path}"}
    amap_polys, _ = _load_water_geoms(str(amap_path))
    amap_water_utm = unary_union([to_utm(p) for p in amap_polys]) if amap_polys \
        else shp_box(0, 0, 0, 0)

    # ---- 差集 + 边界裁剪 ----
    missing = amap_water_utm.difference(osm_water_utm.buffer(osm_buffer_m))
    w, s, e, n = bbox_wgs84
    analysis_box_utm = to_utm(shp_box(w + inset_deg, s + inset_deg,
                                      e - inset_deg, n - inset_deg))
    missing = missing.intersection(analysis_box_utm)

    # ---- 提取 + 圆形度分类 ----
    def centroid_latlon(poly_utm):
        c = gpd.GeoSeries([poly_utm.centroid], crs=f"EPSG:{utm}").to_crs(epsg=4326).iloc[0]
        return round(c.y, 5), round(c.x, 5)  # lat, lon

    compact, noise = [], []
    geoms = missing.geoms if missing.geom_type == "MultiPolygon" else [missing]
    for g in geoms:
        if g.geom_type != "Polygon" or g.area < min_area_m2 or g.length <= 0:
            continue
        lat, lon = centroid_latlon(g)
        circ = 4 * np.pi * g.area / (g.length ** 2)
        rec = {"lat": lat, "lon": lon, "area_m2": int(g.area), "circularity": round(circ, 3)}
        (compact if circ >= circularity_thresh else noise).append(rec)
    compact.sort(key=lambda r: -r["area_m2"])
    noise.sort(key=lambda r: -r["area_m2"])

    result = {
        "n_compact": len(compact),
        "n_noise": len(noise),
        "compact_total_m2": sum(r["area_m2"] for r in compact),
        "noise_total_m2": sum(r["area_m2"] for r in noise),
        "compact_gaps": compact,
        "noise_gaps": noise,
        "viz_path": None,
    }

    if viz_path:
        try:
            _render_fidelity_viz(osm_water_utm, missing, amap_water_utm,
                                 result, viz_path)
            result["viz_path"] = viz_path
        except Exception as ex:
            print(f"  [fidelity] viz failed: {ex}")

    return result


def _render_fidelity_viz(osm_utm, missing_utm, amap_utm, result, out_path):
    """诊断图：灰=OSM 已有水，红=高德有但 OSM 缺."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Polygon as MplPoly
    from matplotlib.collections import PatchCollection

    fig, ax = plt.subplots(figsize=(12, 12))
    ax.set_facecolor("white")

    def plot(geom, fc, ec, alpha):
        gs = geom.geoms if geom.geom_type.startswith("Multi") else [geom]
        patches = [MplPoly(np.array(g.exterior.coords), closed=True)
                   for g in gs if g.geom_type == "Polygon" and not g.is_empty]
        if patches:
            ax.add_collection(PatchCollection(patches, facecolor=fc, edgecolor=ec,
                                              alpha=alpha, linewidth=0.3))

    plot(osm_utm, "#cccccc", "#999999", 0.8)
    plot(missing_utm, "#e74c3c", "#c0392b", 0.85)
    minx, miny, maxx, maxy = amap_utm.bounds
    ax.set_xlim(minx, maxx)
    ax.set_ylim(miny, maxy)
    ax.set_aspect("equal")
    ax.set_title(
        f"Water fidelity: OSM water (gray) vs AMap-only missing (red)\n"
        f"{result['n_compact']} compact gaps ({result['compact_total_m2']:,} m2), "
        f"{result['n_noise']} noise slivers filtered")
    plt.savefig(out_path, dpi=120, bbox_inches="tight")
    plt.close()
