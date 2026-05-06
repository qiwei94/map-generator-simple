"""Relief-style Z mapping for city relief sculpture models.

Provides per-element thickness mapping instead of global Z exaggeration:
  - Terrain: auto-calculated from elevation range, capped 3.5-5.0mm
  - Buildings: OSM height or area proxy, compressed to 3.0-5.3mm
  - Roads: fixed ridge height above terrain
  - Water: carved terrain level + thin solid
"""

import numpy as np

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.config import get_relief_config, estimate_building_height_from_area


def compute_terrain_z_mapping(z_min_m: float, z_max_m: float,
                              base_size_mm: float = 200.0) -> dict:
    """Compute terrain Z mapping from actual elevation to model thickness.

    The target thickness is proportional to actual elevation range relative
    to horizontal extent, capped between RELIEF_STYLE bounds (3.5-5.0mm).

    This matches the reference model approach: flat cities get amplified
    terrain relief (4.5-5.0mm), hilly cities get compressed (3.5-4.0mm).

    Args:
        z_min_m: minimum actual elevation (meters from SRTM)
        z_max_m: maximum actual elevation (meters from SRTM)
        base_size_mm: target longest base edge in mm (default 200)

    Returns:
        dict with keys:
            z_min_m, z_max_m: actual elevation range
            thickness_mm: target model thickness
            scale: multiplier to convert actual_m to model_mm
    """
    cfg = get_relief_config()
    z_range_m = z_max_m - z_min_m

    if z_range_m < 0.01:
        # Flat terrain: use midpoint of range
        thickness_mm = (cfg["terrain_z_min_mm"] + cfg["terrain_z_max_mm"]) / 2
    else:
        # Scale proportionally: actual z_range / 25000m (25km) gives aspect ratio
        # Target: terrain relief appears as ~2-3% of model width
        target_ratio = 0.025  # 2.5% of horizontal extent
        thickness_mm = base_size_mm * z_range_m / 25000.0 / target_ratio
        # Clamp to reference model range
        thickness_mm = max(cfg["terrain_z_min_mm"],
                          min(cfg["terrain_z_max_mm"], thickness_mm))

    scale = thickness_mm / z_range_m if z_range_m > 0.01 else 0.0

    return {
        "z_min_m": z_min_m,
        "z_max_m": z_max_m,
        "z_range_m": z_range_m,
        "thickness_mm": thickness_mm,
        "scale": scale,
    }


def map_terrain_z(vertices: np.ndarray, mapping: dict) -> np.ndarray:
    """Apply terrain Z mapping to vertex array.

    Converts actual elevation (meters) to model thickness (mm)
    using the precomputed mapping.

    Args:
        vertices: Nx3 array of vertex coordinates (meters)
        mapping: output from compute_terrain_z_mapping()

    Returns:
        Nx3 array with Z values replaced by model thickness (mm)
    """
    result = np.array(vertices, dtype=np.float64)
    z_min = mapping["z_min_m"]
    z_range = mapping["z_range_m"]

    if z_range > 0.01:
        result[:, 2] = (vertices[:, 2] - z_min) / z_range * mapping["thickness_mm"]
    else:
        result[:, 2] = mapping["thickness_mm"] / 2  # flat terrain

    return result


def compress_building_height(est_height_m: float, area_m2: float = 0) -> float:
    """Compress building height to relief model bump height.

    Uses OSM est_height if available (> 0), otherwise estimates from
    footprint area (Phase 0: <1% Chinese buildings have height tags).

    Args:
        est_height_m: OSM estimated height (meters), or 0 if unknown
        area_m2: building footprint area (m²), used as fallback

    Returns:
        Bump height in mm (3.0-5.3mm range)
    """
    cfg = get_relief_config()

    # Determine effective height
    if est_height_m > 0:
        effective_height = est_height_m
    else:
        effective_height = estimate_building_height_from_area(area_m2)

    # Linear compression to visual range
    h_min = cfg["building_height_osm_min_m"]
    h_max = cfg["building_height_osm_max_m"]
    m_min = cfg["building_height_min_mm"]
    m_max = cfg["building_height_max_mm"]

    compressed = m_min + (effective_height - h_min) / (h_max - h_min) * (m_max - m_min)
    return max(m_min, min(m_max, compressed))


def apply_relief_z_mapping(mesh, mapping: dict, element_type: str,
                           base_size_mm: float = 200.0) -> object:
    """Apply relief-style Z mapping to a mesh.

    For terrain: uses compute_terrain_z_mapping to scale elevation to
    target thickness (3.5-5.0mm).

    For buildings: already processed with compress_building_height at
    mesh generation time; this is a pass-through.

    For roads: Z = terrain Z + road_ridge_mm (applied at mesh generation).

    For water: Z = carved terrain Z (applied at mesh generation).

    Args:
        mesh: trimesh.Trimesh or None
        mapping: terrain Z mapping from compute_terrain_z_mapping()
        element_type: "terrain", "buildings", "roads", "water"
        base_size_mm: target base size for scaling

    Returns:
        Modified mesh (or same mesh for roads/water/buildings)
    """
    if mesh is None:
        return None

    if element_type == "terrain":
        mesh.vertices = map_terrain_z(mesh.vertices, mapping)

    # Other elements get their Z applied at mesh generation time,
    # not here. They just pass through.
    return mesh


def get_extruder_assignment(element_type: str) -> int:
    """Get extruder number for a mesh element type."""
    cfg = get_relief_config()
    return cfg["extruder_map"].get(element_type, 1)


def get_relief_color(element_type: str) -> str:
    """Get hex color for a mesh element type."""
    cfg = get_relief_config()
    return cfg["colors"].get(element_type, "#808080")
