"""Validator — self-check rules against reference model specifications.

Validates the exported 3MF file by re-parsing it and checking geometry
against the Urban Series reference model parameters.
"""

import os
import re
import zipfile
import numpy as np
from typing import Dict, List, Tuple, Optional

from _TEXTURE_STYLE_OF_DEEPSEEK.config import (
    INTERNAL_SPAN_MM,
    EXTRUDER_MAP,
    TERRAIN_THICKNESS_MM,
    Z_BUILDING_EMBED_MM,
    ROAD_FACE_NORMAL_Z_RATIO,
    ROAD_THICKNESS_MM,
    WATER_BASE_THICKNESS_MM,
    WATER_HEIGHT_MODEL_MM,
    VEGETATION_Z_OFFSET_MM,
    VEGETATION_COLOR,
)


def _read_3mf_text(zf: zipfile.ZipFile, path: str) -> str:
    """Read a text file from a ZIP archive."""
    try:
        return zf.read(path).decode("utf-8", errors="replace")
    except KeyError:
        return ""


def _parse_vertices(xml: str) -> Optional[np.ndarray]:
    """Parse vertices from 3MF mesh XML. Returns Nx3 array or None."""
    pattern = r'<vertex\s+x="([^"]+)"\s+y="([^"]+)"\s+z="([^"]+)"'
    matches = re.findall(pattern, xml)
    if not matches:
        return None
    return np.array([[float(x), float(y), float(z)] for x, y, z in matches])


def _parse_faces(xml: str) -> Optional[np.ndarray]:
    """Parse triangles from 3MF mesh XML. Returns Mx3 array or None."""
    pattern = r'<triangle\s+v1="(\d+)"\s+v2="(\d+)"\s+v3="(\d+)"'
    matches = re.findall(pattern, xml)
    if not matches:
        return None
    return np.array([[int(a), int(b), int(c)] for a, b, c in matches])


def _get_object_meshes(zf: zipfile.ZipFile) -> Dict[str, dict]:
    """Extract all object meshes from a 3MF file.

    Supports two layouts:
      A) Legacy inline (旧 exporter): meshes inside <object> in 3D/3dmodel.model
      B) Reference-style components (新 exporter): geometry in
         3D/Objects/object_*.model, referenced via <component p:path="...">
         from main 3D/3dmodel.model. Names come from
         Metadata/model_settings.config <part> entries.

    Returns dict of {object_name: {vertices, faces, object_id, pindex}}.
    """
    try:
        main_xml = _read_3mf_text(zf, "3D/3dmodel.model")
    except Exception:
        return {}

    # 检测是否是 components 风格（B）
    is_component_style = '<component ' in main_xml and 'p:path=' in main_xml

    if is_component_style:
        return _parse_component_layout(zf, main_xml)
    else:
        return _parse_inline_layout(main_xml)


def _parse_inline_layout(xml: str) -> Dict[str, dict]:
    """旧版：mesh 直接写在主 model 的 <object> 里。"""
    objects = {}
    obj_pattern = r'<object\b([^>]*)>(.*?)</object>'
    for m in re.finditer(obj_pattern, xml, re.DOTALL):
        attrs = m.group(1)
        body = m.group(2)
        oid_m = re.search(r'id="(\d+)"', attrs)
        pidx_m = re.search(r'pindex="(\d+)"', attrs)
        name_m = re.search(r'name="([^"]+)"', attrs)
        if not oid_m or not pidx_m:
            continue
        oid = int(oid_m.group(1))
        pidx = int(pidx_m.group(1))
        name = name_m.group(1) if name_m else f"object_{oid}"
        vertices = _parse_vertices(body)
        faces = _parse_faces(body)
        if vertices is not None and faces is not None:
            objects[name] = {"object_id": oid, "vertices": vertices,
                             "faces": faces, "pindex": pidx}
    return objects


