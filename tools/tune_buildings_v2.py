#!/usr/bin/env python3
"""buildings 调参工具 v2 — 方案 c：道路切 block + 块内 union。

用法：
    # 单组参数
    python tools/tune_buildings_v2.py --print-limit 3500 --road-tier 2 --simplify 5

    # 网格搜索 4×3×3 = 36 组
    python tools/tune_buildings_v2.py --grid

    # 只在 5km 西湖中心子区域跑（快）
    python tools/tune_buildings_v2.py --grid --sub

输入：tmp/osmium_building_*.geojson + tmp/osmium_road_*.geojson
输出：output/tune_buildings_v2/<参数串>.png

算法（方案 c）：
  1. 按 road-tier 过滤 OSM 道路 LineString
  2. 加 bbox 边界 → unary_union → polygonize → city blocks
  3. 大建筑（≥print_limit）个体保留
  4. 小建筑按 centroid 分配到 block
  5. 每 block 内 unary_union → 街区聚合 footprint
  6. simplify + 过滤 < print_limit 的碎片
"""

from __future__ import annotations

import argparse
import os
import pickle
import sys
import time
from itertools import product
from pathlib import Path
from typing import List, Tuple

_PROJECT = Path(__file__).resolve().parent.parent
if str(_PROJECT) not in sys.path:
    sys.path.insert(0, str(_PROJECT))

import numpy as np
import geopandas as gpd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon as MplPoly, Rectangle as MplRect, \
    PathPatch as MplPathPatch
from matplotlib.path import Path as MplPath
from matplotlib.collections import PatchCollection, LineCollection
from shapely import concave_hull as _shapely_concave_hull
from shapely.geometry import (
    LineString, MultiLineString, MultiPolygon, Polygon, box,
)
from shapely.ops import polygonize, unary_union
from shapely.strtree import STRtree

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import (
    bbox_to_utm, project_geodataframe,
)
from _TEXTURE_STYLE_OF_DEEPSEEK._landmark import (
    classify_landmarks_in_gdf, compute_top_percent_threshold,
    compute_hotspot_block_ids,
    is_vegetation_landmark, is_water_landmark, is_road_landmark,
    landmark_priority, is_tag_landmark,
)

# ---------------------------------------------------------------------------
# 字体（macOS 中文支持）
# ---------------------------------------------------------------------------
plt.rcParams['font.sans-serif'] = ['PingFang HK', 'Heiti TC', 'STHeiti',
                                     'Hiragino Sans GB', 'sans-serif']
plt.rcParams['axes.unicode_minus'] = False

# ---------------------------------------------------------------------------
# 城市英文名（用于顶部标题）
# ---------------------------------------------------------------------------
CITY_NAMES = {
    "westlake":  "Hangzhou",
    "chongqing": "Chongqing",
    "chicago":   "Chicago",
}


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OUT_DIR = _PROJECT / "output" / "tune_buildings_v2"

# 默认 bbox（西湖 25km）— 可通过 CLI --bbox 或 --city 覆盖
LAT1, LON1, LAT2, LON2 = 30.13, 120.01, 30.36, 120.29
SUB_LAT1, SUB_LON1, SUB_LAT2, SUB_LON2 = 30.20, 120.10, 30.27, 120.20

# 城市预设  (lat1, lon1, lat2, lon2, pbf_basename)
# 25km × 25km 中心点：杭州西湖 / 重庆朝天门 / 芝加哥 The Loop
CITY_PRESETS = {
    "westlake":   (30.13,    120.01,    30.36,    120.29,    "zhejiang-latest"),
    "chongqing":  (29.4535,  106.4535,  29.6785,  106.7125,  "chongqing-260508"),
    "chicago":    (41.7656,  -87.8926,  41.9906,  -87.5900,  "chicago"),
}


def _geojson_path(tag: str, lat1: float, lon1: float, lat2: float, lon2: float):
    """与 osmium_cli_fetcher 完全一致的命名约定"""
    return _PROJECT / "tmp" / f"osmium_{tag}_{lat1:.4f}_{lon1:.4f}_{lat2:.4f}_{lon2:.4f}.geojson"


def _ensure_geojson(tag: str, lat1: float, lon1: float, lat2: float, lon2: float,
                    pbf_basename: str) -> Path:
    """确保 osmium_*.geojson 存在；缺则调用 osmium CLI 提取。"""
    path = _geojson_path(tag, lat1, lon1, lat2, lon2)
    if path.exists():
        return path
    pbf = _PROJECT / "pbf_cache" / f"{pbf_basename}.osm.pbf"
    if not pbf.exists():
        sys.exit(f"❌ {path.name} 不存在且 PBF {pbf} 也没有，无法 fetch")
    print(f"  Fetching {tag} via osmium CLI from {pbf.name} ...")
    from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osmium_cli_fetcher import (
        get_cli_fetcher,
    )
    fetcher = get_cli_fetcher()
    fetcher.fetch_features(tag_type=tag, pbf_file=str(pbf),
                            south=lat1, west=lon1, north=lat2, east=lon2)
    if not path.exists():
        sys.exit(f"❌ fetch 后仍找不到 {path}（osmium 失败？）")
    return path

# 道路等级分层（高到低）— 越高 tier 越精细，街区切得越多
ROAD_TIERS = {
    1: ["motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link"],
    2: ["motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link",
        "secondary", "secondary_link", "tertiary", "tertiary_link"],
    3: ["motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link",
        "secondary", "secondary_link", "tertiary", "tertiary_link",
        "residential", "unclassified", "living_street"],
    4: ["motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link",
        "secondary", "secondary_link", "tertiary", "tertiary_link",
        "residential", "unclassified", "living_street",
        "service"],   # +小区车道 / 停车场过道
    5: ["motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link",
        "secondary", "secondary_link", "tertiary", "tertiary_link",
        "residential", "unclassified", "living_street",
        "service",
        "pedestrian", "footway", "path", "steps", "track"],   # +所有人行道
}


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_data(sub: bool = False, force: bool = False,
              lat1: float = None, lon1: float = None,
              lat2: float = None, lon2: float = None,
              pbf_basename: str = "zhejiang-latest",
              cache_label: str = "westlake",
             ) -> Tuple:
    """加载 + 缓存全套数据。
    返回 11 元组:
      polys (建筑投影后切单 Polygon),
      landmark_flags (与 polys 等长 bool),
      roads_gdf,
      water_gdf,
      ctx,
      veg_landmark_polys (用于半透绿层),
      building_landmark_records (用于标注 [(geom, name, name_en, priority)]),
      veg_landmark_records,
      water_landmark_records,
      road_landmark_records
    """
    if lat1 is None:
        lat1, lon1, lat2, lon2 = LAT1, LON1, LAT2, LON2
    cache = _PROJECT / "tmp" / f"tune_v2_cache.{cache_label}.{'sub' if sub else 'full'}.pkl"
    if cache.exists() and not force:
        with open(cache, "rb") as f:
            data = pickle.load(f)
        # 缓存版本检查
        required = ("water", "landmark_flags", "veg_landmarks",
                    "veg_landmark_records", "water_landmark_records",
                    "road_landmark_records", "building_landmark_records",
                    "polys_to_row", "bgdf_tags",
                    "railway", "pier", "stadium", "landuse")
        ctx_valid = (isinstance(data.get("ctx"), dict)
                     and data["ctx"].get("bbox_wgs84") is not None
                     and data["ctx"].get("utm_crs") is not None)
        if all(k in data for k in required) and ctx_valid:
            water_gdf = data["water"]
            print(f"  [cache] {cache.name}: "
                  f"{len(data['polys'])} buildings ({sum(data['landmark_flags'])} bldg-landmarks), "
                  f"{len(data['roads'])} roads, "
                  f"{0 if water_gdf is None else len(water_gdf)} water, "
                  f"{len(data['veg_landmarks'])} veg-landmark polys, "
                  f"{len(data['water_landmark_records'])} water-landmark records, "
                  f"{len(data['road_landmark_records'])} road-landmark records, "
                  f"railway={0 if data['railway'] is None else len(data['railway'])} "
                  f"pier={0 if data['pier'] is None else len(data['pier'])} "
                  f"stadium={0 if data['stadium'] is None else len(data['stadium'])} "
                  f"landuse={0 if data['landuse'] is None else len(data['landuse'])}")
            return (data["polys"], data["landmark_flags"], data["roads"],
                    water_gdf, data["ctx"], data["veg_landmarks"],
                    data["building_landmark_records"],
                    data["veg_landmark_records"],
                    data["water_landmark_records"],
                    data["road_landmark_records"],
                    data["polys_to_row"], data["bgdf_tags"],
                    data["railway"], data["pier"], data["stadium"],
                    data["landuse"])
        else:
            missing = [k for k in required if k not in data]
            print(f"  [cache miss] {cache.name} 缺字段 {missing}，重新加载")

    # 子区域：取 bbox 中心 1/3 边长（不同 city 通用）
    if sub:
        clat = (lat1 + lat2) / 2; clon = (lon1 + lon2) / 2
        h_lat = (lat2 - lat1) / 6; h_lon = (lon2 - lon1) / 6
        sub_l1, sub_lo1, sub_l2, sub_lo2 = clat-h_lat, clon-h_lon, clat+h_lat, clon+h_lon
        print(f"  --sub 模式: 中心 1/3 ({sub_l1:.4f},{sub_lo1:.4f})-({sub_l2:.4f},{sub_lo2:.4f})")
        bbox = bbox_to_utm(sub_l1, sub_lo1, sub_l2, sub_lo2)
    else:
        bbox = bbox_to_utm(lat1, lon1, lat2, lon2)

    # 确保 OSM geojson 存在（缺则 fetch）
    bldg_path = _ensure_geojson("building", lat1, lon1, lat2, lon2, pbf_basename)
    road_path = _ensure_geojson("road",     lat1, lon1, lat2, lon2, pbf_basename)
    water_path_obj = _geojson_path("water", lat1, lon1, lat2, lon2)
    if not water_path_obj.exists():
        try:
            water_path_obj = _ensure_geojson("water", lat1, lon1, lat2, lon2, pbf_basename)
        except SystemExit:
            water_path_obj = None  # 没有水体也允许跑

    print(f"  Loading {bldg_path.name}...")
    bgdf = gpd.read_file(bldg_path)
    bgdf = bgdf[bgdf.geometry.type.isin(["Polygon", "MultiPolygon"])].copy()
    bgdf = project_geodataframe(bgdf, bbox["utm_crs"], bbox["origin"], clip_bbox=bbox["utm_bbox"])
    bgdf = bgdf.reset_index(drop=True)

    # 在投影后但拆 MultiPolygon 之前算 landmark 标签（保留 row 标签信息）
    print(f"    classifying landmarks...")
    row_landmarks = classify_landmarks_in_gdf(bgdf, top_percent=0.0, min_area_m2=0.0)
    n_lm = sum(row_landmarks)
    print(f"    {n_lm}/{len(bgdf)} rows tagged as landmark ({n_lm/max(len(bgdf),1)*100:.1f}%)")

    polys: list = []
    landmark_flags: list = []
    polys_to_row: list = []   # polys[i] 对应 bgdf 的原 row index（用于 hotspot 重分类）
    for i, geom in enumerate(bgdf.geometry):
        if geom is None or geom.is_empty: continue
        is_lm = row_landmarks[i]
        if isinstance(geom, MultiPolygon):
            for g in geom.geoms:
                if g.is_empty: continue
                polys.append(g); landmark_flags.append(is_lm)
                polys_to_row.append(i)
        elif isinstance(geom, Polygon):
            polys.append(geom); landmark_flags.append(is_lm)
            polys_to_row.append(i)
    print(f"    buildings (split into single polygons): {len(polys)}")

    # 缓存建筑的 OSM 标签子集（仅 is_tag_landmark 用得到的几列）→ 用于 hotspot 重分类
    LM_TAG_COLS = ['name', 'name:en', 'building',
                    'wikidata', 'wikipedia', 'historic', 'heritage',
                    'tourism', 'amenity', 'man_made', 'tower:type',
                    'religion', 'government', 'military', 'museum']
    cols_present = [c for c in LM_TAG_COLS if c in bgdf.columns]
    bgdf_tags = bgdf[cols_present].copy().reset_index(drop=True)

    print(f"  Loading {road_path.name}...")
    rgdf = gpd.read_file(road_path)
    rgdf = rgdf[rgdf.geometry.type.isin(["LineString", "MultiLineString"])].copy()
    rgdf = project_geodataframe(rgdf, bbox["utm_crs"], bbox["origin"], clip_bbox=bbox["utm_bbox"])
    print(f"    roads: {len(rgdf)}")

    water_gdf = None
    if water_path_obj is not None and water_path_obj.exists():
        print(f"  Loading {water_path_obj.name}...")
        wgdf = gpd.read_file(water_path_obj)
        wgdf = wgdf[wgdf.geometry.type.isin(
            ["Polygon", "MultiPolygon", "LineString", "MultiLineString"])].copy()
        wgdf = project_geodataframe(wgdf, bbox["utm_crs"], bbox["origin"], clip_bbox=bbox["utm_bbox"])
        water_gdf = wgdf
        print(f"    water: {len(water_gdf)}")
    else:
        print(f"  (no water geojson, skipping water)")

    # ---- 新增 sub-mesh: railway / pier / stadium ----
    def _try_load(tag: str, geom_types: list) -> "gpd.GeoDataFrame | None":
        path = _geojson_path(tag, lat1, lon1, lat2, lon2)
        if not path.exists():
            try:
                path = _ensure_geojson(tag, lat1, lon1, lat2, lon2, pbf_basename)
            except SystemExit:
                return None
        if not path.exists():
            return None
        print(f"  Loading {path.name}...")
        g = gpd.read_file(path)
        g = g[g.geometry.type.isin(geom_types)].copy()
        if len(g) == 0:
            print(f"    {tag}: 0 (no features)")
            return None
        g = project_geodataframe(g, bbox["utm_crs"], bbox["origin"],
                                  clip_bbox=bbox["utm_bbox"])
        print(f"    {tag}: {len(g)}")
        return g

    railway_gdf = _try_load("railway", ["LineString", "MultiLineString"])
    pier_gdf    = _try_load("pier",    ["Polygon", "MultiPolygon"])
    stadium_gdf = _try_load("stadium", ["Polygon", "MultiPolygon"])
    landuse_gdf = _try_load("landuse", ["Polygon", "MultiPolygon"])

    # 植被地标 = 普通植被 (forest/grass/wood) + 保护区 (national_park/nature_reserve/wetland)
    veg_landmark_polys: list = []
    for tag_type in ("vegetation", "protected_area"):
        path = _geojson_path(tag_type, lat1, lon1, lat2, lon2)
        if not path.exists():
            try:
                path = _ensure_geojson(tag_type, lat1, lon1, lat2, lon2, pbf_basename)
            except SystemExit:
                continue
        if not path.exists():
            continue
        print(f"  Loading {path.name} (for {tag_type} landmarks)...")
        vgdf = gpd.read_file(path)
        vgdf = vgdf[vgdf.geometry.type.isin(["Polygon", "MultiPolygon"])].copy()
        if len(vgdf) == 0:
            continue
        vgdf = project_geodataframe(vgdf, bbox["utm_crs"], bbox["origin"], clip_bbox=bbox["utm_bbox"])
        n_before = len(veg_landmark_polys)
        for idx, row in vgdf.iterrows():
            geom = row.geometry
            if geom is None or geom.is_empty: continue
            polys_iter = geom.geoms if isinstance(geom, MultiPolygon) else [geom]
            for g in polys_iter:
                if g.is_empty: continue
                if is_vegetation_landmark(row, area_m2=g.area):
                    veg_landmark_polys.append(g)
        print(f"    {tag_type} landmarks added: {len(veg_landmark_polys) - n_before}")
    print(f"  total vegetation landmarks: {len(veg_landmark_polys)}")

    # ========================================================================
    # 4 类地标 records（用于 PNG 标注：top N 按 priority 排序）
    # records 元素 = (geom, name, name_en, priority)
    # ========================================================================
    # 1) 建筑：从 bgdf 抽出有标签命中的（landmark_flags 是逐 row）
    print(f"  Extracting building landmark records...")
    building_landmark_records = _extract_landmarks_from_gdf(bgdf, is_tag_landmark)
    print(f"    {len(building_landmark_records)} building records")

    # 2) 植被：合并 vegetation + protected_area 两源的 records
    print(f"  Extracting vegetation landmark records...")
    veg_landmark_records: list = []
    for tag_type in ("vegetation", "protected_area"):
        path = _geojson_path(tag_type, lat1, lon1, lat2, lon2)
        if not path.exists(): continue
        vgdf2 = gpd.read_file(path)
        vgdf2 = vgdf2[vgdf2.geometry.type.isin(["Polygon", "MultiPolygon"])].copy()
        if len(vgdf2) == 0: continue
        vgdf2 = project_geodataframe(vgdf2, bbox["utm_crs"], bbox["origin"],
                                       clip_bbox=bbox["utm_bbox"])
        veg_landmark_records.extend(
            _extract_landmarks_from_gdf(vgdf2, is_vegetation_landmark))
    print(f"    {len(veg_landmark_records)} vegetation records")

    # 3) 水体：从 water_gdf 抽
    print(f"  Extracting water landmark records...")
    water_landmark_records: list = []
    if water_gdf is not None and len(water_gdf) > 0:
        water_landmark_records = _extract_landmarks_from_gdf(water_gdf, is_water_landmark)
    print(f"    {len(water_landmark_records)} water records")

    # 4) 道路桥梁：从 roads_gdf 抽
    print(f"  Extracting road landmark records (bridges)...")
    road_landmark_records: list = []
    if rgdf is not None and len(rgdf) > 0:
        road_landmark_records = _extract_landmarks_from_gdf(rgdf, is_road_landmark)
    print(f"    {len(road_landmark_records)} road bridge records")

    ctx = {
        "bbox_utm": bbox["utm_bbox"], "origin": bbox["origin"],
        "width_m": bbox["width_m"], "height_m": bbox["height_m"],
        "utm_crs": bbox["utm_crs"],
        "bbox_wgs84": (lat1, lon1, lat2, lon2),
    }
    cache.parent.mkdir(parents=True, exist_ok=True)
    with open(cache, "wb") as f:
        pickle.dump({
            "polys": polys, "landmark_flags": landmark_flags,
            "polys_to_row": polys_to_row, "bgdf_tags": bgdf_tags,
            "roads": rgdf, "water": water_gdf,
            "veg_landmarks": veg_landmark_polys,
            "building_landmark_records": building_landmark_records,
            "veg_landmark_records": veg_landmark_records,
            "water_landmark_records": water_landmark_records,
            "road_landmark_records": road_landmark_records,
            "railway": railway_gdf, "pier": pier_gdf, "stadium": stadium_gdf,
            "landuse": landuse_gdf,
            "ctx": ctx,
        }, f)
    return (polys, landmark_flags, rgdf, water_gdf, ctx, veg_landmark_polys,
            building_landmark_records, veg_landmark_records,
            water_landmark_records, road_landmark_records,
            polys_to_row, bgdf_tags,
            railway_gdf, pier_gdf, stadium_gdf, landuse_gdf)


