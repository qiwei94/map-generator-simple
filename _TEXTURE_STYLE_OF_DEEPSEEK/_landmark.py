"""Landmark-aware building classification — 用 OSM 标签识别地标楼。

地标 = 个体保留（不进 block_fill）。判定规则三层：
  Tier 1: wikidata/wikipedia/historic/tourism (强信号)
  Tier 2: 建筑类型 (stadium/temple/...) 或 man_made/religion
  Tier 3: name 非空 AND building 非住宅类
  + 调用方可选叠加: top X% 面积 OR area ≥ 阈值

依赖最少（仅 pandas），让 tune 工具和主管道都能复用。
"""

from __future__ import annotations

import pandas as pd

# Tier 2: 这些 building=* 一律算地标（curated 清单，不含 school/kindergarten 太多）
LANDMARK_BUILDING_TYPES = frozenset({
    "stadium", "university", "college", "hospital", "train_station",
    "mall", "public", "government", "museum", "cathedral", "church",
    "temple", "mosque", "synagogue", "civic", "library",
    "pagoda", "shrine", "chapel", "monastery", "convent", "abbey",
})

# Tier 1: tourism 子集（hotel/guest_house 太多不算）
LANDMARK_TOURISM = frozenset({
    "museum", "gallery", "attraction", "theme_park", "aquarium",
})

# Tier 2: amenity 子集
LANDMARK_AMENITY = frozenset({
    "university", "hospital", "mall", "theatre", "cinema",
    "place_of_worship", "library", "townhall", "courthouse",
    "college",  # university/college 算，普通 school 不算
})

# Tier 2: man_made 子集
LANDMARK_MAN_MADE = frozenset({"tower", "lighthouse", "water_tower", "obelisk"})

# Tier 3 黑名单：有 name 但仍不算地标的住宅类
NON_LANDMARK_BUILDING_TYPES = frozenset({
    "apartments", "residential", "house", "detached", "dormitory",
    "yes",                  # 默认值，太宽泛
    "garage", "garages", "shed", "carport",
    "greenhouse", "warehouse",
    "industrial",            # 厂房不算地标
})


def is_tag_landmark(row: pd.Series, area_m2: float = None,
                     hotspot: bool = False) -> bool:
    """根据 OSM 标签 (+ 可选面积 + 是否在热点 block) 判断建筑是否为地标。

    输入：
      row     — geopandas row（用 .get(key) 取字段，缺失返回 NaN）
      area_m2 — 几何面积，用于 Tier 3b（name + building=yes + 大面积）的判定；
                None 时该层退化为不通过
      hotspot — True 时放宽阈值（用于热点 block 内）：
                * Tier 3b 面积阈值 1500 → 1000 m²
                * Tier 4 (新)：commercial/retail/office/hotel + name 也算地标

    返回 True → 个体保留；False → 进 block_fill。
    """
    g = row.get  # alias

    # ---- Tier 1: 强信号（无视面积）
    if pd.notna(g("wikidata")) or pd.notna(g("wikipedia")):
        return True
    if pd.notna(g("historic")):
        return True
    tour = g("tourism")
    if pd.notna(tour) and tour in LANDMARK_TOURISM:
        return True
    if pd.notna(g("heritage")):
        return True

    # ---- Tier 2: 类型信号（无视面积，类型本身就有意义）
    bldg = g("building")
    if pd.notna(bldg) and bldg in LANDMARK_BUILDING_TYPES:
        return True
    amen = g("amenity")
    if pd.notna(amen) and amen in LANDMARK_AMENITY:
        return True
    man = g("man_made")
    if pd.notna(man) and man in LANDMARK_MAN_MADE:
        return True
    if pd.notna(g("tower:type")):
        return True
    if pd.notna(g("religion")):
        return True
    if pd.notna(g("government")):
        return True
    if pd.notna(g("military")):
        return True
    if pd.notna(g("museum")):
        return True

    # ---- Tier 3a: name + 非住宅/工业/默认类型 → 命中（不管面积）
    name = g("name")
    if pd.notna(name):
        if pd.notna(bldg) and bldg not in NON_LANDMARK_BUILDING_TYPES and bldg != "yes":
            return True
        # ---- Tier 3b: name + building=yes/缺 → 必须 ≥ 阈值
        # 热点 block 内放宽：1500 → 800 m²
        tier3b_thr = 800.0 if hotspot else 1500.0
        if (pd.isna(bldg) or bldg == "yes") and area_m2 is not None and area_m2 >= tier3b_thr:
            return True
        # ---- Tier 4 (热点专属): 商业/办公 + name 一律算地标（不要求面积）
        if hotspot and pd.notna(bldg) and bldg in {
                "commercial", "retail", "office", "hotel",
                "civic", "public", "industrial", "warehouse"}:
            return True

    return False


# =============================================================================
# 植被地标（Vegetation landmarks: 公园 / 林地 / 湿地 / 名山）
# =============================================================================

