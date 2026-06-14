"""Mesh validation and repair utilities for 3D printing.

Two backends available:
  1. trimesh-native repair (default) — lightweight, fast for small meshes.
  2. Manifold-backed repair — guaranteed watertight output, recommended
     for large meshes and when trimesh repair is insufficient.

Also provides ``optimize_and_repair_mesh`` — a safety net that performs
fast quadric decimation *before* Manifold repair to prevent OOM / timeout
on very large meshes (e.g. 1.7 M faces).
"""

import time
import numpy as np
import trimesh


def validate_and_repair_mesh(mesh: trimesh.Trimesh,
                             name: str = "mesh",
                             fix_watertight: bool = True,
                             fix_normals: bool = True,
                             fix_degenerate: bool = True,
                             fix_duplicates: bool = True,
                             fix_non_manifold: bool = False) -> trimesh.Trimesh:
    """Validate and repair a mesh for 3D printing.

    Args:
        mesh: input trimesh
        name: mesh name for logging
        fix_watertight: attempt to fill holes
        fix_normals: fix face and vertex normals
        fix_degenerate: remove degenerate faces
        fix_duplicates: remove duplicate faces
        fix_non_manifold: attempt to fix non-manifold edges

    Returns:
        Repaired trimesh
    """
    if mesh is None or len(mesh.faces) == 0:
        return mesh

    initial_faces = len(mesh.faces)
    initial_verts = len(mesh.vertices)

    # Merge duplicate vertices
    if fix_duplicates:
        mesh.merge_vertices()

    # Remove degenerate faces
    if fix_degenerate:
        mask = mesh.nondegenerate_faces()
        mesh.update_faces(mask)

    # Remove duplicate faces
    if fix_duplicates:
        mask = mesh.unique_faces()
        mesh.update_faces(mask)

    # Fix normals
    if fix_normals:
        mesh.fix_normals()

    # Attempt watertight repair
    if fix_watertight and not mesh.is_watertight:
        try:
            mesh.fill_holes()
        except Exception:
            pass

    # Non-manifold repair
    if fix_non_manifold:
        try:
            mesh.process(validate=True)
        except Exception:
            pass

    final_faces = len(mesh.faces)
    final_verts = len(mesh.vertices)

    print(f"[{name}] Mesh: {initial_verts}→{final_verts} verts, "
          f"{initial_faces}→{final_faces} faces, "
          f"watertight={mesh.is_watertight}")

    return mesh


def validate_and_repair_mesh_manifold(mesh: trimesh.Trimesh,
                                      name: str = "mesh") -> trimesh.Trimesh:
    """Repair mesh using the Manifold library for guaranteed watertight output.

    This backend performs a full round-trip (trimesh → Manifold → trimesh).
    The Manifold constructor automatically collapses degenerate triangles,
    merges duplicate vertices, and ensures the result is a valid 2-manifold.

    Falls back to the trimesh-native repair if the Manifold library is
    unavailable or the input cannot be represented as a Manifold.

    Args:
        mesh: input trimesh
        name: mesh name for logging

    Returns:
        Repaired trimesh — watertight when the Manifold backend succeeds.
    """
    if mesh is None or len(mesh.faces) == 0:
        return mesh

    initial_faces = len(mesh.faces)
    initial_verts = len(mesh.vertices)

    try:
        from _TEXTURE_STYLE_OF_DEEPSEEK._bridge import (
            trimesh_to_manifold,
            manifold_to_trimesh,
        )

        m = trimesh_to_manifold(mesh)
        result = manifold_to_trimesh(m)

        final_faces = len(result.faces)
        final_verts = len(result.vertices)
        print(f"[{name}] Manifold repair: {initial_verts}→{final_verts} verts, "
              f"{initial_faces}→{final_faces} faces, "
              f"watertight={result.is_watertight}")
        return result

    except ImportError:
        print(f"[{name}] Manifold library not available, "
              f"falling back to trimesh-native repair")
    except Exception as e:
        print(f"[{name}] Manifold repair failed ({e}), "
              f"falling back to trimesh-native repair")

    # Fallback: trimesh-native repair with full options
    return validate_and_repair_mesh(
        mesh, name=name,
        fix_watertight=True,
        fix_normals=True,
        fix_degenerate=True,
        fix_duplicates=True,
    )


def optimize_and_repair_mesh(
    mesh: trimesh.Trimesh,
    max_faces: int = 100_000,
    agg: float = 7.0,
    name: str = "mesh",
) -> trimesh.Trimesh:
    """Decimate a large mesh via fast_simplification C ext, then Manifold-repair.

    This is the **safety net** inserted between Trimesh generation and
    Manifold repair.  It prevents OOM / timeout when the input has
    millions of faces (e.g. 1.7 M from a 1024×1024 DEM).

    Workflow:
      1. If ``len(mesh.faces) > max_faces``, run Fast Quadric simplification
         directly through the compiled C extension (bypasses the Python
         wrapper that requires Python 3.10+).
      2. Send the (now manageable) mesh through
         :func:`validate_and_repair_mesh_manifold` for guaranteed watertight.

    Args:
        mesh: input trimesh (may have millions of faces).
        max_faces: face-count threshold that triggers decimation.
        agg: aggressiveness of the simplification (0 = preserve geometry,
             10 = fast / lower quality).  7 is a good default that keeps
             sharp edges and flat bottoms intact.
        name: label for log messages.

    Returns:
        Watertight trimesh ready for export.
    """
    if mesh is None or len(mesh.faces) == 0:
        return mesh

    current_faces = len(mesh.faces)
    print(f"[{name}] optimize_and_repair: input {current_faces} faces")

    # ── STEP 1: Fast Quadric decimation if needed ──────────────
    if current_faces > max_faces:
        t0 = time.time()
        target_fraction = max_faces / current_faces
        target_count = max_faces
        print(f"[{name}] 🚀 Triggering fast simplification "
              f"({current_faces} → ~{target_count}, "
              f"fraction={target_fraction:.2%}, agg={agg})")

        from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.terrain import (
            _fast_simplify_direct,
        )

        result = _fast_simplify_direct(
            mesh.vertices, mesh.faces,
            target_count=target_count, agg=agg,
        )

        if result is not None:
            simp_verts, simp_faces = result
            # Preserve vertex colors if available
            mesh = trimesh.Trimesh(
                vertices=simp_verts, faces=simp_faces, process=False,
            )
            elapsed = time.time() - t0
            print(f"[{name}] ⏱ Simplified to {len(mesh.faces)} faces "
                  f"in {elapsed:.1f}s")
        else:
            print(f"[{name}] ⚠ fast_simplification C ext not found, "
                  f"skipping decimation (keeping {current_faces} faces)")

    # ── STEP 2: Manifold-backed watertight repair ──────────────
    print(f"[{name}] 🛡 Sending to Manifold for watertight repair "
          f"({len(mesh.faces)} faces)...")
    t1 = time.time()
    mesh = validate_and_repair_mesh_manifold(mesh, name=name)
    print(f"[{name}] ⏱ Manifold repair done in {time.time()-t1:.1f}s, "
          f"watertight={mesh.is_watertight}")

    return mesh