# ---------------------------------------------------------------------------
# 核心：道路 → polygonize → city blocks
# ---------------------------------------------------------------------------


def build_city_blocks(roads_gdf: gpd.GeoDataFrame, ctx: dict, road_tier: int,
                      water_gdf: gpd.GeoDataFrame = None,
                     ) -> List[Polygon]:
    """用道路 LineString + (可选)水体边界 + bbox 边界 polygonize 出城市街区。

    water_gdf 提供时，把水体多边形的 boundary（外轮廓 + 内孔）和河流 LineString
    都加入 noding 的输入，让河流也参与街区切分（湖岸把环湖建筑切成单块、
    钱塘江把两岸切开）。
    """
    allowed = set(ROAD_TIERS[road_tier])

    # 过滤路 highway 等级
    if "highway" in roads_gdf.columns:
        rfilt = roads_gdf[roads_gdf["highway"].isin(allowed)].copy()
    else:
        rfilt = roads_gdf.copy()
    print(f"  road tier {road_tier}: {len(rfilt)}/{len(roads_gdf)} 条道路")

    # bbox 边界
    bx = ctx["bbox_utm"]; ox, oy = ctx["origin"]
    bbox_local = (bx[0] - ox, bx[1] - oy, bx[2] - ox, bx[3] - oy)
    bbox_lines = box(*bbox_local).boundary

    # 收集线段
    lines: list = [bbox_lines]
    for geom in rfilt.geometry:
        if geom is None or geom.is_empty: continue
        if isinstance(geom, MultiLineString):
            lines.extend(geom.geoms)
        elif isinstance(geom, LineString):
            lines.append(geom)
    n_road_lines = len(lines) - 1

    # 水体（可选）— 多边形 boundary + 河流 LineString
    n_water_lines = 0
    if water_gdf is not None and len(water_gdf) > 0:
        for geom in water_gdf.geometry:
            if geom is None or geom.is_empty: continue
            if isinstance(geom, Polygon):
                lines.append(geom.exterior)
                for hole in geom.interiors:
                    lines.append(hole)
                n_water_lines += 1 + len(list(geom.interiors))
            elif isinstance(geom, MultiPolygon):
                for g in geom.geoms:
                    if g.is_empty: continue
                    lines.append(g.exterior)
                    for hole in g.interiors:
                        lines.append(hole)
                    n_water_lines += 1 + len(list(g.interiors))
            elif isinstance(geom, MultiLineString):
                lines.extend(geom.geoms)
                n_water_lines += len(geom.geoms)
            elif isinstance(geom, LineString):
                lines.append(geom)
                n_water_lines += 1
        print(f"  + water: {n_water_lines} 条边界线")

    # noding（让所有线段在交叉点断开）
    print(f"  unary_union noding {len(lines)} segments "
          f"(roads={n_road_lines}, water={n_water_lines})...")
    t0 = time.time()
    noded = unary_union(lines)
    print(f"    {time.time()-t0:.1f}s")

    # polygonize
    t0 = time.time()
    blocks = list(polygonize(noded))
    print(f"  polygonize → {len(blocks)} city blocks in {time.time()-t0:.1f}s")
    return blocks


# ---------------------------------------------------------------------------
# Block 语义分类
# ---------------------------------------------------------------------------

LANDUSE_CLASS_MAP = {
    "residential": "residential",
    "commercial": "commercial", "retail": "commercial",
    "industrial": "industrial", "railway": "industrial", "construction": "industrial",
    "farmland": "farmland", "orchard": "farmland", "meadow": "farmland",
    "allotments": "farmland", "farmyard": "farmland",
    "forest": "forest",
}

BLOCK_STYLE = {
    "residential":    {"face": "#e8e2d4", "edge": "#b6a890", "rot": 10, "shrink": 0.04},
    "commercial":     {"face": "#f0e4d0", "edge": "#c4a878", "rot": 8,  "shrink": 0.03},
    "industrial":     {"face": "#ddddd8", "edge": "#a8a8a4", "rot": 3,  "shrink": 0.02},
    "farmland":       {"face": "#e8ecd4", "edge": "#a8b890", "rot": 15, "shrink": 0.06},
    "forest":         {"face": "#dce8d4", "edge": "#90b080", "rot": 20, "shrink": 0.05},
    "water_adjacent": {"face": "#dce4e8", "edge": "#90a8b0", "rot": 5,  "shrink": 0.03},
    "unclassified":   {"face": "#e8e6e2", "edge": "#b0aca4", "rot": 10, "shrink": 0.04},
}


def classify_blocks(city_blocks: List[Polygon],
                    landuse_gdf: "gpd.GeoDataFrame | None",
                    water_gdf: "gpd.GeoDataFrame | None",
                    building_polys: List[Polygon]) -> List[str]:
    """给每个 city block 打语义标签（7 类）。

    优先级: landuse majority → water_adjacent → has buildings → unclassified
    """
    n = len(city_blocks)
    classes = ["unclassified"] * n

    # --- landuse spatial join ---
    if landuse_gdf is not None and len(landuse_gdf) > 0:
        lu_polys = []
        lu_tags = []
        for idx, row in landuse_gdf.iterrows():
            geom = row.geometry
            tag = row.get("landuse", "")
            if geom is None or geom.is_empty or not tag:
                continue
            if isinstance(geom, MultiPolygon):
                for g in geom.geoms:
                    if not g.is_empty:
                        lu_polys.append(g)
                        lu_tags.append(tag)
            elif isinstance(geom, Polygon):
                lu_polys.append(geom)
                lu_tags.append(tag)

        if lu_polys:
            lu_tree = STRtree(lu_polys)
            for i, block in enumerate(city_blocks):
                if block.is_empty or block.area < 100:
                    continue
                candidates = lu_tree.query(block)
                if len(candidates) == 0:
                    continue
                best_area = 0.0
                best_tag = ""
                for ci in candidates:
                    try:
                        ix_area = block.intersection(lu_polys[ci]).area
                    except Exception:
                        continue
                    if ix_area > best_area:
                        best_area = ix_area
                        best_tag = lu_tags[ci]
                if best_area > block.area * 0.15:
                    cls = LANDUSE_CLASS_MAP.get(best_tag, "")
                    if cls:
                        classes[i] = cls

    # --- water_adjacent: blocks near water but without landuse ---
    if water_gdf is not None and len(water_gdf) > 0:
        water_polys = []
        for geom in water_gdf.geometry:
            if geom is None or geom.is_empty:
                continue
            if isinstance(geom, (Polygon, MultiPolygon)):
                water_polys.append(geom)
        if water_polys:
            water_union = unary_union(water_polys)
            water_buf = water_union.buffer(80)
            for i, block in enumerate(city_blocks):
                if classes[i] != "unclassified":
                    continue
                if block.is_empty:
                    continue
                if water_buf.intersects(block):
                    overlap = block.intersection(water_buf).area
                    if overlap > block.area * 0.3:
                        classes[i] = "water_adjacent"

    # --- fallback: has buildings → residential ---
    if building_polys:
        bldg_tree = STRtree(building_polys)
        for i, block in enumerate(city_blocks):
            if classes[i] != "unclassified":
                continue
            if block.is_empty or block.area < 100:
                continue
            hits = bldg_tree.query(block)
            if len(hits) >= 2:
                classes[i] = "residential"

    return classes


