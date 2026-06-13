"""3MF exporter — reference-style multi-part 3MF (matches Bambu Urban Series).

输出结构（与 demo/杭州 等 reference 文件一致）：

    foo.3mf (ZIP)
    ├── [Content_Types].xml
    ├── _rels/.rels
    ├── 3D/
    │   ├── 3dmodel.model              ← 主文件，含 1 个外部对象 + N 个 component 引用
    │   └── Objects/
    │       └── object_1.model         ← 真正的几何，6 个内部 sub-mesh 各自占一个 <object>
    └── Metadata/
        └── model_settings.config      ← 每个 part 绑 extruder（Bambu Studio 读取）

对比旧的"5 个独立 <object> 内联 mesh"风格：
  - Bambu Studio 把它们当作 1 个可打印模型，不再做对象间几何重叠检测
    （旧版 buildings 嵌入 terrain 0.04mm 触发 684K 非流形冲突报警，本格式消除）
  - XML 体积更紧凑（references 共享几何元数据）
  - 与 demo/* 的 reference 文件结构完全一致
"""

from __future__ import annotations

import os
import uuid as _uuid
import zipfile
from typing import Tuple

import numpy as np
import trimesh

from _TEXTURE_STYLE_OF_DEEPSEEK.config import (
    EXTRUDER_MAP,
    FILAMENT_COLOURS,
    TERRAIN_COLOR,
    BUILDING_COLOR,
    LANDMARK_COLOR,
    ROAD_COLOR,
    WATER_COLOR,
    BASE_WALL_COLOR,
    VEGETATION_COLOR,
    BLOCK_BASE_COLOR,
)

# 3MF namespaces
NS_3MF = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
NS_PRODUCTION = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
NS_BAMBU = "http://schemas.bambulab.com/package/2021"


def _uuid4() -> str:
    """Generate a UUIDv4 hex string for 3MF p:UUID attributes (Bambu Studio
    strictly checks these — missing UUIDs cause '对象体积为零' load error)."""
    return str(_uuid.uuid4())

# Sub-mesh definitions: (objectid_in_sub, name, displaycolor, extruder_1based)
#
# 与 reference 杭州/旧金山一致：每个 sub-mesh 都是单一 closed watertight solid，
# 不再 split surface/walls（split 会产生开口面被 Bambu 当作 124K 非流形）。
#
# terrain key 收的是合并后的 obj4 + buildings + bridges union，整体米白
# （reference mesh 1 + mesh 2 都用 E1 米白色，我们合成一个）。
_SUB_MESH_DEFS: list[Tuple[int, str, str, int]] = [
    (1, "terrain",     TERRAIN_COLOR,    EXTRUDER_MAP["terrain"]),       # E1 灰（含底盖）
    (2, "buildings",   BUILDING_COLOR,   EXTRUDER_MAP["buildings"]),     # E1 灰（block_fill 街区填充，融入 terrain）
    (3, "roads",       ROAD_COLOR,       EXTRUDER_MAP["roads"]),         # E2 黑
    (4, "water",       WATER_COLOR,      EXTRUDER_MAP["water"]),         # E3 黑
    (5, "vegetation",  VEGETATION_COLOR, EXTRUDER_MAP["vegetation"]),    # E4 绿
    (6, "landmarks",   LANDMARK_COLOR,   EXTRUDER_MAP["landmarks"]),     # E5 暖砂石（地标突出）
    (7, "block_base",  BLOCK_BASE_COLOR, EXTRUDER_MAP["block_base"]),    # E6 暖米色（PNG layer 1.5 城市底）
]

# 3MF 主对象 id（包含所有 components 的外层对象，与 reference 杭州的 id=5 对应）
_MAIN_OBJECT_ID = 100

# Identity transform 4×4（行主序 9 位旋转 + 3 位平移），全部 Z 偏移已经焙到几何里
_IDENTITY_TRANSFORM = "1 0 0 0 1 0 0 0 1 0 0 0"


# ---------------------------------------------------------------------------
# Geometry XML — 写入 3D/Objects/object_1.model
# ---------------------------------------------------------------------------


def _format_sub_mesh(oid: int, mesh: trimesh.Trimesh, p_uuid: str) -> str:
    """单个 sub-mesh 的 XML（一个 <object id="N" p:UUID="..."> 内含 <mesh>）。"""
    if mesh is None or len(mesh.faces) == 0:
        return ""

    verts = mesh.vertices
    faces = mesh.faces

    parts = [f'  <object id="{oid}" p:UUID="{p_uuid}" type="model">']
    parts.append("   <mesh>")
    parts.append("    <vertices>")
    for v in verts:
        parts.append(f'     <vertex x="{v[0]:.6f}" y="{v[1]:.6f}" z="{v[2]:.6f}"/>')
    parts.append("    </vertices>")
    parts.append("    <triangles>")
    for f in faces:
        parts.append(f'     <triangle v1="{int(f[0])}" v2="{int(f[1])}" v3="{int(f[2])}"/>')
    parts.append("    </triangles>")
    parts.append("   </mesh>")
    parts.append("  </object>")
    return "\n".join(parts)


