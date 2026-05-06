"""Global configuration constants for terrain3d."""

import logging
import os

# Load .env file if present
_env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
if os.path.isfile(_env_path):
    with open(_env_path, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())
    del _env_path, _f, _line, _k, _v

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --- Cache ---
CACHE_BASE_DIR = os.path.join(PROJECT_ROOT, "cache")
CACHE_TTL_SECONDS = -1  # -1 = never expire; positive = TTL in seconds

# --- Multi-path Cache Support ---
# 多路径缓存配置，按优先级排序（第一个优先使用）
# 支持绝对路径和相对路径
CACHE_PATHS = [
    "F:/map_gen_cache/project_cache",  # F盘主缓存（优先使用，节省C盘空间）
    CACHE_BASE_DIR,  # C盘默认路径（向后兼容，F盘不可用时降级）
    # 其他可选路径：
    # "F:/map_gen_cache/attaraction/cache",  # 高速SSD缓存
    # "F:/map_gen_cache/city/cache",         # 大容量存储
]

# 缓存分配策略：
# "round_robin" - 轮询分配到各个路径
# "capacity_based" - 根据剩余空间分配（空间大的路径优先）
# "priority" - 严格按CACHE_PATHS顺序，第一个满了再用第二个
CACHE_ALLOCATION_STRATEGY = "priority"

# 最小剩余空间阈值（GB），低于此值不再写入该路径
CACHE_MIN_FREE_SPACE_GB = 1.0


def get_cache_paths() -> list:
    """获取有效的缓存路径列表（自动过滤不存在或不可写的路径）"""
    import os
    valid_paths = []
    for path in CACHE_PATHS:
        # 展开相对路径
        if not os.path.isabs(path):
            path = os.path.join(PROJECT_ROOT, path)
        
        # 检查路径是否存在且可写
        if os.path.exists(path) and os.access(path, os.W_OK):
            valid_paths.append(path)
        elif not os.path.exists(path):
            # 尝试创建目录
            try:
                os.makedirs(path, exist_ok=True)
                valid_paths.append(path)
            except OSError:
                logger.warning(f"无法创建缓存目录: {path}")
        else:
            logger.warning(f"缓存路径不可写: {path}")
    
    if not valid_paths:
        # 兜底：确保至少有一个可用路径
        fallback = os.path.join(PROJECT_ROOT, "cache")
        os.makedirs(fallback, exist_ok=True)
        valid_paths.append(fallback)
        logger.info(f"使用兜底缓存路径: {fallback}")
    
    return valid_paths


def select_cache_path(data_size_mb: float = 0) -> str:
    """根据策略选择合适的缓存路径
    
    Args:
        data_size_mb: 预估数据大小(MB)，用于容量规划
    
    Returns:
        选定的缓存路径
    """
    import shutil
    paths = get_cache_paths()
    
    if CACHE_ALLOCATION_STRATEGY == "priority":
        # 优先级模式：依次检查，返回第一个有足够空间的
        for path in paths:
            try:
                stat = shutil.disk_usage(path)
                free_gb = stat.free / (1024**3)
                if free_gb >= CACHE_MIN_FREE_SPACE_GB:
                    return path
            except OSError:
                continue
        # 如果都不够，返回第一个
        return paths[0] if paths else CACHE_BASE_DIR
        
    elif CACHE_ALLOCATION_STRATEGY == "capacity_based":
        # 容量模式：选择剩余空间最大的
        best_path = paths[0]
        max_free = 0
        for path in paths:
            try:
                stat = shutil.disk_usage(path)
                free_gb = stat.free / (1024**3)
                if free_gb > max_free:
                    max_free = free_gb
                    best_path = path
            except OSError:
                continue
        return best_path
        
    else:  # round_robin
        # 轮询模式：简单循环（需要维护状态）
        # 这里简化处理，仍返回第一个可用路径
        return paths[0] if paths else CACHE_BASE_DIR

