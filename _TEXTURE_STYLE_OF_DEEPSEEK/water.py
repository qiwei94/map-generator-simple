"""Water processor — base plate + water relief (底板+水体浮雕).

Matching reference model obj_3: a full-area flat base plate with water
features (rivers, lakes) extruded upward as bas-relief on top.

Uses Manifold library for guaranteed-watertight boolean union output.

Structure (model space):
    ┌──────────────────┐  ← water feature top (water_height above base)
    │  water features  │     (extruded upward)
    ├──────────────────┤  ← base plate top (Z=0, shared surface)
    │  base plate      │     (solid block covering full bbox)
    └──────────────────┘  ← base plate bottom (Z=-base_thickness)
"""

import numpy as np
import trimesh
import manifold3d
from shapely.geometry import Polygon, MultiPolygon, LineString, MultiLineString
from shapely.ops import unary_union
import geopandas as gpd

from _TEXTURE_STYLE_OF_DEEPSEEK.config import (
    WATER_HEIGHT_MODEL_MM,
    WATER_BASE_THICKNESS_MM,
    WATER_MIN_AREA_M2,
    WATER_MAX_EDGE_M,
    WATERWAY_WIDTHS,
    WATER_COLOR,
    Z_WATER_BASE_MM,
)

# =====================================================================
#  Winding helpers
#  Manifold CrossSection(FillRule.Positive) expects exterior → CCW,
#  holes → CW.  OSM data often has CW exteriors, so we normalise.
# =====================================================================


def _signed_area_2d(contour: np.ndarray) -> float:
    """Signed area of a 2D closed contour (positive = CCW)."""
    x = contour[:, 0]
    y = contour[:, 1]
    return 0.5 * float(np.sum(x[:-1] * y[1:] - x[1:] * y[:-1]))


def _ensure_ccw(contour: np.ndarray) -> np.ndarray:
    """Reverse contour if it is clockwise."""
    if _signed_area_2d(contour) < 0:
        return contour[::-1]
    return contour


def _ensure_cw(contour: np.ndarray) -> np.ndarray:
    """Reverse contour if it is counter-clockwise."""
    if _signed_area_2d(contour) > 0:
        return contour[::-1]
    return contour


def _shapely_poly_to_crosssection(poly: Polygon) -> manifold3d.CrossSection:
    """Convert a Shapely Polygon to a Manifold CrossSection.

    Normalises winding so exterior is CCW and holes are CW,
    which is what CrossSection(FillRule.Positive) expects.

    Returns empty CrossSection on failure.
    """
    if poly.is_empty or len(poly.exterior.coords) < 4:
        return manifold3d.CrossSection()

    try:
        # Exterior ring (must be closed: first == last)
        exterior = np.array(poly.exterior.coords, dtype=np.float64)
        if exterior.shape[1] >= 3:
            exterior = exterior[:, :2]
        exterior = _ensure_ccw(exterior)

        contours = [exterior]

        # Interior rings (holes) — must be CW for FillRule.Positive
        for interior in poly.interiors:
            hole = np.array(interior.coords, dtype=np.float64)
            if hole.shape[1] >= 3:
                hole = hole[:, :2]
            if len(hole) >= 3:
                hole = _ensure_cw(hole)
                contours.append(hole)

        return manifold3d.CrossSection(contours)
    except Exception:
        return manifold3d.CrossSection()


def _densify_polygon(poly: Polygon, max_edge_m: float) -> Polygon:
    """Densify polygon exterior boundary (preserves holes as-is)."""
    boundary = np.array(poly.exterior.coords)
    dense = _densify_ring(boundary, max_edge_m)
    try:
        return Polygon(dense, holes=[np.array(h.coords) for h in poly.interiors])
    except Exception:
        return poly


# =====================================================================
#  Geometry helpers (unchanged from original)
# =====================================================================


def _densify_ring(coords: np.ndarray, max_edge_m: float) -> np.ndarray:
    """Insert vertices so no edge exceeds max_edge_m (meters)."""
    if len(coords) < 2:
        return coords

    result = [coords[0]]
    for i in range(1, len(coords)):
        p0 = coords[i - 1]
        p1 = coords[i]
        seg_len = np.linalg.norm(p1[:2] - p0[:2])
        if seg_len > max_edge_m:
            n_splits = int(np.ceil(seg_len / max_edge_m))
            for j in range(1, n_splits + 1):
                t = j / n_splits
                result.append(p0 + t * (p1 - p0))
        else:
            result.append(p1)

    # Ensure closure
    if np.linalg.norm(result[-1][:2] - result[0][:2]) > 1e-10:
        result.append(result[0])
    return np.array(result)


