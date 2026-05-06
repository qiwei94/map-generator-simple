"""Bridge between trimesh and Manifold (manifold3d) libraries.

Provides conversion functions for round-tripping meshes, enabling
Manifold's guaranteed-manifold boolean operations and repair.
"""

import numpy as np
import trimesh
import manifold3d


def trimesh_to_manifold(tm: trimesh.Trimesh) -> manifold3d.Manifold:
    """Convert a trimesh to a Manifold object.

    Pre-processes the mesh (merge vertices, remove degenerate faces,
    fix normals) before conversion, since Manifold requires a valid
    oriented 2-manifold as input.

    Raises:
        ValueError: if the input mesh is empty or cannot be converted
                    to a valid Manifold even after pre-processing.

    Returns:
        manifold3d.Manifold — guaranteed to be a valid 2-manifold.
    """
    if tm is None or len(tm.faces) == 0:
        raise ValueError("Empty mesh cannot be converted to Manifold")

    # Pre-process: ensure mesh is a clean oriented 2-manifold
    tm = tm.copy()
    tm.merge_vertices()
    tm.update_faces(tm.nondegenerate_faces())
    tm.update_faces(tm.unique_faces())
    tm.fix_normals()  # Manifold requires consistent face winding

    if len(tm.faces) == 0:
        raise ValueError("Mesh has no valid faces after pre-processing")

    verts = np.ascontiguousarray(tm.vertices, dtype=np.float32)
    faces = np.ascontiguousarray(tm.faces, dtype=np.uint32)

    mesh = manifold3d.Mesh(vert_properties=verts, tri_verts=faces)
    m = manifold3d.Manifold(mesh)

    if m.is_empty():
        raise ValueError(
            "trimesh -> Manifold conversion failed: "
            "the input mesh is not a valid 2-manifold "
            "even after pre-processing"
        )

    return m


def manifold_to_trimesh(m: manifold3d.Manifold) -> trimesh.Trimesh:
    """Convert a Manifold object back to trimesh.

    The output mesh is guaranteed to be watertight (closed 2-manifold).

    Returns:
        trimesh.Trimesh — watertight mesh.
    """
    if m.is_empty():
        return trimesh.Trimesh(vertices=[], faces=[])

    out = m.to_mesh()
    verts = np.asarray(out.vert_properties, dtype=np.float64)
    faces = np.asarray(out.tri_verts, dtype=np.int64)

    return trimesh.Trimesh(vertices=verts, faces=faces, process=False)


def is_manifold_available() -> bool:
    """Check whether the manifold3d library is importable."""
    try:
        import manifold3d  # noqa: F401
        return True
    except ImportError:
        return False