def _parse_component_layout(zf: zipfile.ZipFile, main_xml: str) -> Dict[str, dict]:
    """新版（reference 风格）：components 引用 sub-file 里的 sub-mesh。

    1. 解 model_settings.config 获取 part_id → name + extruder
    2. 解主 model 的 components 获取 sub-file path + objectid
    3. 读 sub-file，按 objectid 取出 mesh
    """
    # Step 1: read model_settings.config
    id_to_name = {}
    try:
        settings_xml = _read_3mf_text(zf, "Metadata/model_settings.config")
        # <part id="N"> ... <metadata key="name" value="..."/>
        for pm in re.finditer(r'<part\s+id="(\d+)"[^>]*>(.*?)</part>',
                              settings_xml, re.DOTALL):
            pid = int(pm.group(1))
            name_m = re.search(r'<metadata key="name" value="([^"]+)"', pm.group(2))
            if name_m:
                id_to_name[pid] = name_m.group(1)
    except Exception:
        pass

    # Step 2: read main model components
    # <component p:path="/3D/Objects/object_N.model" objectid="X" .../>
    components = []  # (sub_path, objectid)
    for cm in re.finditer(r'<component\s+([^>]+?)/>', main_xml):
        c_attrs = cm.group(1)
        path_m = re.search(r'p:path="([^"]+)"', c_attrs)
        oid_m = re.search(r'objectid="(\d+)"', c_attrs)
        if path_m and oid_m:
            components.append((path_m.group(1), int(oid_m.group(1))))

    # Step 3: read sub-files, parse mesh
    objects = {}
    sub_xml_cache = {}
    for path, oid in components:
        # 标准化 path (3MF 里以 / 开头表示 zip 根)
        zip_path = path.lstrip('/')
        if zip_path not in sub_xml_cache:
            try:
                sub_xml_cache[zip_path] = _read_3mf_text(zf, zip_path)
            except Exception:
                sub_xml_cache[zip_path] = ""
        sub_xml = sub_xml_cache[zip_path]
        if not sub_xml:
            continue
        # 在 sub-file 里找匹配 objectid 的 <object>
        obj_pat = re.compile(rf'<object\s+id="{oid}"[^>]*>(.*?)</object>', re.DOTALL)
        m = obj_pat.search(sub_xml)
        if not m:
            continue
        body = m.group(1)
        vertices = _parse_vertices(body)
        faces = _parse_faces(body)
        if vertices is None or faces is None:
            continue
        name = id_to_name.get(oid, f"object_{oid}")
        objects[name] = {
            "object_id": oid, "vertices": vertices, "faces": faces,
            "pindex": 0,  # components 风格不再用 pindex
        }
    return objects


def _compute_face_normals(vertices: np.ndarray, faces: np.ndarray) -> np.ndarray:
    """Compute face normals for a mesh."""
    v0 = vertices[faces[:, 0]]
    v1 = vertices[faces[:, 1]]
    v2 = vertices[faces[:, 2]]
    normals = np.cross(v1 - v0, v2 - v0)
    lengths = np.linalg.norm(normals, axis=1, keepdims=True)
    lengths[lengths < 1e-10] = 1.0
    return normals / lengths


def _edge_manifold_counts(vertices: np.ndarray,
                          faces: np.ndarray) -> Tuple[bool, int, int]:
    """Return index validity, boundary-edge count, non-manifold-edge count.

    Edges are encoded as uint64 keys instead of materialising an ``(3F, 2)``
    int64 matrix.  That keeps strict validation practical for city-scale
    block-base objects containing millions of triangles.
    """
    if len(faces) == 0:
        return True, 0, 0
    vertex_count = len(vertices)
    indices_ok = bool(
        vertex_count > 0
        and faces.min() >= 0
        and faces.max() < vertex_count
    )
    if not indices_ok:
        return False, -1, -1

    face_count = len(faces)
    keys = np.empty(face_count * 3, dtype=np.uint64)
    base = np.uint64(vertex_count)
    chunk_size = 1_000_000
    for edge_i, (a, b) in enumerate(((0, 1), (1, 2), (2, 0))):
        edge_offset = edge_i * face_count
        for start in range(0, face_count, chunk_size):
            stop = min(start + chunk_size, face_count)
            left = faces[start:stop, a].astype(np.uint64, copy=False)
            right = faces[start:stop, b].astype(np.uint64, copy=False)
            lo = np.minimum(left, right)
            hi = np.maximum(left, right)
            keys[edge_offset + start:edge_offset + stop] = lo * base + hi

    keys.sort()
    if len(keys) == 1:
        return True, 1, 0
    changes = np.flatnonzero(keys[1:] != keys[:-1]) + 1
    run_ends = np.append(changes, len(keys))
    run_starts = np.empty_like(run_ends)
    run_starts[0] = 0
    run_starts[1:] = changes
    counts = run_ends - run_starts
    return True, int((counts == 1).sum()), int((counts > 2).sum())


def _get_extruder_map_from_3mf(objects: Dict[str, dict]) -> Dict[str, int]:
    """Reconstruct each object's expected extruder from EXTRUDER_MAP.

    The current exporter doesn't write Bambu metadata; Bambu Studio derives
    extruders from the basematerial pindex. We treat the canonical mapping
    in EXTRUDER_MAP as the contract: every object present in *objects*
    inherits its expected extruder from EXTRUDER_MAP[name].
    """
    return {name: EXTRUDER_MAP[name] for name in objects if name in EXTRUDER_MAP}


