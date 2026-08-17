"""City feature detection from OSM GeoDataFrames and elevation data.

Detects terrain relief, water coverage, building density, road density,
vegetation ratio, coastal presence, and OSM data quality to build a
CityProfile that drives parameter selection.

CityProfile uses Pydantic V2 for strict type validation and documentation
of each spatial dimension field.
"""
from __future__ import annotations

import numpy as np
from pydantic import BaseModel, Field
from typing import Literal

try:
    import geopandas as gpd
except ImportError:
    gpd = None


class CityProfile(BaseModel):
    """Feature vector describing a city's spatial characteristics.

    All 11 dimensions are validated at construction time via Pydantic V2.
    """

    area_km2: float = Field(
        ge=0, description="Bounding-box area in square kilometers."
    )
    elevation_range_m: float = Field(
        ge=0, description="Elevation range (max - min) in meters."
    )
    relief_ratio: Literal["flat", "moderate", "mountainous"] = Field(
        description="Terrain relief classification based on elevation_range / bbox_diagonal."
    )
    water_ratio: float = Field(
        ge=0, le=1,
        description="Fraction of bounding-box area covered by water polygons (0..1).",
    )
    building_density: float = Field(
        ge=0, description="Number of building footprints per km²."
    )
    avg_building_area_m2: float = Field(
        ge=0, description="Mean 2D area of building footprints in m²."
    )
    height_tag_coverage: float = Field(
        ge=0, le=1,
        description="Fraction of buildings with explicit height or building:levels tag (0..1).",
    )
    road_density_km_per_km2: float = Field(
        ge=0, description="Total road length (km) per km² of area."
    )
    vegetation_ratio: float = Field(
        ge=0, le=1,
        description="Fraction of bounding-box area covered by vegetation polygons (0..1).",
    )
    is_coastal: bool = Field(
        description="True if any feature in water_gdf has natural=coastline tag."
    )
    osm_quality: Literal["poor", "fair", "good"] = Field(
        description="Assessed OSM data quality based on building/road density and height coverage."
    )

    def to_dict(self) -> dict:
        """Serialize to a plain Python dict (Pydantic V2 model_dump)."""
        return self.model_dump()


# ─── Relief thresholds ───────────────────────────────────────────────
_RELIEF_FLAT_THRESHOLD = 0.01
_RELIEF_MOUNTAINOUS_THRESHOLD = 0.05

# ─── OSM quality thresholds ──────────────────────────────────────────
_QUALITY_GOOD_BUILDING_DENSITY = 500     # buildings/km²
_QUALITY_GOOD_HEIGHT_COVERAGE = 0.30
_QUALITY_GOOD_ROAD_DENSITY = 8.0         # km/km²


def detect_city_profile(
    bbox_area_km2: float,
    elevation_grid: np.ndarray,
    buildings_gdf,
    roads_gdf,
    water_gdf,
    vegetation_gdf,
    bbox_local_area_m2: float,
) -> CityProfile:
    """Detect city features from preprocessed geodata.

    All GeoDataFrames should be in local UTM coordinates (meters).
    bbox_local_area_m2 is the bounding box area in m².
    """
    area_km2 = bbox_area_km2

    # ── Elevation / Relief ──
    elevation_range_m, relief_ratio_str = _detect_relief(
        elevation_grid, bbox_local_area_m2
    )

    # ── Water coverage ──
    water_ratio = _detect_water_ratio(water_gdf, bbox_local_area_m2)

    # ── Building metrics ──
    building_density, avg_building_area, height_coverage = _detect_building_metrics(
        buildings_gdf, area_km2
    )

    # ── Road density ──
    road_density = _detect_road_density(roads_gdf, area_km2)

    # ── Vegetation ──
    vegetation_ratio = _detect_vegetation_ratio(vegetation_gdf, bbox_local_area_m2)

    # ── Coastal detection ──
    is_coastal = _detect_coastal(water_gdf)

    # ── OSM quality ──
    osm_quality = _assess_osm_quality(
        building_density, height_coverage, road_density
    )

    return CityProfile(
        area_km2=area_km2,
        elevation_range_m=elevation_range_m,
        relief_ratio=relief_ratio_str,
        water_ratio=water_ratio,
        building_density=building_density,
        avg_building_area_m2=avg_building_area,
        height_tag_coverage=height_coverage,
        road_density_km_per_km2=road_density,
        vegetation_ratio=vegetation_ratio,
        is_coastal=is_coastal,
        osm_quality=osm_quality,
    )


def _detect_relief(
    elevation_grid: np.ndarray, bbox_area_m2: float
) -> tuple[float, str]:
    """Compute elevation range and classify relief."""
    if elevation_grid is None or elevation_grid.size == 0:
        return 0.0, "flat"

    elev_min = float(np.nanmin(elevation_grid))
    elev_max = float(np.nanmax(elevation_grid))
    elevation_range = elev_max - elev_min

    bbox_diagonal_m = (bbox_area_m2 ** 0.5) * (2 ** 0.5)
    if bbox_diagonal_m < 1.0:
        return elevation_range, "flat"

    ratio = elevation_range / bbox_diagonal_m

    if ratio >= _RELIEF_MOUNTAINOUS_THRESHOLD:
        relief = "mountainous"
    elif ratio >= _RELIEF_FLAT_THRESHOLD:
        relief = "moderate"
    else:
        relief = "flat"

    return elevation_range, relief