def _build_water_line(line: LineString, width_m: float) -> Polygon:
    """Convert a water line to a buffered polygon."""
    buffered = line.buffer(width_m / 2, cap_style=2, join_style=2)
    if isinstance(buffered, MultiPolygon):
        buffered = unary_union(buffered)
    if isinstance(buffered, Polygon):
        return buffered
    return None


# =====================================================================
#  Main entry point
# =====================================================================


def _extrude_water_manifold(poly: Polygon, height: float) -> manifold3d.Manifold:
    """Extrude a single water polygon using Manifold.

    Returns empty Manifold on failure (caller should skip).
    """
    cs = _shapely_poly_to_crosssection(poly)
    if cs.is_empty():
        return manifold3d.Manifold()
    try:
        return cs.extrude(height=height)
    except Exception:
        return manifold3d.Manifold()


def _build_base_plate_manifold(
    x_min: float, y_min: float, x_max: float, y_max: float,
    thickness: float,
) -> manifold3d.Manifold:
    """Build the full-area base plate as a Manifold solid.

    The plate spans (x_min, y_min) → (x_max, y_max) in XY,
    and extends from z=-thickness to z=0.
    """
    cs = manifold3d.CrossSection.square((x_max - x_min, y_max - y_min))
    plate = cs.extrude(height=thickness)
    plate = plate.translate((x_min, y_min, -thickness))
    return plate


def build_deepseek_water(gdf: gpd.GeoDataFrame,
                         bbox_x_min: float, bbox_y_min: float,
                         bbox_x_max: float, bbox_y_max: float,
                         scale: float = 1.0) -> trimesh.Trimesh:
    """Build deepseek-style water plate: full-area base + extruded water features.

    Uses Manifold boolean union for guaranteed watertight output.

    This matches reference model obj_3 structure:
    - A flat base plate covering the full bbox
    - Water features extruded upward on top of the base

    Args:
        gdf: GeoDataFrame of water features in local UTM meters.
        bbox_x_min/y_min/max: Full bounding box for the base plate (local coords).
        scale: mm per meter scale factor (for converting base thickness).

    Returns:
        Single watertight trimesh of base plate + water features, or None if no data.
    """
    if gdf is None or len(gdf) == 0:
        return None

    # Convert mm to model meters
    base_thickness_m = WATER_BASE_THICKNESS_MM / scale if scale > 0 else 0.0
    water_height_m = WATER_HEIGHT_MODEL_MM

    manifold_parts = []

    # ── 1. Full-area base plate (Manifold) ──
    try:
        plate = _build_base_plate_manifold(
            bbox_x_min, bbox_y_min, bbox_x_max, bbox_y_max,
            base_thickness_m,
        )
        manifold_parts.append(plate)
    except Exception as e:
        print(f"  ⚠ Base plate (Manifold) failed: {e}")
        # Fallback: build as trimesh then convert
        base_plate = _build_base_plate_trimesh(
            bbox_x_min, bbox_y_min, bbox_x_max, bbox_y_max,
            base_thickness_m,
        )
        from _TEXTURE_STYLE_OF_DEEPSEEK._bridge import trimesh_to_manifold
        manifold_parts.append(trimesh_to_manifold(base_plate))

    # ── 2. Extrude water features ──
    n_features = 0
    n_skipped_small = 0
    n_skipped_overlap = 0
    n_fail = 0

    # Step 1: Process LineStrings first, collect their buffered coverage
    linestring_coverage = None  # Union of all buffered LineStrings
    linestring_parts = []
    linestring_polygons = []  # Track buffered polygons for overlap detection

    for _, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue

        if isinstance(geom, (LineString, MultiLineString)):
            if isinstance(geom, MultiLineString):
                lines = list(geom.geoms)
            else:
                lines = [geom]
            waterway = row.get("waterway", "river")

            # Priority: OSM width tag > WATERWAY_WIDTHS config
            osm_width = row.get("width", None)
            width = WATERWAY_WIDTHS.get(waterway, 60.0)  # default 60m
            if osm_width is not None:
                try:
                    import math
                    if isinstance(osm_width, float) and math.isnan(osm_width):
                        pass
                    else:
                        parsed = float(osm_width)
                        if parsed > 0 and parsed < 10000:
                            width = parsed
                except (ValueError, TypeError):
                    pass

            for line in lines:
                if line.length < 10.0:
                    continue
                poly = _build_water_line(line, width)
                if poly is not None and not poly.is_empty and poly.area >= WATER_MIN_AREA_M2:
                    poly = _densify_polygon(poly, WATER_MAX_EDGE_M)
                    linestring_polygons.append(poly)
                    # Track coverage for overlap detection
                    if linestring_coverage is None:
                        linestring_coverage = poly
                    else:
                        linestring_coverage = linestring_coverage.union(poly)

    # Step 2: Process Polygons, check overlap with LineString coverage
    polygon_parts = []
    polygons_with_overlap = []  # Polygons that overlap LineString coverage

    for _, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue

        if isinstance(geom, (Polygon, MultiPolygon)):
            polygons_to_process = []
            if isinstance(geom, MultiPolygon):
                polygons_to_process = list(geom.geoms)
            else:
                polygons_to_process = [geom]

            for poly in polygons_to_process:
                if poly.is_empty or poly.area < WATER_MIN_AREA_M2:
                    n_skipped_small += 1
                    continue

                poly = _densify_polygon(poly, WATER_MAX_EDGE_M)
                
                # Check overlap with LineString coverage
                is_overlap = False
                if linestring_coverage is not None and poly.intersects(linestring_coverage):
                    overlap_ratio = poly.intersection(linestring_coverage).area / poly.area
                    if overlap_ratio > 0.3:  # >30% overlap, Polygon takes precedence
                        is_overlap = True
                        n_skipped_overlap += 1
                        polygons_with_overlap.append(poly)

                man = _extrude_water_manifold(poly, water_height_m)
                if man.is_empty():
                    n_fail += 1
                    continue

                polygon_parts.append(man)
                n_features += 1

    # Step 3: Add LineString parts that DON'T overlap with high-priority Polygons
    for poly in linestring_polygons:
        # Check if this LineString overlaps with any high-priority Polygon
        should_skip = False
        for overlap_poly in polygons_with_overlap:
            if poly.intersects(overlap_poly):
                overlap_ratio = poly.intersection(overlap_poly).area / poly.area
                if overlap_ratio > 0.3:  # LineString overlaps with Polygon, skip it
                    should_skip = True
                    break
        
        if not should_skip:
            man = _extrude_water_manifold(poly, water_height_m)
            if not man.is_empty():
                linestring_parts.append(man)
                n_features += 1

    # Combine: base plate (already in manifold_parts) + Polygon parts + LineString parts
    manifold_parts.extend(polygon_parts)
    manifold_parts.extend(linestring_parts)

    print(f"  Water features: {n_features} extruded, "
          f"{n_skipped_small} skipped (too small), "
          f"{n_skipped_overlap} LineString skipped (Polygon takes precedence)"
          + (f", {n_fail} Manifold failures" if n_fail else ""))

    if len(manifold_parts) == 0:
        return None

    # ── 3. Boolean union (one call, guaranteed watertight) ──
    if len(manifold_parts) == 1:
        combined_man = manifold_parts[0]
    else:
        combined_man = manifold3d.Manifold.batch_boolean(
            manifold_parts, manifold3d.OpType.Add,
        )

    if combined_man.is_empty():
        print("  ⚠ Manifold union produced empty result")
        return None

    # ── 4. Convert to trimesh ──
    mesh_data = combined_man.to_mesh()
    verts = np.array(mesh_data.vert_properties, dtype=np.float64)
    faces = np.array(mesh_data.tri_verts, dtype=np.int64)
    combined = trimesh.Trimesh(vertices=verts, faces=faces, process=False)

    # ── 5. Scale from meters to mm, then position bottom at Z_WATER_BASE_MM ──
    combined.vertices *= scale
    z_min = combined.vertices[:, 2].min()
    combined.vertices[:, 2] += Z_WATER_BASE_MM - z_min

    return combined


