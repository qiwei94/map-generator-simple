"""All constants for _TEXTURE_STYLE_OF_DEEPSEEK — the single source of truth.

Values derived from reverse-engineering 33 Urban Series reference 3MF models
(Bambu Lab "Urban Texture 1/125K" series, 7 cities in demo/).

Origin key (per constant):
  [REF]   — measured from reference 3MF models (see doc/reference_3mf_analysis.md)
  [CALC]  — calculated from physical/hardware constraints
  [TUNE]  — tuned through grid-search or visual iteration (see doc/session_*.md)
  [HW]    — hardware specification (Bambu Lab A1/P1)
  [CONV]  — domain convention (OSM / GIS / architecture / FDM printing)
  [DESIGN]— aesthetic design decision, no single empirical source
"""

# ---------------------------------------------------------------------------
# Physical dimensions & scale
# ---------------------------------------------------------------------------
INTERNAL_SPAN_MM = 196.0       # [REF] 200mm plate − 2mm margin/side; reference models use 100/127 transform scale
BUILD_PLATE_MM = 200.0         # [HW]  Bambu Lab A1/P1 build plate


def compute_scale(width_m: float, height_m: float) -> float:
    """Compute the mm-per-meter scale factor to normalize area to INTERNAL_SPAN_MM.
    
    Ensures the model always fills ~254mm regardless of real-world extent.
    """
    return INTERNAL_SPAN_MM / max(width_m, height_m)

# ---------------------------------------------------------------------------
# 3-Extruder mapping  [REF] reverse-engineered from 5 reference cities
# ---------------------------------------------------------------------------
# E1=白 (block_base + buildings + landmarks)
# E2=灰 (terrain + roads + vegetation)
# E3=黑 (water)
EXTRUDER_MAP = {
    "terrain": 2,
    "buildings": 1,
    "roads": 2,
    "water": 3,
    "vegetation": 2,
    "landmarks": 1,
    "block_base": 1,
}

# [REF] from reference 3MF basematerials metadata
FILAMENT_COLOURS = ["#FFFFFF", "#9A9A9A", "#000000"]  # E1=白, E2=灰, E3=黑

# ---------------------------------------------------------------------------
# Display colours  [REF] from reference 3MF displaycolor values
# ---------------------------------------------------------------------------
TERRAIN_COLOR = "#9A9A9A"      # gray (E2)
BUILDING_COLOR = "#FFFFFF"     # white (E1)
LANDMARK_COLOR = "#FFFFFF"     # white (E1)
ROAD_COLOR = "#9A9A9A"         # gray (E2)
WATER_COLOR = "#000000"        # black (E3)
BASE_WALL_COLOR = "#9A9A9A"    # gray (E2, terrain 底盖)
BLOCK_BASE_COLOR = "#FFFFFF"   # white (E1)

# ---------------------------------------------------------------------------
# Z stacking (model mm, bottom-to-top)
# ---------------------------------------------------------------------------
Z_WATER_BASE_MM = -2.00        # [REF] the single most stable constant across all 5 reference cities
Z_TERRAIN_BASE = -1.60         # [CALC] = Z_WATER_BASE_MM + WATER_BASE_THICKNESS_MM
Z_BUILDING_EMBED_MM = 0.04     # [REF] measured from reference models, for FDM fusion at boundary
Z_ROAD_ABOVE_TERRAIN_MM = 0.51 # [REF] Hangzhou/San Francisco = 0.512

# ---------------------------------------------------------------------------
# Per-layer thickness (model mm)
# ---------------------------------------------------------------------------
TERRAIN_THICKNESS_MM = 4.0     # [REF] reference range 3.5-5.0mm, 4.0 = midpoint
Z_GAMMA = 0.45                  # [TUNE] auto-param adaptive: flat→0.60, normal→0.45, steep→0.35
BUILDING_HEIGHT_MM = 4.0       # [TUNE] from reference terrain3d 4.5→4.0 (visual match)
BUILDING_HEIGHT_MIN_MM = 2.8   # [DESIGN] floor: must be > AGGREGATE_HEIGHT(0.625) for BL/BO separation
BUILDING_HEIGHT_MAX_MM = 4.0   # [TUNE] was 5.3 in terrain3d/config, reduced to match reference models
BUILDING_EXCLUSION_TOP_MM = 5.0  # [CALC] clears max building height(4.0) + margin
ROAD_THICKNESS_MM = 0.4        # [CONV] = 1x nozzle diameter, single-extrusion ribbon
WATER_THICKNESS_MM = 0.5       # [REF] flat plate portion (Hangzhou reference: 1.52mm total with relief)