def _detect_water_ratio(water_gdf, bbox_area_m2: float) -> float:
    """Compute fraction of bbox covered by water polygons."""
    if water_gdf is None or len(water_gdf) == 0 or bbox_area_m2 <= 0:
        return 0.0

    polygon_mask = water_gdf.geometry.geom_type.isin(
        ["Polygon", "MultiPolygon"]
    )
    if not polygon_mask.any():
        return 0.0

    # Multiple sources can overlap (OSM + secondary imagery). Summing areas
    # double-counts those overlaps and can turn a lake into >100% coverage.
    from shapely.ops import unary_union
    geoms = [g for g in water_gdf.loc[polygon_mask, "geometry"]
             if g is not None and not g.is_empty]
    water_area = unary_union(geoms).area if geoms else 0.0
    return min(1.0, water_area / bbox_area_m2)


def _detect_building_metrics(
    buildings_gdf, area_km2: float
) -> tuple[float, float, float]:
    """Compute building density, avg area, and height tag coverage."""
    if buildings_gdf is None or len(buildings_gdf) == 0:
        return 0.0, 0.0, 0.0

    polygon_mask = buildings_gdf.geometry.geom_type.isin(
        ["Polygon", "MultiPolygon"]
    )
    building_polys = buildings_gdf[polygon_mask]
    n_buildings = len(building_polys)

    if n_buildings == 0:
        return 0.0, 0.0, 0.0

    density = n_buildings / max(area_km2, 0.01)
    avg_area = float(building_polys.geometry.area.mean())

    # Height coverage: 优先看富化后的 est_height（与 preprocess 的
    # assess_height_data_quality 口径一致）；无 est_height 时回退原始 OSM tag。
    height_coverage = 0.0
    if "est_height" in building_polys.columns:
        from _TEXTURE_STYLE_OF_DEEPSEEK.config import BUILDING_DEFAULT_HEIGHT_M
        est = building_polys["est_height"]
        has_real = est.notna() & (est != BUILDING_DEFAULT_HEIGHT_M) & (est > 0)
        height_coverage = float(has_real.sum() / n_buildings)
    elif "height" in building_polys.columns:
        has_height = building_polys["height"].notna() & (
            building_polys["height"] != ""
        )
        height_coverage = has_height.sum() / n_buildings
    elif "building:levels" in building_polys.columns:
        has_levels = building_polys["building:levels"].notna() & (
            building_polys["building:levels"] != ""
        )
        height_coverage = has_levels.sum() / n_buildings

    return density, avg_area, height_coverage


def _detect_road_density(roads_gdf, area_km2: float) -> float:
    """Compute total road length (km) per km²."""
    if roads_gdf is None or len(roads_gdf) == 0:
        return 0.0

    line_mask = roads_gdf.geometry.geom_type.isin(
        ["LineString", "MultiLineString"]
    )
    if not line_mask.any():
        return 0.0

    total_length_m = roads_gdf.loc[line_mask, "geometry"].length.sum()
    total_length_km = total_length_m / 1000.0
    return total_length_km / max(area_km2, 0.01)


def _detect_vegetation_ratio(vegetation_gdf, bbox_area_m2: float) -> float:
    """Compute fraction of bbox covered by vegetation."""
    if vegetation_gdf is None or len(vegetation_gdf) == 0 or bbox_area_m2 <= 0:
        return 0.0

    polygon_mask = vegetation_gdf.geometry.geom_type.isin(
        ["Polygon", "MultiPolygon"]
    )
    if not polygon_mask.any():
        return 0.0

    veg_area = vegetation_gdf.loc[polygon_mask, "geometry"].area.sum()
    return min(1.0, veg_area / bbox_area_m2)


def _detect_coastal(water_gdf) -> bool:
    """Check if any feature has natural=coastline tag."""
    if water_gdf is None or len(water_gdf) == 0:
        return False

    if "natural" in water_gdf.columns:
        return (water_gdf["natural"] == "coastline").any()

    return False


def _assess_osm_quality(
    building_density: float,
    height_coverage: float,
    road_density: float,
) -> str:
    """Assess overall OSM data quality for this area."""
    score = 0

    if building_density >= _QUALITY_GOOD_BUILDING_DENSITY:
        score += 1
    elif building_density >= _QUALITY_GOOD_BUILDING_DENSITY * 0.3:
        score += 0.5

    if height_coverage >= _QUALITY_GOOD_HEIGHT_COVERAGE:
        score += 1
    elif height_coverage >= _QUALITY_GOOD_HEIGHT_COVERAGE * 0.5:
        score += 0.5

    if road_density >= _QUALITY_GOOD_ROAD_DENSITY:
        score += 1
    elif road_density >= _QUALITY_GOOD_ROAD_DENSITY * 0.5:
        score += 0.5

    if score >= 2.5:
        return "good"
    elif score >= 1.5:
        return "fair"
    return "poor"
