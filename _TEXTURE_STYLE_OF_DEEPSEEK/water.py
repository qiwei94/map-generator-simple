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


def prepare_deepseek_water_relief(
    terrain_mesh: trimesh.Trimesh,
    WL_polys,
    WO_polys,
    scale: float,
    *,
    base_thickness_mm: float,
    surface_thickness_mm: float,
) -> dict:
    """Recess terrain under printable water caps without a global boolean.

    The legacy formal pipeline stopped cutting water holes because the global
    Manifold subtraction destroyed terrain detail.  A flat E3 base plate then
    became completely hidden by the intact terrain.  This function preserves
    the terrain topology and only lowers top-surface vertices inside each
    selected WL/WO polygon.  The matching water builder places a closed,
    printable cap over the recessed surface.

    Polygon coordinates are local metres; terrain XY and all returned Z values
    are model millimetres.
    """
    all_polys = list(WL_polys) + list(WO_polys)
    if terrain_mesh is None or not all_polys:
        return {
            "surface_levels_mm": [],
            "carved_vertex_count": 0,
            "surface_thickness_mm": float(surface_thickness_mm),
        }
    if scale <= 0:
        raise ValueError("scale must be positive")
    if surface_thickness_mm <= 0:
        raise ValueError("surface thickness must be positive")

    import shapely
    from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import sample_deepseek_terrain_z

    terrain_base_z = Z_WATER_BASE_MM + float(base_thickness_mm)
    inset_mm = min(float(surface_thickness_mm) / 2.0, 0.12)
    verts = terrain_mesh.vertices
    levels: list[float] = []
    carved: set[int] = set()

    for poly in all_polys:
        if poly is None or poly.is_empty:
            levels.append(terrain_base_z + float(surface_thickness_mm))
            continue

        boundary = np.asarray(poly.exterior.coords, dtype=np.float64)
        if len(boundary) > 256:
            boundary = boundary[np.linspace(
                0, len(boundary) - 1, 256, dtype=np.int64)]
        boundary_z = sample_deepseek_terrain_z(
            terrain_mesh, boundary[:, 0] * scale, boundary[:, 1] * scale,
        )
        valid_z = boundary_z[np.isfinite(boundary_z)]
        if len(valid_z):
            water_top_z = float(np.percentile(valid_z, 25)) - inset_mm
        else:
            water_top_z = terrain_base_z + float(surface_thickness_mm)
        water_top_z = max(
            water_top_z,
            terrain_base_z + float(surface_thickness_mm),
        )
        levels.append(water_top_z)

        minx, miny, maxx, maxy = poly.bounds
        in_box = (
            (verts[:, 0] >= minx * scale) &
            (verts[:, 0] <= maxx * scale) &
            (verts[:, 1] >= miny * scale) &
            (verts[:, 1] <= maxy * scale) &
            (verts[:, 2] > terrain_base_z + 1e-6)
        )
        candidates = np.flatnonzero(in_box)
        if not len(candidates):
            continue
        points = shapely.points(
            verts[candidates, 0] / scale,
            verts[candidates, 1] / scale,
        )
        inside = shapely.covers(poly, points)
        carve_idx = candidates[np.asarray(inside, dtype=bool)]
        if not len(carve_idx):
            continue
        cap_bottom_z = max(
            terrain_base_z,
            water_top_z - float(surface_thickness_mm),
        )
        above = verts[carve_idx, 2] > cap_bottom_z
        carve_idx = carve_idx[above]
        if len(carve_idx):
            verts[carve_idx, 2] = cap_bottom_z
            carved.update(int(i) for i in carve_idx)

    terrain_mesh.vertices = verts
    print(
        f"  Water(v3): recessed {len(carved):,} terrain vertices for "
        f"{len(levels)} printable caps ({surface_thickness_mm:.2f}mm)"
    )
    return {
        "surface_levels_mm": levels,
        "carved_vertex_count": len(carved),
        "surface_thickness_mm": float(surface_thickness_mm),
    }


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
    base_thickness_mm: float | None = None,
    surface_levels_mm: list[float] | None = None,
    surface_thickness_mm: float = 0.24,
) -> trimesh.Trimesh:
    """V3 water builder.

    Args:
        WL_polys: 水体地标 polygon（main rivers/lakes）
        WO_polys: 小水体 polygon
        bbox_*: full bounding box (local coords, meters)
        scale: mm-per-meter
        flat_only: True=只出平底板（仅供兼容和故障回归测试），
                   False=底板+可打印水体壳层
        surface_levels_mm: each WL/WO cap top in model millimetres.  When
                   provided, caps are exactly ``surface_thickness_mm`` thick.

    Returns:
        Watertight trimesh of base plate (+ optional water features), or None.
    """
    all_polys = list(WL_polys) + list(WO_polys)
    if not all_polys:
        return None

    n_wl = len(WL_polys)

    # Function-level import keeps this value identical to the request used by
    # terrain and GLB preview, instead of a module-import-time snapshot.
    from _TEXTURE_STYLE_OF_DEEPSEEK.config import WATER_BASE_THICKNESS_MM
    resolved_base_mm = (WATER_BASE_THICKNESS_MM if base_thickness_mm is None
                        else float(base_thickness_mm))
    if resolved_base_mm < 0.4:
        raise ValueError("base thickness must be at least 0.4mm")
    base_thickness_m = resolved_base_mm / scale if scale > 0 else 0.0

    if flat_only:
        combined = _build_base_plate_manifold(
            bbox_x_min, bbox_y_min, bbox_x_max, bbox_y_max, base_thickness_m,
        )
        print(f"  Water(v3): flat base plate {resolved_base_mm:.1f}mm, "
              f"{n_wl} WL + {len(WO_polys)} WO skipped")
    else:
        if scale <= 0:
            raise ValueError("scale must be positive")
        if surface_thickness_mm <= 0:
            raise ValueError("surface thickness must be positive")
        if surface_levels_mm is not None and len(surface_levels_mm) != len(all_polys):
            raise ValueError("surface level count must match WL + WO polygons")

        terrain_base_z = Z_WATER_BASE_MM + resolved_base_mm

        parts: list[manifold3d.Manifold] = [
            _build_base_plate_manifold(
                bbox_x_min, bbox_y_min, bbox_x_max, bbox_y_max, base_thickness_m,
            )
        ]

        n_extruded = 0
        n_fail = 0
        for i, poly in enumerate(all_polys):
            if surface_levels_mm is None:
                # Compatibility fallback for callers without terrain-relative
                # levels.  Formal pipelines always provide explicit levels.
                h_mm = 2.0 if i < n_wl else 1.5
                z_bottom_mm = terrain_base_z
            else:
                h_mm = float(surface_thickness_mm)
                z_bottom_mm = max(
                    terrain_base_z,
                    float(surface_levels_mm[i]) - h_mm,
                )
            h = h_mm / scale
            man = _extrude_water_manifold(poly, h)
            if man.is_empty():
                n_fail += 1
                continue
            if z_bottom_mm != terrain_base_z:
                man = man.translate((0.0, 0.0,
                                     (z_bottom_mm - terrain_base_z) / scale))
            parts.append(man)
            n_extruded += 1

        print(
            f"  Water(v3): {n_extruded} printable caps "
            f"({n_wl} WL, {len(WO_polys)} WO, {surface_thickness_mm:.2f}mm)"
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
