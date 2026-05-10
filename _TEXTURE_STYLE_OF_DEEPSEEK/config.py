"""All constants for _TEXTURE_STYLE_OF_DEEPSEEK — the single source of truth.

Values derived from reverse-engineering 33 Urban Series reference 3MF models.
"""

# ---------------------------------------------------------------------------
# Physical dimensions & scale
# ---------------------------------------------------------------------------
INTERNAL_SPAN_MM = 196.0       # full XY span of terrain+water+roads (model coords), fits 200mm plate with 2mm margin
BUILD_PLATE_MM = 200.0         # Bambu Lab build plate target


def compute_scale(width_m: float, height_m: float) -> float:
    """Compute the mm-per-meter scale factor to normalize area to INTERNAL_SPAN_MM.
    
    Ensures the model always fills ~254mm regardless of real-world extent.
    """
    return INTERNAL_SPAN_MM / max(width_m, height_m)

# ---------------------------------------------------------------------------
# 4-Extruder mapping (MATCHES REFERENCE MODELS + vegetation)
# ---------------------------------------------------------------------------
# Reference: E1=terrain+buildings (white PLA), E2=roads (gray PLA), E3=water (black PLA), E4=vegetation (green)
EXTRUDER_MAP = {
    "terrain": 1,
    "buildings": 1,
    "roads": 2,
    "water": 3,
    "vegetation": 4,
}

# Filament colours (actual print filament, from Bambu Studio metadata)
FILAMENT_COLOURS = ["#FFFFFF", "#8E9089", "#000000", "#6B8E23"]  # E1, E2, E3, E4

# ---------------------------------------------------------------------------
# Display colours (for 3MF basematerials displaycolor — Bambu Studio viewport)
# ---------------------------------------------------------------------------
TERRAIN_COLOR = "#C8B48C"      # warm sand
BUILDING_COLOR = "#F5E6C8"     # warm sandstone (visible against terrain)
ROAD_COLOR = "#5A5A5A"         # dark gray
WATER_COLOR = "#3C96DC"        # lake blue
BASE_WALL_COLOR = "#464137"    # dark brown (plinth)

# ---------------------------------------------------------------------------
# Z stacking (model mm, bottom-to-top)
# ---------------------------------------------------------------------------
Z_WATER_BASE_MM = -2.00        # water bottom on build plate
Z_TERRAIN_BASE = -0.17         # terrain bottom reference plane
Z_BUILDING_EMBED_MM = 0.04     # buildings embedded into terrain (matches reference: 0.04mm)
Z_ROAD_ABOVE_TERRAIN_MM = 0.51 # roads above terrain surface (mm)

# ---------------------------------------------------------------------------
# Per-layer thickness (model mm)
# ---------------------------------------------------------------------------
TERRAIN_THICKNESS_MM = 4.0     # watertight solid thickness (3.5-5.0mm range for city relief)
Z_GAMMA = 0.45                  # power-curve exponent: <1 boosts low relief (islands, shores)
BUILDING_HEIGHT_MM = 4.0       # default building bump height
BUILDING_HEIGHT_MIN_MM = 3.0   # minimum building bump
BUILDING_HEIGHT_MAX_MM = 5.3   # maximum building bump
ROAD_THICKNESS_MM = 0.4        # thin ribbon thickness
WATER_THICKNESS_MM = 0.5       # flat plate thickness

# ---------------------------------------------------------------------------
# Building height estimation
# ---------------------------------------------------------------------------
BUILDING_DEFAULT_HEIGHT_M = 10.0
BUILDING_LEVEL_HEIGHT_M = 3.5
BUILDING_HEIGHT_OSM_MIN_M = 5.0
BUILDING_HEIGHT_OSM_MAX_M = 150.0

# Area proxy heights (when OSM height tag is missing)
BUILDING_AREA_HEIGHTS = {
    100: 8.0,      # <100 m^2 -> 8m (small residential)
    200: 10.0,     # 100-200 m^2 -> 10m (typical residential)
    500: 15.0,     # 200-500 m^2 -> 15m (commercial)
    1000: 25.0,    # 500-1000 m^2 -> 25m (office/complex)
    2000: 40.0,    # 1000-2000 m^2 -> 40m (tall building)
}

# Building LOD (min footprint area m^2)
BUILDING_MIN_AREA = {
    "small": 30,
    "medium": 80,
    "large": 200,
}

BUILDING_SIMPLIFY_TOL_M = 2.0   # footprint simplification (Douglas-Peucker)

# ---------------------------------------------------------------------------
# Road constants
# ---------------------------------------------------------------------------
ROAD_WIDTH_MULTIPLIER = 2.5     # visual width amplification (matches reference)
ROAD_FACE_NORMAL_Z_RATIO = 0.90 # >=90% faces must point +Z
ROAD_DENSIFY_MAX_M = 10.0       # max segment length before densifying

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

ROAD_FILTER = {
    "small": None,
    "medium": None,
    "large": {"motorway", "motorway_link", "trunk", "trunk_link",
              "primary", "primary_link", "secondary", "secondary_link"},
}

# ---------------------------------------------------------------------------
# Water constants (base plate + water relief style)
# ---------------------------------------------------------------------------
WATER_HEIGHT_MODEL_MM = 100.0   # water feature height above base (model meters, ~1.0mm at 25km scale)
WATER_BASE_THICKNESS_MM = 0.4   # base plate thickness in mm
WATER_MIN_AREA_M2 = 50000.0     # min area for water features (m²) — filter small features
WATER_DECIMATE_RATIO = 0.0      # mesh decimation ratio (0.0 = none)
WATER_POLYGON_TAGS = {"natural": "water", "landuse": "reservoir"}
WATER_LINE_TAGS = {"waterway": True}
WATER_MAX_EDGE_M = 100.0

WATERWAY_WIDTHS = {
    "river": 500,     # Default width for rivers (major rivers can be 500-2000m)
    "riverbank": 1000, # Very wide river sections / estuaries
    "canal": 30,      # Canals and smaller waterways
    "stream": 12,
    "drain": 6,
    "ditch": 4,
}

# ---------------------------------------------------------------------------
# Vegetation constants
# ---------------------------------------------------------------------------
VEGETATION_COLOR = "#6B8E23"    # olive green (vegetation in Bambu Studio)
VEGETATION_Z_OFFSET_MM = 0.1    # slight elevation above terrain (mm)
VEGETATION_MIN_AREA_M2 = 5000.0 # minimum vegetation area (m^2) - increased to filter small fragments
VEGETATION_MAX_EDGE_M = 20.0    # max edge length for boundary densification (m)
VEGETATION_SIMPLIFY_TOL_M = 5.0 # simplification tolerance for Douglas-Peucker (m)

VEGETATION_TAGS = {
    "landuse": ["forest", "grass", "meadow", "village_green"],
    "natural": ["wood", "grassland", "scrub", "heath"],
}
PARKS_TAGS = {
    "leisure": ["park", "garden", "nature_reserve"],
    "landuse": ["recreation_ground"],
}

# ---------------------------------------------------------------------------
# Area classification
# ---------------------------------------------------------------------------
AREA_SMALL_THRESHOLD = 5.0   # km^2
AREA_LARGE_THRESHOLD = 50.0  # km^2

# ---------------------------------------------------------------------------
# Terrain grid resolution
# ---------------------------------------------------------------------------
TERRAIN_GRID = {
    "small": 512,
    "medium": 768,
    "large": 1024,
}

DECIMATION_TARGETS = {
    "small": None,
    "medium": 450_000,
    "large": 280_000,
}

ELEVATION_SMOOTHING_SIGMA = 2.5

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