# ---------------------------------------------------------------------------
# 块内聚合
# ---------------------------------------------------------------------------

def _compactness(poly: Polygon) -> float:
    """Polsby–Popper 紧凑度: 4π·area / perimeter²
    圆 = 1.0, 正方形 ≈ 0.785, 正六边形 ≈ 0.907,
    长条 / 三角 sliver → 趋近 0。
    用来过滤狭长 / 三角形 block。
    """
    L = poly.length
    if L <= 0:
        return 0.0
    return 4.0 * np.pi * poly.area / (L * L)


def _subtract(polys: List[Polygon], minus_geom) -> List[Polygon]:
    """Use the pipeline's repaired, prepared subtraction implementation.

    The PNG renderer used to keep a second sequential copy here.  Dense city
    renders then rebuilt the same GEOS predicate topology for tens of thousands
    of blocks even though preprocessing had already fixed that exact pattern.
    Keeping one implementation also guarantees PNG and 3MF subtraction handle
    invalid OSM polygons identically.
    """
    from _TEXTURE_STYLE_OF_DEEPSEEK._layer_preprocess import (
        _subtract as subtract_layer_polygons,
    )

    return subtract_layer_polygons(polys, minus_geom)


def _extract_landmarks_from_gdf(gdf: gpd.GeoDataFrame, predicate) -> list:
    """对 gdf 每行应用 predicate(row, area_m2)，返回 [(geom, name, name_en, priority)]。
    geom = Polygon / MultiPolygon / LineString（取决于源 gdf 类型）。
    LineString 的 area=0，仍然能匹配只看标签的 predicate。
    """
    out = []
    for idx, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty: continue
        # 多 polygon 时只取最大块作为标注定位用
        repr_geom = geom
        a = 0.0
        if isinstance(geom, MultiPolygon):
            repr_geom = max(geom.geoms, key=lambda g: g.area)
            a = repr_geom.area
        elif isinstance(geom, Polygon):
            a = geom.area
        # LineString / MultiLineString 保留 a=0
        if predicate(row, area_m2=a):
            out.append((repr_geom,
                        row.get("name"), row.get("name:en"),
                        landmark_priority(row, area_m2=a)))
    return out


def _convex_quadrilateral(poly: Polygon) -> Polygon:
    """保证输出凸 + ≥ 4 顶点（审美：不要三角形 / 内凹）。
    convex_hull 兜底；若 hull 仍是三角形，升级到 min_rotated_rectangle。
    """
    if not isinstance(poly, Polygon) or poly.is_empty:
        return poly
    hull = poly.convex_hull
    if not isinstance(hull, Polygon) or hull.is_empty:
        return poly.minimum_rotated_rectangle
    # 顶点数（exterior.coords 含闭合点，所以 n - 1 是 unique）
    n_unique = len(hull.exterior.coords) - 1
    if n_unique < 4:
        return poly.minimum_rotated_rectangle
    return hull


def aggregate_in_blocks(small_polys: List[Polygon],
                        blocks: List[Polygon],
                        print_limit_m2: float,
                        simplify_m: float,
                        mode: str = "buffered_union",
                        bldg_buffer_m: float = 8.0,
                        density_threshold: float = 0.25,
                        concave_ratio: float = 0.6,
                        min_block_compactness: float = 0.0,
                        min_block_area_m2: float = 0.0,
                        max_block_area_m2: float = 0.0,
                        min_buildings_per_block: int = 0,
                        count_threshold: int = 3,
                        block_fill_convex: bool = True,
                        landmark_polys: List[Polygon] = None,
                        out_filled_ids: list = None,
                       ) -> List[Polygon]:
    """每 block 内的小楼聚合成街区 footprint。

    Mode（追求"饱满 / 无内凹 / 近矩形/六边形"形状）:
      'union' :          直接 unary_union(polys) ∩ block        — 散，有内凹
      'buffered_union':  unary_union(polys.buffer(B)) ∩ block   — 连片但保形状
      'density_fill':    block 密 → 整个 block 当 footprint     — 跟随路网形状
      'block_fill':      count + density 双阈值通过 → 整 block 当 footprint, 否则丢弃
                         （无 fallback，最贴近 reference 杭州/武汉/重庆 demo 的风格）
      'convex_hull':     unary_union(polys).convex_hull ∩ block — 楼簇凸包，绝无内凹
      'concave_hull':    concave_hull(union, ratio) ∩ block     — 0=紧贴 1=凸包
      'oriented_bbox':   min_rotated_rectangle ∩ block          — 强制最小外接矩形

    过滤:
      min_block_compactness — Polsby-Popper < 阈值则跳过（去三角 sliver）
                              正方形 ≈ 0.78, 正六边形 ≈ 0.91；建议 0.25~0.40
      min_block_area_m2     — block 太小直接跳过
      min_buildings_per_block — block 内少于此数则跳过（防水体被填）

    landmark_polys（仅 block_fill 触发用）:
      地标 footprint 列表，参与 count + density 计算（让带地标的 block 更容易触发 fill,
      给地标"城市文脉"包围），但**不进入输出几何**（地标作为独立 individuals 渲染）。
      主入口仍是"有小楼的 block"，纯地标 / 无小楼的 block 不进入循环。

    用 STRtree 加速 building → block 配对。
    """
    if not small_polys or not blocks:
        return []

    centroids = [p.centroid for p in small_polys]
    block_tree = STRtree(blocks)

    bldg_in_block: dict[int, list] = {}
    for bi, c in enumerate(centroids):
        for ci in block_tree.query(c):
            if blocks[ci].contains(c):
                bldg_in_block.setdefault(ci, []).append(bi)
                break

    # 地标 centroid → block 平行映射（仅用于 count/density 触发，不画几何）
    landmark_in_block: dict[int, list] = {}
    if landmark_polys:
        for li, c in enumerate([p.centroid for p in landmark_polys]):
            for ci in block_tree.query(c):
                if blocks[ci].contains(c):
                    landmark_in_block.setdefault(ci, []).append(li)
                    break

    print(f"    分配 {sum(len(v) for v in bldg_in_block.values())} 栋小楼到 "
          f"{len(bldg_in_block)} 个 block (共 {len(blocks)} 个), mode={mode}; "
          f"+ {sum(len(v) for v in landmark_in_block.values())} 地标贡献 count/density")

    n_skipped_compact = 0
    n_skipped_area_min = 0
    n_skipped_area_max = 0
    n_skipped_few = 0
    aggregated: list[Polygon] = []
    for ci, bi_list in bldg_in_block.items():
        block = blocks[ci]

        # block 建筑数量过滤（防水体/空地被填）
        if min_buildings_per_block > 0 and len(bi_list) < min_buildings_per_block:
            n_skipped_few += 1
            continue
        # block 面积过滤（去三角 sliver / 巨大 rural cell）
        if min_block_area_m2 > 0 and block.area < min_block_area_m2:
            n_skipped_area_min += 1
            continue
        if max_block_area_m2 > 0 and block.area > max_block_area_m2:
            n_skipped_area_max += 1
            continue
        if min_block_compactness > 0 and _compactness(block) < min_block_compactness:
            n_skipped_compact += 1
            continue

        block_polys = [small_polys[bi] for bi in bi_list]

        if mode == "block_fill":
            # reference 风格：count AND density 双阈值通过 → 整 block 当 footprint
            # 不达标 → 直接丢弃整个 block（不显示任何建筑），无 fallback
            # 地标参与 count/density 计算（让 block 更易被认定为"城市区域"），但不画几何
            lm_in_this = [landmark_polys[li] for li in landmark_in_block.get(ci, [])] \
                          if landmark_polys else []
            n_polys = len(block_polys) + len(lm_in_this)
            total_area = sum(p.area for p in block_polys) + sum(p.area for p in lm_in_this)
            density = total_area / max(block.area, 1.0)
            if n_polys >= count_threshold and density >= density_threshold:
                shape = _convex_quadrilateral(block) if block_fill_convex else block
                if out_filled_ids is not None:
                    out_filled_ids.append(ci)
            else:
                continue
        elif mode == "density_fill":
            total_area = sum(p.area for p in block_polys)
            if total_area / max(block.area, 1.0) >= density_threshold:
                shape = block
            else:
                # 不够密就用 buffered_union 兜底
                buffered = [p.buffer(bldg_buffer_m, join_style=2) for p in block_polys]
                shape = unary_union(buffered).intersection(block)
        elif mode == "buffered_union":
            buffered = [p.buffer(bldg_buffer_m, join_style=2) for p in block_polys]
            shape = unary_union(buffered).intersection(block)
        elif mode == "convex_hull":
            # 楼簇凸包：绝无内凹；用 block 裁剪保证不跨道路
            shape = unary_union(block_polys).convex_hull.intersection(block)
        elif mode == "concave_hull":
            # concave_hull(ratio): 0=尽可能紧贴, 1=convex_hull
            try:
                hull = _shapely_concave_hull(
                    unary_union(block_polys), ratio=concave_ratio, allow_holes=False
                )
                shape = hull.intersection(block)
            except Exception:
                # 退化几何（< 3 点）退化为 buffered_union
                buffered = [p.buffer(bldg_buffer_m, join_style=2) for p in block_polys]
                shape = unary_union(buffered).intersection(block)
        elif mode == "oriented_bbox":
            # 最小外接矩形：强制矩形形状
            shape = unary_union(block_polys).minimum_rotated_rectangle.intersection(block)
        else:  # 'union'
            shape = unary_union(block_polys).intersection(block)

        if shape.is_empty: continue
        if simplify_m > 0:
            shape = shape.simplify(simplify_m, preserve_topology=True)
        if shape.is_empty: continue

        if isinstance(shape, Polygon):
            polys_out = [shape]
        elif hasattr(shape, "geoms"):
            polys_out = [g for g in shape.geoms if isinstance(g, Polygon)]
        else:
            continue
        # block_fill: shape = block 本身，已被前置 count/density/compactness/area 过滤，
        #             不再叠加 print_limit 出口面积过滤（否则把小街区全杀掉）
        # 其它模式：聚合输出 polygon 还需 ≥ print_limit 才印得出，保留过滤
        if mode == "block_fill":
            for p in polys_out:
                if not p.is_empty:
                    aggregated.append(p)
        else:
            for p in polys_out:
                if not p.is_empty and p.area >= print_limit_m2:
                    aggregated.append(p)

    if n_skipped_compact or n_skipped_area_min or n_skipped_area_max or n_skipped_few:
        print(f"    跳过 block: n<{min_buildings_per_block}={n_skipped_few}, "
              f"compact<{min_block_compactness}={n_skipped_compact}, "
              f"area<{min_block_area_m2}={n_skipped_area_min}, "
              f"area>{max_block_area_m2}={n_skipped_area_max}")
    return aggregated


def veg_block_fill(non_lm_veg_polys: List[Polygon],
                    blocks: List[Polygon],
                    excluded_block_ids: set = None,
                    veg_count_thr: int = 2,
                    veg_density_thr: float = 0.10,
                    max_block_area_m2: float = 500000.0,
                    block_fill_convex: bool = True,
                   ) -> List[Polygon]:
    """对非地标植被做 block_fill 风格化，跳过已被建筑占的 block（建筑优先）。
    """
    if not non_lm_veg_polys or not blocks:
        return []
    excluded = excluded_block_ids or set()
    btree = STRtree(blocks)
    veg_in_block: dict[int, list] = {}
    for vi, c in enumerate([p.centroid for p in non_lm_veg_polys]):
        for ci in btree.query(c):
            if blocks[ci].contains(c):
                veg_in_block.setdefault(ci, []).append(vi)
                break
    out: list = []
    n_skipped_excluded = 0
    n_skipped_area = 0
    for ci, vi_list in veg_in_block.items():
        if ci in excluded:
            n_skipped_excluded += 1
            continue
        block = blocks[ci]
        if max_block_area_m2 > 0 and block.area > max_block_area_m2:
            n_skipped_area += 1
            continue
        veg_count = len(vi_list)
        veg_area = sum(non_lm_veg_polys[vi].area for vi in vi_list)
        density = veg_area / max(block.area, 1.0)
        if veg_count >= veg_count_thr and density >= veg_density_thr:
            out.append(_convex_quadrilateral(block) if block_fill_convex else block)
    if n_skipped_excluded or n_skipped_area:
        print(f"    veg_block_fill 跳过 {n_skipped_excluded} block (被建筑占), "
              f"{n_skipped_area} block (太大)")
    return out


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def _polys_to_collection(polys, **kw):
    # 带洞多边形必须走 Path（exterior+interiors 多环），否则 union 后
    # 出现的环状水体（细带+江面包住街区）的洞会被填实成蓝色大块。
    patches = []
    for p in polys:
        if not isinstance(p, Polygon) or p.is_empty:
            continue
        verts, codes = [], []
        for ring in [p.exterior, *p.interiors]:
            coords = list(ring.coords)
            verts.extend(coords)
            codes.append(MplPath.MOVETO)
            codes.extend([MplPath.LINETO] * (len(coords) - 2))
            codes.append(MplPath.CLOSEPOLY)
        patches.append(MplPathPatch(MplPath(verts, codes)))
    return PatchCollection(patches, **kw)


