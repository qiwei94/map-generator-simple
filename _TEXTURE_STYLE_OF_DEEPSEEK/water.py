"""Water processor — base plate + water relief (底板+水体浮雕).

Matches reference model obj_3: a full-area flat base plate with water
features (rivers, lakes) extruded upward as bas-relief on top.

Uses Manifold for guaranteed-watertight boolean union output.

Structure (model space):
    ┌──────────────────┐  ← water feature top (water_height above base)
    │  water features  │     (extruded upward)
    ├──────────────────┤  ← base plate top (Z=0, shared surface)
    │  base plate      │     (solid block covering full bbox)
    └──────────────────┘  ← base plate bottom (Z=-base_thickness)
"""

from __future__ import annotations

import numpy as np
import trimesh
import manifold3d
import geopandas as gpd

from _TEXTURE_STYLE_OF_DEEPSEEK._geom_utils import (
    collect_water_polygons,
    shapely_poly_to_crosssection,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.config import (
    WATER_HEIGHT_MODEL_MM,
    WATER_BASE_THICKNESS_MM,
    WATER_MAX_EDGE_M,
    Z_WATER_BASE_MM,
    Z_TERRAIN_BASE,
)


# ---------------------------------------------------------------------------
# Base plate
# ---------------------------------------------------------------------------


def _build_base_plate_manifold(
    x_min: float, y_min: float, x_max: float, y_max: float,
    thickness: float,
) -> manifold3d.Manifold:
    """Full-area base plate from (x_min, y_min) → (x_max, y_max), z=[-thickness, 0]."""
    cs = manifold3d.CrossSection.square((x_max - x_min, y_max - y_min))
    plate = cs.extrude(height=thickness)
    return plate.translate((x_min, y_min, -thickness))


def _extrude_water_manifold(poly, height: float) -> manifold3d.Manifold:
    """Extrude one water polygon. Returns empty Manifold on failure."""
    cs = shapely_poly_to_crosssection(poly)
    if cs.is_empty():
        return manifold3d.Manifold()
    try:
        return cs.extrude(height=height)
    except Exception:
        return manifold3d.Manifold()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_deepseek_water(gdf: gpd.GeoDataFrame,
                         bbox_x_min: float, bbox_y_min: float,
                         bbox_x_max: float, bbox_y_max: float,
                         scale: float = 1.0) -> trimesh.Trimesh:
    """Build base-plate + water-relief mesh (obj_3).

    Polygons come from :func:`collect_water_polygons` so this matches the
    cutter set used by :mod:`object4_terrain_with_holes` exactly.

    Args:
        gdf: GeoDataFrame of water features in local UTM meters.
        bbox_*: full bounding box for the base plate (local coords, meters).
        scale: mm-per-meter scale factor.

    Returns:
        Watertight trimesh of base plate + water features, or None.
    """
    if gdf is None or len(gdf) == 0:
        return None

    # Function-level import: allows runtime monkey-patch from auto-params
    from _TEXTURE_STYLE_OF_DEEPSEEK.config import WATER_MIN_AREA_M2

    # Convert config mm thickness to model meters (the Manifold space)
    base_thickness_m = WATER_BASE_THICKNESS_MM / scale if scale > 0 else 0.0
    water_height_m = WATER_HEIGHT_MODEL_MM  # name kept for backward compat (model meters)

    parts: list[manifold3d.Manifold] = [
        _build_base_plate_manifold(
            bbox_x_min, bbox_y_min, bbox_x_max, bbox_y_max, base_thickness_m,
        )
    ]

    polys = collect_water_polygons(
        gdf, min_area_m2=WATER_MIN_AREA_M2, max_edge_m=WATER_MAX_EDGE_M,
    )

    n_extruded = 0
    n_fail = 0
    for poly in polys:
        man = _extrude_water_manifold(poly, water_height_m)
        if man.is_empty():
            n_fail += 1
            continue
        parts.append(man)
        n_extruded += 1

    print(
        f"  Water features: {n_extruded} extruded from {len(polys)} polygons"
        + (f", {n_fail} Manifold failures" if n_fail else "")
    )

    # Union: base plate + all water features
    if len(parts) == 1:
        combined = parts[0]
    else:
        combined = manifold3d.Manifold.batch_boolean(parts, manifold3d.OpType.Add)

    if combined.is_empty():
        print("  ⚠ Manifold union produced empty result")
        return None

    mesh_data = combined.to_mesh()
    verts = np.array(mesh_data.vert_properties, dtype=np.float64)
    faces = np.array(mesh_data.tri_verts, dtype=np.int64)
    mesh = trimesh.Trimesh(vertices=verts, faces=faces, process=False)

    # Scale meters → mm, then anchor bottom at Z_WATER_BASE_MM
    mesh.vertices *= scale
    z_min = mesh.vertices[:, 2].min()
    mesh.vertices[:, 2] += Z_WATER_BASE_MM - z_min

    return mesh


# ---------------------------------------------------------------------------
# V3 builder: 接收 preprocess 输出的 WL/WO polygon，区分凹陷深度
# ---------------------------------------------------------------------------

def build_deepseek_water_v3(
    WL_polys: List[Polygon],
    WO_polys: List[Polygon],
    bbox_x_min: float, bbox_y_min: float,
    bbox_x_max: float, bbox_y_max: float,
    scale: float = 1.0,
    flat_only: bool = True,
) -> trimesh.Trimesh:
    """V3 water builder.

    Args:
        WL_polys: 水体地标 polygon（main rivers/lakes）
        WO_polys: 小水体 polygon
        bbox_*: full bounding box (local coords, meters)
        scale: mm-per-meter
        flat_only: True=只出平底板（水体形状由 terrain 镂空表达），
                   False=底板+水体凸起（旧行为）

    Returns:
        Watertight trimesh of base plate (+ optional water features), or None.
    """
    all_polys = list(WL_polys) + list(WO_polys)
    if not all_polys:
        return None

    n_wl = len(WL_polys)

    # Convert config mm thickness to model meters
    base_thickness_m = WATER_BASE_THICKNESS_MM / scale if scale > 0 else 0.0

    if flat_only:
        combined = _build_base_plate_manifold(
            bbox_x_min, bbox_y_min, bbox_x_max, bbox_y_max, base_thickness_m,
        )
        print(f"  Water(v3): flat base plate {WATER_BASE_THICKNESS_MM:.1f}mm, "
              f"{n_wl} WL + {len(WO_polys)} WO skipped")
    else:
        # Water feature heights in model meters (two levels)
        wl_height_m = 2.0 / scale if scale > 0 else 50.0
        wo_height_m = 1.5 / scale if scale > 0 else 37.5

        parts: list[manifold3d.Manifold] = [
            _build_base_plate_manifold(
                bbox_x_min, bbox_y_min, bbox_x_max, bbox_y_max, base_thickness_m,
            )
        ]

        n_extruded = 0
        n_fail = 0
        for i, poly in enumerate(all_polys):
            h = wl_height_m if i < n_wl else wo_height_m
            man = _extrude_water_manifold(poly, h)
            if man.is_empty():
                n_fail += 1
                continue
            parts.append(man)
            n_extruded += 1

        print(
            f"  Water(v3): {n_extruded} extruded ({n_wl} WL, {len(WO_polys)} WO)"
            + (f", {n_fail} failures" if n_fail else "")
        )

        if len(parts) == 1:
            combined = parts[0]
        else:
            combined = manifold3d.Manifold.batch_boolean(parts, manifold3d.OpType.Add)

    if combined.is_empty():
        print("  Water(v3): produced empty result")
        return None

    mesh_data = combined.to_mesh()
    verts = np.array(mesh_data.vert_properties, dtype=np.float64)
    faces = np.array(mesh_data.tri_verts, dtype=np.int64)
    mesh = trimesh.Trimesh(vertices=verts, faces=faces, process=False)

    # Scale meters → mm, then anchor bottom at Z_WATER_BASE_MM
    mesh.vertices *= scale
    z_min = mesh.vertices[:, 2].min()
    mesh.vertices[:, 2] += Z_WATER_BASE_MM - z_min

    return mesh