# ---------------------------------------------------------------------------
# Building height estimation
# ---------------------------------------------------------------------------
BUILDING_DEFAULT_HEIGHT_M = 10.0       # [CONV] ~3 floors at 3.5m/floor
BUILDING_LEVEL_HEIGHT_M = 3.5         # [CONV] standard floor-to-floor
BUILDING_HEIGHT_OSM_MIN_M = 5.0       # [CONV] shortest recognizable building
BUILDING_HEIGHT_OSM_MAX_M = 150.0     # [CONV] practical skyscraper ceiling

# [CONV] area→height proxy when OSM lacks height tags (<1% in Chinese cities)
BUILDING_AREA_HEIGHTS = {
    100: 8.0,      # <100 m^2 -> 8m (small residential)
    200: 10.0,     # 100-200 m^2 -> 10m (typical residential)
    500: 15.0,     # 200-500 m^2 -> 15m (commercial)
    1000: 25.0,    # 500-1000 m^2 -> 25m (office/complex)
    2000: 40.0,    # 1000-2000 m^2 -> 40m (tall building)
}

# --- Height data quality assessment ---
HEIGHT_QUALITY_COVERAGE_THRESHOLD = 0.30   # [TUNE] coverage < 30% → flat mode

# --- Flat mode: BL heights by area tier (when height data is unreliable) ---
BUILDING_FLAT_HEIGHT_LOW_MM = 0.8     # [DESIGN] small landmark (< 5000m²)
BUILDING_FLAT_HEIGHT_MID_MM = 1.0     # [DESIGN] medium landmark (5000-20000m²)
BUILDING_FLAT_HEIGHT_HIGH_MM = 1.3    # [DESIGN] large landmark (≥ 20000m²)
BUILDING_FLAT_AREA_MID_M2 = 5000.0
BUILDING_FLAT_AREA_HIGH_M2 = 20000.0

# Building 单阈值策略（基于打印精度）  [CALC]
#
# 核心约束: 0.4mm 喷嘴 × scale (~0.0078mm/m for 25km) ≈ 51m 实地距离
#         51 × 51 ≈ 2600m² 是单个能打出形状的最小 footprint
#         PRINT_LIMIT=3500 留 35% 余量，约等于 1 个喷嘴宽度 × 1.2
#
# 流程:
#   ≥ PRINT_LIMIT  → 个体保留（按 OSM 真实高度压缩到 mm）
#   < PRINT_LIMIT  → 进聚合管道：
#     buffer(+B) → unary_union → buffer(-B+slack) → simplify
#     聚合后还要 ≥ PRINT_LIMIT 才保留
#   < MIN_AREA     → 噪声直接丢弃
BUILDING_MIN_AREA = {
    "small": 30,
    "medium": 80,
    "large": 100,    # [CONV] 第一道粗筛 — 比这小的纯噪声
}

# [CALC→TUNE] N8实验从3500降至2500; 物理下限=nozzle²/scale²≈2600m²
BUILDING_PRINT_LIMIT_M2 = 2500.0

# 聚合管道参数  [TUNE] session_2026_05_16 grid search (N8 lock)
BUILDING_AGGREGATE_BUFFER_M = 20.0       # 让相邻 40m 内的小楼合并成街区
BUILDING_AGGREGATE_SHRINK_SLACK_M = 5.0  # [DESIGN] 街区边缘留 5m"块感"
BUILDING_AGGREGATE_SIMPLIFY_M = 15.0     # [TUNE] v1 pipeline; simplify 是 dominant parameter
BUILDING_AGGREGATE_HEIGHT_MM = 0.625     # [TUNE] 3.0→2.5→0.625, 逐步降低至超薄含蓄