# ---------------------------------------------------------------------------
# block tessellation: jitter + low-poly shading（demo.jpg 风格化）
# ---------------------------------------------------------------------------

def _resample_ring(coords, segment_m):
    """沿 ring resample 每 segment_m 一个点，返回不闭合点列。"""
    line = LineString(coords)
    L = line.length
    if L < segment_m * 1.5:
        return [c for c in list(coords)[:-1]]
    n = max(int(round(L / segment_m)), 4)
    return [line.interpolate(i / n, normalized=True).coords[0]
            for i in range(n)]


def _jitter_polygon(poly: Polygon, segment_m: float, jitter_m: float,
                    seed: int) -> Polygon:
    """resample 边 + 法向 ±jitter_m 随机偏移，生成手绘感 polygon。"""
    if not isinstance(poly, Polygon) or poly.is_empty:
        return poly
    rng = np.random.default_rng(seed)
    pts = _resample_ring(list(poly.exterior.coords), segment_m)
    n = len(pts)
    if n < 4:
        return poly
    out = []
    for i in range(n):
        prev = pts[(i - 1) % n]
        nxt = pts[(i + 1) % n]
        dx = nxt[0] - prev[0]
        dy = nxt[1] - prev[1]
        L = np.hypot(dx, dy)
        if L == 0:
            out.append(pts[i])
            continue
        # 与切向垂直方向（外法向假设逆时针，符号无所谓只要随机即可）
        nx, ny = -dy / L, dx / L
        d = rng.uniform(-jitter_m, jitter_m)
        out.append((pts[i][0] + nx * d, pts[i][1] + ny * d))
    out.append(out[0])
    new_poly = Polygon(out)
    if not new_poly.is_valid:
        try:
            fixed = new_poly.buffer(0)
        except Exception:
            return poly
        if isinstance(fixed, MultiPolygon):
            fixed = max(fixed.geoms, key=lambda g: g.area)
        if not isinstance(fixed, Polygon) or fixed.is_empty:
            return poly
        new_poly = fixed
    return new_poly


def _lowpoly_triangles_in(poly: Polygon, max_aspect: float = 6.0) -> list:
    """对 polygon 做 Delaunay，过滤出位于 polygon 内的三角形。
    max_aspect: 长宽比上限（最长边 / 最短边），过滤 sliver 三角（防巨型 block 出放射线）。
    """
    from shapely.ops import triangulate as _shapely_triangulate
    if not isinstance(poly, Polygon) or poly.is_empty:
        return []
    try:
        tris = _shapely_triangulate(poly)
    except Exception:
        return []
    out = []
    for t in tris:
        if not isinstance(t, Polygon) or t.is_empty:
            continue
        if not poly.contains(t.centroid):
            continue
        if max_aspect and max_aspect > 0:
            xs, ys = t.exterior.xy
            edges = []
            for i in range(3):
                dx = xs[i + 1] - xs[i]
                dy = ys[i + 1] - ys[i]
                edges.append(np.hypot(dx, dy))
            emin = min(edges)
            emax = max(edges)
            if emin <= 0 or emax / emin > max_aspect:
                continue
        out.append(t)
    return out


def _draw_jittered_block_layer(ax, blocks, base_color: str,
                                segment_m: float = 12.0,
                                jitter_m: float = 2.5,
                                lowpoly: bool = True,
                                shadow_color: str = "#c8c2b4",
                                seed_base: int = 42,
                                max_area_m2: float = 200000.0):
    """画 jittered block + low-poly 阴影。替代原 step 1.5 的 _polys_to_collection。
    max_area_m2: 超大 block（如水未切干净留下的巨型 polygon）跳过 lowpoly，避免长瘦三角伪线。
    """
    from matplotlib.colors import to_rgba

    valid_blocks = [b for b in blocks
                    if isinstance(b, Polygon) and not b.is_empty
                    and b.area >= 1000.0]
    if not valid_blocks:
        return

    jittered: list = []
    tri_patches: list = []
    tri_face: list = []
    rng_alpha = np.random.default_rng(seed_base ^ 0xA5A5)
    shadow_rgb = to_rgba(shadow_color)[:3]
    for i, b in enumerate(valid_blocks):
        jp = _jitter_polygon(b, segment_m=segment_m,
                              jitter_m=jitter_m, seed=seed_base + i)
        if not isinstance(jp, Polygon) or jp.is_empty:
            continue
        jittered.append(jp)
        if not lowpoly:
            continue
        # 超大 block（水未切干净的产物）只画 base，不画 lowpoly
        if max_area_m2 > 0 and b.area > max_area_m2:
            continue
        # 关键：用原始 block 的简化版做 triangulate，避免 jittered 30+ 点 → 25 三角的密度爆炸。
        # 简化容差按 block 边长比例：sqrt(area) * 0.15。
        simp_tol = max(np.sqrt(b.area) * 0.15, 8.0)
        simp_block = b.simplify(simp_tol, preserve_topology=True)
        if not isinstance(simp_block, Polygon) or simp_block.is_empty:
            continue
        # 顶点 ≥ 12 仍跳过 lowpoly（碎形 block 不要 facet）
        if len(simp_block.exterior.coords) - 1 > 12:
            continue
        for t in _lowpoly_triangles_in(simp_block, max_aspect=3.5):
            a = float(rng_alpha.uniform(0.0, 0.10))
            tri_patches.append(MplPoly(np.array(t.exterior.coords)))
            tri_face.append((shadow_rgb[0], shadow_rgb[1], shadow_rgb[2], a))

    # Layer A: 平铺 base color（jitter 过的 block 多边形）
    ax.add_collection(_polys_to_collection(
        jittered, facecolor=base_color, edgecolor="#b6b1a3",
        linewidths=0.4, alpha=1.0))

    # Layer B: low-poly shadow（同色稍深 + 随机 alpha 0..0.28）
    if tri_patches:
        coll = PatchCollection(tri_patches, match_original=False,
                                edgecolor="none", linewidths=0)
        coll.set_facecolor(tri_face)
        ax.add_collection(coll)


# 打印精度物理常量（与 config.py 一致）
NOZZLE_DIAM_MM = 0.4
INTERNAL_SPAN_MM = 196.0


def _compute_nozzle_m(ctx) -> float:
    """0.4 mm 喷嘴对应实地多少米。"""
    span_m = max(ctx.get("width_m", 25000), ctx.get("height_m", 25000))
    scale = INTERNAL_SPAN_MM / span_m       # mm/m
    return NOZZLE_DIAM_MM / scale            # m


def _draw_print_grid(ax, ctx, nozzle_real_m, every_n_nozzles=5):
    """A: 浅灰网格遮罩（每 N nozzles 一条线，z=30 永远在最上）
    B: 右下角 1 nozzle 参考方块（axes 坐标，z=31）
    """
    bx = ctx["bbox_utm"]; ox, oy = ctx["origin"]
    x_min, y_min = bx[0]-ox, bx[1]-oy
    x_max, y_max = bx[2]-ox, bx[3]-oy
    spacing = nozzle_real_m * every_n_nozzles
    # A: 网格线（lw=0.3 可见，alpha=0.40 明显但不抢主图）
    for x in np.arange(x_min, x_max + 1, spacing):
        ax.axvline(x, color='#222', lw=0.3, alpha=0.35, zorder=30)
    for y in np.arange(y_min, y_max + 1, spacing):
        ax.axhline(y, color='#222', lw=0.3, alpha=0.35, zorder=30)
    # B: 右下角参考方块（axes 坐标，红色 15×15 pt 方块 + 标签）
    from matplotlib.patches import FancyBboxPatch
    box_w = 0.018  # axes fraction ≈ 15pt
    box_x = 0.965 - box_w
    box_y = 0.018
    rect = FancyBboxPatch((box_x, box_y), box_w, box_w,
                           boxstyle="square,pad=0",
                           facecolor='#ff3333', edgecolor='#000', lw=1.0,
                           alpha=0.95, zorder=31, transform=ax.transAxes,
                           clip_on=False)
    ax.add_patch(rect)
    label = f'1 nozzle ≈ {nozzle_real_m:.0f}m\n(0.4 mm × 0.4 mm)'
    ax.text(box_x + box_w / 2, box_y - 0.018, label,
            fontsize=9, ha='center', va='top',
            family='monospace', color='#222', zorder=31,
            transform=ax.transAxes, clip_on=False,
            bbox=dict(boxstyle='round,pad=0.3', fc='white',
                       ec='#444', lw=0.5, alpha=0.95))



def _draw_color_legend(ax):
    """右上角颜色图例（axes 坐标）。"""
    legend_items = [
        ("#a8c8e8", "建筑街区 (BO)"),
        ("#e85a2c", "大体量地标 (Size)"),
        ("#22aa55", "标签地标 (Tag)"),
        ("#2d6e3a", "植被地标 (VL)"),
        ("#bdd5a3", "普通植被 (VO)"),
        ("#0e74a8", "水体地标 (WL)"),
        ("#a8c8e0", "小水体 (WO)"),
        ("#dadada", "原始 OSM 建筑"),
        # block base 语义分类
        (BLOCK_STYLE["residential"]["face"],    "住宅区"),
        (BLOCK_STYLE["commercial"]["face"],     "商业区"),
        (BLOCK_STYLE["industrial"]["face"],     "工业区"),
        (BLOCK_STYLE["farmland"]["face"],       "农田"),
        (BLOCK_STYLE["forest"]["face"],         "林地"),
        (BLOCK_STYLE["water_adjacent"]["face"], "近水区"),
        (BLOCK_STYLE["unclassified"]["face"],   "未分类"),
    ]

    box_x = 0.968
    box_y = 0.968
    line_h = 0.024
    swatch_w = 0.018
    gap = 0.004

    for i, (color, label) in enumerate(legend_items):
        y = box_y - (i + 1) * line_h
        ax.add_patch(plt.Rectangle(
            (box_x - swatch_w - gap, y - line_h * 0.35),
            swatch_w, line_h * 0.7,
            facecolor=color, edgecolor='#333', lw=0.4,
            alpha=1.0, zorder=32, transform=ax.transAxes, clip_on=False))
        ax.text(box_x, y, label,
                fontsize=8, ha='right', va='center',
                family='sans-serif', color='#222', zorder=32,
                transform=ax.transAxes, clip_on=False)


# 4 类地标的标注颜色（深色，黑边白底气泡）
_LM_LABEL_COLOR = {
    "building": "#0a3a18",   # 深绿
    "vegetation": "#0a3a18", # 同建筑（地标系都是深绿色）
    "water": "#0e3a4e",      # 深湖蓝
    "road": "#3a3a3a",       # 深灰（桥）
}


def _render_annotations(ax, fig, records_by_type: dict, top_n: int,
                          ctx: dict, city_title_text: str = None):
    """在 PNG 上画 4 类地标标签 + 引线。

    records_by_type: {"building": [(geom, name, name_en, pri)], "vegetation": ...}
    布局：每类按 priority 取 top_n，按 centroid x 分左右两组，y 排序后均布在 margin。
    """
    bx = ctx["bbox_utm"]; ox, oy = ctx["origin"]
    x_min, y_min = bx[0]-ox, bx[1]-oy
    x_max, y_max = bx[2]-ox, bx[3]-oy
    w = x_max - x_min; h = y_max - y_min

    # 收集要标的 records
    selected: list = []  # (kind, geom, name, name_en, priority, cx, cy)
    for kind, recs in records_by_type.items():
        if not recs: continue
        # 排序去重 (按 name)
        seen_names = set()
        recs_sorted = sorted(recs, key=lambda r: -r[3])  # by priority desc
        for geom, name, name_en, pri in recs_sorted:
            if not name: continue
            if isinstance(name, float) and np.isnan(name): continue  # 过滤 NaN
            if name in seen_names: continue
            seen_names.add(name)
            try:
                cx, cy = geom.centroid.x, geom.centroid.y
            except Exception:
                continue
            if not (x_min <= cx <= x_max and y_min <= cy <= y_max):
                continue
            selected.append((kind, geom, name, name_en, pri, cx, cy))
            if sum(1 for s in selected if s[0] == kind) >= top_n:
                break

    if not selected:
        return

    # Inline 模式：label 贴在 centroid 旁，不画 leader line。
    # 偏移让 label 不直接盖住 centroid 标记；fontsize 调小避免拥挤。
    offset = max(w, h) * 0.012  # 约 1.2% bbox 边长
    for kind, geom, name, name_en, pri, cx, cy in selected:
        color = _LM_LABEL_COLOR.get(kind, "#222")
        label = f"{name}"
        if name_en and isinstance(name_en, str) and name_en.strip():
            label = f"{name}\n{name_en}"
        ax.text(
            cx + offset, cy + offset, label,
            fontsize=8, ha='left', va='bottom',
            color=color, family='sans-serif',
            bbox=dict(boxstyle='round,pad=0.25', fc='#ffffff',
                      ec=color, lw=0.5, alpha=0.88),
            zorder=20, clip_on=True,
        )

    # 底部居中城市标题
    if city_title_text:
        ax.text(0.5, -0.03, city_title_text,
                transform=ax.transAxes, fontsize=28, ha='center', va='top',
                family='monospace', weight='bold', color='#222',
                zorder=21)