# --- Terrain Grid Resolution by Area ---
# Higher resolution = finer, smoother terrain.
TERRAIN_GRID = {
    "small": 512,    # < 5 km²
    "medium": 768,   # 5-50 km²
    "large": 1024,   # > 50 km²
}

# --- Elevation smoothing (reduces blocky appearance from coarse DEM sources) ---
# Gaussian sigma in grid cells; 0 = disabled. Higher = smoother terrain (e.g. 2.5).
ELEVATION_SMOOTHING_SIGMA = 1.0

# Mesh decimation target face counts (higher = smoother terrain, larger files)
DECIMATION_TARGETS = {
    "small": None,       # no decimation for small areas
    "medium": 450_000,
    "large": 280_000,
}

# --- Building Defaults ---
BUILDING_DEFAULT_HEIGHT_M = 10.0
BUILDING_LEVEL_HEIGHT_M = 3.5

# Building LOD: min footprint area (m²) to include
BUILDING_MIN_AREA = {
    "small": 0,
    "medium": 20,
    "large": 50,
}

# Building LOD for 3D print: more aggressive filtering (tiny details won't print)
BUILDING_MIN_AREA_PRINT = {
    "small": 30,
    "medium": 50,
    "large": 100,
}

# Building simplification tolerance for print (meters)
BUILDING_SIMPLIFY_TOL_M = 2.0

# Water simplification for print: reduce triangulation density
# Larger value = fewer boundary vertices = cleaner triangulation (but longer edges)
# Smaller value = more vertices = breaks up radiating lines but may cause jagged edges
WATER_PRINT_VERTEX_SPACING_M = 50.0  # max spacing between water mesh boundary vertices