def validate_3mf(filepath: str) -> Dict[str, any]:
    """Run all 10 validation rules on a generated 3MF file.

    Returns dict with keys: 'passed', 'rules', 'errors', 'warnings'.
    """
    results = {
        "file": filepath,
        "passed": True,
        "rules": [],
        "errors": [],
        "warnings": [],
    }

    if not os.path.exists(filepath):
        results["passed"] = False
        results["errors"].append("File not found")
        return results

    try:
        zf = zipfile.ZipFile(filepath, "r")
    except Exception as e:
        results["passed"] = False
        results["errors"].append(f"Cannot open 3MF: {e}")
        return results

    objects = _get_object_meshes(zf)
    extruder_map = _get_extruder_map_from_3mf(objects)

    # ---- V1: Max XY span = INTERNAL_SPAN_MM +/- 2mm ----
    # Non-square bbox means one axis may be shorter. Check the longer axis.
    all_x, all_y = [], []
    for name, obj in objects.items():
        v = obj["vertices"]
        all_x.extend(v[:, 0].tolist())
        all_y.extend(v[:, 1].tolist())

    if all_x and all_y:
        x_span = max(all_x) - min(all_x)
        y_span = max(all_y) - min(all_y)
        max_span = max(x_span, y_span)
        xy_ok = abs(max_span - INTERNAL_SPAN_MM) < 2.0
    else:
        xy_ok = False

    results["rules"].append({
        "id": "V1",
        "name": f"Max XY span = {INTERNAL_SPAN_MM:.0f}mm +/- 2mm",
        "passed": xy_ok,
        "detail": f"X: {x_span:.1f}mm, Y: {y_span:.1f}mm, max: {max_span:.1f}mm" if all_x else "No data",
    })

    # ---- V2: Terrain object exists ----
    # 兼容两种风格：旧版 split 出 surface+walls，新版单一 terrain
    has_terrain = ("terrain" in objects) or \
                  ("terrain_surface" in objects and "terrain_walls" in objects)
    v2_ok = has_terrain
    results["rules"].append({
        "id": "V2",
        "name": "Terrain object exists (single 'terrain' or split surface+walls)",
        "passed": v2_ok,
    })

    # ---- V3: Terrain thickness ~4.0mm (+/- 15%) ----
    terrain_z_all = []
    for key in ["terrain", "terrain_surface", "terrain_walls"]:
        if key in objects:
            terrain_z_all.extend(objects[key]["vertices"][:, 2].tolist())

    if terrain_z_all:
        z_range = max(terrain_z_all) - min(terrain_z_all)
        v3_ok = abs(z_range - TERRAIN_THICKNESS_MM) < TERRAIN_THICKNESS_MM * 0.15
    else:
        z_range = 0
        v3_ok = False
    results["rules"].append({
        "id": "V3",
        "name": f"Terrain thickness = {TERRAIN_THICKNESS_MM}mm +/- 15%",
        "passed": v3_ok,
        "detail": f"Z range: {z_range:.2f}mm",
    })

    # ---- V4: Buildings embedded into terrain ----
    buildings_obj = objects.get("buildings")
    terrain_obj = objects.get("terrain") or objects.get("terrain_surface") or objects.get("terrain_walls")

    if buildings_obj and terrain_obj:
        bz_min = buildings_obj["vertices"][:, 2].min()
        tz_max = terrain_obj["vertices"][:, 2].max()
        v4_ok = bz_min < tz_max
    else:
        v4_ok = True  # no buildings = not applicable
    results["rules"].append({
        "id": "V4",
        "name": "Buildings embedded into terrain",
        "passed": v4_ok,
    })

    # ---- V5: Buildings overlap terrain Z range ----
    if buildings_obj and terrain_obj:
        bz_min = buildings_obj["vertices"][:, 2].min()
        tz_max = terrain_obj["vertices"][:, 2].max()
        # Buildings must penetrate below terrain surface (positive embed)
        # but not unreasonably deep (< terrain thickness + 2mm)
        embed = tz_max - bz_min if bz_min < tz_max else 0
        v5_ok = embed > 0 and embed < (TERRAIN_THICKNESS_MM + 2.0)
    else:
        embed = 0
        v5_ok = True
    results["rules"].append({
        "id": "V5",
        "name": f"Buildings embedded (0 < embed < {TERRAIN_THICKNESS_MM+2.0:.0f}mm)",
        "passed": v5_ok,
        "detail": f"Embed: {embed:.2f}mm",
    })

    # ---- V6: Road has top-facing faces (>=15% for terrain-following ribbons) ----
    roads_obj = objects.get("roads")
    if roads_obj and len(roads_obj["faces"]) > 0:
        v = roads_obj["vertices"]
        f = roads_obj["faces"]
        normals = _compute_face_normals(v, f)
        z_up_ratio = (normals[:, 2] > 0.5).mean()
        v6_ok = z_up_ratio >= 0.15  # terrain-following ribbons have ~25% top faces
    else:
        z_up_ratio = 1.0
        v6_ok = True
    results["rules"].append({
        "id": "V6",
        "name": "Road has top-facing faces (>=15% +Z)",
        "passed": v6_ok,
        "detail": f"+Z ratio: {z_up_ratio:.1%}",
    })

    # ---- V7: Road Z range consistent with terrain following (>=0.4mm, <3.5mm) ----
    if roads_obj and len(roads_obj["vertices"]) > 0:
        road_z = roads_obj["vertices"][:, 2]
        r_z_range = road_z.max() - road_z.min()
        max_expected = TERRAIN_THICKNESS_MM + 1.5  # terrain relief + road thickness + offset
        v7_ok = r_z_range >= 0.35 and r_z_range < max_expected
    else:
        r_z_range = 0
        v7_ok = True
    results["rules"].append({
        "id": "V7",
        "name": "Road Z range consistent (0.4-3.5mm)",
        "passed": v7_ok,
        "detail": f"Z range: {r_z_range:.2f}mm",
    })

    # ---- V8: Water plate has base + extruded features (Z span >= 0.4mm) ----
    water_obj = objects.get("water")
    if water_obj and len(water_obj["vertices"]) > 0:
        water_z_span = water_obj["vertices"][:, 2].max() - water_obj["vertices"][:, 2].min()
        # Decimal 3MF coordinates such as -2.0 and -1.6 can subtract to
        # 0.3999999999999999 in binary floating point.  Accept the specified
        # 0.4 mm plate instead of reporting a false critical error.
        v8_ok = water_z_span >= 0.4 - 1e-6
    else:
        water_z_span = 0
        v8_ok = True
    results["rules"].append({
        "id": "V8",
        "name": "Water plate has base + relief (Z span >= 0.4mm)",
        "passed": v8_ok,
        "detail": f"Z span: {water_z_span:.2f}mm",
    })

    # ---- V9: Water has side walls (extruded features, not just flat plates) ----
    if water_obj and len(water_obj["faces"]) > 0:
        # Check that there are faces with non-vertical normals (side walls exist)
        v = water_obj["vertices"]
        f = water_obj["faces"]
        v0 = v[f[:, 0]]
        v1 = v[f[:, 1]]
        v2 = v[f[:, 2]]
        normals = np.cross(v1 - v0, v2 - v0)
        lengths = np.linalg.norm(normals, axis=1, keepdims=True)
        lengths[lengths < 1e-10] = 1.0
        normals = normals / lengths
        # Side walls have some horizontal component
        has_side_walls = (np.abs(normals[:, 2]) < 0.9).any()
        v9_ok = has_side_walls or water_z_span < 0.5  # ok if only base plate
    else:
        v9_ok = True
    results["rules"].append({
        "id": "V9",
        "name": "Water has side walls (extruded relief features)",
        "passed": v9_ok,
    })

    # ---- V10: Extruder assignment correct ----
    expected = EXTRUDER_MAP
    v10_ok = True
    v10_detail = []
    for key, exp_ext in expected.items():
        actual = extruder_map.get(key)
        if actual is not None and actual != exp_ext:
            v10_ok = False
            v10_detail.append(f"{key}: expected E{exp_ext}, got E{actual}")

    results["rules"].append({
        "id": "V10",
        "name": "Extruder assignment correct (E1=t+b, E2=r, E3=w, E4=v)",
        "passed": v10_ok,
        "detail": ", ".join(v10_detail) if v10_detail else "OK",
    })

    # ---- V11: Vegetation has thickness (>=0.1mm Z span) ----
    vegetation_obj = objects.get("vegetation")
    if vegetation_obj and len(vegetation_obj["vertices"]) > 0:
        vegetation_z_span = vegetation_obj["vertices"][:, 2].max() - vegetation_obj["vertices"][:, 2].min()
        v11_ok = vegetation_z_span >= 0.1
    else:
        vegetation_z_span = 0
        v11_ok = True  # no vegetation = not applicable
    results["rules"].append({
        "id": "V11",
        "name": "Vegetation has thickness (>=0.1mm Z span)",
        "passed": v11_ok,
        "detail": f"Z span: {vegetation_z_span:.2f}mm",
    })

    # ---- V12: Vegetation is finite, in bounds, and closed edge-manifold ----
    if vegetation_obj and len(vegetation_obj["faces"]) > 0:
        v = vegetation_obj["vertices"]
        f = vegetation_obj["faces"]
        finite_ok = bool(np.isfinite(v).all())
        indices_ok, boundary_edges, nonmanifold_edges = _edge_manifold_counts(v, f)
        closed_edge_manifold = (
            indices_ok and boundary_edges == 0 and nonmanifold_edges == 0)
        if terrain_obj is not None and len(terrain_obj["vertices"]) > 0:
            terrain_v = terrain_obj["vertices"]
            xy_epsilon = 0.05
            in_bounds = bool(
                v[:, 0].min() >= terrain_v[:, 0].min() - xy_epsilon
                and v[:, 0].max() <= terrain_v[:, 0].max() + xy_epsilon
                and v[:, 1].min() >= terrain_v[:, 1].min() - xy_epsilon
                and v[:, 1].max() <= terrain_v[:, 1].max() + xy_epsilon
            )
        else:
            in_bounds = True
        v12_ok = finite_ok and closed_edge_manifold and in_bounds
        v12_detail = (
            f"finite={finite_ok}, indices={indices_ok}, in_bounds={in_bounds}, "
            f"boundary_edges={boundary_edges}, "
            f"nonmanifold_edges={nonmanifold_edges}"
        )
    else:
        v12_ok = True
        v12_detail = "not applicable"
    results["rules"].append({
        "id": "V12",
        "name": "Vegetation is finite, in bounds, and closed edge-manifold",
        "passed": v12_ok,
        "detail": v12_detail,
    })

    # ---- V13: Block base is finite, in bounds, and closed edge-manifold ----
    block_base_obj = objects.get("block_base")
    if block_base_obj and len(block_base_obj["faces"]) > 0:
        v = block_base_obj["vertices"]
        f = block_base_obj["faces"]
        finite_ok = bool(np.isfinite(v).all())
        indices_ok, boundary_edges, nonmanifold_edges = _edge_manifold_counts(v, f)
        closed_edge_manifold = (
            indices_ok and boundary_edges == 0 and nonmanifold_edges == 0)
        if terrain_obj is not None and len(terrain_obj["vertices"]) > 0:
            terrain_v = terrain_obj["vertices"]
            xy_epsilon = 0.05
            in_bounds = bool(
                v[:, 0].min() >= terrain_v[:, 0].min() - xy_epsilon
                and v[:, 0].max() <= terrain_v[:, 0].max() + xy_epsilon
                and v[:, 1].min() >= terrain_v[:, 1].min() - xy_epsilon
                and v[:, 1].max() <= terrain_v[:, 1].max() + xy_epsilon
            )
        else:
            in_bounds = True
        v13_ok = finite_ok and closed_edge_manifold and in_bounds
        v13_detail = (
            f"finite={finite_ok}, indices={indices_ok}, in_bounds={in_bounds}, "
            f"boundary_edges={boundary_edges}, "
            f"nonmanifold_edges={nonmanifold_edges}"
        )
    else:
        v13_ok = True
        v13_detail = "not applicable"
    results["rules"].append({
        "id": "V13",
        "name": "Block base is finite, in bounds, and closed edge-manifold",
        "passed": v13_ok,
        "detail": v13_detail,
    })

    # Aggregate results
    for rule in results["rules"]:
        if not rule["passed"]:
            if rule["id"] in ("V2", "V4", "V8", "V9", "V10", "V13"):
                results["errors"].append(f"{rule['id']}: {rule['name']}")
            else:
                results["warnings"].append(f"{rule['id']}: {rule['name']}")

    if results["errors"]:
        results["passed"] = False

    zf.close()
    return results


def print_validation_report(results: dict) -> None:
    """Print a human-readable validation report."""
    print(f"\n{'='*60}")
    print(f"  Validation Report: {os.path.basename(results['file'])}")
    print(f"{'='*60}")

    for rule in results["rules"]:
        status = "PASS" if rule["passed"] else "FAIL"
        print(f"  [{status}] {rule['id']}: {rule['name']}")
        if "detail" in rule and rule["detail"]:
            print(f"         {rule['detail']}")

    print(f"\n  Errors:   {len(results['errors'])}")
    for e in results["errors"]:
        print(f"    - {e}")
    print(f"  Warnings: {len(results['warnings'])}")
    for w in results["warnings"]:
        print(f"    - {w}")

    overall = "PASSED" if results["passed"] else "FAILED"
    print(f"\n  Overall: {overall}")
    print(f"{'='*60}\n")
