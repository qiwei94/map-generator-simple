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

from typing import Iterable, List

import numpy as np
import trimesh
import manifold3d
import geopandas as gpd
from shapely.affinity import scale as scale_geometry
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, box

from _TEXTURE_STYLE_OF_DEEPSEEK._geom_utils import (
    collect_water_polygons,
    shapely_poly_to_crosssection,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.config import (
    WATER_HEIGHT_MODEL_MM,
    WATER_BASE_THICKNESS_MM,
    WATER_MIN_AREA_M2,
    WATER_MAX_EDGE_M,
    Z_WATER_BASE_MM,
    Z_TERRAIN_BASE,
    WATER_OVERLAY_TOP_MM,
    WATER_OVERLAY_EMBED_MM,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.terrain import sample_terrain_z


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


def _polygon_parts(geom) -> Iterable[Polygon]:
    """Yield non-empty Polygon components from a Shapely result."""
    if geom is None or geom.is_empty:
        return
    if isinstance(geom, Polygon):
        yield geom
    elif isinstance(geom, (MultiPolygon, GeometryCollection)):
        for part in geom.geoms:
            yield from _polygon_parts(part)


def _draped_water_mesh(
    polygon: Polygon,
    terrain_mesh: trimesh.Trimesh,
    scale: float,
    top_offset_mm: float,
    embed_mm: float,
) -> trimesh.Trimesh | None:
    """Create a watertight water overlay following the terrain triangles.

    Water input coordinates are local metres, while terrain vertices are model
    millimetres.  The overlay is clipped against the regular terrain grid, so
    every top face uses the same interpolation and diagonal as the terrain.
    This avoids a lake surface cutting through hills (or vanishing below them)
    without using a destructive boolean on the entire terrain solid.
    """
    if scale <= 0:
        return None

    sampler = terrain_mesh.metadata.get("_regular_grid_sampler")
    if sampler is None:
        return None

    z_grid = np.asarray(sampler.get("z_grid"), dtype=np.float64)
    if z_grid.ndim != 2 or min(z_grid.shape) < 2:
        return None

    rows, cols = z_grid.shape
    x_min, x_max = float(sampler["x_min"]), float(sampler["x_max"])
    y_min, y_max = float(sampler["y_min"]), float(sampler["y_max"])
    dx = (x_max - x_min) / (cols - 1)
    dy = (y_max - y_min) / (rows - 1)
    if dx <= 0 or dy <= 0:
        return None

    # Scale only XY.  The source geometries are in metres; the sampler uses mm.
    poly_mm = scale_geometry(polygon, xfact=scale, yfact=scale, origin=(0, 0))
    poly_mm = poly_mm.intersection(box(x_min, y_min, x_max, y_max))
    if poly_mm.is_empty:
        return None

    min_x, min_y, max_x, max_y = poly_mm.bounds
    col_start = max(0, int(np.floor((min_x - x_min) / dx)))
    col_stop = min(cols - 2, int(np.ceil((max_x - x_min) / dx)) - 1)
    row_start = max(0, int(np.floor((min_y - y_min) / dy)))
    row_stop = min(rows - 2, int(np.ceil((max_y - y_min) / dy)) - 1)
    if col_stop < col_start or row_stop < row_start:
        return None

    vertices: list[np.ndarray] = []
    faces: list[np.ndarray] = []
    vertex_count = 0
    for row in range(row_start, row_stop + 1):
        y0 = y_min + row * dy
        y1 = y0 + dy
        for col in range(col_start, col_stop + 1):
            x0 = x_min + col * dx
            x1 = x0 + dx
            # Match ``_generate_grid_faces`` exactly: SW-NW-SE, SE-NW-NE.
            terrain_triangles = (
                Polygon(((x0, y0), (x0, y1), (x1, y0))),
                Polygon(((x1, y0), (x0, y1), (x1, y1))),
            )
            for terrain_triangle in terrain_triangles:
                if poly_mm.covers(terrain_triangle):
                    clipped = terrain_triangle
                else:
                    clipped = poly_mm.intersection(terrain_triangle)

                for part in _polygon_parts(clipped):
                    if part.area <= 1e-10:
                        continue
                    try:
                        xy, tri_faces = trimesh.creation.triangulate_polygon(
                            part, engine="earcut",
                        )
                    except Exception:
                        continue
                    if len(tri_faces) == 0:
                        continue
                    z = sample_terrain_z(terrain_mesh, xy[:, 0], xy[:, 1])
                    if not np.all(np.isfinite(z)):
                        continue
                    vertices.append(np.column_stack((xy, z + top_offset_mm)))
                    faces.append(np.asarray(tri_faces, dtype=np.int64) + vertex_count)
                    vertex_count += len(xy)

    if not vertices:
        return None

    top = trimesh.Trimesh(
        vertices=np.vstack(vertices), faces=np.vstack(faces), process=False,
    )
    top.merge_vertices()
    top.update_faces(top.unique_faces())
    top.remove_unreferenced_vertices()
    trimesh.repair.fix_normals(top, multibody=True)

    # Duplicate the terrain-conforming top below the terrain surface.  The
    # boundary of the triangulated patch becomes the vertical side wall.
    n_vertices = len(top.vertices)
    lower_vertices = top.vertices.copy()
    lower_vertices[:, 2] -= top_offset_mm + embed_mm

    directed_edges = np.vstack((
        top.faces[:, [0, 1]], top.faces[:, [1, 2]], top.faces[:, [2, 0]],
    ))
    sorted_edges = np.sort(directed_edges, axis=1)
    _, inverse, counts = np.unique(
        sorted_edges, axis=0, return_inverse=True, return_counts=True,
    )
    boundary_edges = directed_edges[counts[inverse] == 1]
    if len(boundary_edges) == 0:
        return None

    a, b = boundary_edges[:, 0], boundary_edges[:, 1]
    wall_faces = np.vstack((
        np.column_stack((a, a + n_vertices, b)),
        np.column_stack((b, a + n_vertices, b + n_vertices)),
    ))
    mesh = trimesh.Trimesh(
        vertices=np.vstack((top.vertices, lower_vertices)),
        faces=np.vstack((top.faces, top.faces[:, ::-1] + n_vertices, wall_faces)),
        process=False,
    )
    trimesh.repair.fix_normals(mesh, multibody=True)
    return mesh if mesh.is_watertight else None

def build_deepseek_water_v3(
    WL_polys: List[Polygon],
    WO_polys: List[Polygon],
    bbox_x_min: float, bbox_y_min: float,
    bbox_x_max: float, bbox_y_max: float,
    scale: float = 1.0,
    flat_only: bool = False,
    terrain_mesh: trimesh.Trimesh | None = None,
) -> trimesh.Trimesh:
    """V3 water builder.

    Args:
        WL_polys: 水体地标 polygon（main rivers/lakes）
        WO_polys: 小水体 polygon
        bbox_*: full bounding box (local coords, meters)
        scale: mm-per-meter
        flat_only: True=only emit the legacy flat base plate.  Kept for
                   compatibility; it is not suitable when terrain has no
                   water holes.
        terrain_mesh: terrain solid carrying the regular-grid sampler.  When
                   supplied, emit terrain-conforming water overlays instead
                   of the legacy full-area base plate.

    Returns:
        Watertight trimesh of base plate (+ optional water features), or None.
    """
    all_polys = list(WL_polys) + list(WO_polys)
    if not all_polys:
        return None

    n_wl = len(WL_polys)

    if terrain_mesh is not None and not flat_only:
        parts: list[trimesh.Trimesh] = []
        failed = 0
        for poly in WL_polys:
            mesh = _draped_water_mesh(
                poly, terrain_mesh, scale,
                top_offset_mm=WATER_OVERLAY_TOP_MM,
                embed_mm=WATER_OVERLAY_EMBED_MM,
            )
            if mesh is None:
                failed += 1
            else:
                parts.append(mesh)
        for poly in WO_polys:
            mesh = _draped_water_mesh(
                poly, terrain_mesh, scale,
                top_offset_mm=WATER_OVERLAY_TOP_MM * 0.75,
                embed_mm=WATER_OVERLAY_EMBED_MM,
            )
            if mesh is None:
                failed += 1
            else:
                parts.append(mesh)

        if not parts:
            print("  Water(v3): no terrain-conforming overlays generated")
            return None
        result = trimesh.util.concatenate(parts)
        print(
            f"  Water(v3): {len(parts)} terrain-conforming overlays "
            f"({n_wl} WL, {len(WO_polys)} WO)"
            + (f", {failed} failures" if failed else "")
        )
        return result

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
