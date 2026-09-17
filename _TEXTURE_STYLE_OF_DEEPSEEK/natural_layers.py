"""Natural-landscape material layers.

The urban pipeline uses one terrain solid.  Snow and frost landscapes need a
second, printable solid: a thin white cap above a neutral substrate.  Its
thickness is the visual signal; no texture or per-face colour is involved.
"""

from __future__ import annotations

import numpy as np
import trimesh
from scipy.ndimage import gaussian_filter

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import (
    TerrainSurfacePlan,
    _add_walls_and_bottom,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.terrain import (
    build_terrain_mesh,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.mesh_repair import (
    validate_and_repair_mesh,
)


TOPCOAT_POLICY_VERSION = "natural-variable-topcoat-v1"


def derive_snow_topcoat_thickness(
    elevation_grid_m: np.ndarray,
    *,
    minimum_mm: float = 0.16,
    maximum_mm: float = 1.20,
    snow_likelihood: np.ndarray | None = None,
) -> np.ndarray:
    """Return a continuous white-cap thickness field in millimetres.

    This is deliberately a bounded terrain-only first pass.  It highlights
    high ground and ridges while preserving a one-layer white skin elsewhere,
    which is the material arrangement measured in the Changbai reference.
    A satellite-derived snow mask can later modulate this field without
    changing the mesh/export contract.
    """
    values = np.asarray(elevation_grid_m, dtype=np.float64)
    if values.ndim != 2 or min(values.shape) < 3:
        raise ValueError("topcoat needs a 2D elevation grid")
    if not (0.08 <= minimum_mm < maximum_mm <= 1.60):
        raise ValueError("topcoat thickness must be 0.08..1.60mm and non-flat")

    low, high = np.percentile(values[np.isfinite(values)], [1.0, 99.5])
    if high - low < 0.01:
        return np.full(values.shape, minimum_mm, dtype=np.float64)
    normalized = np.clip((values - low) / (high - low), 0.0, 1.0)
    # A broad elevation field establishes the snowline.  The local positive
    # residual makes radial ridges and crater rims read more strongly, without
    # adding random texture or sharp polygonal outlines.
    broad = gaussian_filter(normalized, sigma=10.0, mode="nearest")
    ridge = np.clip(normalized - gaussian_filter(normalized, sigma=3.0,
                                                   mode="nearest"), 0.0, None)
    ridge_scale = float(np.percentile(ridge, 99.0))
    if ridge_scale > 1e-9:
        ridge = np.clip(ridge / ridge_scale, 0.0, 1.0)
    field = 0.78 * np.power(broad, 1.45) + 0.22 * ridge
    if snow_likelihood is not None:
        likelihood = np.asarray(snow_likelihood, dtype=np.float64)
        if likelihood.shape != values.shape:
            raise ValueError("satellite snow likelihood must match the DEM grid")
        likelihood = np.clip(likelihood, 0.0, 1.0)
        # Satellite evidence has the stronger say over where white becomes
        # visibly thick; terrain retains a quiet contribution for continuous
        # ridge transitions and image shadows.
        field = 0.28 * field + 0.72 * likelihood
    return minimum_mm + (maximum_mm - minimum_mm) * np.clip(field, 0.0, 1.0)


def _surface_mesh(plan: TerrainSurfacePlan, z_grid_mm: np.ndarray) -> trimesh.Trimesh:
    surface = build_terrain_mesh(
        plan.regular_grid_m, plan.width_m, plan.height_m, 0.0,
        allow_qem_decimation=False,
    )
    surface.vertices[:, :2] *= plan.scale_mm_per_m
    surface.vertices[:, 2] = np.asarray(z_grid_mm, dtype=np.float64).ravel()
    return surface


def _closed_cap(top_surface: trimesh.Trimesh,
                bottom_z_grid_mm: np.ndarray) -> trimesh.Trimesh:
    """Make a watertight variable-thickness solid below ``top_surface``."""
    count = len(top_surface.vertices)
    top = np.asarray(top_surface.vertices, dtype=np.float64)
    bottom = top.copy()
    bottom[:, 2] = np.asarray(bottom_z_grid_mm, dtype=np.float64).ravel()
    faces = np.asarray(top_surface.faces, dtype=np.int64)

    # Boundary edges occur once in a regular surface triangulation.  Connect
    # them to the matching lower edge; internal edges remain inside the cap.
    directed = np.vstack((faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]))
    keys = np.sort(directed, axis=1)
    _, first, counts = np.unique(keys, axis=0, return_index=True,
                                 return_counts=True)
    boundary = directed[first[counts == 1]]
    walls = np.empty((len(boundary) * 2, 3), dtype=np.int64)
    walls[0::2] = np.column_stack((boundary[:, 0], boundary[:, 1],
                                   boundary[:, 1] + count))
    walls[1::2] = np.column_stack((boundary[:, 0], boundary[:, 1] + count,
                                   boundary[:, 0] + count))
    cap = trimesh.Trimesh(
        vertices=np.vstack((top, bottom)),
        faces=np.vstack((faces, faces[:, ::-1] + count, walls)),
        process=False,
    )
    cap = validate_and_repair_mesh(
        cap, name="snowcap", fix_watertight=True, fix_normals=True,
        fix_degenerate=True, fix_duplicates=True,
    )
    if not cap.is_watertight:
        raise ValueError("variable snowcap is not watertight")
    return cap


def build_masked_surface_cap(plan: TerrainSurfacePlan, mask: np.ndarray, *,
                             thickness_mm: float = 0.22,
                             name: str = "natural_overlay") -> trimesh.Trimesh | None:
    """Create a watertight, material-coloured cap only where ``mask`` is true.

    The mask is sampled on the terrain grid.  Keeping only triangles whose
    three vertices are selected avoids sliver triangles at the boundary; the
    resulting perimeter is then closed down into the terrain by one nozzle-
    printable layer.  It is intended for broad satellite classes such as
    vegetation, not for photographic texture.
    """
    selected = np.asarray(mask, dtype=bool)
    if selected.shape != plan.surface_z_grid_mm.shape:
        raise ValueError("overlay mask must match the terrain grid")
    if thickness_mm < 0.12:
        raise ValueError("overlay thickness must be at least one printable layer")
    surface = _surface_mesh(plan, plan.surface_z_grid_mm + thickness_mm)
    faces = surface.faces
    keep = selected.ravel()[faces].all(axis=1)
    if not np.any(keep):
        return None
    picked_faces = faces[keep]
    used, remapped = np.unique(picked_faces.ravel(), return_inverse=True)
    top = surface.vertices[used]
    partial_surface = trimesh.Trimesh(
        vertices=top, faces=remapped.reshape((-1, 3)), process=False)
    # Regions which touch only at one grid vertex are topologically separate
    # printable islands.  Close them one-by-one; treating a point contact as
    # a single volume would create a non-manifold vertex in the slicer.
    caps = []
    for island in partial_surface.split(only_watertight=False):
        if len(island.faces) == 0:
            continue
        caps.append(_closed_cap(island, island.vertices[:, 2] - thickness_mm - .02))
    if not caps:
        return None
    cap = trimesh.util.concatenate(caps)
    cap.metadata["name"] = name
    return cap


def build_recessed_terrain_substrate(plan: TerrainSurfacePlan, *,
                                     recess_mm: float = 0.24) -> trimesh.Trimesh:
    """Return a solid base recessed beneath satellite-classified overlays."""
    if recess_mm < 0.12:
        raise ValueError("substrate recess must be at least one printable layer")
    surface = _surface_mesh(plan, plan.surface_z_grid_mm - recess_mm)
    terrain = _add_walls_and_bottom(surface, plan.terrain_base_z_mm)
    terrain = validate_and_repair_mesh(
        terrain, name="terrain", fix_watertight=True, fix_normals=True,
        fix_degenerate=True, fix_duplicates=True,
    )
    if not terrain.is_watertight:
        raise ValueError("natural terrain substrate is not watertight")
    return terrain


def build_variable_topcoat(plan: TerrainSurfacePlan, *,
                           minimum_mm: float = 0.16,
                           maximum_mm: float = 1.20,
                           snow_likelihood: np.ndarray | None = None,
                           ) -> tuple[trimesh.Trimesh, trimesh.Trimesh, dict]:
    """Build the gray substrate and its white, variable-thickness cap."""
    thickness = derive_snow_topcoat_thickness(
        plan.regular_grid_m, minimum_mm=minimum_mm, maximum_mm=maximum_mm,
        snow_likelihood=snow_likelihood)
    top_z = np.asarray(plan.surface_z_grid_mm, dtype=np.float64)
    gray_top_z = top_z - thickness
    substrate_surface = _surface_mesh(plan, gray_top_z)
    substrate = _add_walls_and_bottom(substrate_surface, plan.terrain_base_z_mm)
    substrate = validate_and_repair_mesh(
        substrate, name="terrain", fix_watertight=True, fix_normals=True,
        fix_degenerate=True, fix_duplicates=True,
    )
    cap = _closed_cap(_surface_mesh(plan, top_z), gray_top_z - 0.02)
    evidence = {
        "policy_version": TOPCOAT_POLICY_VERSION,
        "minimum_mm": float(minimum_mm),
        "maximum_mm": float(maximum_mm),
        "resolved_thickness_mm": {
            "p0": float(np.min(thickness)),
            "p50": float(np.median(thickness)),
            "p90": float(np.percentile(thickness, 90)),
            "p100": float(np.max(thickness)),
        },
        "satellite_guidance": snow_likelihood is not None,
    }
    return substrate, cap, evidence
