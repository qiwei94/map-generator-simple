"""Mesh validation and repair utilities for 3D printing.

Two backends available:
  1. trimesh-native repair (default) — lightweight, fast for small meshes.
  2. Manifold-backed repair — guaranteed watertight output, recommended
     for large meshes and when trimesh repair is insufficient.
"""

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
