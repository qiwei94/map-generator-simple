"""3MF exporter — exact reference model XML structure with Bambu metadata.

Produces a 3MF ZIP archive matching the Urban Series reference models:
  - Object hierarchy: terrain_surface(1), terrain_walls(2), buildings(3),
    roads(4), water(5)
  - Extruders: E1=terrain+buildings, E2=roads, E3=water
  - Bambu Studio metadata: model_settings.config + project_settings.config
"""

import os
import xml.etree.ElementTree as ET
import zipfile
import json
import numpy as np
import trimesh

from _TEXTURE_STYLE_OF_DEEPSEEK.config import (
    EXTRUDER_MAP,
    FILAMENT_COLOURS,
    TERRAIN_COLOR,
    BUILDING_COLOR,
    ROAD_COLOR,
    WATER_COLOR,
    BASE_WALL_COLOR,
    VEGETATION_COLOR,
    Z_TERRAIN_BASE,
    Z_BUILDING_EMBED_MM,
    Z_ROAD_ABOVE_TERRAIN_MM,
    Z_WATER_BASE_MM,
    VEGETATION_Z_OFFSET_MM,
)

# 3MF namespace constants
NS_3MF = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
NS_SLIC3R = "http://schemas.slic3r.org/3mf/2017/06"


def _escape_xml(s: str) -> str:
    """Escape special XML characters."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _make_slic3r_metadata(key: str, value: str) -> ET.Element:
    """Create a slic3r:metadata element."""
    el = ET.Element(f"{{{NS_SLIC3R}}}metadata")
    el.set("key", key)
    el.set("value", value)
    return el


def _make_metadata(key: str, value: str) -> ET.Element:
    """Create a plain metadata element (non-slic3r)."""
    el = ET.Element("metadata")
    el.set("key", key)
    el.set("value", value)
    return el


def _format_vertices_xml(vertices: np.ndarray) -> str:
    """Format vertices as 3MF XML vertices block."""
    lines = ["      <vertices>"]
    for v in vertices:
        lines.append(
            f'        <vertex x="{v[0]:.6f}" y="{v[1]:.6f}" z="{v[2]:.6f}"/>'
        )
    lines.append("      </vertices>")
    return "\n".join(lines)


def _format_triangles_xml(faces: np.ndarray, pid: int = 1, pindex: int = 0) -> str:
    """Format triangles as 3MF XML triangles block."""
    lines = ["      <triangles>"]
    for f in faces:
        lines.append(
            f'        <triangle v1="{f[0]}" v2="{f[1]}" v3="{f[2]}" pid="{pid}" p1="{pindex}"/>'
        )
    lines.append("      </triangles>")
    return "\n".join(lines)


def _build_3mf_xml(meshes: dict) -> str:
    """Build the full 3D/3dmodel.model XML string.

    Meshes dict keys: 'terrain_surface', 'terrain_walls', 'buildings', 'roads', 'water'
    Each mesh is a trimesh.Trimesh in model mm coordinates.

    Material mapping (matches reference Urban Series models):
      - Basematerials id=1 with 5 materials:
          pindex=0: terrain_surface -> #C8B48C
          pindex=1: terrain_walls   -> #464137
          pindex=2: buildings       -> #F5E6C8
          pindex=3: roads           -> #5A5A5A
          pindex=4: water           -> #3C96DC
      - Each object gets its own pindex for correct display color in Bambu Studio
    """
    ET.register_namespace("", NS_3MF)
    ET.register_namespace("slic3r", NS_SLIC3R)

    model = ET.Element("model", {
        "unit": "millimeter",
        "xml:lang": "en-US",
        "xmlns": NS_3MF,
        "xmlns:slic3r": NS_SLIC3R,
    })

    resources = ET.SubElement(model, "resources")

    # Basematerials — one per object type (matches reference model structure)
    basematerials = ET.SubElement(resources, "basematerials", {"id": "1"})
    mat_defs = [
        ("terrain_surface", TERRAIN_COLOR),
        ("terrain_walls", BASE_WALL_COLOR),
        ("buildings", BUILDING_COLOR),
        ("roads", ROAD_COLOR),
        ("water", WATER_COLOR),
        ("vegetation", VEGETATION_COLOR),
    ]
    for name, color in mat_defs:
        ET.SubElement(basematerials, "base", {
            "name": name,
            "displaycolor": color,
        })

    # Object definitions — each object gets its own pindex
    # id=1 is reserved for basematerials, objects start at id=2
    object_defs = [
        ("2", "terrain_surface", 0),
        ("3", "terrain_walls", 1),
        ("4", "buildings", 2),
        ("5", "roads", 3),
        ("6", "water", 4),
        ("7", "vegetation", 5),
    ]

    # Only include objects that have actual mesh data
    active_objects = []
    for oid, name, pidx in object_defs:
        obj_mesh = meshes.get(name)
        if obj_mesh is None or len(obj_mesh.faces) == 0:
            continue  # skip empty placeholders — Bambu warns "volume=0"

        active_objects.append((oid, name, pidx, obj_mesh))

    for oid, name, pidx, obj_mesh in active_objects:
        obj_el = ET.SubElement(resources, "object", {
            "id": oid, "name": name, "type": "model", "pid": "1", "pindex": str(pidx),
        })
        mesh_el = ET.SubElement(obj_el, "mesh")

        # Vertices
        verts_el = ET.SubElement(mesh_el, "vertices")
        for v in obj_mesh.vertices:
            ET.SubElement(verts_el, "vertex", {
                "x": f"{v[0]:.6f}",
                "y": f"{v[1]:.6f}",
                "z": f"{v[2]:.6f}",
            })

        # Triangles
        tris_el = ET.SubElement(mesh_el, "triangles")
        pid_str = str(pidx)
        for f in obj_mesh.faces:
            ET.SubElement(tris_el, "triangle", {
                "v1": str(int(f[0])),
                "v2": str(int(f[1])),
                "v3": str(int(f[2])),
                "pid": "1",
                "p1": pid_str,
            })

    # Build section — only include active objects
    build = ET.SubElement(model, "build")
    for oid, _, _, _ in active_objects:
        ET.SubElement(build, "item", {"objectid": oid})

    # Extract elementree string
    xml_str = ET.tostring(model, encoding="unicode", xml_declaration=True)
    return xml_str


def _generate_bambu_metadata(meshes: dict) -> dict:
    """Generate Bambu Studio metadata files for the 3MF.

    Returns dict of {archive_path: content_string}.
    """
    metadata = {}

    # model_settings.config — per-object extruder assignments and source offsets
    model_settings_lines = []
    model_settings_lines.append('<?xml version="1.0" encoding="UTF-8"?>')
    model_settings_lines.append('<config>')

    # Filament colours
    filament_json = json.dumps(FILAMENT_COLOURS)
    model_settings_lines.append(f'  <metadata key="filament_colour" value=\'{filament_json}\'/>')

    # Object definitions with extruder assignments and source Z offsets
    # Object IDs must match 3MF XML (id=2-7, since id=1 is basematerials)
    object_configs = [
        ("2", "terrain_surface", "1", f"{Z_TERRAIN_BASE:.2f}"),
        ("3", "terrain_walls", "1", f"{Z_TERRAIN_BASE:.2f}"),
        ("4", "buildings", "1", f"{-Z_BUILDING_EMBED_MM:.2f}"),  # embedded below terrain
        ("5", "roads", "2", f"{Z_ROAD_ABOVE_TERRAIN_MM:.2f}"),
        ("6", "water", "3", f"{Z_WATER_BASE_MM:.2f}"),
        ("7", "vegetation", "4", f"{VEGETATION_Z_OFFSET_MM:.2f}"),
    ]

    for oid, name, extruder, z_offset in object_configs:
        obj_mesh = meshes.get(name)
        face_count = len(obj_mesh.faces) if obj_mesh is not None and obj_mesh.faces is not None else 0
        model_settings_lines.append(f'  <object id="{oid}">')
        model_settings_lines.append(f'    <metadata key="name" value="{name}"/>')
        model_settings_lines.append(f'    <metadata key="extruder" value="{extruder}"/>')
        if face_count > 0:
            model_settings_lines.append(f'    <part id="0" source_file="" source_object_id="{oid}" face_count="{face_count}">')
            model_settings_lines.append(f'      <metadata key="source_offset_z" value="{z_offset}"/>')
            model_settings_lines.append(f'    </part>')
        model_settings_lines.append(f'  </object>')

    model_settings_lines.append('</config>')
    metadata["Metadata/model_settings.config"] = "\n".join(model_settings_lines) + "\n"

    # project_settings.config
    metadata["Metadata/project_settings.config"] = """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <metadata key="extruders_count" value="4"/>