def _build_object_1_model(active_meshes: list, sub_uuids: list) -> str:
    """3D/Objects/object_1.model — 收纳所有 sub-mesh 的几何文件。

    active_meshes: list of (oid, name, displaycolor, extruder, mesh)
    sub_uuids:     list of UUID strings, one per sub-mesh
    """
    body = []
    body.append('<?xml version="1.0" encoding="UTF-8"?>')
    body.append(
        f'<model unit="millimeter" xml:lang="en-US" '
        f'xmlns="{NS_3MF}" xmlns:p="{NS_PRODUCTION}" requiredextensions="p" '
        f'xmlns:BambuStudio="{NS_BAMBU}">'
    )
    body.append(' <resources>')
    for (oid, _name, _color, _ext, mesh), uid in zip(active_meshes, sub_uuids):
        body.append(_format_sub_mesh(oid, mesh, uid))
    body.append(' </resources>')
    body.append(' <build/>')
    body.append('</model>')
    return "\n".join(b for b in body if b)


# ---------------------------------------------------------------------------
# Main XML — 3D/3dmodel.model
# ---------------------------------------------------------------------------


def _build_main_model(active_meshes: list, sub_uuids: list,
                       main_uuid: str, build_uuid: str, item_uuid: str,
                       build_root_uuid: str) -> str:
    """主 3D/3dmodel.model：1 个外部对象 + N 个 components 引用 object_1.model。

    Bambu Studio 严格要求 p:UUID — 缺失会导致 "对象体积为零" 加载错误。
    """
    body = []
    body.append('<?xml version="1.0" encoding="UTF-8"?>')
    body.append(
        f'<model unit="millimeter" xml:lang="en-US" '
        f'xmlns="{NS_3MF}" xmlns:p="{NS_PRODUCTION}" requiredextensions="p" '
        f'xmlns:BambuStudio="{NS_BAMBU}">'
    )
    body.append(' <metadata name="Application">deepseek-pipeline</metadata>')
    body.append(' <metadata name="Title">City Texture 1/125K</metadata>')
    body.append(' <resources>')

    # basematerials
    body.append('  <basematerials id="1">')
    for _oid, name, color, _ext, _mesh in active_meshes:
        body.append(f'   <base name="{name}" displaycolor="{color}"/>')
    body.append('  </basematerials>')

    # 主外部对象 id=100，含全部 components
    body.append(f'  <object id="{_MAIN_OBJECT_ID}" p:UUID="{main_uuid}" type="model">')
    body.append('   <components>')
    for (oid, _name, _color, _ext, _mesh), uid in zip(active_meshes, sub_uuids):
        # component 的 p:UUID 与 sub-mesh 的 UUID 不同，但通常是基于 sub UUID 派生
        comp_uuid = _uuid4()
        body.append(
            f'    <component p:path="/3D/Objects/object_1.model" '
            f'objectid="{oid}" p:UUID="{comp_uuid}" '
            f'transform="{_IDENTITY_TRANSFORM}"/>'
        )
    body.append('   </components>')
    body.append('  </object>')
    body.append(' </resources>')

    # build 区块
    body.append(f' <build p:UUID="{build_root_uuid}">')
    body.append(
        f'  <item objectid="{_MAIN_OBJECT_ID}" p:UUID="{item_uuid}" '
        f'transform="{_IDENTITY_TRANSFORM}" printable="1"/>'
    )
    body.append(' </build>')
    body.append('</model>')
    return "\n".join(body)


# ---------------------------------------------------------------------------
# model_settings.config — Bambu Studio 读取的 per-part metadata
# ---------------------------------------------------------------------------


def _build_model_settings(active_meshes: list) -> str:
    """Metadata/model_settings.config — 把每个 sub-mesh 标为 part，绑 extruder。

    与 reference 杭州/旧金山的 config 结构一致：
        <object id="100">
          <metadata key="name" value="..."/>
          <part id="N">
            <metadata key="name" value="terrain_surface"/>
            <metadata key="extruder" value="1"/>
            <metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>
          </part>
          ...
        </object>
    """
    lines = []
    lines.append('<?xml version="1.0" encoding="UTF-8"?>')
    lines.append('<config>')
    lines.append(f'  <object id="{_MAIN_OBJECT_ID}">')
    lines.append('    <metadata key="name" value="City Texture 1/125K"/>')
    lines.append(f'    <metadata key="extruder" value="1"/>')

    # 4×4 identity matrix（行优先按 model_settings 的写法）
    identity_44 = "1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"

    for oid, name, _color, ext, mesh in active_meshes:
        face_count = len(mesh.faces) if mesh is not None else 0
        lines.append(f'    <part id="{oid}" subtype="normal_part">')
        lines.append(f'      <metadata key="name" value="{name}"/>')
        lines.append(f'      <metadata key="extruder" value="{ext}"/>')
        lines.append(f'      <metadata key="matrix" value="{identity_44}"/>')
        lines.append(f'      <mesh_stat face_count="{face_count}"/>')
        lines.append('    </part>')

    lines.append('  </object>')

    # 第一台 plate（包含主对象）
    lines.append('  <plate>')
    lines.append('    <metadata key="plater_id" value="1"/>')
    lines.append('    <metadata key="plater_name" value="City"/>')
    lines.append('    <model_instance>')
    lines.append(f'      <metadata key="object_id" value="{_MAIN_OBJECT_ID}"/>')
    lines.append('      <metadata key="instance_id" value="0"/>')
    lines.append('    </model_instance>')
    lines.append('  </plate>')
    lines.append('</config>')
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 静态文件
# ---------------------------------------------------------------------------