# Buildings v2: 路网 polygonize 聚合（替代 v1 buffer-union）
# [TUNE] 全部锁定自 N8 调参 (session_2026_05_16): BF_P2500_T5_W_S60_C30_N1_D5
#   参考风格：路网+水网切 block, count≥1 AND density≥5% → 整 block 填充
BUILDING_V2_ENABLED = True
BUILDING_V2_MODE = "oriented_bbox"             # [TUNE] union→buffered_union→density_fill→oriented_bbox 演进
BUILDING_V2_ROAD_TIER = 5                    # [TUNE] N8: T5=最细含人行道
BUILDING_V2_USE_WATER_BLOCKS = True          # [TUNE] N8: 水体边界参与街区切分
BUILDING_V2_BLOCK_BUFFER_M = 20.0            # [TUNE] buffered_union 的 buffer 半径 m
BUILDING_V2_DENSITY_THRESHOLD = 0.005        # [TUNE] 0.5%, 极度激进 reference 风格 (grid search 0.15-0.35)
BUILDING_V2_COUNT_THRESHOLD = 1              # [TUNE] N8: ≥1 才填充
BUILDING_V2_CONCAVE_RATIO = 0.70
BUILDING_V2_MIN_BLOCK_COMPACTNESS = 0.0
BUILDING_V2_MIN_BLOCK_AREA_M2 = 0.0
BUILDING_V2_INDIVIDUAL_SHAPE = "raw"
BUILDING_V2_AGGREGATE_SIMPLIFY_M = 60.0      # [TUNE] N8: _S60
BUILDING_V2_MIN_BUILDINGS_PER_BLOCK = 0
BUILDING_V2_USE_LANDMARK_TAGS = True
BUILDING_V2_LANDMARK_TOP_PERCENT = 1.0
BUILDING_V2_BLOCK_FILL_CONVEX = True

# 地标 simplify
BUILDING_SIMPLIFY_TOL_M = 25.0   # [TUNE] 大楼 footprint 简化 (D-P)

# ---------------------------------------------------------------------------
# Brick texture defaults  [TUNE] 手绘砖石风格几何变换参数
# ---------------------------------------------------------------------------
BRICK_CORNER_R_M = 8.0           # [TUNE] 圆角半径 (m)
BRICK_ROT_DEG = 10.0             # [TUNE] 随机旋转幅度 (degrees)
BRICK_SHIFT_M = 8.0              # [TUNE] 随机平移幅度 (m)
BRICK_PERLIN_AMP = 4.0           # [TUNE] Perlin 噪声振幅 (m); auto-param range 2.0-8.0
BRICK_PERLIN_FREQ = 0.15         # [TUNE] Perlin 噪声频率
BRICK_RESAMPLE_M = 12.0          # [TUNE] 边界重采样间距 (m)

# ---------------------------------------------------------------------------
# 地标增强: Kevin Lynch 4-category classification
# ---------------------------------------------------------------------------
from _TEXTURE_STYLE_OF_DEEPSEEK._landmark import LandmarkCategory

LANDMARK_CATEGORY_PARAMS = {
    # Cat 1: Spiritual/Cultural Anchors (historic, religious)
    # 加性偏移：保留建筑间相对高度差异
    LandmarkCategory.SPIRITUAL: {
        "height_add_mm": 1.2,        # [TUNE] 最高偏移，文化地标最突出
        "buffer_m": 5.0,
        "exclusion_buffer_m": 12.0,
    },
    # Cat 2: Urban Machinery Hubs (stadiums, stations, hospitals)
    LandmarkCategory.URBAN_HUB: {
        "height_add_mm": 0.8,        # [TUNE] 大型公共建筑
        "buffer_m": 8.0,
        "exclusion_buffer_m": 15.0,
    },
    # Cat 3: Visual Rulers (geometric outliers by height/area)
    LandmarkCategory.GEOMETRIC: {
        "height_add_mm": 0.8,        # [TUNE] 高度/面积异常建筑
        "buffer_m": 5.0,
        "exclusion_buffer_m": 10.0,
    },
    # Cat 4: Semantic Matches (name regex)
    LandmarkCategory.SEMANTIC: {
        "height_add_mm": 0.6,        # [TUNE] 命名建筑
        "buffer_m": 5.0,
        "exclusion_buffer_m": 8.0,
    },
}

# Geometric outlier thresholds (Cat 3)
LANDMARK_HEIGHT_TOP_PERCENT = 2.0       # height top 2%
LANDMARK_AREA_TOP_PERCENT = 5.0         # area top 5%