</config>
"""

    return metadata


def _generate_content_types() -> str:
    """Generate [Content_Types].xml."""
    return '''<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
  <Default Extension="config" ContentType="text/xml"/>
</Types>'''


def _generate_rels() -> str:
    """Generate _rels/.rels."""
    return '''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"
                Target="/3D/3dmodel.model" Id="rel0"/>
</Relationships>'''


def export_deepseek_3mf(meshes: dict, output_path: str,
                        extruders: int = 3) -> str:
    """Export a _TEXTURE_STYLE_OF_DEEPSEEK 3MF file.

    Args:
        meshes: dict with keys:
            'terrain_surface' - terrain top surface (trimesh)
            'terrain_walls'   - terrain walls + bottom (trimesh)
            'buildings'       - building blocks (trimesh or None)
            'roads'           - road ribbons (trimesh or None)
            'water'           - water plates (trimesh or None)
        output_path: path for the .3mf file
        extruders: number of extruders (always 3 for this pipeline)

    Returns:
        Path to the created .3mf file.
    """
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)

    # Ensure all required keys exist (empty mesh for missing layers)
    empty_mesh = trimesh.Trimesh(vertices=np.zeros((0, 3)), faces=np.zeros((0, 3), dtype=np.int64))
    for key in ["terrain_surface", "terrain_walls", "buildings", "roads", "water", "vegetation"]:
        if key not in meshes or meshes[key] is None:
            meshes[key] = empty_mesh

    # Build 3MF XML
    xml_content = _build_3mf_xml(meshes)

    # Build Bambu metadata
    bambu_meta = _generate_bambu_metadata(meshes)

    # Write ZIP — NO Bambu metadata files (reference model works without them)
    # Bambu Studio determines extruders from basematerials in 3dmodel.model
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _generate_content_types())
        zf.writestr("_rels/.rels", _generate_rels())
        zf.writestr("3D/3dmodel.model", xml_content)

        # Skip Bambu metadata files - they cause all objects to default to extruder 1
        # for path, content in bambu_meta.items():
        #     zf.writestr(path, content)

    return output_path


def split_terrain_mesh(terrain_solid: trimesh.Trimesh,
                       surface_color_threshold_z: float = None) -> dict:
    """Split a watertight terrain solid into surface and walls/bottom.

    The terrain solid has:
      - Top surface: faces with normals pointing roughly +Z
      - Walls + bottom: everything else

    Args:
        terrain_solid: watertight terrain trimesh (model mm)
        surface_color_threshold_z: not used here, kept for compatibility

    Returns:
        dict with 'terrain_surface' and 'terrain_walls' meshes.
    """
    if terrain_solid is None or len(terrain_solid.faces) == 0:
        return {"terrain_surface": None, "terrain_walls": None}

    normals = terrain_solid.face_normals
    z_component = normals[:, 2]

    # Faces with normal Z > 0 are the top surface
    surface_mask = z_component > 0.1
    walls_mask = ~surface_mask

    surface_indices = np.where(surface_mask)[0]
    walls_indices = np.where(walls_mask)[0]

    if len(surface_indices) == 0:
        return {"terrain_surface": None, "terrain_walls": terrain_solid}

    if len(walls_indices) == 0:
        return {"terrain_surface": terrain_solid, "terrain_walls": None}

    # Build surface mesh from face subset
    surface_verts = terrain_solid.vertices.copy()
    surface_faces = terrain_solid.faces[surface_indices]
    # Remap face indices to use only referenced vertices
    used_verts, inverse = np.unique(surface_faces, return_inverse=True)
    surface_faces_remapped = inverse.reshape(surface_faces.shape)
    surface_mesh = trimesh.Trimesh(
        vertices=surface_verts[used_verts],
        faces=surface_faces_remapped,
    )

    # Build walls mesh from face subset
    walls_verts = terrain_solid.vertices.copy()
    walls_faces = terrain_solid.faces[walls_indices]
    used_verts_w, inverse_w = np.unique(walls_faces, return_inverse=True)
    walls_faces_remapped = inverse_w.reshape(walls_faces.shape)
    walls_mesh = trimesh.Trimesh(
        vertices=walls_verts[used_verts_w],
        faces=walls_faces_remapped,
    )

    return {"terrain_surface": surface_mesh, "terrain_walls": walls_mesh}