_CONTENT_TYPES = '''<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
  <Default Extension="config" ContentType="text/xml"/>
</Types>'''

_RELS = '''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"
                Target="/3D/3dmodel.model" Id="rel0"/>
</Relationships>'''

# 主 model 引用 sub-file (object_*.model) 时，必须在 3D/_rels/3dmodel.model.rels
# 里登记 Relationship。Bambu Studio 按 OPC 规范严格检查，缺这个会导致主 model
# 的 component 解不到几何，报"对象体积为零"错误。
_MAIN_RELS_TEMPLATE = '''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
{relationships}
</Relationships>'''


def _build_main_rels(sub_paths: list) -> str:
    """生成 3D/_rels/3dmodel.model.rels — 登记 main model 引用的每个 sub-file。"""
    rels = []
    for i, path in enumerate(sub_paths, start=1):
        rels.append(
            f' <Relationship Target="{path}" Id="rel-{i}" '
            f'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
        )
    return _MAIN_RELS_TEMPLATE.format(relationships="\n".join(rels))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def export_deepseek_3mf(meshes: dict, output_path: str) -> str:
    """Write a reference-style 3MF with shared geometry via components.

    Args:
        meshes: dict with keys 'terrain_surface', 'terrain_walls', 'buildings',
            'roads', 'water', 'vegetation'. Missing/None/empty entries are dropped.
        output_path: target .3mf path.

    Returns:
        output_path on success.
    """
    parent = os.path.dirname(output_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    # 收集非空的 sub-mesh
    active = []
    for oid, name, color, ext in _SUB_MESH_DEFS:
        mesh = meshes.get(name)
        if mesh is None or len(mesh.faces) == 0:
            continue
        active.append((oid, name, color, ext, mesh))

    if not active:
        raise ValueError("No non-empty meshes to export")

    # 为每个 sub-mesh + 主对象 + build/item 生成 UUID
    sub_uuids = [_uuid4() for _ in active]
    main_uuid = _uuid4()
    build_root_uuid = _uuid4()
    item_uuid = _uuid4()
    build_uuid = _uuid4()

    # 构造 3 个 XML
    object_1_xml = _build_object_1_model(active, sub_uuids)
    main_xml = _build_main_model(
        active, sub_uuids, main_uuid, build_uuid, item_uuid, build_root_uuid
    )
    settings_xml = _build_model_settings(active)

    # 登记主 model 引用的所有 sub-file（OPC 规范，Bambu 严格检查）
    main_rels = _build_main_rels(["/3D/Objects/object_1.model"])

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("_rels/.rels", _RELS)
        zf.writestr("3D/_rels/3dmodel.model.rels", main_rels)  # ★ 关键：登记 sub-file 引用
        zf.writestr("3D/3dmodel.model", main_xml)
        zf.writestr("3D/Objects/object_1.model", object_1_xml)
        zf.writestr("Metadata/model_settings.config", settings_xml)

    return output_path


def split_terrain_mesh(terrain_solid: trimesh.Trimesh) -> dict:
    """Split a watertight terrain solid into top surface vs walls/bottom.

    Faces with normal Z > 0.1 are top surface; the rest are walls and bottom.
    Each subset is repacked into its own trimesh with remapped vertex indices.
    """
    if terrain_solid is None or len(terrain_solid.faces) == 0:
        return {"terrain_surface": None, "terrain_walls": None}

    z_component = terrain_solid.face_normals[:, 2]
    surface_mask = z_component > 0.1
    walls_mask = ~surface_mask

    def _repack(mask):
        idx = np.where(mask)[0]
        if len(idx) == 0:
            return None
        sub_faces = terrain_solid.faces[idx]
        used, inverse = np.unique(sub_faces, return_inverse=True)
        return trimesh.Trimesh(
            vertices=terrain_solid.vertices[used],
            faces=inverse.reshape(sub_faces.shape),
        )

    return {
        "terrain_surface": _repack(surface_mask),
        "terrain_walls": _repack(walls_mask),
    }