# Name regex (Cat 4)
LANDMARK_NAME_REGEX = (
    r"(?:"
    r"塔|中心|大厦|大楼|广场|宮|宫|寺|庙|廟|院|祠|阁|閣|楼|樓"
    r"|Tower|Center|Plaza|Headquarters|Cathedral|Palace|Museum"
    r")"
)
LANDMARK_NAME_MIN_AREA_M2 = 2500.0      # Cat 4 面积下限

# Backward-compat aliases (for v1 builder, tune tools, logs)
LANDMARK_HEIGHT_BOOST = 1.8             # max across categories
LANDMARK_HEIGHT_BOOST_CAP_MM = 6.0      # max across categories
LANDMARK_BUFFER_M = 5.0                 # min across categories
LANDMARK_EXCLUSION_BUFFER_M = 8.0       # fallback for empty categories list

# ---------------------------------------------------------------------------
# Overture Maps 高度注入开关
# ---------------------------------------------------------------------------
OVERTURE_ENABLED = True                 # [TOGGLE] 启用 Overture AI 高度注入
OVERTURE_CACHE_DIR = "data/height_cache"  # 离线 Parquet 缓存目录
OVERTURE_AUTO_DOWNLOAD = False          # 缓存未命中时是否自动下载
WIKIDATA_HEIGHT_ENABLED = True          # 读取 OSM wikidata 标记的地标高度缓存
WIKIDATA_HEIGHT_AUTO_FETCH = False      # 缺失 QID 是否访问 Wikibase API；默认离线

BUILDING_VERIFIED_HEIGHT_ONLY = True    # [TOGGLE] True=只保留有真实高度的建筑(osm_height/osm_levels/overture)，跳过默认10m推测

# Building hotspot 相关
BUILDING_V2_HOTSPOT_RELAX = 10.0                 # [TUNE] top X% 热点 block 内放宽 landmark 阈值
BUILDING_V2_MAX_BLOCK_AREA_M2 = 500000.0         # [TUNE] block_fill 过滤过大 block

# ---------------------------------------------------------------------------
# 精度过滤
# ---------------------------------------------------------------------------
NOZZLE_DIAM_MM = 0.4                             # [HW] Bambu Lab 0.4mm nozzle
MIN_PRINTABLE_AREA_M2 = 4000.0                   # [CALC] (nozzle/scale × 1.5)² ≈ (51m)² with margin

# ---------------------------------------------------------------------------
# Block base (E6 暖米色城市底层；对应 PNG layer 1.5 tessellation)
# ---------------------------------------------------------------------------
BLOCK_BASE_THICKNESS_MM = 0.5                    # [DESIGN] Z-texture displacement 的材料层
BLOCK_BASE_MIN_AREA_M2 = 1000.0                  # [DESIGN] 对齐 PNG layer 1.5 阈值

# ---------------------------------------------------------------------------
# WATERWAY 半宽  [TUNE] session_2026_05_17: 钱塘江/京杭运河 buffer 实测
# ---------------------------------------------------------------------------
WATERWAY_HALF_WIDTH = {
    "river": 90.0,
    "riverbank": 200.0,
    "canal": 25.0,
    "stream": 10.0,
    "drain": 6.0,
    "ditch": 4.0,
}

# ---------------------------------------------------------------------------
# Road constants
# ---------------------------------------------------------------------------
ROAD_WIDTH_MULTIPLIER = 5.0     # [TUNE] baseline 2.5→5.0 for chunkier grid; auto-param range 2.0-8.0
ROAD_FACE_NORMAL_Z_RATIO = 0.90 # [CONV] >=90% faces must point +Z (mesh quality check)
ROAD_DENSIFY_MAX_M = 10.0       # [CONV] max segment length before densifying
ROAD_MIN_LINE_LENGTH_M = 10.0   # [CONV] skip parking lot fragments
ROAD_DEFAULT_WIDTH_M = 6.0      # [CONV] = "residential" width fallback
ROAD_BRIDGE_EXTRA_MM = 0.20     # [DESIGN] 0.5x nozzle, visually float bridges above road

ROAD_WIDTHS = {                  # [CONV] real-world road widths (meters) by OSM highway tag
    "motorway": 16,
    "motorway_link": 10,
    "trunk": 14,
    "trunk_link": 8,
    "primary": 12,
    "primary_link": 7,
    "secondary": 8,
    "secondary_link": 6,
    "tertiary": 7,
    "tertiary_link": 5,
    "residential": 6,
    "living_street": 5,
    "service": 4,
    "unclassified": 6,
}