# Tier 1 强信号：tourism 子集（hotel/guest_house 不算）
LANDMARK_TOURISM_VEG = frozenset({
    "attraction", "theme_park", "zoo", "aquarium", "park", "viewpoint",
})

# Tier 2 leisure 信号
LANDMARK_LEISURE_VEG = frozenset({
    "nature_reserve", "park", "garden",
})

# Tier 2 boundary 信号（保护区 / 国家公园 / 风景名胜）
LANDMARK_BOUNDARY_VEG = frozenset({
    "national_park", "protected_area", "nature_reserve",
})


def is_vegetation_landmark(row: pd.Series, area_m2: float = None) -> bool:
    """植被地标（公园 / 林地 / 山 / 湿地 / 景区）。

    Tier 1: wikidata/wikipedia/heritage/tourism in {attraction|theme_park|...}
    Tier 2: boundary in {national_park|protected_area|...} OR leisure in {nature_reserve|park|garden}
            (大面积优先：面积 ≥ 50,000 m² 才算地标 leisure)
    Tier 3: name 命名 + (landuse in {forest|grass|...} OR natural in {wood|grassland|wetland|...})
            + 面积 ≥ 50,000 m²（5 公顷起步，过滤小区花坛）
    """
    g = row.get

    # ---- Tier 1
    if pd.notna(g("wikidata")) or pd.notna(g("wikipedia")):
        return True
    if pd.notna(g("heritage")):
        return True
    tour = g("tourism")
    if pd.notna(tour) and tour in LANDMARK_TOURISM_VEG:
        return True

    # ---- Tier 2: 边界 / 保护区
    bnd = g("boundary")
    if pd.notna(bnd) and bnd in LANDMARK_BOUNDARY_VEG:
        return True
    leisure = g("leisure")
    if pd.notna(leisure) and leisure in LANDMARK_LEISURE_VEG:
        # 大公园 / 自然保护区算地标，小区花园不算
        if area_m2 is not None and area_m2 >= 20000.0:
            return True

    # ---- Tier 3: 命名 + 自然类型 + 大面积（≥ 2 公顷）
    name = g("name")
    if pd.notna(name) and (area_m2 is not None and area_m2 >= 20000.0):
        landuse = g("landuse")
        natural = g("natural")
        if pd.notna(landuse) and landuse in {"forest", "grass", "meadow",
                                              "village_green", "recreation_ground",
                                              "conservation", "cemetery", "orchard",
                                              "vineyard", "allotments"}:
            return True
        if pd.notna(natural) and natural in {"wood", "grassland", "wetland",
                                              "scrub", "heath", "fell"}:
            return True

    return False


# =============================================================================
# 水体地标（Water landmarks: 命名河流 / 大湖泊）
# =============================================================================

def is_water_landmark(row: pd.Series, area_m2: float = None) -> bool:
    """水体地标：命名河流 / 大湖泊 / 大型 natural=water（即使 OSM name 丢失）。

    Tier 1: wikidata/wikipedia
    Tier 2: name + waterway in {river, canal}（命名河流任意大小）
    Tier 3: name + 面积 ≥ 5 公顷（命名水体，过滤小区水景）
    Tier 4: water=river/canal + natural=water 且 ≥ 10 公顷（兜底：relation 导出常丢 name）
    Tier 5: natural=water 且 ≥ 50 公顷（大面积水体，无需 name/water tag）
    Tier 6: 纯面积 ≥ 50 公顷（已在水体 GDF 中即可，relation 导出常丢全部 tag）
    """
    g = row.get
    if pd.notna(g("wikidata")) or pd.notna(g("wikipedia")):
        return True
    name = g("name")
    if pd.notna(name):
        wway = g("waterway")
        if pd.notna(wway) and wway in {"river", "canal"}:
            return True
        if area_m2 is not None and area_m2 >= 50000.0:
            return True
    # Tier 4 兜底（无 name 也认，需 water=river/canal）
    water_kind = g("water")
    natural = g("natural")
    if (pd.notna(water_kind) and water_kind in ("river", "canal")
            and pd.notna(natural) and natural == "water"
            and area_m2 is not None and area_m2 >= 100000.0):
        return True
    # Tier 5: natural=water 且面积 ≥ 50万m²（大河段/大湖，无需 name 或 water tag）
    if (pd.notna(natural) and natural == "water"
            and area_m2 is not None and area_m2 >= 500000.0):
        return True
    # Tier 6: 纯面积兜底 — 已进入水体 GDF 说明数据采集认定为水，
    #         ≥50万m² 不可能是噪声（relation 导出常丢所有 tag）
    if area_m2 is not None and area_m2 >= 500000.0:
        return True
    return False


# =============================================================================
# 道路地标（Road landmarks: 著名桥梁 / 高架）
# =============================================================================

# 不算"地标桥"的小道/支路类型
_NON_LANDMARK_HIGHWAY = frozenset({
    "footway", "path", "steps", "track", "cycleway",
    "service", "pedestrian", "bridleway", "corridor",
})