# =====================================================================
#  Trimesh fallbacks (only used if Manifold constructor fails)
# =====================================================================


def _build_base_plate_trimesh(bbox_x_min, bbox_y_min, bbox_x_max, bbox_y_max,
                              base_thickness_m: float) -> trimesh.Trimesh:
    """Fallback base plate using manual trimesh construction."""
    bottom_z = -abs(base_thickness_m)
    base_z = 0.0

    bv = np.array([
        [bbox_x_min, bbox_y_min, bottom_z],
        [bbox_x_max, bbox_y_min, bottom_z],
        [bbox_x_max, bbox_y_max, bottom_z],
        [bbox_x_min, bbox_y_max, bottom_z],
    ], dtype=np.float64)

    tv = np.array([
        [bbox_x_min, bbox_y_min, base_z],
        [bbox_x_max, bbox_y_min, base_z],
        [bbox_x_max, bbox_y_max, base_z],
        [bbox_x_min, bbox_y_max, base_z],
    ], dtype=np.float64)

    bf = np.array([[2, 1, 0], [0, 3, 2]], dtype=np.int64)
    tf = np.array([[4, 5, 6], [4, 6, 7]], dtype=np.int64)
    sw = np.array([
        [0, 1, 5], [0, 5, 4],
        [1, 2, 6], [1, 6, 5],
        [3, 0, 4], [3, 4, 7],
        [2, 3, 7], [2, 7, 6],
    ], dtype=np.int64)

    all_verts = np.vstack([bv, tv])
    all_faces = np.vstack([bf, tf, sw])
    return trimesh.Trimesh(vertices=all_verts, faces=all_faces, process=False)