def render(out_path: Path, raw_polys, individuals, blocks_aggregated,
           city_blocks_outline, ctx, title, params, stats,
           fig_inches: float = 18.0, dpi: int = 220,
           tag_landmarks=None, size_landmarks=None,
           veg_landmarks=None,
           # ---- 新参数（v2 完整版） ----
           water_landmark_polys=None,    # WL 几何
           small_water_polys=None,        # WO 几何（已 minus WL）
           veg_fill_polys=None,           # VO block_fill 几何（已被建筑跳过）
           records_by_type: dict = None,  # 4 类标注 records
           annotate: bool = False,
           annotate_top: int = 6,
           city_title: str = None,
           block_tessellation: bool = True,
           show_print_grid: bool = False,
           draw_bo_fill: bool = True,
           block_jitter: bool = False,
           railway_gdf: "gpd.GeoDataFrame | None" = None,
           pier_gdf: "gpd.GeoDataFrame | None" = None,
           stadium_gdf: "gpd.GeoDataFrame | None" = None,
           block_classes: List[str] = None,
           ):
    """高分辨率渲染（v2 完整版）。

    Foreground / Background z-order（含 geometry 减法预处理）：
      1. terrain (灰底，由 raw_polys 充当)
      2. veg block_fill (浅橄榄半透)
      3. small water (浅蓝半透)
      4. road grid lines (灰色细线)
      5. building block_fill (浅蓝)
      6. veg landmarks (深森林绿半透 — 已减 BL)
      7. water landmarks (深湖蓝实色)
      8. building landmarks size (橙)
      9. building landmarks tag (绿)
      10. annotations + 引线
      11. city title
    """
    from tools.brick_render import render_layer as brick_render_layer

    # 5 步 geometry 减法（让低层不画在高层之上）
    bl_polys = (tag_landmarks or []) + (size_landmarks or [])
    vl_polys = veg_landmarks or []
    wl_polys = water_landmark_polys or []
    all_lm = bl_polys + vl_polys + wl_polys
    all_lm_geom = unary_union(all_lm) if all_lm else None
    bo_filled_geom = unary_union(blocks_aggregated) if blocks_aggregated else None
    bl_geom = unary_union(bl_polys) if bl_polys else None
    wl_geom = unary_union(wl_polys) if wl_polys else None

    blocks_aggregated_clean = _subtract(blocks_aggregated or [], all_lm_geom)
    veg_fill_clean = _subtract(veg_fill_polys or [],
                                 unary_union([g for g in [all_lm_geom, bo_filled_geom]
                                                if g is not None and not g.is_empty]))
    small_water_clean = _subtract(small_water_polys or [], wl_geom)
    # veg 减建筑 + 水：避免 leisure=park 圈住的湖面（如西湖、Lincoln Park 沿湖）以半透绿叠在水蓝上
    veg_landmarks_clean = _subtract(
        vl_polys,
        unary_union([g for g in [bl_geom, wl_geom] if g is not None and not g.is_empty]))

    # ---- 画图 ----
    if annotate:
        # 留出左右 margin 给标注（额外 4 inch）
        fig, ax = plt.subplots(figsize=(fig_inches + 5, fig_inches), dpi=dpi)
    else:
        fig, ax = plt.subplots(figsize=(fig_inches, fig_inches), dpi=dpi)

    # 1.5. block tessellation —— brick 风格（圆角+Perlin+灰缝+微旋）
    #      按 block_classes 语义分类着不同色调
    if block_tessellation and city_blocks_outline:
        valid_mask = [(isinstance(b, Polygon) and not b.is_empty and b.area >= 1000.0)
                      for b in city_blocks_outline]
        valid_blocks = [b for b, m in zip(city_blocks_outline, valid_mask) if m]
        valid_classes = ([c for c, m in zip(block_classes, valid_mask) if m]
                        if block_classes and len(block_classes) == len(city_blocks_outline)
                        else ["unclassified"] * len(valid_blocks))

        if block_jitter and valid_blocks:
            for cls, style in BLOCK_STYLE.items():
                cls_polys = [b for b, c in zip(valid_blocks, valid_classes) if c == cls]
                if not cls_polys:
                    continue
                brick_render_layer(
                    ax, cls_polys,
                    face_color=style["face"], edge_color=style["edge"], edge_lw=0.35,
                    brick_density=0.0, seed_perlin_amp=10.0,
                    corner_r_m=8.0, rot_deg=style["rot"], shift_m=8.0,
                    perlin_amp=4.0, perlin_freq=0.15,
                    resample_m=12.0, shrink_ratio=style["shrink"],
                    noise_seed=2026, label=f"block_{cls}")
        elif valid_blocks:
            for cls, style in BLOCK_STYLE.items():
                cls_polys = [b for b, c in zip(valid_blocks, valid_classes) if c == cls]
                if not cls_polys:
                    continue
                ax.add_collection(_polys_to_collection(
                    cls_polys, facecolor=style["face"], edgecolor="none", alpha=0.85))

    # 2. veg block_fill (background, 浅橄榄半透)
    if veg_fill_clean:
        ax.add_collection(_polys_to_collection(
            veg_fill_clean, facecolor="#bdd5a3", edgecolor="#7a9460",
            linewidths=0.25, alpha=0.55))

    # 3. small water (background, 浅蓝半透)
    if small_water_clean:
        ax.add_collection(_polys_to_collection(
            small_water_clean, facecolor="#a8c8e0", edgecolor="none", alpha=0.65))

    # 4. block 边界（街道线 grid）
    if city_blocks_outline:
        for cb in city_blocks_outline:
            xy = np.array(cb.exterior.coords)
            ax.plot(xy[:, 0], xy[:, 1], color="#aaaaaa", linewidth=0.15)

    # 4.5 railway —— 钢灰色细线，按 way 上 #6E6E72 (subway/light_rail/tram 同色)
    # Z-jitter 在 PNG 用 alpha + linewidth 微随机来等价表达"高低分化"
    if railway_gdf is not None and len(railway_gdf) > 0:
        rng_rail = np.random.default_rng(2026)
        for idx, row in railway_gdf.iterrows():
            geom = row.geometry
            if geom is None or geom.is_empty:
                continue
            lines = geom.geoms if hasattr(geom, "geoms") and \
                geom.geom_type == "MultiLineString" else [geom]
            jitter = rng_rail.uniform(0.85, 1.15)  # ±15% lw
            lw = 0.55 * jitter
            for ln in lines:
                if ln.is_empty:
                    continue
                xy = np.array(ln.coords)
                ax.plot(xy[:, 0], xy[:, 1], color="#6E6E72",
                        linewidth=lw, alpha=0.85, zorder=4)

    # 5. building block_fill (浅蓝；clean = 减去地标后)
    if draw_bo_fill and blocks_aggregated_clean:
        ax.add_collection(_polys_to_collection(
            blocks_aggregated_clean, facecolor="#a8c8e8", edgecolor="#1a1a1a",
            linewidths=0.25, alpha=0.85))

    # 6. veg landmarks (深森林绿半透)
    if veg_landmarks_clean:
        ax.add_collection(_polys_to_collection(
            veg_landmarks_clean, facecolor="#2d6e3a", edgecolor="#0a3a18",
            linewidths=0.6, alpha=0.55))

    # 7. water landmarks (深湖蓝实色)
    # 用 union 后的几何画：细缓冲带与宽江面同属 WL，union 吸收内部
    # 边界；若按原始列表画，细带的深色描边会在江面中间留下“细线”。
    if wl_geom is not None and not wl_geom.is_empty:
        wl_draw = (list(wl_geom.geoms)
                   if wl_geom.geom_type == "MultiPolygon" else [wl_geom])
        ax.add_collection(_polys_to_collection(
            wl_draw, facecolor="#0e74a8", edgecolor="#072e44",
            linewidths=0.6, alpha=0.95))

    # 7.5 pier / breakwater —— 浅米陆地伸入水里，皮+芯：底芯 #d4c8a8，顶皮 #b8a884
    if pier_gdf is not None and len(pier_gdf) > 0:
        pier_polys = []
        for geom in pier_gdf.geometry:
            if geom is None or geom.is_empty:
                continue
            if isinstance(geom, MultiPolygon):
                pier_polys.extend(g for g in geom.geoms if not g.is_empty)
            elif isinstance(geom, Polygon):
                pier_polys.append(geom)
        if pier_polys:
            ax.add_collection(_polys_to_collection(
                pier_polys, facecolor="#d4c8a8", edgecolor="#7a6f50",
                linewidths=0.4, alpha=0.95))

    # 兼容旧调用
    if tag_landmarks is None and size_landmarks is None:
        tag_landmarks = individuals or []
        size_landmarks = []

    def _scatter_centroids(polys, color_face, color_edge):
        cx = [p.centroid.x for p in polys if isinstance(p, Polygon) and not p.is_empty]
        cy = [p.centroid.y for p in polys if isinstance(p, Polygon) and not p.is_empty]
        if cx:
            ax.scatter(cx, cy, s=4, facecolor=color_face, edgecolor=color_edge,
                       linewidths=0.3, alpha=0.95, zorder=5)

    # 8. 大体量地标 (橙红 brick)
    if size_landmarks:
        if block_jitter:
            brick_render_layer(
                ax, size_landmarks,
                face_color="#e85a2c", edge_color="#7a2810", edge_lw=0.35,
                brick_density=0.0, seed_perlin_amp=10.0,
                corner_r_m=8.0, rot_deg=10.0, shift_m=8.0,
                perlin_amp=4.0, perlin_freq=0.15,
                resample_m=12.0, shrink_ratio=0.04,
                noise_seed=2026 + 7777, label="size_landmark")
        else:
            ax.add_collection(_polys_to_collection(
                size_landmarks, facecolor="#e85a2c", edgecolor="#3a1208",
                linewidths=0.5, alpha=0.95))
        _scatter_centroids(size_landmarks, "#e85a2c", "#3a1208")

    # 9. 标签地标 (翠绿 brick)
    if tag_landmarks:
        if block_jitter:
            brick_render_layer(
                ax, tag_landmarks,
                face_color="#22aa55", edge_color="#0a3a18", edge_lw=0.35,
                brick_density=0.0, seed_perlin_amp=10.0,
                corner_r_m=8.0, rot_deg=10.0, shift_m=8.0,
                perlin_amp=4.0, perlin_freq=0.15,
                resample_m=12.0, shrink_ratio=0.04,
                noise_seed=2026 + 8888, label="tag_landmark")
        else:
            ax.add_collection(_polys_to_collection(
                tag_landmarks, facecolor="#22aa55", edgecolor="#0a3a18",
                linewidths=0.5, alpha=0.95))
        _scatter_centroids(tag_landmarks, "#22aa55", "#0a3a18")

    # 9.3 stadium / sports_centre / marina —— 砖红 civic landmark
    # 放在 BL 之后让它 dominate 体育场区域（Soldier Field 这种"建筑+停车场+草坪"
    # 整片当一块红色色块呈现，符合"事件型地标"语义）
    if stadium_gdf is not None and len(stadium_gdf) > 0:
        stadium_polys = []
        for geom in stadium_gdf.geometry:
            if geom is None or geom.is_empty:
                continue
            if isinstance(geom, MultiPolygon):
                stadium_polys.extend(g for g in geom.geoms if not g.is_empty)
            elif isinstance(geom, Polygon):
                stadium_polys.append(geom)
        if stadium_polys:
            ax.add_collection(_polys_to_collection(
                stadium_polys, facecolor="#A85B3C", edgecolor="#3a1208",
                linewidths=0.5, alpha=0.88))

    bx = ctx["bbox_utm"]; ox, oy = ctx["origin"]
    ax.set_xlim(bx[0]-ox, bx[2]-ox); ax.set_ylim(bx[1]-oy, bx[3]-oy)
    ax.set_aspect("equal"); ax.set_xticks([]); ax.set_yticks([])

    # 9.5 打印精度网格 + 角标尺（在标注之前，让 label 盖住网格）
    if show_print_grid:
        nozzle_m = _compute_nozzle_m(ctx)
        _draw_print_grid(ax, ctx, nozzle_m, every_n_nozzles=5)

    # 10/11. annotations + city title
    if annotate and records_by_type:
        _render_annotations(ax, fig, records_by_type, annotate_top, ctx, city_title)

    # 11.5 右上角颜色图例
    _draw_color_legend(ax)

    info = " ".join(f"{k}={v}" for k, v in params.items())
    stat = " ".join(f"{k}={v}" for k, v in stats.items())
    ax.set_title(f"{title}\n{info}\n{stat}", fontsize=14, family='monospace')
    fig.tight_layout(pad=0.5)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight",
                facecolor="white", pad_inches=0.2)
    plt.close(fig)