ROAD_FILTER = {                  # [DESIGN] LOD: >50km² only keep major roads to avoid clutter
    "small": None,
    "medium": None,
    "large": {"motorway", "motorway_link", "trunk", "trunk_link",
              "primary", "primary_link", "secondary", "secondary_link"},
}

# ---------------------------------------------------------------------------
# Water constants (base plate + water relief style)
# ---------------------------------------------------------------------------
WATER_HEIGHT_MODEL_MM = 100.0   # [DESIGN] water feature height above base (model meters)
WATER_BASE_THICKNESS_MM = 0.4   # [CONV] = 1x nozzle diameter, minimum printable plate
WATER_MIN_AREA_M2 = 50000.0     # [DESIGN] filter small ponds unprintable at 1:125K
WATER_DECIMATE_RATIO = 0.0
WATER_POLYGON_TAGS = {"natural": "water", "landuse": "reservoir"}  # [CONV] OSM tags
WATER_LINE_TAGS = {"waterway": True}
WATER_MAX_EDGE_M = 100.0        # [CONV] densification step

WATERWAY_WIDTHS = {              # [DESIGN] render widths for visual prominence at 1:125K
    "river": 500,     # major rivers (Yangtze-class: 500-2000m)
    "riverbank": 1000,
    "canal": 30,
    "stream": 12,
    "drain": 6,
    "ditch": 4,
}

# ---------------------------------------------------------------------------
# Vegetation constants
# ---------------------------------------------------------------------------
VEGETATION_COLOR = "#6B8E23"    # [REF] olive green from reference 3MF
VEGETATION_Z_OFFSET_MM = 0.1    # [DESIGN] slight elevation above terrain
VEGETATION_THICKNESS_MM = 0.2   # [CONV] 0.5x nozzle, thin overlay
VL_Z_OFFSET_MM = 0.15           # [DESIGN] VL 0.05mm > VO = 1/8 nozzle, visually near-flat
VO_Z_OFFSET_MM = 0.10           # [DESIGN] ordinary vegetation Z offset
VEGETATION_MIN_AREA_M2 = 5000.0 # [DESIGN] filter small fragments; may need adaptive (GEOS timeout risk)
VEGETATION_MAX_EDGE_M = 20.0    # [CONV] boundary densification step
VEGETATION_SIMPLIFY_TOL_M = 5.0 # [CONV] Douglas-Peucker tolerance

VEGETATION_TAGS = {              # [CONV] OSM tagging standard
    "landuse": ["forest", "grass", "meadow", "village_green"],
    "natural": ["wood", "grassland", "scrub", "heath"],
}
PARKS_TAGS = {
    "leisure": ["park", "garden", "nature_reserve"],
    "landuse": ["recreation_ground"],
}

# ---------------------------------------------------------------------------
# Area classification  [CONV] three-tier LOD system
# ---------------------------------------------------------------------------
AREA_SMALL_THRESHOLD = 5.0   # km^2
AREA_LARGE_THRESHOLD = 50.0  # km^2

# ---------------------------------------------------------------------------
# Terrain grid resolution  [DESIGN] resolution/performance tradeoff
# ---------------------------------------------------------------------------
TERRAIN_GRID = {
    "small": 512,
    "medium": 768,
    "large": 1024,
}

DECIMATION_TARGETS = {         # [DESIGN] face count caps for printable file sizes
    "small": None,
    "medium": 450_000,
    "large": 280_000,
}

ELEVATION_SMOOTHING_SIGMA = 2.5  # [TUNE] terrain3d uses 1.0; DeepSeek 2.5 for more smoothing

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_area_class(area_km2: float) -> str:
    """Classify area size for LOD decisions."""
    if area_km2 < AREA_SMALL_THRESHOLD:
        return "small"
    elif area_km2 < AREA_LARGE_THRESHOLD:
        return "medium"
    else:
        return "large"


def estimate_building_height_from_area(area_m2: float) -> float:
    """Estimate building height from footprint area when OSM has no height tag."""
    for threshold, height in sorted(BUILDING_AREA_HEIGHTS.items()):
        if area_m2 < threshold:
            return height
    return 60.0  # >2000 m^2 -> skyscraper
