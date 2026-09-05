"""Terrain processor — watertight terrain solid.

Builds terrain in model mm space directly, avoiding Z-mapping issues from
mixing real-meter and model-mm coordinate systems.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

import numpy as np
import trimesh
from scipy.interpolate import RegularGridInterpolator
from scipy.ndimage import gaussian_filter
from scipy.spatial import cKDTree

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.terrain import (
    build_terrain_mesh,
    sample_terrain_z,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.mesh_repair import (
    validate_and_repair_mesh,
)


TERRAIN_GRID_POLICY_VERSION = "regular-printer-grid-v1"
TERRAIN_HEIGHT_POLICY_VERSION = "robust-scale-aware-v2"
TERRAIN_DETAIL_POLICY_VERSION = "dem-multiscale-landform-v1"
TERRAIN_CONDITIONING_POLICY_VERSION = "relief-aware-dem-denoise-v1"
TERRAIN_SURFACE_PLAN_VERSION = "terrain-surface-plan-v2"


@dataclass(frozen=True)
class TerrainSurfacePlan:
    """Immutable semantic terrain surface shared by preview and formal mesh.

    Arrays are copied and marked read-only by the resolver.  The plan carries
    resolved Z and printer-grid decisions, but no triangles or boolean state.
    """

    regular_grid_m: np.ndarray
    surface_z_grid_mm: np.ndarray
    width_m: float
    height_m: float
    scale_mm_per_m: float
    terrain_base_z_mm: float
    max_surface_edge_mm: float
    min_surface_height_mm: float
    grid_evidence: dict
    height_mapping: dict
    detail_evidence: dict
    fingerprint: str


def _smoothstep(edge0: float, edge1: float,
                values: np.ndarray | float) -> np.ndarray:
    """Hermite smoothstep used to avoid hard terrain-policy boundaries."""
    if edge1 <= edge0:
        raise ValueError("smoothstep edge1 must be greater than edge0")
    t = np.clip((np.asarray(values, dtype=np.float64) - edge0) /
                (edge1 - edge0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def enhance_terrain_detail(
    normalized_surface: np.ndarray,
    *,
    robust_range_m: float,
    output_relief_mm: float,
    cell_size_mm: tuple[float, float],
) -> tuple[np.ndarray, dict]:
    """Enhance DEM-backed mid-frequency landform detail without adding noise.

    The reference models allocate more Z contrast to ridges and gullies at
    roughly 0.8--3.5 mm model scale.  Apply a bounded deterministic unsharp
    mask only where the DEM supplies landform evidence; low-relief city scenes
    remain quiet and no random vegetation-like texture is invented.
    """
    surface = np.asarray(normalized_surface, dtype=np.float64)
    if surface.ndim != 2 or min(surface.shape) < 2:
        raise ValueError("normalized_surface must be a 2D grid")
    if not np.isfinite(surface).all():
        raise ValueError("normalized_surface must contain finite values")
    if robust_range_m < 0 or output_relief_mm <= 0:
        raise ValueError("terrain detail inputs must be non-negative")

    cell_x_mm, cell_y_mm = (float(cell_size_mm[0]),
                            float(cell_size_mm[1]))
    mean_cell_mm = 0.5 * (cell_x_mm + cell_y_mm)
    if mean_cell_mm <= 0:
        raise ValueError("terrain cell size must be positive")

    # Express filters in physical model millimetres so 15/25/30 km products
    # follow one design rule rather than one raster resolution.
    fine_sigma = max(0.65, 0.72 / mean_cell_mm)
    mid_sigma = max(fine_sigma + 0.5, 2.10 / mean_cell_mm)
    fine_blur = gaussian_filter(surface, sigma=fine_sigma, mode="reflect")
    mid_blur = gaussian_filter(surface, sigma=mid_sigma, mode="reflect")
    fine_band = surface - fine_blur
    mid_band = fine_blur - mid_blur

    gy, gx = np.gradient(surface, cell_y_mm, cell_x_mm)
    gradient = np.hypot(gx, gy)
    gradient_p90 = float(np.quantile(gradient, 0.90))
    gradient_scale = max(gradient_p90, 1e-9)

    # Height locates hills; gradient protects lower ridge flanks and gullies.
    elevation_mask = _smoothstep(0.055, 0.32, surface)
    slope_mask = _smoothstep(0.20, 1.10, gradient / gradient_scale)
    landform_mask = np.maximum(elevation_mask, 0.72 * slope_mask)
    landform_mask = gaussian_filter(landform_mask, sigma=0.8, mode="reflect")

    # Below ~35 m robust range this is normally SRTM/urban noise; reach full
    # strength only for genuinely hilly scenes.  No city-name switch is used.
    scene_strength = float(_smoothstep(35.0, 150.0, robust_range_m))
    raw_delta_mm = output_relief_mm * (
        0.28 * fine_band + 0.20 * mid_band)
    bounded_delta_mm = np.clip(raw_delta_mm, -0.09, 0.09)
    delta_mm = bounded_delta_mm * landform_mask * scene_strength
    enhanced = np.clip(surface + delta_mm / output_relief_mm, 0.0, 1.0)

    active = landform_mask >= 0.25
    active_delta = delta_mm[active]
    evidence = {
        "policy_version": TERRAIN_DETAIL_POLICY_VERSION,
        "method": "bounded_dem_multiscale_unsharp",
        "random_texture": False,
        "cell_size_mm": [cell_x_mm, cell_y_mm],
        "filter_sigma_cells": [float(fine_sigma), float(mid_sigma)],
        "filter_scale_mm": [float(fine_sigma * mean_cell_mm),
                            float(mid_sigma * mean_cell_mm)],
        "robust_range_m": float(robust_range_m),
        "scene_strength": scene_strength,
        "mask_coverage": float(np.mean(active)),
        "delta_rms_mm": float(np.sqrt(np.mean(delta_mm * delta_mm))),
        "delta_p95_abs_mm": float(np.quantile(np.abs(delta_mm), 0.95)),
        "delta_max_abs_mm": float(np.max(np.abs(delta_mm))),
        "active_delta_rms_mm": (
            float(np.sqrt(np.mean(active_delta * active_delta)))
            if active_delta.size else 0.0
        ),
    }
    return enhanced, evidence


def condition_terrain_source(
    regular_grid_m: np.ndarray,
    *,
    cell_size_mm: tuple[float, float],
) -> tuple[np.ndarray, dict]:
    """Suppress urban DEM noise without softening genuine landforms.

    SRTM1/GLO-30 is retained at source resolution.  Flat urban scenes can,
    however, contain building and acquisition residuals that become false
    mountains when a small real elevation range is stretched in Z.  Resolve a
    deterministic blend from the measured robust range: below 35 m denoise at
    a 1.7 mm model scale; above 150 m preserve the source exactly.
    """
    source = np.asarray(regular_grid_m, dtype=np.float64)
    if source.ndim != 2 or min(source.shape) < 2:
        raise ValueError("regular_grid_m must be a 2D grid")
    if not np.isfinite(source).all():
        raise ValueError("regular_grid_m must contain finite values")
    cell_x_mm, cell_y_mm = map(float, cell_size_mm)
    mean_cell_mm = 0.5 * (cell_x_mm + cell_y_mm)
    if mean_cell_mm <= 0.0:
        raise ValueError("terrain cell size must be positive")

    robust_low, robust_high = np.quantile(source, [0.005, 0.999])
    robust_range_m = float(max(0.0, robust_high - robust_low))
    landform_strength = float(_smoothstep(35.0, 150.0, robust_range_m))
    denoise_strength = 1.0 - landform_strength
    sigma_cells = max(0.65, 1.70 / mean_cell_mm)
    if denoise_strength <= 1e-12:
        conditioned = source.copy()
        delta = np.zeros_like(source)
    else:
        low_pass = gaussian_filter(
            source, sigma=sigma_cells, mode="reflect")
        conditioned = (
            source * landform_strength
            + low_pass * denoise_strength
        )
        delta = conditioned - source

    evidence = {
        "policy_version": TERRAIN_CONDITIONING_POLICY_VERSION,
        "method": "relief_aware_physical_low_pass",
        "random_texture": False,
        "input_robust_range_m": robust_range_m,
        "landform_strength": landform_strength,
        "denoise_strength": denoise_strength,
        "filter_sigma_cells": float(sigma_cells),
        "filter_scale_mm": float(sigma_cells * mean_cell_mm),
        "delta_rms_m": float(np.sqrt(np.mean(delta * delta))),
        "delta_p95_abs_m": float(np.quantile(np.abs(delta), 0.95)),
    }
    return conditioned, evidence


def regularize_elevation_grid(
    elevation_grid: np.ndarray,
    *,
    model_width_mm: float,
    model_height_mm: float,
    max_surface_edge_mm: float,
) -> tuple[np.ndarray, dict]:
    """Downsample a DEM onto a regular printer-bounded grid.

    A regular grid constrains every top triangle.  This is intentionally done
    before triangulation instead of simplifying an already-triangulated
    surface with QEM, whose face-count target provides no maximum-edge bound.
    """
    source = np.asarray(elevation_grid, dtype=np.float64)
    if source.ndim != 2 or min(source.shape) < 2:
        raise ValueError("elevation_grid must be a 2D grid of at least 2x2")
    if not np.isfinite([model_width_mm, model_height_mm,
                        max_surface_edge_mm]).all():
        raise ValueError("terrain grid dimensions must be finite")
    if model_width_mm <= 0 or model_height_mm <= 0:
        raise ValueError("terrain model dimensions must be positive")
    if max_surface_edge_mm <= 0:
        raise ValueError("max_surface_edge_mm must be positive")

    finite = np.isfinite(source)
    if not finite.any():
        raise ValueError("elevation_grid has no finite values")
    if not finite.all():
        source = source.copy()
        source[~finite] = float(np.nanmedian(source))

    source_rows, source_cols = source.shape
    # Each cell is split along its diagonal.  Bounding both cell axes by
    # max_edge/sqrt(2) bounds that diagonal by max_surface_edge_mm.
    max_cell_axis_mm = max_surface_edge_mm / np.sqrt(2.0)
    requested_cols = max(2, int(np.ceil(model_width_mm /
                                         max_cell_axis_mm)) + 1)
    requested_rows = max(2, int(np.ceil(model_height_mm /
                                         max_cell_axis_mm)) + 1)
    # The contract is geometric, not informational.  A coarser source may be
    # interpolated onto the bounded grid: it adds no false DEM detail, but it
    # does prevent large printable facets and keeps downstream draping stable.
    target_cols = requested_cols
    target_rows = requested_rows

    if (target_rows, target_cols) == source.shape:
        output = source.copy()
    else:
        source_y = np.linspace(0.0, 1.0, source_rows)
        source_x = np.linspace(0.0, 1.0, source_cols)
        target_y = np.linspace(0.0, 1.0, target_rows)
        target_x = np.linspace(0.0, 1.0, target_cols)
        yy, xx = np.meshgrid(target_y, target_x, indexing="ij")
        interpolator = RegularGridInterpolator(
            (source_y, source_x), source,
            method="linear", bounds_error=False, fill_value=None,
        )
        output = interpolator(
            np.column_stack([yy.ravel(), xx.ravel()]))
        output = output.reshape(target_rows, target_cols)

    cell_x_mm = model_width_mm / (target_cols - 1)
    cell_y_mm = model_height_mm / (target_rows - 1)
    triangle_diagonal_mm = float(np.hypot(cell_x_mm, cell_y_mm))
    evidence = {
        "policy_version": TERRAIN_GRID_POLICY_VERSION,
        "method": "regular_raster_resample",
        "qem_decimation": False,
        "source_shape": [int(source_rows), int(source_cols)],
        "output_shape": [int(target_rows), int(target_cols)],
        "model_size_mm": [float(model_width_mm), float(model_height_mm)],
        "cell_size_mm": [float(cell_x_mm), float(cell_y_mm)],
        "max_surface_edge_mm": float(max_surface_edge_mm),
        "expected_triangle_diagonal_mm": triangle_diagonal_mm,
    }
    return output, evidence


def resolve_terrain_height_mapping(
    elevation_grid: np.ndarray,
    *,
    scale_mm_per_m: float,
    requested_relief_mm: float,
    requested_gamma: float,
) -> dict:
    """Resolve a robust, scale-aware elevation-to-model mapping.

    The old raw-min/raw-max + gamma=0.35 mapping allowed a few DEM outliers to
    set the entire range and lifted ordinary plains almost as strongly as
    hills.  The new mapping clips only the outer 0.5/0.1 percent tails, limits
    vertical exaggeration, and protects lowland-dominated city scenes from an
    over-aggressive gamma curve.
    """
    values = np.asarray(elevation_grid, dtype=np.float64)
    values = values[np.isfinite(values)]
    if not len(values):
        raise ValueError("elevation_grid has no finite values")
    if scale_mm_per_m <= 0 or requested_relief_mm <= 0:
        raise ValueError("terrain scale and requested relief must be positive")
    if requested_gamma <= 0 or not np.isfinite(requested_gamma):
        raise ValueError("requested terrain gamma must be finite and positive")

    quantile_levels = (0.0, 0.005, 0.01, 0.25, 0.5, 0.75,
                       0.99, 0.999, 1.0)
    quantile_values = np.quantile(values, quantile_levels)
    quantiles = {
        f"p{level * 100:g}": float(value)
        for level, value in zip(quantile_levels, quantile_values)
    }
    robust_low_m = float(quantile_values[1])
    robust_high_m = float(quantile_values[-2])
    robust_range_m = robust_high_m - robust_low_m

    if robust_range_m <= 0.01:
        return {
            "policy_version": TERRAIN_HEIGHT_POLICY_VERSION,
            "source_quantiles_m": quantiles,
            "robust_low_m": robust_low_m,
            "robust_high_m": robust_high_m,
            "robust_range_m": max(0.0, robust_range_m),
            "requested_gamma": float(requested_gamma),
            "resolved_gamma": 1.0,
            "requested_relief_mm": float(requested_relief_mm),
            "natural_scale_relief_mm": 0.0,
            "output_relief_mm": float(requested_relief_mm),
            "lowland_dominated": False,
            "clip_percentiles": [0.5, 99.9],
        }

    q75_ratio = float(np.clip(
        (quantile_values[5] - robust_low_m) / robust_range_m, 0.0, 1.0))
    lowland_dominated = q75_ratio < 0.15
    gamma_floor = 0.85 if lowland_dominated else 0.75
    resolved_gamma = max(float(requested_gamma), gamma_floor)

    natural_scale_relief_mm = robust_range_m * float(scale_mm_per_m)
    # Modest vertical exaggeration preserves landform identity without making
    # a garden city look mountainous.  1.28x aligns the measured 25 km West
    # Lake terrain span with the reference model while remaining below the
    # configured relief cap.  Keep only a three-layer floor for low-relief
    # cities; the former 1.2 mm floor magnified urban DEM residuals into false
    # topography.
    relief_floor_mm = min(0.36, float(requested_relief_mm))
    output_relief_mm = min(
        float(requested_relief_mm),
        max(relief_floor_mm,
            natural_scale_relief_mm * 1.28),
    )
    return {
        "policy_version": TERRAIN_HEIGHT_POLICY_VERSION,
        "source_quantiles_m": quantiles,
        "robust_low_m": robust_low_m,
        "robust_high_m": robust_high_m,
        "robust_range_m": robust_range_m,
        "requested_gamma": float(requested_gamma),
        "resolved_gamma": float(resolved_gamma),
        "requested_relief_mm": float(requested_relief_mm),
        "natural_scale_relief_mm": float(natural_scale_relief_mm),
        "output_relief_mm": float(output_relief_mm),
        "relief_floor_mm": float(relief_floor_mm),
        "lowland_dominated": bool(lowland_dominated),
        "lowland_q75_ratio": q75_ratio,
        "clip_percentiles": [0.5, 99.9],
    }


def resolve_terrain_surface_plan(
    elevation_grid: np.ndarray,
    width_m: float,
    height_m: float,
    scale_mm_per_m: float,
    *,
    base_thickness_mm: float | None = None,
    max_surface_edge_mm: float = 0.84,
    min_surface_height_mm: float = 0.24,
) -> TerrainSurfacePlan:
    """Resolve the complete DEM-to-model surface before mesh materialization."""

    from _TEXTURE_STYLE_OF_DEEPSEEK.config import (
        Z_GAMMA,
        TERRAIN_THICKNESS_MM,
        WATER_BASE_THICKNESS_MM,
        Z_WATER_BASE_MM,
    )

    scale_mm_per_m = float(scale_mm_per_m)
    width_m = float(width_m)
    height_m = float(height_m)
    resolved_base_mm = (
        WATER_BASE_THICKNESS_MM
        if base_thickness_mm is None else float(base_thickness_mm)
    )
    if scale_mm_per_m <= 0 or width_m <= 0 or height_m <= 0:
        raise ValueError("terrain plan dimensions and scale must be positive")
    if resolved_base_mm < 0.4:
        raise ValueError("base thickness must be at least 0.4mm")
    if min_surface_height_mm <= 0:
        raise ValueError("min surface height must be positive")

    regular_grid, grid_evidence = regularize_elevation_grid(
        elevation_grid,
        model_width_mm=width_m * scale_mm_per_m,
        model_height_mm=height_m * scale_mm_per_m,
        max_surface_edge_mm=max_surface_edge_mm,
    )
    regular_grid, conditioning_evidence = condition_terrain_source(
        regular_grid,
        cell_size_mm=tuple(grid_evidence["cell_size_mm"]),
    )
    grid_evidence["source_conditioning"] = conditioning_evidence
    height_mapping = resolve_terrain_height_mapping(
        regular_grid,
        scale_mm_per_m=scale_mm_per_m,
        requested_relief_mm=max(
            0.01, TERRAIN_THICKNESS_MM - min_surface_height_mm),
        requested_gamma=Z_GAMMA,
    )
    height_mapping["requested_total_height_mm"] = float(
        TERRAIN_THICKNESS_MM)
    height_mapping["output_total_height_mm"] = float(
        height_mapping["output_relief_mm"] + min_surface_height_mm)

    terrain_base_z = float(Z_WATER_BASE_MM + resolved_base_mm)
    robust_low = float(height_mapping["robust_low_m"])
    robust_range = float(height_mapping["robust_range_m"])
    output_relief = float(height_mapping["output_relief_mm"])
    resolved_gamma = float(height_mapping["resolved_gamma"])
    detail_evidence = {
        "policy_version": TERRAIN_DETAIL_POLICY_VERSION,
        "method": "disabled_flat_surface",
        "random_texture": False,
        "scene_strength": 0.0,
        "delta_rms_mm": 0.0,
        "delta_p95_abs_mm": 0.0,
        "delta_max_abs_mm": 0.0,
    }
    if robust_range > 0.01:
        normalized = np.clip(
            (regular_grid - robust_low) / robust_range, 0.0, 1.0)
        normalized = np.power(normalized, resolved_gamma)
        normalized, detail_evidence = enhance_terrain_detail(
            normalized,
            robust_range_m=robust_range,
            output_relief_mm=output_relief,
            cell_size_mm=tuple(grid_evidence["cell_size_mm"]),
        )
        surface_z = (
            normalized * output_relief
            + terrain_base_z
            + min_surface_height_mm
        )
    else:
        surface_z = np.full(
            regular_grid.shape,
            output_relief + terrain_base_z + min_surface_height_mm,
            dtype=np.float64,
        )

    regular_grid = np.array(regular_grid, dtype=np.float64, copy=True)
    surface_z = np.array(surface_z, dtype=np.float64, copy=True)
    regular_grid.setflags(write=False)
    surface_z.setflags(write=False)
    fingerprint_payload = {
        "version": TERRAIN_SURFACE_PLAN_VERSION,
        "width_m": width_m,
        "height_m": height_m,
        "scale_mm_per_m": scale_mm_per_m,
        "terrain_base_z_mm": terrain_base_z,
        "max_surface_edge_mm": float(max_surface_edge_mm),
        "min_surface_height_mm": float(min_surface_height_mm),
        "grid_evidence": grid_evidence,
        "height_mapping": height_mapping,
        "detail_evidence": detail_evidence,
    }
    digest = hashlib.sha256()
    digest.update(json.dumps(
        fingerprint_payload, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8"))
    digest.update(regular_grid.tobytes(order="C"))
    digest.update(surface_z.tobytes(order="C"))
    return TerrainSurfacePlan(
        regular_grid_m=regular_grid,
        surface_z_grid_mm=surface_z,
        width_m=width_m,
        height_m=height_m,
        scale_mm_per_m=scale_mm_per_m,
        terrain_base_z_mm=terrain_base_z,
        max_surface_edge_mm=float(max_surface_edge_mm),
        min_surface_height_mm=float(min_surface_height_mm),
        grid_evidence=dict(grid_evidence),
        height_mapping=dict(height_mapping),
        detail_evidence=dict(detail_evidence),
        fingerprint=digest.hexdigest(),
    )


def terrain_surface_plan_evidence(plan: TerrainSurfacePlan) -> dict:
    """Return JSON-safe evidence without embedding raster arrays."""

    return {
        "policy_version": TERRAIN_SURFACE_PLAN_VERSION,
        "fingerprint": plan.fingerprint,
        "model_size_mm": [
            plan.width_m * plan.scale_mm_per_m,
            plan.height_m * plan.scale_mm_per_m,
        ],
        "terrain_base_z_mm": plan.terrain_base_z_mm,
        "surface_z_bounds_mm": [
            float(np.min(plan.surface_z_grid_mm)),
            float(np.max(plan.surface_z_grid_mm)),
        ],
        "grid": dict(plan.grid_evidence),
        "height_mapping": dict(plan.height_mapping),
        "detail_enhancement": dict(plan.detail_evidence),
        "formal_qem_decimation": False,
        "min_surface_height_mm": plan.min_surface_height_mm,
    }


def sample_terrain_surface_plan_z(
    plan: TerrainSurfacePlan,
    x_mm: np.ndarray,
    y_mm: np.ndarray,
) -> np.ndarray:
    """Sample semantic Z with the same 8-neighbour rule as formal meshes."""

    x_axis = np.linspace(
        -plan.width_m * plan.scale_mm_per_m / 2.0,
        plan.width_m * plan.scale_mm_per_m / 2.0,
        plan.surface_z_grid_mm.shape[1],
    )
    y_axis = np.linspace(
        -plan.height_m * plan.scale_mm_per_m / 2.0,
        plan.height_m * plan.scale_mm_per_m / 2.0,
        plan.surface_z_grid_mm.shape[0],
    )
    xx, yy = np.meshgrid(x_axis, y_axis)
    source_xy = np.column_stack([xx.ravel(), yy.ravel()])
    query_xy = np.column_stack([
        np.asarray(x_mm, dtype=np.float64),
        np.asarray(y_mm, dtype=np.float64),
    ])
    k = min(8, len(source_xy))
    _distance, indices = cKDTree(source_xy).query(query_xy, k=k)
    if k == 1:
        indices = indices[:, np.newaxis]
    return np.max(plan.surface_z_grid_mm.ravel()[indices], axis=1)


def measure_terrain_surface_mesh(mesh: trimesh.Trimesh) -> dict:
    """Measure actual top-surface triangle footprints in model millimetres."""
    if mesh is None or len(mesh.faces) == 0:
        return {"top_face_count": 0}
    vertices = np.asarray(mesh.vertices, dtype=np.float64)
    faces = np.asarray(mesh.faces, dtype=np.int64)
    face_vertices = vertices[faces]
    bottom_z = float(vertices[:, 2].min())
    top_mask = (
        (np.asarray(mesh.face_normals)[:, 2] > 0.05)
        & (face_vertices[:, :, 2].max(axis=1) > bottom_z + 1e-6)
    )
    top = face_vertices[top_mask]
    if not len(top):
        return {"top_face_count": 0}
    edge_01 = np.linalg.norm(top[:, 0, :2] - top[:, 1, :2], axis=1)
    edge_12 = np.linalg.norm(top[:, 1, :2] - top[:, 2, :2], axis=1)
    edge_20 = np.linalg.norm(top[:, 2, :2] - top[:, 0, :2], axis=1)
    longest = np.maximum.reduce([edge_01, edge_12, edge_20])
    return {
        "top_face_count": int(len(top)),
        "max_xy_edge_mm": float(longest.max()),
        "p99_xy_edge_mm": float(np.quantile(longest, 0.99)),
        "p999_xy_edge_mm": float(np.quantile(longest, 0.999)),
        "faces_over_2mm": int(np.count_nonzero(longest > 2.0)),
        "faces_over_5mm": int(np.count_nonzero(longest > 5.0)),
    }


def build_terrain_evidence(mesh: trimesh.Trimesh) -> dict:
    """Return final artifact evidence, including any post-build Z carving."""
    evidence = dict((mesh.metadata or {}).get("terrain_evidence") or {})
    evidence["surface_mesh"] = measure_terrain_surface_mesh(mesh)
    if mesh is not None and len(mesh.vertices):
        z_min = float(mesh.vertices[:, 2].min())
        z_max = float(mesh.vertices[:, 2].max())
        evidence["artifact_z_bounds_mm"] = [z_min, z_max]
        evidence["artifact_z_span_mm"] = z_max - z_min
    return evidence


def _add_walls_and_bottom(surface_mesh: trimesh.Trimesh,
                          bottom_z: float) -> trimesh.Trimesh:
    """Convert an open surface mesh to a watertight solid.

    Adds vertical walls from boundary edges down to bottom_z,
    and a flat bottom cap at bottom_z.

    Args:
        surface_mesh: open surface trimesh (already in model mm)
        bottom_z: Z coordinate for the bottom face

    Returns:
        Watertight solid trimesh.
    """
    # Find boundary edges (those appearing in exactly one face)
    from collections import Counter, defaultdict
    edge_counts = Counter(tuple(e) for e in surface_mesh.edges_sorted)
    boundary_edges = [e for e, c in edge_counts.items() if c == 1]

    if len(boundary_edges) < 3:
        return surface_mesh

    # Build adjacency graph
    adj = defaultdict(list)
    for a, b in boundary_edges:
        adj[a].append(b)
        adj[b].append(a)

    # Walk the boundary loop
    visited_edges = set()
    best_loop = []

    for start_edge in boundary_edges:
        start = start_edge[0]
        if tuple(sorted(start_edge)) in visited_edges:
            continue

        loop = []
        current = start
        prev = None

        while True:
            loop.append(current)
            neighbors = [n for n in adj[current] if n != prev]
            if not neighbors:
                break

            next_v = neighbors[0]
            edge = tuple(sorted([current, next_v]))
            if edge in visited_edges:
                break

            visited_edges.add(edge)
            prev = current
            current = next_v
            if current == start:
                break

        if len(loop) > len(best_loop):
            best_loop = loop

    if len(best_loop) < 3:
        return surface_mesh

    # best_loop is already vertex indices — use directly (no dict lookup)
    loop_indices = np.array(best_loop, dtype=np.int64)
    n_boundary = len(loop_indices)
    n_surf_verts = len(surface_mesh.vertices)
    surf_verts = surface_mesh.vertices

    # Build wall quads: each boundary edge → 2 triangles connecting top→bottom
    # Bottom vertices: same XY as boundary, Z=bottom_z
    boundary_coords = surf_verts[loop_indices]
    bottom_verts = boundary_coords.copy()
    bottom_verts[:, 2] = bottom_z

    # Wall face indices (vectorized)
    # Top vertex i connects to bottom vertex i (at n_surf_verts + i)
    i_arr = np.arange(n_boundary, dtype=np.int64)
    j_arr = (i_arr + 1) % n_boundary

    top_i = loop_indices[i_arr]
    top_j = loop_indices[j_arr]
    bot_i = n_surf_verts + i_arr
    bot_j = n_surf_verts + j_arr

    # Two triangles per quad (CCW winding for outward faces)
    wall_faces = np.empty((n_boundary * 2, 3), dtype=np.int64)
    wall_faces[0::2, 0] = top_i
    wall_faces[0::2, 1] = top_j
    wall_faces[0::2, 2] = bot_i
    wall_faces[1::2, 0] = top_j
    wall_faces[1::2, 1] = bot_j
    wall_faces[1::2, 2] = bot_i

    # Bottom cap: earcut triangulation of boundary polygon
    boundary_xy = boundary_coords[:, :2].astype(np.float64)
    cap_vert_offset = n_surf_verts + n_boundary
    bot_cap_verts = np.column_stack([boundary_xy, np.full(n_boundary, bottom_z)])
    bottom_faces = None

    try:
        from mapbox_earcut import triangulate_float64 as earcut
        ring_end = np.array([n_boundary], dtype=np.int32)
        ear_indices = earcut(boundary_xy, ring_end)
        if ear_indices is not None and len(ear_indices) >= 3:
            ear_faces = ear_indices.reshape(-1, 3) + cap_vert_offset
            # Reverse winding for downward-facing bottom
            bottom_faces = ear_faces[:, ::-1]
    except (ImportError, Exception):
        pass

    # Fallback: simple fan
    if bottom_faces is None:
        fan = np.empty((n_boundary - 2, 3), dtype=np.int64)
        fan[:, 0] = cap_vert_offset
        fan[:, 1] = cap_vert_offset + np.arange(2, n_boundary, dtype=np.int64)
        fan[:, 2] = cap_vert_offset + np.arange(1, n_boundary - 1, dtype=np.int64)
        bottom_faces = fan

    # Combine all vertices and faces
    all_verts = np.vstack([surf_verts, bottom_verts, bot_cap_verts])
    all_faces = np.vstack([surface_mesh.faces, wall_faces, bottom_faces])

    solid = trimesh.Trimesh(vertices=all_verts, faces=all_faces, process=False)
    solid.merge_vertices()
    solid.update_faces(solid.nondegenerate_faces())
    solid.update_faces(solid.unique_faces())

    return solid


def materialize_terrain_surface_plan(
    plan: TerrainSurfacePlan,
    *,
    area_km2: float = 0.0,
) -> trimesh.Trimesh:
    """Materialize triangles from an already frozen semantic surface plan."""

    mesh = build_terrain_mesh(
        plan.regular_grid_m,
        plan.width_m,
        plan.height_m,
        area_km2,
        allow_qem_decimation=False,
    )
    mesh.vertices[:, :2] *= plan.scale_mm_per_m
    mesh.vertices[:, 2] = plan.surface_z_grid_mm.ravel()
    solid = _add_walls_and_bottom(mesh, plan.terrain_base_z_mm)
    solid = validate_and_repair_mesh(
        solid,
        name="terrain",
        fix_watertight=True,
        fix_normals=True,
        fix_degenerate=True,
        fix_duplicates=True,
    )
    if not solid.is_watertight:
        raise ValueError("regular terrain solid is not watertight after repair")

    plan_evidence = terrain_surface_plan_evidence(plan)
    solid.metadata["terrain_evidence"] = plan_evidence
    mesh_evidence = measure_terrain_surface_mesh(solid)
    max_actual = float(mesh_evidence.get("max_xy_edge_mm", float("inf")))
    if max_actual > plan.max_surface_edge_mm + 1e-6:
        raise ValueError(
            "regular terrain surface exceeded its triangle edge contract: "
            f"{max_actual:.6f}mm > {plan.max_surface_edge_mm:.6f}mm"
        )
    solid.metadata["terrain_evidence"]["surface_mesh"] = mesh_evidence

    mapping = plan.height_mapping
    detail = plan.detail_evidence
    print(
        "[terrain] regular grid "
        f"{plan.grid_evidence['source_shape']}→"
        f"{plan.grid_evidence['output_shape']}, "
        f"max_xy_edge={max_actual:.3f}mm, QEM=off; "
        f"height p0.5–p99.9={mapping['robust_low_m']:.1f}–"
        f"{mapping['robust_high_m']:.1f}m, "
        f"gamma={mapping['resolved_gamma']:.2f}, "
        f"relief={mapping['output_relief_mm']:.2f}mm; "
        f"detail strength={detail['scene_strength']:.2f}, "
        f"rms={detail['delta_rms_mm']:.3f}mm"
    )
    return solid


def build_deepseek_terrain(elevation_grid: np.ndarray,
                           width_m: float,
                           height_m: float,
                           area_km2: float,
                           scale: float,
                           water_gdf=None,
                           base_thickness_mm: float | None = None,
                           max_surface_edge_mm: float = 0.84,
                           min_surface_height_mm: float = 0.24,
                           surface_plan: TerrainSurfacePlan | None = None,
                           ) -> trimesh.Trimesh:
    """Build the deepseek-style terrain: watertight terrain solid.

    Args:
        elevation_grid: 2D numpy array (rows, cols) in meters
        width_m: terrain width in meters (X)
        height_m: terrain height in meters (Y)
        area_km2: area in km^2 for LOD decisions
        water_gdf: unused (water is now a separate base plate object)
        max_surface_edge_mm: maximum regular-grid triangle diagonal in model
            millimetres.  The formal CLI derives this from the dedicated
            terrain sampling bound in the selected printer profile.
        min_surface_height_mm: structural separation between the terrain
            bottom and its lowest top-surface point, normally two layers.

    Returns:
        Watertight trimesh scaled to model mm, Z-mapped to terrain thickness,
        positioned at Z_TERRAIN_BASE (-0.17mm).
    """
    plan = surface_plan or resolve_terrain_surface_plan(
        elevation_grid,
        width_m,
        height_m,
        scale,
        base_thickness_mm=base_thickness_mm,
        max_surface_edge_mm=max_surface_edge_mm,
        min_surface_height_mm=min_surface_height_mm,
    )
    if abs(plan.width_m - float(width_m)) > 1e-6:
        raise ValueError("terrain surface plan width does not match request")
    if abs(plan.height_m - float(height_m)) > 1e-6:
        raise ValueError("terrain surface plan height does not match request")
    if abs(plan.scale_mm_per_m - float(scale)) > 1e-12:
        raise ValueError("terrain surface plan scale does not match request")
    return materialize_terrain_surface_plan(plan, area_km2=area_km2)


def sample_deepseek_terrain_z(terrain_mesh: trimesh.Trimesh,
                              x: np.ndarray,
                              y: np.ndarray) -> np.ndarray:
    """Sample terrain Z at given XY positions.

    Wrapper around terrain3d's sample_terrain_z for convenience.
    """
    return sample_terrain_z(terrain_mesh, x, y)