# ---------------------------------------------------------------------------
# 一组参数的完整流程
# ---------------------------------------------------------------------------

def run_one(polys, roads_gdf, ctx, *, print_limit, road_tier, simplify_m,
            mode="buffered_union", bldg_buffer=8.0, density_thr=0.25,
            individual_simplify=25.0, sub_label="", out_dir: Path = OUT_DIR,
            blocks_cache: dict = None, fig_inches: float = 18.0,
            dpi: int = 220, water_gdf: gpd.GeoDataFrame = None,
            use_water: bool = False,
            individual_shape: str = "raw",
            concave_ratio: float = 0.6,
            min_block_compactness: float = 0.0,
            min_block_area_m2: float = 0.0,
            max_block_area_m2: float = 0.0,
            min_buildings_per_block: int = 0,
            count_threshold: int = 3,
            landmark_flags: List[bool] = None,
            use_landmarks: bool = False,
            landmark_top_percent: float = 0.0,
            block_fill_convex: bool = True,
            veg_landmark_polys: list = None,
            # ---- v2 完整版新增 ----
            water_gdf_full: gpd.GeoDataFrame = None,
            water_landmark_records: list = None,
            building_landmark_records: list = None,
            veg_landmark_records: list = None,
            road_landmark_records: list = None,
            with_water_landmarks: bool = False,
            with_small_water: bool = False,
            with_veg_fill: bool = False,
            annotate: bool = False,
            annotate_top: int = 6,
            city_key: str = "",
            polys_to_row: list = None,
            bgdf_tags=None,
            hotspot_relax: float = 0.0,
            block_tessellation: bool = True,
            show_print_grid: bool = False,
            draw_bo_fill: bool = True,
            block_jitter: bool = False,
            railway_gdf: gpd.GeoDataFrame = None,
            pier_gdf: gpd.GeoDataFrame = None,
            stadium_gdf: gpd.GeoDataFrame = None,
            draw_railway: bool = True,
            draw_pier: bool = True,
            draw_stadium: bool = True,
            landuse_gdf: "gpd.GeoDataFrame | None" = None):
    t0 = time.time()

    # 计算 percentile 兜底阈值（兜底地标）
    if use_landmarks and landmark_top_percent > 0:
        all_areas = [p.area for p in polys if p.area >= 1.0]
        top_thr = compute_top_percent_threshold(all_areas, landmark_top_percent)
    else:
        top_thr = float("inf")

    # Step 0: 切 city blocks（提前到 Step 1 前面，因为 hotspot 检测需要 blocks）
    cache_key = (road_tier, bool(use_water))
    if blocks_cache is not None and cache_key in blocks_cache:
        city_blocks = blocks_cache[cache_key]
    else:
        wgdf = water_gdf if use_water else None
        city_blocks = build_city_blocks(roads_gdf, ctx, road_tier, water_gdf=wgdf)
        if blocks_cache is not None:
            blocks_cache[cache_key] = city_blocks

    # Step 0.5: 热点 block 检测 + 重分类建筑地标
    effective_lm_flags = list(landmark_flags) if landmark_flags else [False] * len(polys)
    if (hotspot_relax > 0 and polys_to_row is not None
            and bgdf_tags is not None and use_landmarks):
        hotspot_ids = compute_hotspot_block_ids(
            city_blocks, polys, top_percent=hotspot_relax)
        print(f"  hotspot blocks: {len(hotspot_ids)} / {len(city_blocks)} "
              f"(top {hotspot_relax}%)")
        btree_blocks = STRtree(city_blocks)
        n_added = 0
        for i, p in enumerate(polys):
            if effective_lm_flags[i]: continue
            if p.area < 1.0: continue
            c = p.centroid
            in_hotspot = False
            for ci in btree_blocks.query(c):
                if city_blocks[ci].contains(c):
                    in_hotspot = ci in hotspot_ids
                    break
            if not in_hotspot: continue
            row_idx = polys_to_row[i]
            if 0 <= row_idx < len(bgdf_tags):
                row = bgdf_tags.iloc[row_idx]
                if is_tag_landmark(row, area_m2=p.area, hotspot=True):
                    effective_lm_flags[i] = True
                    n_added += 1
        print(f"  hotspot relax: +{n_added} extra building landmarks")

    # Step 1: 三类分流：
    #   tag_landmarks  — OSM 标签命中（绿色，文化/类型/命名地标）
    #   size_landmarks — 仅面积达标 (≥ print_limit 或 top X%)，OSM 无地标标签（橙色，纯大体量）
    #   smalls         — 走 block_fill（蓝色街区填充）
    tag_landmarks: list = []
    size_landmarks: list = []
    smalls = []
    for i, p in enumerate(polys):
        if p.area < 1.0: continue
        s = p.simplify(individual_simplify, preserve_topology=True)
        if s.is_empty or s.area < 1.0: continue
        if isinstance(s, MultiPolygon):
            s = max(s.geoms, key=lambda g: g.area)
        if not isinstance(s, Polygon): continue

        is_tag_lm = (use_landmarks and effective_lm_flags is not None
                     and i < len(effective_lm_flags) and effective_lm_flags[i])
        is_size_lm = s.area >= print_limit or s.area >= top_thr

        if is_tag_lm or is_size_lm:
            if individual_shape == "convex":
                s = s.convex_hull
            elif individual_shape == "bbox":
                s = s.minimum_rotated_rectangle
            if isinstance(s, Polygon) and not s.is_empty:
                if is_tag_lm:
                    tag_landmarks.append(s)
                else:
                    size_landmarks.append(s)
        else:
            smalls.append(s)
    individuals = tag_landmarks + size_landmarks   # 兼容下游统计用

    # (city_blocks 已在 Step 0 构建)

    # Step 3: 块内 union
    bo_filled_ids: list = []
    aggregated = aggregate_in_blocks(
        smalls, city_blocks, print_limit, simplify_m,
        mode=mode, bldg_buffer_m=bldg_buffer, density_threshold=density_thr,
        concave_ratio=concave_ratio,
        min_block_compactness=min_block_compactness,
        min_block_area_m2=min_block_area_m2,
        max_block_area_m2=max_block_area_m2,
        min_buildings_per_block=min_buildings_per_block,
        count_threshold=count_threshold,
        block_fill_convex=block_fill_convex,
        landmark_polys=tag_landmarks + size_landmarks,
        out_filled_ids=bo_filled_ids,
    )

    # Step 3b: 水体地标 / 细碎水体（v2 完整版）
    # 命名河流 (waterway=river/canal LineString) → 高德补全 + 自适应 buffer
    WATERWAY_HALF_WIDTH = {
        "river": 90,
        "riverbank": 200,
        "canal": 25,
        "stream": 10,
        "drain": 6,
        "ditch": 4,
    }
    water_landmark_polys: list = []
    small_water_polys: list = []
    wl_lines_raw: list = []
    if (with_water_landmarks or with_small_water) and water_gdf_full is not None:
        for idx, row in water_gdf_full.iterrows():
            geom = row.geometry
            if geom is None or geom.is_empty: continue

            # ---- Polygon 类水体（湖泊）
            if isinstance(geom, (Polygon, MultiPolygon)):
                polys_iter = geom.geoms if isinstance(geom, MultiPolygon) else [geom]
                for g in polys_iter:
                    if g.is_empty: continue
                    a = g.area
                    if is_water_landmark(row, area_m2=a):
                        if with_water_landmarks: water_landmark_polys.append(g)
                    elif with_small_water and a >= 1000.0:
                        small_water_polys.append(g)

            # ---- LineString 类水体 → 收集 raw lines 供补全
            elif isinstance(geom, (LineString, MultiLineString)):
                if not with_water_landmarks: continue
                if not is_water_landmark(row, area_m2=0.0): continue
                wway = row.get("waterway", "river")
                lines_iter = geom.geoms if isinstance(geom, MultiLineString) else [geom]
                for L in lines_iter:
                    if L.is_empty or L.length < 10.0: continue
                    wl_lines_raw.append((L, wway))

        # 水体补全：高德 + 自适应 buffer（替代固定宽度）
        if wl_lines_raw and with_water_landmarks:
            bbox_wgs84 = ctx.get("bbox_wgs84")
            utm_crs = ctx.get("utm_crs")
            origin_xy = ctx.get("origin")
            try:
                from _TEXTURE_STYLE_OF_DEEPSEEK._water_supplement import supplement_wl_coverage
                water_landmark_polys = supplement_wl_coverage(
                    water_landmark_polys, wl_lines_raw, bbox_wgs84,
                    utm_crs=utm_crs, origin=origin_xy,
                )
            except Exception as e:
                print(f"  [water_supplement] failed, falling back to fixed buffer: {e}")
                for L, wway in wl_lines_raw:
                    half_w = WATERWAY_HALF_WIDTH.get(wway, 30)
                    buffered = L.buffer(half_w, cap_style=2)
                    if isinstance(buffered, Polygon) and not buffered.is_empty:
                        water_landmark_polys.append(buffered)
                    elif hasattr(buffered, "geoms"):
                        for g in buffered.geoms:
                            if isinstance(g, Polygon) and not g.is_empty:
                                water_landmark_polys.append(g)

    # Step 3c: 植被 block_fill (VO) — 待加载全 vegetation polys 后再启用
    # 当前版本仅画 landmark，不做 VO 兜底（避免视觉过载；--with-veg-fill 暂为 no-op）
    veg_fill_polys: list = []

    elapsed = time.time() - t0

    median_indv = float(np.median([p.area for p in individuals])) if individuals else 0
    median_agg = float(np.median([p.area for p in aggregated])) if aggregated else 0

    stats = {
        "tag": len(tag_landmarks),
        "size": len(size_landmarks),
        "blocks": len(aggregated),
        "total": len(individuals) + len(aggregated),
        "med_indv": f"{median_indv:.0f}m²",
        "med_block": f"{median_agg:.0f}m²",
        "T": f"{elapsed:.1f}s",
    }
    params = {
        "PRINT": int(print_limit),
        "TIER": road_tier,
        "SIMP": int(simplify_m),
    }

    # 文件名 tag：city 在前，区域大小在后
    if "[" in sub_label and "]" in sub_label:
        city_tag = "_" + sub_label.split("[")[1].split("]")[0]
    else:
        city_tag = ""
    size_tag = "_sub" if sub_label.startswith("5km") else "_25km"
    sub_tag = city_tag + size_tag
    w_tag = "_W" if use_water else ""
    cp_tag = f"_C{int(min_block_compactness*100)}" if min_block_compactness > 0 else ""
    iv_tag = "" if individual_shape == "raw" else f"_I{individual_shape[0].upper()}"
    mode_prefix = {
        "buffered_union": "BU", "density_fill": "DF", "union": "UN",
        "convex_hull": "CH", "concave_hull": "NH", "oriented_bbox": "OB",
        "block_fill": "BF",
    }.get(mode, mode[:3].upper())
    common = f"P{int(print_limit)}_T{road_tier}{w_tag}_S{int(simplify_m)}{cp_tag}{iv_tag}"
    if mode == "buffered_union":
        name = f"{mode_prefix}_{common}_B{int(bldg_buffer)}{sub_tag}.png"
    elif mode == "density_fill":
        name = f"{mode_prefix}_{common}_D{int(density_thr*100)}{sub_tag}.png"
    elif mode == "concave_hull":
        name = f"{mode_prefix}_{common}_R{int(concave_ratio*100)}{sub_tag}.png"
    elif mode == "block_fill":
        name = f"{mode_prefix}_{common}_N{count_threshold}_D{int(density_thr*100)}{sub_tag}.png"
    else:
        name = f"{mode_prefix}_{common}{sub_tag}.png"
    out_path = out_dir / name

    # 标注用 records（用于 _render_annotations）
    records_by_type = {
        "building":   building_landmark_records or [],
        "vegetation": veg_landmark_records or [],
        "water":      water_landmark_records or [],
        "road":       road_landmark_records or [],
    } if annotate else None
    city_title = CITY_NAMES.get(city_key, city_key.title()) if city_key else None

    # brick 风格需要 road/water exclusion 来制造砖块间的留白通道
    if block_jitter and water_gdf is not None:
        from _TEXTURE_STYLE_OF_DEEPSEEK._block_filter import build_and_subtract_exclusions
        city_blocks_for_render = build_and_subtract_exclusions(
            city_blocks, water_gdf, veg_landmark_polys,
            roads_gdf=roads_gdf, road_inset=25.0, water_inset=40.0)
    else:
        city_blocks_for_render = city_blocks

    # block 语义分类（在 exclusion 之后，对最终渲染用的 blocks 分类）
    render_classes = classify_blocks(city_blocks_for_render, landuse_gdf, water_gdf, polys)
    cls_counts = {}
    for c in render_classes:
        cls_counts[c] = cls_counts.get(c, 0) + 1
    print(f"  block classes: {cls_counts}")

    render(out_path, polys, individuals, aggregated, city_blocks_for_render, ctx,
           f"buildings tune v2 (road blocks) {sub_label}", params, stats,
           fig_inches=fig_inches, dpi=dpi,
           tag_landmarks=tag_landmarks, size_landmarks=size_landmarks,
           veg_landmarks=veg_landmark_polys,
           water_landmark_polys=water_landmark_polys,
           small_water_polys=small_water_polys,
           veg_fill_polys=veg_fill_polys,
           records_by_type=records_by_type,
           annotate=annotate, annotate_top=annotate_top, city_title=city_title,
           block_tessellation=block_tessellation,
           show_print_grid=show_print_grid,
           draw_bo_fill=draw_bo_fill,
           block_jitter=block_jitter,
           railway_gdf=railway_gdf if draw_railway else None,
           pier_gdf=pier_gdf if draw_pier else None,
           stadium_gdf=stadium_gdf if draw_stadium else None,
           block_classes=render_classes)
    print(f"  → {name}  tag={len(tag_landmarks):>5}  size={len(size_landmarks):>5}  "
          f"blocks={len(aggregated):>5}  smalls={len(smalls):>5}  "
          f"veg={len(veg_landmark_polys or [])}  "
          f"WL={len(water_landmark_polys)}  WO={len(small_water_polys)}  "
          f"({elapsed:.1f}s)")
    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--print-limit", type=float, default=3500,
                    help="footprint 阈值 m²")
    ap.add_argument("--road-tier", type=int, default=4, choices=[1, 2, 3, 4, 5],
                    help="道路等级（1=主干, 2=+次干, 3=+居住路, "
                         "4=+服务路 service [推荐], 5=+人行道）")
    ap.add_argument("--simplify", type=float, default=5.0,
                    help="block 内 union 后简化容差 m")
    ap.add_argument("--mode",
                    choices=["union", "buffered_union", "density_fill",
                             "convex_hull", "concave_hull", "oriented_bbox",
                             "block_fill"],
                    default="buffered_union",
                    help="block 内聚合算法（block_fill = reference 风格：count+density 双阈值通过则整块填充，否则丢弃）")
    ap.add_argument("--bldg-buffer", type=float, default=8.0,
                    help="buffered_union 的 buffer 半径 m")
    ap.add_argument("--density-thr", type=float, default=0.25,
                    help="density_fill 的密度阈值 (0..1)")
    ap.add_argument("--concave-ratio", type=float, default=0.6,
                    help="concave_hull 的 ratio (0=紧贴, 1=convex_hull)")
    ap.add_argument("--min-compactness", type=float, default=0.0,
                    help="block 紧凑度过滤 Polsby-Popper（正方形≈0.78，建议 0.25~0.40 去三角 sliver）")
    ap.add_argument("--min-block-area", type=float, default=0.0,
                    help="block 最小面积 m²，太小直接跳过")
    ap.add_argument("--max-block-area", type=float, default=500000.0,
                    help="block 最大面积 m²（默认 500,000 = 0.5 km²，过滤山区/远郊巨型 polygonize cell；0 = 关闭）")
    ap.add_argument("--count-thr", type=int, default=3,
                    help="block_fill 的建筑数量阈值（必须满足 count ≥ N AND density ≥ D 才整块填充）")
    ap.add_argument("--use-landmarks", action="store_true",
                    help="按 OSM 标签识别地标（tourism/historic/temple/...）作为个体，无视 print_limit")
    ap.add_argument("--landmark-top-percent", type=float, default=0.0,
                    help="兜底：top X%% 面积的建筑也算地标 (0 = 关闭)")
    ap.add_argument("--no-block-convex", action="store_true",
                    help="关闭 block_fill 输出的「凸+≥4 顶点」强制约束（默认开启）")
    # ----- 标注 / 风格化 -----
    ap.add_argument("--annotate", action=argparse.BooleanOptionalAction, default=True,
                    help="在 PNG 上加文字标签 + 引线（reference 风格，4 类地标）；--no-annotate 关闭")
    ap.add_argument("--annotate-top", type=int, default=6,
                    help="每类地标显示 top N 个（按 priority 排）")
    ap.add_argument("--with-water-landmarks", action=argparse.BooleanOptionalAction, default=True,
                    help="渲染水体地标（西湖/钱塘江/密歇根湖等深蓝色块）；默认开；--no-with-water-landmarks 关")
    ap.add_argument("--with-small-water", action=argparse.BooleanOptionalAction, default=True,
                    help="渲染细碎水体（半透浅蓝）；默认开；--no-with-small-water 关")
    ap.add_argument("--with-veg-fill", action="store_true",
                    help="把细碎植被也做 block_fill 浅绿填充")
    ap.add_argument("--hotspot-relax", type=float, default=0.0,
                    help="热点 block top X%% 内放宽 building landmark 阈值（建议 10；0=关闭）")
    ap.add_argument("--no-block-tessellation", action="store_true",
                    help="关闭「全 block 浅米色底」填充（默认开启，消除留白感）")
    ap.add_argument("--bo-fill", action=argparse.BooleanOptionalAction, default=False,
                    help="是否画蓝色「建筑街区填充」层；默认 False（buffered_union 模式形态破碎，PNG 里丑）；--bo-fill 临时启用")
    ap.add_argument("--block-jitter", action=argparse.BooleanOptionalAction, default=True,
                    help="block tessellation 边缘 jitter + low-poly 三角阴影（手绘感）；默认开；--no-block-jitter 关")
    ap.add_argument("--railway", action=argparse.BooleanOptionalAction, default=False,
                    help="画铁路层（钢灰细线，含 L/地铁/通勤/有轨电车）；默认关；--railway 开")
    ap.add_argument("--pier", action=argparse.BooleanOptionalAction, default=True,
                    help="画码头层（man_made=pier/breakwater/wharf 浅米陆地）；默认开；--no-pier 关")
    ap.add_argument("--stadium", action=argparse.BooleanOptionalAction, default=True,
                    help="画体育场馆层（leisure=stadium/sports_centre/marina 砖红色）；默认开；--no-stadium 关")
    ap.add_argument("--show-print-grid", action="store_true",
                    help="叠加打印精度网格（每 5 nozzles ≈ 250m）+ 右下角 51m 参考方块")
    ap.add_argument("--min-buildings", type=int, default=0,
                    help="block 内最少建筑数量，低于此数跳过（防水体被填）")
    ap.add_argument("--individual-shape", choices=["raw", "convex", "bbox"],
                    default="raw",
                    help="大楼（≥print_limit）的外形：raw=OSM原样, convex=凸包, bbox=最小外接矩形")
    ap.add_argument("--dpi", type=int, default=220,
                    help="输出 PNG dpi（默认 220 ≈ 4000px 边长）")
    ap.add_argument("--fig-inches", type=float, default=18.0,
                    help="figure 边长（inch）— 与 dpi 一起决定输出像素")
    ap.add_argument("--grid", action="store_true",
                    help="网格搜索 4 print × 3 tier × 3 simplify = 36 组")
    ap.add_argument("--grid-n5", action="store_true",
                    help="N5 网格：水体边界进 polygonize + density_fill + 高 simplify")
    ap.add_argument("--grid-n6", action="store_true",
                    help="N6 网格：在 BU/T5/W/B=20 锁定下扫 S↑ + P↓（更块感 + 更密地标）")
    ap.add_argument("--grid-n7", action="store_true",
                    help="N7 网格：饱满形状（convex/concave/bbox）+ 紧凑度过滤")
    ap.add_argument("--grid-n8", action="store_true",
                    help="N8 网格：block_fill 纯阈值（reference 风格，count × density 双阈值扫描）")
    ap.add_argument("--sub", action="store_true",
                    help="只用 5km 西湖中心子区域")
    ap.add_argument("--use-water", action=argparse.BooleanOptionalAction, default=True,
                    help="把水体边界（湖岸 + 河流）也送进 polygonize 切 block")
    ap.add_argument("--no-cache", action="store_true",
                    help="忽略缓存重读 GeoJSON")
    ap.add_argument("--city", choices=list(CITY_PRESETS.keys()), default="westlake",
                    help="城市预设（决定 bbox + PBF 来源）")
    ap.add_argument("--lat1", type=float, help="bbox south latitude（覆盖 --city 预设）")
    ap.add_argument("--lon1", type=float, help="bbox west longitude")
    ap.add_argument("--lat2", type=float, help="bbox north latitude")
    ap.add_argument("--lon2", type=float, help="bbox east longitude")
    args = ap.parse_args()

    # bbox 决议：CLI 显式 > city preset
    preset = CITY_PRESETS[args.city]
    bbox_lat1, bbox_lon1, bbox_lat2, bbox_lon2, pbf_base = preset
    if args.lat1 is not None: bbox_lat1 = args.lat1
    if args.lon1 is not None: bbox_lon1 = args.lon1
    if args.lat2 is not None: bbox_lat2 = args.lat2
    if args.lon2 is not None: bbox_lon2 = args.lon2
    cache_label = args.city
    print(f"  City: {args.city}  bbox=({bbox_lat1},{bbox_lon1})-({bbox_lat2},{bbox_lon2})  pbf={pbf_base}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    sub_label = ("5km sub " if args.sub else "25km full ") + f"[{args.city}]"
    print(f"\n=== 加载数据 ({sub_label}) ===")
    (polys, landmark_flags, roads_gdf, water_gdf, ctx, veg_landmark_polys,
     bldg_lm_records, veg_lm_records, water_lm_records, road_lm_records,
     polys_to_row, bgdf_tags,
     railway_gdf, pier_gdf, stadium_gdf, landuse_gdf
     ) = load_data(
        sub=args.sub, force=args.no_cache,
        lat1=bbox_lat1, lon1=bbox_lon1, lat2=bbox_lat2, lon2=bbox_lon2,
        pbf_basename=pbf_base, cache_label=cache_label)

    # block 缓存（key = (road_tier, use_water)，避免不同 water 配置的产物互相污染）
    blocks_cache: dict = {}

    if args.grid_n8:
        print("\n=== N8 网格：block_fill 纯阈值（reference 风格）===")
        # 用户洞察：reference demo（杭州/武汉/重庆）的建筑层基本是
        # "路网+水网切 block，密度+数量阈值通过则整块填充，否则丢弃"
        # 不达标的 block 完全不显示建筑（无 buffered_union fallback）
        # 锁定 T=5 / W=on / P=2500 / S=60 / compactness=0.30
        configs = []
        # count × density 双阈值扫描（共 15 组）
        # count: reference 看上去是 ≥ 3-5；density: 看着不高，0.05-0.25
        for count_n in [1, 2, 3, 5, 8]:
            for dens in [0.05, 0.15, 0.25]:
                configs.append({
                    "mode": "block_fill",
                    "print_limit": 2500, "simplify_m": 60.0,
                    "bldg_buffer": 20.0,
                    "count_thr": count_n, "density_thr": dens,
                    "min_compact": 0.30,
                })
        n = len(configs)
        print(f"  {n} 组参数  ({sub_label})")
        print(f"  锁定: T=5 / W=on / P=2500 / S=60 / compact≥0.30 / individual=raw")
        print(f"  扫: count_thr ∈ [1,2,3,5,8]  ×  density_thr ∈ [0.05,0.15,0.25]")
        print()
        for i, cfg in enumerate(configs, 1):
            print(f"\n[{i}/{n}] {cfg}")
            run_one(polys, roads_gdf, ctx,
                    print_limit=cfg["print_limit"],
                    road_tier=5,
                    simplify_m=cfg["simplify_m"],
                    mode=cfg["mode"],
                    bldg_buffer=cfg["bldg_buffer"],
                    density_thr=cfg["density_thr"],
                    sub_label=sub_label, blocks_cache=blocks_cache,
                    fig_inches=args.fig_inches, dpi=args.dpi,
                    water_gdf=water_gdf, use_water=True,
                    individual_shape="raw",
                    concave_ratio=0.6,
                    min_block_compactness=cfg["min_compact"],
                    min_block_area_m2=0.0,
                    min_buildings_per_block=0,        # block_fill 自带 count 过滤
                    count_threshold=cfg["count_thr"],
                    landmark_flags=landmark_flags,
                    use_landmarks=args.use_landmarks,
                    landmark_top_percent=args.landmark_top_percent,
                    block_fill_convex=not args.no_block_convex,
                    max_block_area_m2=args.max_block_area,
                    veg_landmark_polys=veg_landmark_polys,
                    landuse_gdf=landuse_gdf)
        print(f"\n→ {OUT_DIR}")
    elif args.grid_n7:
        print("\n=== N7 网格：饱满形状 + 紧凑度过滤 ===")
        # 目标：摆脱 union 的三角/内凹 finger，向矩形 / 凸形靠拢
        # 锁定 T=5 / W=on / P=2500（用户偏好）/ S=80 / B=20
        # 扫 mode × compactness 阈值
        configs = []
        # convex_hull：极端饱满
        for cp in [0.0, 0.25, 0.40]:
            configs.append({"mode": "convex_hull",
                            "print_limit": 2500, "simplify_m": 80.0,
                            "bldg_buffer": 20.0, "density_thr": 0.25,
                            "concave_ratio": 0.6, "min_compact": cp,
                            "individual_shape": "convex"})
        # concave_hull：可调凹度
        for cp in [0.0, 0.30]:
            for r in [0.4, 0.7, 0.9]:
                configs.append({"mode": "concave_hull",
                                "print_limit": 2500, "simplify_m": 80.0,
                                "bldg_buffer": 20.0, "density_thr": 0.25,
                                "concave_ratio": r, "min_compact": cp,
                                "individual_shape": "convex"})
        # oriented_bbox：强制矩形
        for cp in [0.0, 0.30]:
            configs.append({"mode": "oriented_bbox",
                            "print_limit": 2500, "simplify_m": 80.0,
                            "bldg_buffer": 20.0, "density_thr": 0.25,
                            "concave_ratio": 0.6, "min_compact": cp,
                            "individual_shape": "bbox"})
        # density_fill 对照（加 compactness 过滤后看效果）
        for cp in [0.0, 0.30, 0.50]:
            configs.append({"mode": "density_fill",
                            "print_limit": 2500, "simplify_m": 80.0,
                            "bldg_buffer": 20.0, "density_thr": 0.20,
                            "concave_ratio": 0.6, "min_compact": cp,
                            "individual_shape": "convex"})
        n = len(configs)
        print(f"  {n} 组参数  ({sub_label})")
        print(f"  锁定: T=5 / W=on / P=2500 / S=80 / B=20")
        print(f"  扫: mode ∈ [CH, NH(r=0.4/0.7/0.9), OB, DF] × min_compact ∈ [0,0.25,0.30,0.40,0.50]")
        print()
        for i, cfg in enumerate(configs, 1):
            print(f"\n[{i}/{n}] {cfg}")
            run_one(polys, roads_gdf, ctx,
                    print_limit=cfg["print_limit"],
                    road_tier=5,
                    simplify_m=cfg["simplify_m"],
                    mode=cfg["mode"],
                    bldg_buffer=cfg["bldg_buffer"],
                    density_thr=cfg["density_thr"],
                    sub_label=sub_label, blocks_cache=blocks_cache,
                    fig_inches=args.fig_inches, dpi=args.dpi,
                    water_gdf=water_gdf, use_water=True,
                    individual_shape=cfg["individual_shape"],
                    concave_ratio=cfg["concave_ratio"],
                    min_block_compactness=cfg["min_compact"],
                    min_block_area_m2=0.0,
                    min_buildings_per_block=args.min_buildings,
                    count_threshold=args.count_thr,
                    landmark_flags=landmark_flags,
                    use_landmarks=args.use_landmarks,
                    landmark_top_percent=args.landmark_top_percent,
                    block_fill_convex=not args.no_block_convex,
                    max_block_area_m2=args.max_block_area,
                    veg_landmark_polys=veg_landmark_polys,
                    landuse_gdf=landuse_gdf)
        print(f"\n→ {OUT_DIR}")
    elif args.grid_n6:
        print("\n=== N6 网格：BU/T5/W/B=20 锁定，S↑ + P↓（更块感 + 更密地标）===")
        # 用户偏好：BU_P3000_T5_W_B*_S40 系列
        # 方向：simplify 上推 → 更直更块；print_limit 下拉 → 更多橙点（地标）
        configs = []
        for pl in [1500, 2000, 2500, 3000]:
            for simp in [60, 100, 150, 200]:
                configs.append({
                    "mode": "buffered_union",
                    "print_limit": pl, "road_tier": 5,
                    "simplify_m": float(simp), "bldg_buffer": 20.0,
                    "density_thr": 0.25,
                })
        n = len(configs)
        print(f"  {n} 组参数  ({sub_label})")
        print(f"  P ∈ [1500,2000,2500,3000]  ×  S ∈ [60,100,150,200]")
        print()
        for i, cfg in enumerate(configs, 1):
            print(f"\n[{i}/{n}] {cfg}")
            run_one(polys, roads_gdf, ctx,
                    print_limit=cfg["print_limit"],
                    road_tier=cfg["road_tier"],
                    simplify_m=cfg["simplify_m"],
                    mode=cfg["mode"],
                    bldg_buffer=cfg["bldg_buffer"],
                    density_thr=cfg["density_thr"],
                    sub_label=sub_label, blocks_cache=blocks_cache,
                    fig_inches=args.fig_inches, dpi=args.dpi,
                    water_gdf=water_gdf, use_water=True,
                    individual_shape=args.individual_shape,
                    concave_ratio=args.concave_ratio,
                    min_block_compactness=args.min_compactness,
                    min_block_area_m2=args.min_block_area,
                    min_buildings_per_block=args.min_buildings,
                    count_threshold=args.count_thr,
                    landmark_flags=landmark_flags,
                    use_landmarks=args.use_landmarks,
                    landmark_top_percent=args.landmark_top_percent,
                    block_fill_convex=not args.no_block_convex,
                    max_block_area_m2=args.max_block_area,
                    veg_landmark_polys=veg_landmark_polys,
                    landuse_gdf=landuse_gdf)
        print(f"\n→ {OUT_DIR}")
    elif args.grid_n5:
        print("\n=== N5 网格：水体边界进 polygonize + density_fill 高 simplify ===")
        # 假设：水体参与切分能解决环湖建筑跨湖问题；
        # density_fill 让密集 block 整块铺满，高 simplify 把锯齿边界拉直成直线
        # 对照组：water on/off × density_fill / buffered_union × simplify [10,40,80]
        configs = []
        # density_fill + 水体（核心目标）
        for use_w in [True]:
            for thr in [0.15, 0.25, 0.35]:
                for simp in [10, 40, 80]:
                    for tier in [4, 5]:
                        configs.append({
                            "mode": "density_fill",
                            "print_limit": 500, "road_tier": tier,
                            "simplify_m": float(simp), "bldg_buffer": 8.0,
                            "density_thr": thr, "use_water": use_w,
                        })
        # buffered_union + 水体（对照：看水体边界本身的影响）
        for use_w in [True]:
            for bf in [15, 30]:
                for simp in [40, 80]:
                    configs.append({
                        "mode": "buffered_union",
                        "print_limit": 3000, "road_tier": 5,
                        "simplify_m": float(simp), "bldg_buffer": float(bf),
                        "density_thr": 0.25, "use_water": use_w,
                    })
        n = len(configs)
        print(f"  {n} 组参数  ({sub_label})")
        print(f"  density_fill+water: thr=[0.15,0.25,0.35] × simp=[10,40,80] × tier=[4,5]")
        print(f"  buffered_union+water: buffer=[15,30] × simp=[40,80] @ T5")
        print()
        for i, cfg in enumerate(configs, 1):
            print(f"\n[{i}/{n}] {cfg}")
            run_one(polys, roads_gdf, ctx,
                    print_limit=cfg["print_limit"],
                    road_tier=cfg["road_tier"],
                    simplify_m=cfg["simplify_m"],
                    mode=cfg["mode"],
                    bldg_buffer=cfg["bldg_buffer"],
                    density_thr=cfg["density_thr"],
                    sub_label=sub_label, blocks_cache=blocks_cache,
                    fig_inches=args.fig_inches, dpi=args.dpi,
                    water_gdf=water_gdf, use_water=cfg["use_water"],
                    individual_shape=args.individual_shape,
                    concave_ratio=args.concave_ratio,
                    min_block_compactness=args.min_compactness,
                    min_block_area_m2=args.min_block_area,
                    min_buildings_per_block=args.min_buildings,
                    count_threshold=args.count_thr,
                    landmark_flags=landmark_flags,
                    use_landmarks=args.use_landmarks,
                    landmark_top_percent=args.landmark_top_percent,
                    block_fill_convex=not args.no_block_convex,
                    max_block_area_m2=args.max_block_area,
                    veg_landmark_polys=veg_landmark_polys,
                    landuse_gdf=landuse_gdf)
        print(f"\n→ {OUT_DIR}")
    elif args.grid:
        print("\n=== 网格搜索 ===")
        # 聚焦"街区聚拢度"维度，往大 buffer / 大 print_limit / density_fill 倾斜
        # 同时含 buffered_union 和 density_fill 两种 mode
        # 每组 25km 大约 7 秒，36 组 ≈ 4-5 分钟
        configs = []
        # buffered_union 系列：3 种 print_limit × 3 种 buffer × 2 种 tier
        for pl in [1000, 3000, 8000]:
            for bf in [8, 15, 25]:
                for tier in [4, 5]:
                    configs.append({
                        "mode": "buffered_union",
                        "print_limit": pl, "road_tier": tier,
                        "simplify_m": 5.0, "bldg_buffer": bf,
                        "density_thr": 0.25,
                    })
        # density_fill 系列：3 种 threshold × 2 种 tier
        for thr in [0.15, 0.25, 0.40]:
            for tier in [4, 5]:
                configs.append({
                    "mode": "density_fill",
                    "print_limit": 500, "road_tier": tier,
                    "simplify_m": 5.0, "bldg_buffer": 8.0,
                    "density_thr": thr,
                })
        n = len(configs)
        print(f"  {n} 组参数  ({sub_label})")
        print(f"  buffered_union: print=[1000,3000,8000] × buffer=[8,15,25] × tier=[4,5]")
        print(f"  density_fill:   threshold=[0.15,0.25,0.40] × tier=[4,5]")
        print()
        for i, cfg in enumerate(configs, 1):
            print(f"\n[{i}/{n}] mode={cfg['mode']:<15} {cfg}")
            run_one(polys, roads_gdf, ctx,
                    print_limit=cfg["print_limit"],
                    road_tier=cfg["road_tier"],
                    simplify_m=cfg["simplify_m"],
                    mode=cfg["mode"],
                    bldg_buffer=cfg["bldg_buffer"],
                    density_thr=cfg["density_thr"],
                    sub_label=sub_label, blocks_cache=blocks_cache,
                    fig_inches=args.fig_inches, dpi=args.dpi,
                    water_gdf=water_gdf, use_water=args.use_water,
                    individual_shape=args.individual_shape,
                    concave_ratio=args.concave_ratio,
                    min_block_compactness=args.min_compactness,
                    min_block_area_m2=args.min_block_area,
                    min_buildings_per_block=args.min_buildings,
                    count_threshold=args.count_thr,
                    landmark_flags=landmark_flags,
                    use_landmarks=args.use_landmarks,
                    landmark_top_percent=args.landmark_top_percent,
                    block_fill_convex=not args.no_block_convex,
                    max_block_area_m2=args.max_block_area,
                    veg_landmark_polys=veg_landmark_polys,
                    landuse_gdf=landuse_gdf)
        print(f"\n→ {OUT_DIR}")
    else:
        print(f"\n=== 单组 ===")
        run_one(polys, roads_gdf, ctx,
                print_limit=args.print_limit,
                road_tier=args.road_tier,
                simplify_m=args.simplify,
                mode=args.mode,
                bldg_buffer=args.bldg_buffer,
                density_thr=args.density_thr,
                sub_label=sub_label, blocks_cache=blocks_cache,
                fig_inches=args.fig_inches, dpi=args.dpi,
                water_gdf=water_gdf, use_water=args.use_water,
                individual_shape=args.individual_shape,
                concave_ratio=args.concave_ratio,
                min_block_compactness=args.min_compactness,
                min_block_area_m2=args.min_block_area,
                min_buildings_per_block=args.min_buildings,
                count_threshold=args.count_thr,
                landmark_flags=landmark_flags,
                use_landmarks=args.use_landmarks,
                landmark_top_percent=args.landmark_top_percent,
                block_fill_convex=not args.no_block_convex,
                max_block_area_m2=args.max_block_area,
                veg_landmark_polys=veg_landmark_polys,
                # ---- v2 完整版 ----
                water_gdf_full=water_gdf,
                water_landmark_records=water_lm_records,
                building_landmark_records=bldg_lm_records,
                veg_landmark_records=veg_lm_records,
                road_landmark_records=road_lm_records,
                with_water_landmarks=args.with_water_landmarks,
                with_small_water=args.with_small_water,
                with_veg_fill=args.with_veg_fill,
                annotate=args.annotate,
                annotate_top=args.annotate_top,
                city_key=args.city,
                polys_to_row=polys_to_row,
                bgdf_tags=bgdf_tags,
                hotspot_relax=args.hotspot_relax,
                block_tessellation=not args.no_block_tessellation,
                show_print_grid=args.show_print_grid,
                draw_bo_fill=args.bo_fill,
                block_jitter=args.block_jitter,
                railway_gdf=railway_gdf,
                pier_gdf=pier_gdf,
                stadium_gdf=stadium_gdf,
                draw_railway=args.railway,
                draw_pier=args.pier,
                draw_stadium=args.stadium,
                landuse_gdf=landuse_gdf)


if __name__ == "__main__":
    main()