# --- Road Widths (meters) by OSM highway type ---
ROAD_WIDTHS = {
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

# Road LOD: highway types to include by area size
ROAD_FILTER = {
    "small": None,  # include all
    "medium": None,
    "large": {"motorway", "motorway_link", "trunk", "trunk_link",
              "primary", "primary_link", "secondary", "secondary_link"},
}

# --- Z-axis layer order (bottom to top): terrain < buildings < roads < water < vegetation < parks < wetlands ---
# All layers sit on terrain; offsets in meters define draw order (higher = on top, visible).
# Vegetation/parks/wetlands are ABOVE water to represent green areas higher than water surface.
LAYER_Z_OFFSET = {
    "terrain": 0.0,
    "buildings": 0.05,
    "roads": 0.15,
    "water": 0.25,
    "vegetation": 0.35,
    "parks": 0.38,
    "wetlands": 0.40,
    "pins": 0.45,
}
ROAD_Z_OFFSET = LAYER_Z_OFFSET["roads"]
BUILDING_Z_OFFSET = LAYER_Z_OFFSET["buildings"]

# --- Water ---
WATER_POLYGON_TAGS = {"natural": "water", "landuse": "reservoir"}
WATER_LINE_TAGS = {"waterway": True}
WATER_Z_OFFSET = LAYER_Z_OFFSET["water"]
# Chaikin smoothing for water boundaries/lines: default off; when on, iterations = smooth strength (1–4).
WATER_SMOOTH_CHAIKIN = False
WATER_SMOOTH_CHAIKIN_ITERATIONS = 2

# High-detail water: gentler simplification for lakes/islands (e.g. 三潭印月), fewer triangulation artifacts.
# Set via CLI --high-detail-water. When True: smaller OSM simplify tol; no/second simplify for polygons with holes; buffer(0) retry before Delaunay fallback.
WATER_HIGH_DETAIL = False

# Max allowed edge length (m) on water polygon boundary before triangulation.
# Edges exceeding this are densified so earcut won't generate spanning triangles.
# If edges still exceed this after densification, it likely indicates data loss.
WATER_MAX_EDGE_M = 100.0


def get_water_smooth_enabled() -> bool:
    return WATER_SMOOTH_CHAIKIN


def get_water_smooth_iterations() -> int:
    return max(1, min(4, WATER_SMOOTH_CHAIKIN_ITERATIONS))


def get_water_high_detail() -> bool:
    return WATER_HIGH_DETAIL


WATERWAY_WIDTHS = {
    "river": 60,
    "canal": 30,
    "stream": 12,
    "drain": 6,
    "ditch": 4,
}

# --- Colors (RGBA 0-255) — Distinct per layer for preview/GLB ---
COLORS = {
    "terrain_low": (200, 210, 170, 255),     # light green (low elevation)
    "terrain_high": (120, 95, 60, 255),      # brown (high elevation)
    "building": (255, 250, 240, 255),        # cream white
    "road": (100, 100, 100, 255),           # medium gray
    "water": (50, 120, 200, 255),           # blue
    "vegetation": (140, 220, 120, 255),     # 嫩绿 (natural plants)
    "parks": (140, 220, 120, 255),          # 嫩绿
    "wetlands": (140, 220, 120, 255),       # 嫩绿
    "base_wall": (90, 90, 90, 255),         # gray (terrain base sides + bottom)
    "pins": (255, 80, 60, 255),              # warm red-orange (hotspot markers)
}

# --- 3D Print Colors (RGBA 0-255) — High contrast, terrain clearly visible ---
# Terrain: warm stone gradient with good low/high separation.
# Water: clear lake blue so 西湖/rivers stand out in 3MF.
# Buildings: warm sandstone (distinct from terrain gray).
PRINT_COLORS = {
    "terrain_low":  (200, 180, 140, 255),   # warm sand (low elev)
    "terrain_high": (110, 90, 65, 255),     # dark brown (high elev)
    "building":     (245, 230, 200, 255),   # warm sandstone (distinct from terrain)
    "road":         (90, 90, 90, 255),      # dark gray (high contrast)
    "water":        (60, 150, 220, 255),    # clear lake blue (西湖 / rivers)
    "vegetation":   (120, 200, 100, 255),   # green (natural plants)
    "parks":        (120, 200, 100, 255),   # green (公园)
    "wetlands":     (120, 200, 100, 255),   # green (湿地)
    "base_wall":    (70, 65, 55, 255),      # dark brown (plinth, distinct from terrain)
    "pins":         (230, 65, 50, 255),     # warm red-orange (hotspot markers)
}

# --- 3D Print Base ---
PRINT_BASE_THICKNESS_M = 50.0  # model meters (~5mm at 1:10000 scale)

# --- 3D Print: layer separation (mm, applied after scaling) ---
# Each feature layer sits above the terrain surface by this amount so
# they don't intersect the terrain mesh.
PRINT_LAYER_HEIGHT_MM = 0.2

# --- Vegetation (forests, grass, meadow; parks & wetlands are separate) ---
VEGETATION_TAGS = {
    "landuse": ["forest", "grass", "meadow", "village_green"],
    "natural": ["wood", "grassland", "scrub", "heath"],
}
VEGETATION_Z_OFFSET = LAYER_Z_OFFSET["vegetation"]

# --- Parks & wetlands (标志性景色: 公园、湿地等) ---
PARKS_TAGS = {
    "leisure": ["park", "garden", "nature_reserve"],
    "landuse": ["recreation_ground"],
}
WETLANDS_TAGS = {"natural": ["wetland", "marsh", "swamp"]}
PARKS_Z_OFFSET = LAYER_Z_OFFSET["parks"]
WETLANDS_Z_OFFSET = LAYER_Z_OFFSET["wetlands"]
PARKS_MIN_AREA_M2 = 10.0
WETLANDS_MIN_AREA_M2 = 15.0

# --- Export ---
DEFAULT_OUTPUT_DIR = "output"
PREVIEW_SERVER_PORT = 8080

# --- Blender ---
BLENDER_PATH = r"C:\Program Files\blender-4.0.1\blender.exe"


def get_blender_path():
    """Return path to Blender executable, or None if not found."""
    import shutil
    path = os.environ.get("TERRAIN3D_BLENDER_PATH")
    if path and os.path.isfile(path):
        return path
    if os.path.isfile(BLENDER_PATH):
        return BLENDER_PATH
    found = shutil.which("blender")
    if found:
        return found
    return None

# --- Mesh build parallelism (buildings, roads, water, vegetation) ---
# 0 = auto (CPU count), 1 = sequential (no threads)
MESH_WORKERS = 0

# --- Low-poly / model quality (fewer triangles for roads & water, faster + smaller) ---
# "normal" = more detail; "low" = fewer triangles (roads/water smoother, good for models).
# GPU: current stack (trimesh/numpy/shapely) is CPU-only; use --low-poly to reduce work.
MESH_QUALITY = "normal"

# Road: max segment length when densifying centerline (m). Larger = fewer segments = fewer triangles.
ROAD_DENSIFY_MAX_SEGMENT_M = 10.0  # overridden to 25 when MESH_QUALITY == "low"
# Water polygon: simplify boundary when exterior points exceed this; tol = length / ratio.
WATER_POLYGON_SIMPLIFY_MAX_POINTS = 500
WATER_POLYGON_SIMPLIFY_TOL_RATIO = 1000  # overridden when MESH_QUALITY == "low"

# --- Hotspot Pins (photo density markers) ---
PIN_COLOR = (255, 80, 60, 255)   # warm red-orange for pins
PRINT_PIN_COLOR = (230, 65, 50, 255)   # slightly darker for print
PIN_DBSCAN_EPS_M = 120.0  # DBSCAN epsilon in meters
PIN_DBSCAN_MIN_SAMPLES = 2
PIN_CYLINDER_SEGMENTS = 16

# --- Style Templates (product-spec 4.1) ---
# Each template defines layer visibility and emphasis.
# Keys: include_buildings, include_roads, include_water, include_vegetation,
#        include_parks, include_wetlands, road_filter_override, description
STYLE_TEMPLATES = {
    "classic": {
        "description": "Balanced default: terrain + water + main roads + parks + hotspot pins",
        "include_buildings": False,
        "include_roads": True,
        "include_water": True,
        "include_vegetation": False,
        "include_parks": True,
        "include_wetlands": False,
        "road_filter_override": None,  # use area-class default
    },
    "water-first": {
        "description": "Emphasize water and coastline, fewer roads",
        "include_buildings": False,
        "include_roads": True,
        "include_water": True,
        "include_vegetation": False,
        "include_parks": False,
        "include_wetlands": True,
        "road_filter_override": {"motorway", "trunk", "primary"},
    },
    "terrain-first": {
        "description": "Emphasize terrain relief, minimal overlays",
        "include_buildings": False,
        "include_roads": True,
        "include_water": True,
        "include_vegetation": False,
        "include_parks": False,
        "include_wetlands": False,
        "road_filter_override": {"motorway", "trunk"},
    },
    "minimal": {
        "description": "Outline + water + pins only (fastest, safest)",
        "include_buildings": False,
        "include_roads": False,
        "include_water": True,
        "include_vegetation": False,
        "include_parks": False,
        "include_wetlands": False,
        "road_filter_override": set(),  # no roads
    },
}


def get_style_template(name: str) -> dict:
    """Get style template by name, default to 'classic'."""
    return STYLE_TEMPLATES.get(name, STYLE_TEMPLATES["classic"])


# --- Area thresholds (km²) ---
AREA_SMALL_THRESHOLD = 5.0
AREA_LARGE_THRESHOLD = 50.0

# --- Data noise filter (drop tiny/noisy features that hurt print quality) ---
# Minimum road segment length (m); segments shorter are dropped.
ROAD_MIN_LENGTH_M = 2.0
# Minimum water polygon area (m²); smaller are dropped.
WATER_MIN_AREA_M2 = 5.0


# --- Relief Style Pipeline ---
# Target: "city relief sculpture" style referencing 32 city 3MF models.
# Scale: 1:125,000 (25km x 25km -> ~200mm x 200mm)
RELIEF_STYLE = {
    # Terrain thickness: auto-calculated from actual elevation range,
    # capped between these bounds (matching reference model range 3.5-5.0mm)
    "terrain_z_min_mm": 3.5,
    "terrain_z_max_mm": 5.0,

    # Building height: hybrid OSM + area proxy, compressed to narrow range
    "building_height_mm": 4.5,         # default when no data available
    "building_height_min_mm": 3.0,     # min bump height (short buildings)
    "building_height_max_mm": 5.3,     # max bump height (tall buildings)
    "building_height_osm_min_m": 5.0,  # OSM height min for compression
    "building_height_osm_max_m": 150.0,  # OSM height max for compression

    # Building area proxy (used when OSM height tag is missing)
    "building_area_heights": {
        100: 8.0,     # <100m² → 8m (small residential)
        200: 10.0,    # 100-200m² → 10m (typical residential)
        500: 15.0,    # 200-500m² → 15m (commercial)
        1000: 25.0,   # 500-1000m² → 25m (office/complex)
        2000: 40.0,   # 1000-2000m² → 40m (tall building)
    },  # >2000m² → 60m (skyscraper/large complex)

    # Road ridge height above terrain surface (mm)
    "road_ridge_mm": 2.0,

    # Water: carved terrain level + solidified downward
    "water_thickness_mm": 1.5,  # matches reference models (1.52mm for Hangzhou)

    # 3-extruder mapping (E1=Terrain+Buildings, E2=Water, E3=Roads)
    "extruder_map": {
        "terrain": 1,
        "buildings": 1,  # merged with terrain on E1
        "roads": 3,
        "water": 2,
    },

    # Colors (for 3MF displaycolor — matches reference model palette)
    "colors": {
        "terrain": "#C8B48C",      # warm sand (low elevation terrain)
        "buildings": "#F5E6C8",    # warm sandstone (distinct from terrain)
        "roads": "#5A5A5A",        # dark gray (raised ridges)
        "water": "#3C96DC",        # lake blue (recessed water)
        "base_wall": "#464137",    # dark brown (plinth)
    },
}


def get_relief_config() -> dict:
    """Get relief style configuration."""
    return RELIEF_STYLE


def estimate_building_height_from_area(area_m2: float) -> float:
    """Estimate building height from footprint area when OSM has no height tag.

    Phase 0 data shows Chinese cities have <1% buildings with height tags,
    so area proxy is the primary height source for most buildings.
    """
    thresholds = RELIEF_STYLE["building_area_heights"]
    for threshold, height in sorted(thresholds.items()):
        if area_m2 < threshold:
            return height
    return 60.0  # >2000m² → skyscraper


def get_area_class(area_km2: float) -> str:
    """Classify area size for LOD decisions."""
    if area_km2 < AREA_SMALL_THRESHOLD:
        return "small"
    elif area_km2 < AREA_LARGE_THRESHOLD:
        return "medium"
    else:
        return "large"


def get_mesh_workers() -> int:
    """Number of workers for parallel mesh building (0 = auto)."""
    if MESH_WORKERS > 0:
        return MESH_WORKERS
    return min(8, (os.cpu_count() or 4))


def get_road_densify_segment_m() -> float:
    """Max segment length (m) when densifying road centerline. Larger = fewer triangles."""
    if MESH_QUALITY == "low":
        return 25.0
    return ROAD_DENSIFY_MAX_SEGMENT_M


def get_water_polygon_simplify_params():
    """(max_points, tol_ratio) for water polygon simplification before triangulation."""
    if MESH_QUALITY == "low":
        return 200, 300
    if get_water_high_detail():
        # Gentler simplify: keep more points so complex shapes (e.g. 三潭印月) don't get damaged.
        return 2000, 2500
    return WATER_POLYGON_SIMPLIFY_MAX_POINTS, WATER_POLYGON_SIMPLIFY_TOL_RATIO