def is_road_landmark(row: pd.Series, area_m2: float = None) -> bool:
    """道路地标：命名 + bridge=yes + 主要道路（非小道）。
    大量 OSM `bridge=yes` 是立交支路 / 小桥 / 步道桥，需收紧才有"地标"含义。
    （area_m2 参数仅为统一签名，道路 LineString 没意义）
    """
    g = row.get
    name = g("name")
    if pd.isna(name): return False
    # 必须是道路 feature（防止 building/landuse 误进）
    hw = g("highway")
    if pd.isna(hw): return False
    # 必须是桥
    bridge = g("bridge")
    if pd.isna(bridge) or bridge in ("no", "0"): return False
    # 排除小桥 / 小道
    if hw in _NON_LANDMARK_HIGHWAY: return False
    # 强信号：wikidata / wikipedia
    if pd.notna(g("wikidata")) or pd.notna(g("wikipedia")):
        return True
    # 命名含"大桥"等关键字 → 算地标
    name_str = str(name)
    if any(k in name_str for k in ("大桥", "Bridge", "Viaduct", "高架", "立交")):
        return True
    return False


# =============================================================================
# 地标优先级评分（用于 top N 标注排序）
# =============================================================================

def landmark_priority(row: pd.Series, area_m2: float = 0.0) -> float:
    """综合优先级评分（越高越知名）。

    用于在 PNG 上标注地标时，每类只取 top N 的排序依据。
    评分规则：
        wikidata=*    → +100  （已上 Wiki 的肯定知名）
        wikipedia=*   → +50
        heritage=*    → +30
        tourism=*     → +20   （景点）
        historic=*    → +20
        + 面积奖励：min(area / 10000, 50)
    """
    score = 0.0
    g = row.get
    if pd.notna(g("wikidata")):  score += 100.0
    if pd.notna(g("wikipedia")): score += 50.0
    if pd.notna(g("heritage")):  score += 30.0
    if pd.notna(g("tourism")):   score += 20.0
    if pd.notna(g("historic")):  score += 20.0
    score += min(area_m2 / 10000.0, 50.0)
    return score


def compute_hotspot_block_ids(blocks: list, building_polys: list,
                                top_percent: float = 10.0) -> set:
    """按 block 内建筑面积/block 面积，取 top X% 为热点 block。

    用于：在热点 block 内放宽 building landmark 阈值（is_tag_landmark hotspot=True）。

    输入：
      blocks         — list[Polygon]，路网+水网 polygonize 出来的 city blocks
      building_polys — list[Polygon]，全部投影后的建筑 polygon
      top_percent    — 前 X% 算热点（默认 10）

    返回：set[int] of block 索引
    """
    if not blocks or not building_polys or top_percent <= 0:
        return set()
    from shapely.strtree import STRtree
    btree = STRtree(blocks)
    block_bldg_area = [0.0] * len(blocks)
    for b in building_polys:
        if b is None or b.is_empty: continue
        c = b.centroid
        for ci in btree.query(c):
            if blocks[ci].contains(c):
                block_bldg_area[ci] += b.area
                break
    # 仅在"有建筑的 block"上取 top X%（避免空 block 把 percentile 拉到 0）
    nonzero = [(i, a / max(blocks[i].area, 1.0))
               for i, a in enumerate(block_bldg_area) if a > 0]
    if not nonzero: return set()
    nonzero.sort(key=lambda x: -x[1])
    n_top = max(1, int(len(nonzero) * top_percent / 100.0))
    return {i for i, _ in nonzero[:n_top]}


def compute_top_percent_threshold(areas: list, top_percent: float) -> float:
    """返回"前 X% 面积"对应的阈值。areas 不需预先排序。"""
    if top_percent <= 0 or not areas:
        return float("inf")  # 关闭 percentile 兜底
    n = len(areas)
    n_top = max(1, int(n * top_percent / 100.0))
    sorted_desc = sorted(areas, reverse=True)
    return sorted_desc[min(n_top, n) - 1]


def classify_landmarks_in_gdf(gdf, top_percent: float = 0.0,
                              min_area_m2: float = 0.0,
                              ) -> list:
    """为 gdf 每行计算 is_landmark 标志（用于 tune 工具的预计算缓存）。

    is_landmark = is_tag_landmark(row) OR (area ≥ top_percent_threshold)
                                       OR (area ≥ min_area_m2 absolute floor)

    返回 [bool] 列表，长度等于 gdf 行数（包含 None / 空几何）。
    """
    geoms = gdf.geometry.tolist()
    areas = [g.area if (g is not None and not g.is_empty) else 0.0 for g in geoms]
    top_thr = compute_top_percent_threshold(areas, top_percent)

    flags: list[bool] = []
    for (idx, row), area in zip(gdf.iterrows(), areas):
        if area <= 0:
            flags.append(False); continue
        if is_tag_landmark(row, area_m2=area):
            flags.append(True); continue
        if min_area_m2 > 0 and area >= min_area_m2:
            flags.append(True); continue
        if area >= top_thr:
            flags.append(True); continue
        flags.append(False)
    return flags
