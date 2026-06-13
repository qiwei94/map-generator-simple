#!/usr/bin/env python3
"""Post-processing tool: split 3MF sub-meshes into skin + core via Z-plane slab cut.

Reads an existing 3MF, splits selected parts into a thin visible "skin" (keeps
original extruder) and a buried "core" (reassigned to a cheaper extruder),
then writes a new optimized 3MF.

Usage:
    ./venv/bin/python tools/split_skin_core.py input.3mf -o output_optimized.3mf
    ./venv/bin/python tools/split_skin_core.py input.3mf --skin-mm 0.6 --core-extruder 2
    ./venv/bin/python tools/split_skin_core.py input.3mf --dry-run
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import zipfile
from typing import Optional

import numpy as np
import trimesh

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _TEXTURE_STYLE_OF_DEEPSEEK._bridge import trimesh_to_manifold, manifold_to_trimesh


# ─── Configuration ────────────────────────────────────────────────────────────

SPLIT_TARGETS = {
    # part_name: (skin_mm, min_thickness_to_split)
    "terrain":    (0.6, 1.5),
    "landmarks":  (0.8, 1.5),
    "block_base": (0.4, 0.9),
    "water":      (0.3, 0.7),
    # roads / vegetation: too thin, skip
}


# ─── 3MF XML Parsing ─────────────────────────────────────────────────────────

def parse_mesh_from_xml(xml_section: str) -> Optional[trimesh.Trimesh]:
    """Parse <vertices> and <triangles> from an object XML section."""
    verts_raw = re.findall(
        r'<vertex x="([^"]+)" y="([^"]+)" z="([^"]+)"', xml_section
    )
    faces_raw = re.findall(
        r'<triangle v1="([^"]+)" v2="([^"]+)" v3="([^"]+)"', xml_section
    )
    if not verts_raw or not faces_raw:
        return None

    verts = np.array(verts_raw, dtype=np.float64)
    faces = np.array(faces_raw, dtype=np.int64)
    return trimesh.Trimesh(vertices=verts, faces=faces, process=False)


def parse_3mf_parts(zip_path: str) -> dict:
    """Parse a 3MF file and return part info.

    Returns dict with:
      - 'parts': list of {id, name, extruder, mesh, xml_section}
      - 'main_model_xml': full main model XML
      - 'config_xml': model_settings.config
      - 'other_files': dict of path->bytes for non-mesh files
    """
    with zipfile.ZipFile(zip_path, "r") as zf:
        object_xml = zf.read("3D/Objects/object_1.model").decode()
        main_xml = zf.read("3D/3dmodel.model").decode()
        config_xml = zf.read("Metadata/model_settings.config").decode()

        other_files = {}
        for name in zf.namelist():
            if name not in ("3D/Objects/object_1.model", "3D/3dmodel.model",
                            "Metadata/model_settings.config"):
                other_files[name] = zf.read(name)

    # Parse parts from config
    parts = []
    for match in re.finditer(
        r'<part id="(\d+)"[^>]*>.*?<metadata key="name" value="([^"]+)".*?'
        r'<metadata key="extruder" value="(\d+)".*?</part>',
        config_xml, re.DOTALL
    ):
        part_id = int(match.group(1))
        name = match.group(2)
        extruder = int(match.group(3))

        # Find corresponding object section in object_1.model
        obj_pattern = rf'<object id="{part_id}"[^>]*>.*?</object>'
        obj_match = re.search(obj_pattern, object_xml, re.DOTALL)
        mesh = None
        if obj_match:
            mesh = parse_mesh_from_xml(obj_match.group(0))

        parts.append({
            "id": part_id,
            "name": name,
            "extruder": extruder,
            "mesh": mesh,
        })

    return {
        "parts": parts,
        "main_model_xml": main_xml,
        "config_xml": config_xml,
        "other_files": other_files,
    }


# ─── Slab Split ───────────────────────────────────────────────────────────────

def _to_manifold_direct(mesh: trimesh.Trimesh):
    """Convert trimesh to Manifold directly (bypasses trimesh repair which can
    break near-manifold meshes with a few non-manifold edges)."""
    import manifold3d
    verts = np.ascontiguousarray(mesh.vertices, dtype=np.float32)
    faces = np.ascontiguousarray(mesh.faces, dtype=np.uint32)
    mesh_obj = manifold3d.Mesh(vert_properties=verts, tri_verts=faces)
    m = manifold3d.Manifold(mesh_obj)
    if m.is_empty():
        raise ValueError("Direct Manifold conversion produced empty result")
    return m


def split_slab(
    mesh: trimesh.Trimesh,
    skin_mm: float,
    min_thickness: float,
) -> tuple[trimesh.Trimesh, Optional[trimesh.Trimesh]]:
    """Z-plane slab split: top skin_mm = skin, rest = core.

    Returns (skin, core). core is None if mesh is too thin or split fails.
    """
    if mesh is None or len(mesh.faces) == 0:
        return mesh, None

    z_top = float(mesh.vertices[:, 2].max())
    z_bot = float(mesh.vertices[:, 2].min())
    thickness = z_top - z_bot

    if thickness < min_thickness:
        return mesh, None

    # Try standard bridge conversion first, fall back to direct
    try:
        m = trimesh_to_manifold(mesh)
    except ValueError:
        try:
            m = _to_manifold_direct(mesh)
        except (ValueError, Exception):
            return mesh, None

    z_cut = z_top - skin_mm

    skin_mfd, core_mfd = m.split_by_plane([0.0, 0.0, 1.0], z_cut)

    if core_mfd.is_empty():
        return mesh, None

    # Check core is meaningful (> 5% volume)
    total_vol = abs(m.volume())
    core_vol = abs(core_mfd.volume())
    if total_vol > 0 and core_vol / total_vol < 0.05:
        return mesh, None

    skin_tm = manifold_to_trimesh(skin_mfd)
    core_tm = manifold_to_trimesh(core_mfd)

    if len(skin_tm.faces) == 0:
        return mesh, None

    return skin_tm, core_tm


# ─── 3MF Writer ──────────────────────────────────────────────────────────────

def write_optimized_3mf(
    parts_out: list[dict],
    other_files: dict,
    output_path: str,
):
    """Write optimized 3MF with skin/core parts."""
    import uuid

    def uuid4():
        return str(uuid.uuid4())

    NS_3MF = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
    NS_PRODUCTION = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
    NS_BAMBU = "http://schemas.bambulab.com/package/2021"
    MAIN_OBJECT_ID = 100
    IDENTITY = "1 0 0 0 1 0 0 0 1 0 0 0"
    IDENTITY_44 = "1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"

    # Build object_1.model
    obj_lines = []
    obj_lines.append('<?xml version="1.0" encoding="UTF-8"?>')
    obj_lines.append(
        f'<model unit="millimeter" xml:lang="en-US" '
        f'xmlns="{NS_3MF}" xmlns:p="{NS_PRODUCTION}" requiredextensions="p" '
        f'xmlns:BambuStudio="{NS_BAMBU}">'
    )
    obj_lines.append(' <resources>')

    for p in parts_out:
        mesh = p["mesh"]
        if mesh is None or len(mesh.faces) == 0:
            continue
        oid = p["id"]
        p_uuid = uuid4()
        obj_lines.append(f'  <object id="{oid}" p:UUID="{p_uuid}" type="model">')
        obj_lines.append("   <mesh>")
        obj_lines.append("    <vertices>")
        for v in mesh.vertices:
            obj_lines.append(f'     <vertex x="{v[0]:.6f}" y="{v[1]:.6f}" z="{v[2]:.6f}"/>')
        obj_lines.append("    </vertices>")
        obj_lines.append("    <triangles>")
        for f in mesh.faces:
            obj_lines.append(f'     <triangle v1="{int(f[0])}" v2="{int(f[1])}" v3="{int(f[2])}"/>')
        obj_lines.append("    </triangles>")
        obj_lines.append("   </mesh>")
        obj_lines.append("  </object>")

    obj_lines.append(' </resources>')
    obj_lines.append(' <build/>')
    obj_lines.append('</model>')
    object_1_xml = "\n".join(obj_lines)

    # Build 3dmodel.model (main with components)
    main_lines = []
    main_lines.append('<?xml version="1.0" encoding="UTF-8"?>')
    main_lines.append(
        f'<model unit="millimeter" xml:lang="en-US" '
        f'xmlns="{NS_3MF}" xmlns:p="{NS_PRODUCTION}" requiredextensions="p" '
        f'xmlns:BambuStudio="{NS_BAMBU}">'
    )
    main_lines.append(' <metadata name="Application">deepseek-pipeline</metadata>')
    main_lines.append(' <metadata name="Title">City Texture 1/125K (skin-core optimized)</metadata>')
    main_lines.append(' <resources>')

    main_lines.append('  <basematerials id="1">')
    for p in parts_out:
        if p["mesh"] is not None and len(p["mesh"].faces) > 0:
            main_lines.append(f'   <base name="{p["name"]}" displaycolor="{p.get("color", "#9A9A9A")}"/>')
    main_lines.append('  </basematerials>')

    main_uuid = uuid4()
    main_lines.append(f'  <object id="{MAIN_OBJECT_ID}" p:UUID="{main_uuid}" type="model">')
    main_lines.append('   <components>')
    for p in parts_out:
        if p["mesh"] is not None and len(p["mesh"].faces) > 0:
            comp_uuid = uuid4()
            main_lines.append(
                f'    <component p:path="/3D/Objects/object_1.model" '
                f'objectid="{p["id"]}" p:UUID="{comp_uuid}" '
                f'transform="{IDENTITY}"/>'
            )
    main_lines.append('   </components>')
    main_lines.append('  </object>')
    main_lines.append(' </resources>')

    build_uuid = uuid4()
    item_uuid = uuid4()
    main_lines.append(f' <build p:UUID="{uuid4()}">')
    main_lines.append(
        f'  <item objectid="{MAIN_OBJECT_ID}" p:UUID="{item_uuid}" '
        f'transform="{IDENTITY}" printable="1"/>'
    )
    main_lines.append(' </build>')
    main_lines.append('</model>')
    main_model_xml = "\n".join(main_lines)

    # Build model_settings.config
    cfg_lines = []
    cfg_lines.append('<?xml version="1.0" encoding="UTF-8"?>')
    cfg_lines.append('<config>')
    cfg_lines.append(f'  <object id="{MAIN_OBJECT_ID}">')
    cfg_lines.append('    <metadata key="name" value="City Texture 1/125K (skin-core optimized)"/>')
    cfg_lines.append('    <metadata key="extruder" value="1"/>')

    for p in parts_out:
        mesh = p["mesh"]
        if mesh is None or len(mesh.faces) == 0:
            continue
        face_count = len(mesh.faces)
        cfg_lines.append(f'    <part id="{p["id"]}" subtype="normal_part">')
        cfg_lines.append(f'      <metadata key="name" value="{p["name"]}"/>')
        cfg_lines.append(f'      <metadata key="extruder" value="{p["extruder"]}"/>')
        cfg_lines.append(f'      <metadata key="matrix" value="{IDENTITY_44}"/>')
        cfg_lines.append(f'      <mesh_stat face_count="{face_count}"/>')
        cfg_lines.append('    </part>')

    cfg_lines.append('  </object>')
    cfg_lines.append('  <plate>')
    cfg_lines.append('    <metadata key="plater_id" value="1"/>')
    cfg_lines.append('    <metadata key="plater_name" value="City"/>')
    cfg_lines.append('    <model_instance>')
    cfg_lines.append(f'      <metadata key="object_id" value="{MAIN_OBJECT_ID}"/>')
    cfg_lines.append('      <metadata key="instance_id" value="0"/>')
    cfg_lines.append('    </model_instance>')
    cfg_lines.append('  </plate>')
    cfg_lines.append('</config>')
    config_xml = "\n".join(cfg_lines)

    # 3D/_rels/3dmodel.model.rels
    main_rels = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        ' <Relationship Target="/3D/Objects/object_1.model" Id="rel-1" '
        'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n'
        '</Relationships>'
    )

    # Write ZIP
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", other_files.get("[Content_Types].xml", b"").decode()
                    if "[Content_Types].xml" in other_files else
                    '<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
                    '  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
                    '  <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
                    '  <Default Extension="config" ContentType="text/xml"/>\n'
                    '</Types>')
        zf.writestr("_rels/.rels", other_files.get("_rels/.rels", b"").decode()
                    if "_rels/.rels" in other_files else
                    '<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
                    '  <Relationship Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"'
                    ' Target="/3D/3dmodel.model" Id="rel0"/>\n'
                    '</Relationships>')
        zf.writestr("3D/_rels/3dmodel.model.rels", main_rels)
        zf.writestr("3D/3dmodel.model", main_model_xml)
        zf.writestr("3D/Objects/object_1.model", object_1_xml)
        zf.writestr("Metadata/model_settings.config", config_xml)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Split 3MF sub-meshes into skin + core (slab Z-plane cut)"
    )
    parser.add_argument("input_3mf", help="Input .3mf file path")
    parser.add_argument("-o", "--output", help="Output .3mf path (default: input_skincore.3mf)")
    parser.add_argument("--skin-mm", type=float, default=None,
                        help="Override skin thickness for all targets (mm)")
    parser.add_argument("--core-extruder", type=int, default=2,
                        help="Extruder for core parts (default: 2 = gray)")
    parser.add_argument("--targets", nargs="*", default=None,
                        help="Only split these parts (e.g. terrain landmarks)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Analyze and report without writing output")
    args = parser.parse_args()

    if not os.path.exists(args.input_3mf):
        print(f"Error: {args.input_3mf} not found")
        sys.exit(1)

    if args.output is None:
        base, ext = os.path.splitext(args.input_3mf)
        args.output = f"{base}_skincore{ext}"

    print(f"Input:  {args.input_3mf}")
    print(f"Output: {args.output}")
    print(f"Core extruder: E{args.core_extruder}")
    print()

    # Parse
    t0 = time.time()
    data = parse_3mf_parts(args.input_3mf)
    print(f"Parsed {len(data['parts'])} parts in {time.time()-t0:.1f}s")
    print()

    # Determine targets
    targets = SPLIT_TARGETS.copy()
    if args.targets:
        targets = {k: v for k, v in targets.items() if k in args.targets}
    if args.skin_mm is not None:
        targets = {k: (args.skin_mm, v[1]) for k, v in targets.items()}

    # Analyze and split
    parts_out = []
    next_id = 20  # IDs for new core parts (avoid collision with existing 1-7)

    print("=" * 60)
    print(f"{'Part':<12} {'Faces':>10} {'Z range':>14} {'Thick':>6} {'Action':<20}")
    print("-" * 60)

    for part in data["parts"]:
        name = part["name"]
        mesh = part["mesh"]

        if mesh is None or len(mesh.faces) == 0:
            parts_out.append(part)
            print(f"{name:<12} {'(empty)':>10} {'':>14} {'':>6} skip (empty)")
            continue

        z_min = mesh.vertices[:, 2].min()
        z_max = mesh.vertices[:, 2].max()
        thickness = z_max - z_min
        z_range = f"{z_min:.1f}→{z_max:.1f}"

        if name not in targets:
            parts_out.append(part)
            print(f"{name:<12} {len(mesh.faces):>10,} {z_range:>14} {thickness:>5.1f}mm skip (not target)")
            continue

        skin_mm, min_thick = targets[name]

        if thickness < min_thick:
            parts_out.append(part)
            print(f"{name:<12} {len(mesh.faces):>10,} {z_range:>14} {thickness:>5.1f}mm skip (too thin)")
            continue

        # Do the split
        t1 = time.time()
        skin_mesh, core_mesh = split_slab(mesh, skin_mm, min_thick)
        dt = time.time() - t1

        if core_mesh is None:
            parts_out.append(part)
            print(f"{name:<12} {len(mesh.faces):>10,} {z_range:>14} {thickness:>5.1f}mm skip (split failed)")
            continue

        skin_faces = len(skin_mesh.faces)
        core_faces = len(core_mesh.faces)
        core_pct = core_mesh.volume / (skin_mesh.volume + core_mesh.volume) * 100 if (skin_mesh.volume + core_mesh.volume) > 0 else 0

        # Skin keeps original extruder
        parts_out.append({
            "id": part["id"],
            "name": f"{name}",
            "extruder": part["extruder"],
            "mesh": skin_mesh,
            "color": "#FFFFFF",
        })
        # Core gets reassigned
        parts_out.append({
            "id": next_id,
            "name": f"{name}_core",
            "extruder": args.core_extruder,
            "mesh": core_mesh,
            "color": "#9A9A9A",
        })
        next_id += 1

        print(
            f"{name:<12} {len(mesh.faces):>10,} {z_range:>14} {thickness:>5.1f}mm "
            f"SPLIT → skin {skin_faces:,}f + core {core_faces:,}f "
            f"({core_pct:.0f}% vol, {dt*1000:.0f}ms)"
        )

    print("=" * 60)
    print()

    # Summary
    original_parts = len(data["parts"])
    final_parts = len([p for p in parts_out if p.get("mesh") is not None and len(p["mesh"].faces) > 0])
    split_count = final_parts - original_parts
    print(f"Parts: {original_parts} → {final_parts} (+{split_count} cores)")

    # Extruder usage summary
    extruders_before = set()
    for p in data["parts"]:
        if p["mesh"] is not None and len(p["mesh"].faces) > 0:
            extruders_before.add(p["extruder"])
    extruders_after = set()
    for p in parts_out:
        if p.get("mesh") is not None and len(p["mesh"].faces) > 0:
            extruders_after.add(p["extruder"])
    print(f"Extruders used: {sorted(extruders_before)} → {sorted(extruders_after)}")

    if args.dry_run:
        print("\n[DRY RUN] No output written.")
        return

    # Write output
    print(f"\nWriting {args.output}...")
    t_write = time.time()
    write_optimized_3mf(parts_out, data["other_files"], args.output)
    file_size_mb = os.path.getsize(args.output) / (1024 * 1024)
    print(f"Done in {time.time()-t_write:.1f}s, size: {file_size_mb:.1f} MB")


if __name__ == "__main__":
    main()
