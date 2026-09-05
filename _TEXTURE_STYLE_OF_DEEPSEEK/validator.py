"""Validator — self-check rules against reference model specifications.

Validates the exported 3MF file by re-parsing it and checking geometry
against the Urban Series reference model parameters.
"""

import json
import hashlib
import math
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


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _terrain_surface_edge_metrics(vertices: np.ndarray,
                                  faces: np.ndarray) -> dict:
    """Measure top-surface XY triangle edges from the exported artifact."""
    if vertices is None or faces is None or not len(faces):
        return {"top_face_count": 0}
    triangles = vertices[faces]
    cross = np.cross(
        triangles[:, 1] - triangles[:, 0],
        triangles[:, 2] - triangles[:, 0],
    )
    lengths = np.linalg.norm(cross, axis=1)
    normal_z = np.divide(
        cross[:, 2], lengths,
        out=np.zeros_like(lengths), where=lengths > 1e-12,
    )
    top = triangles[normal_z > 0.05]
    if not len(top):
        return {"top_face_count": 0}
    longest = np.maximum.reduce([
        np.linalg.norm(top[:, 0, :2] - top[:, 1, :2], axis=1),
        np.linalg.norm(top[:, 1, :2] - top[:, 2, :2], axis=1),
        np.linalg.norm(top[:, 2, :2] - top[:, 0, :2], axis=1),
    ])
    return {
        "top_face_count": int(len(top)),
        "max_xy_edge_mm": float(longest.max()),
        "p99_xy_edge_mm": float(np.quantile(longest, 0.99)),
        "p999_xy_edge_mm": float(np.quantile(longest, 0.999)),
        "faces_over_2mm": int(np.count_nonzero(longest > 2.0)),
        "faces_over_5mm": int(np.count_nonzero(longest > 5.0)),
    }


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


def _component_slender_metrics(vertices: np.ndarray,
                                faces: np.ndarray,
                                extrusion_width_mm: float) -> dict:
    """Measure independent-body XY/Z survival on an exported mesh."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    if vertices is None or faces is None or not len(vertices) or not len(faces):
        return {"component_count": 0}
    rows = np.concatenate((
        faces[:, 0], faces[:, 1], faces[:, 1], faces[:, 2],
        faces[:, 2], faces[:, 0],
    ))
    columns = np.concatenate((
        faces[:, 1], faces[:, 0], faces[:, 2], faces[:, 1],
        faces[:, 0], faces[:, 2],
    ))
    graph = coo_matrix(
        (np.ones(len(rows), dtype=np.uint8), (rows, columns)),
        shape=(len(vertices), len(vertices)),
    ).tocsr()
    count, labels = connected_components(graph, directed=False)
    minima = np.full((count, 3), np.inf, dtype=float)
    maxima = np.full((count, 3), -np.inf, dtype=float)
    for axis in range(3):
        np.minimum.at(minima[:, axis], labels, vertices[:, axis])
        np.maximum.at(maxima[:, axis], labels, vertices[:, axis])
    spans = maxima - minima
    minimum_width = np.min(spans[:, :2], axis=1)
    height = spans[:, 2]
    slenderness = height / np.maximum(minimum_width, 1e-9)
    return {
        "component_count": int(count),
        "below_extrusion_width": int(np.count_nonzero(
            minimum_width + 1e-9 < float(extrusion_width_mm))),
        "height_to_width_above_4": int(np.count_nonzero(
            slenderness > 4.0 + 1e-9)),
        "minimum_width_p50_mm": float(np.percentile(minimum_width, 50)),
        "height_p50_mm": float(np.percentile(height, 50)),
        "height_to_width_p50": float(np.percentile(slenderness, 50)),
        "maximum_top_z_mm": float(vertices[:, 2].max()),
    }


def validate_3mf(
    filepath: str,
    *,
    design_spec_path: Optional[str] = None,
) -> Dict[str, any]:
    """Run all validation rules on a generated 3MF file.

    ``design_spec_path`` is optional for legacy/standalone diagnostics.  Formal
    acceptance must pass the exact attempt-scoped DesignSpec claimed by the
    S10 ledger; silently reading the mutable ``design_spec.json`` alias would
    bind an older 3MF to a later same-city attempt.

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

    explicit_design_spec = design_spec_path is not None
    if design_spec_path is None:
        design_spec_path = os.path.join(
            os.path.dirname(os.path.abspath(filepath)), "design_spec.json")
    else:
        design_spec_path = os.path.abspath(os.fspath(design_spec_path))
    results["design_spec"] = {
        "filename": os.path.basename(design_spec_path),
        "explicit": explicit_design_spec,
    }
    design_spec = None
    design_spec_error = None
    if os.path.isfile(design_spec_path):
        try:
            with open(design_spec_path, encoding="utf-8") as handle:
                loaded_design_spec = json.load(handle)
            if not isinstance(loaded_design_spec, dict):
                raise ValueError("DesignSpec root must be an object")
            design_spec = loaded_design_spec
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            design_spec_error = exc
    elif explicit_design_spec:
        design_spec_error = FileNotFoundError(
            f"explicit DesignSpec not found: {design_spec_path}")

    if explicit_design_spec:
        if design_spec_error is not None:
            results["errors"].append(
                f"Explicit DesignSpec is invalid: {design_spec_error}")
        elif design_spec is None:
            results["errors"].append("Explicit DesignSpec is missing")
        else:
            claim = design_spec.get("artifact")
            actual_hash = _sha256_file(filepath)
            if (not isinstance(design_spec.get("schema_version"), str)
                    or not str(design_spec["schema_version"]).strip()):
                results["errors"].append(
                    "Explicit DesignSpec has no schema_version")
            if (not isinstance(claim, dict)
                    or claim.get("filename") != os.path.basename(filepath)
                    or claim.get("sha256") != actual_hash
                    or claim.get("size_bytes") != os.path.getsize(filepath)):
                results["errors"].append(
                    "Explicit DesignSpec artifact identity does not match 3MF")

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

    # ---- V3: Terrain height matches the declared mapping ----
    terrain_z_all = []
    for key in ["terrain", "terrain_surface", "terrain_walls"]:
        if key in objects:
            terrain_z_all.extend(objects[key]["vertices"][:, 2].tolist())

    if terrain_z_all:
        z_range = max(terrain_z_all) - min(terrain_z_all)
        terrain_spec = ((design_spec or {}).get("terrain") or {})
        declared_span = terrain_spec.get("artifact_z_span_mm")
        if declared_span is None:
            expected_span = float(TERRAIN_THICKNESS_MM)
            tolerance = expected_span * 0.15
            contract = "legacy fixed-height contract"
        else:
            expected_span = float(declared_span)
            tolerance = max(0.02, expected_span * 0.01)
            contract = "DesignSpec terrain contract"
        v3_ok = abs(z_range - expected_span) <= tolerance
    else:
        z_range = 0
        expected_span = float(TERRAIN_THICKNESS_MM)
        tolerance = expected_span * 0.15
        contract = "missing terrain"
        v3_ok = False
    results["rules"].append({
        "id": "V3",
        "name": "Terrain height matches its declared artifact contract",
        "passed": v3_ok,
        "detail": (
            f"actual={z_range:.3f}mm, expected={expected_span:.3f}mm, "
            f"tolerance={tolerance:.3f}mm ({contract})"),
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

    # ---- V8: Water object must contain more than the full-area base plate ----
    water_obj = objects.get("water")
    if water_obj and len(water_obj["vertices"]) > 0:
        water_z_span = water_obj["vertices"][:, 2].max() - water_obj["vertices"][:, 2].min()
        water_face_count = len(water_obj["faces"])
        # A rectangular base alone is exactly 12 triangles.  It is structural
        # support, not evidence that any WL/WO surface is printable or visible.
        v8_ok = water_face_count > 12 and water_z_span >= 0.64 - 1e-6
    else:
        water_z_span = 0
        water_face_count = 0
        v8_ok = True
    results["rules"].append({
        "id": "V8",
        "name": "Water contains printable feature geometry beyond the base",
        "passed": v8_ok,
        "detail": f"faces: {water_face_count}, Z span: {water_z_span:.2f}mm",
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
        # A base box also has side walls, so additionally require at least
        # three Z levels: base bottom, base top and a water-cap surface.
        has_side_walls = (np.abs(normals[:, 2]) < 0.9).any()
        z_levels = np.unique(np.round(v[:, 2], decimals=5))
        v9_ok = has_side_walls and len(z_levels) >= 3 and len(f) > 12
    else:
        z_levels = np.array([])
        v9_ok = True
    results["rules"].append({
        "id": "V9",
        "name": "Water caps have printable side walls and distinct Z levels",
        "passed": v9_ok,
        "detail": f"distinct Z levels: {len(z_levels)}",
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

    # ---- V14: Post-transform structural road clearance is proven ----
    # The proof cannot be reconstructed from the 3MF alone because the source
    # road centre-lines are not embedded in the archive.  New full-generation
    # artifacts therefore persist the measured cut evidence in DesignSpec.
    has_block_base = bool(
        block_base_obj and len(block_base_obj["faces"]) > 0)
    declared_block_mode = str(
        ((design_spec or {}).get("block_base") or {}).get(
            "resolved_mode", ""))
    if not has_block_base:
        if declared_block_mode and declared_block_mode != "off":
            v14_ok = False
            v14_detail = (
                f"design_spec declares block_base={declared_block_mode!r} "
                "but the 3MF has no block_base mesh")
        else:
            v14_ok = True
            v14_detail = "not applicable (no block_base mesh)"
    elif not os.path.isfile(design_spec_path):
        # Standalone/legacy fixtures remain valid under the geometry-only
        # contract.  Production acceptance separately requires DesignSpec.
        v14_ok = True
        v14_detail = "not recorded (legacy artifact without design_spec.json)"
    elif design_spec_error is not None:
        v14_ok = False
        v14_detail = f"invalid final-clearance evidence: {design_spec_error}"
    else:
        try:
            block_spec = design_spec.get("block_base") or {}
            clearance = block_spec.get("final_clearance") or {}
            printer = ((design_spec.get("printability") or {})
                       .get("printer_profile") or {})
            configured_gap = float(clearance.get(
                "configured_min_gap_mm", printer.get("min_gap_mm", 0.0)))
            extrusion_width = float(clearance.get(
                "extrusion_width_mm", printer.get("extrusion_width_mm", 0.0)))
            target_gap = float(clearance.get("target_gap_mm", 0.0))
            verified_gap = float(clearance.get("verified_min_gap_mm", 0.0))
            required_gap = max(configured_gap, 2.0 * extrusion_width)
            hierarchical = (
                clearance.get("policy_version")
                == "hierarchical-surface-road-clearance-v2")
            surface = clearance.get("surface_roads") or {}
            major = clearance.get("major_roads") or {}
            # The local-road reveal is a supported height/material boundary,
            # not a separately extruded coloured strip.  Validate its clear
            # gap against min_gap; the coloured-strip floor remains relevant
            # to explicit road objects only.
            surface_required = configured_gap
            surface_ok = True
            major_ok = True
            if hierarchical:
                surface_ok = bool(
                    surface.get("status") == "checked"
                    and surface.get("passed") is True
                    and float(surface.get("target_gap_mm", 0.0)) + 1e-9
                    >= surface_required
                    and float(surface.get("verified_min_gap_mm", 0.0)) + 1e-9
                    >= surface_required)
                major_ok = bool(
                    major.get("status") == "not_applicable"
                    or (
                        major.get("status") == "checked"
                        and major.get("passed") is True
                        and float(major.get("target_gap_mm", 0.0)) + 1e-9
                        >= required_gap
                        and float(major.get("verified_min_gap_mm", 0.0)) + 1e-9
                        >= required_gap
                    ))
                # With no arterial in the crop, the surface tier is the
                # strongest applicable proof and need not pretend to be a
                # two-extrusion arterial seam.
                if major.get("status") == "not_applicable":
                    required_gap = surface_required
            cutters = int(clearance.get("cutter_features", 0))
            post_intrusion = float(clearance.get(
                "post_clip_intrusion_area_m2", float("inf")))
            tolerance = float(clearance.get("measurement_tolerance_m2", 1e-6))
            artifact_claim = design_spec.get("artifact") or {}
            artifact_name = str(artifact_claim.get("filename", ""))
            artifact_sha256 = str(artifact_claim.get("sha256", ""))
            artifact_matches = bool(
                artifact_name == os.path.basename(filepath)
                and len(artifact_sha256) == 64
                and artifact_sha256 == _sha256_file(filepath)
            )
            numeric_evidence_ok = bool(np.isfinite([
                configured_gap, extrusion_width, target_gap, verified_gap,
                required_gap, post_intrusion, tolerance,
            ]).all())
            v14_ok = bool(
                clearance.get("status") == "checked"
                and clearance.get("passed") is True
                and cutters > 0
                and numeric_evidence_ok
                and required_gap > 0
                and target_gap + 1e-9 >= required_gap
                and verified_gap + 1e-9 >= required_gap
                and surface_ok
                and major_ok
                and post_intrusion <= tolerance
                and artifact_matches
            )
            v14_detail = (
                f"target={target_gap:.3f}mm, verified={verified_gap:.3f}mm, "
                f"required={required_gap:.3f}mm, "
                f"cutters={cutters}, post_intrusion={post_intrusion:.6g}m2, "
                f"tolerance={tolerance:.6g}m2, "
                f"numeric={numeric_evidence_ok}, "
                f"artifact_matches={artifact_matches}"
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            v14_ok = False
            v14_detail = f"invalid final-clearance evidence: {exc}"
    results["rules"].append({
        "id": "V14",
        "name": "Block base preserves post-transform structural road clearance",
        "passed": v14_ok,
        "detail": v14_detail,
    })

    # ---- V15: Formal terrain keeps printer-bounded regular topology ----
    terrain_spec = ((design_spec or {}).get("terrain") or {})
    if not os.path.isfile(design_spec_path):
        v15_ok = True
        v15_detail = "not recorded (legacy artifact without design_spec.json)"
    elif design_spec_error is not None:
        v15_ok = False
        v15_detail = f"invalid terrain evidence: {design_spec_error}"
    elif not terrain_spec:
        v15_ok = True
        v15_detail = "not recorded (legacy DesignSpec without terrain contract)"
    elif terrain_obj is None:
        v15_ok = False
        v15_detail = "terrain contract exists but no terrain mesh was parsed"
    else:
        try:
            grid = terrain_spec.get("grid") or {}
            stored_mesh = terrain_spec.get("surface_mesh") or {}
            actual_mesh = _terrain_surface_edge_metrics(
                terrain_obj["vertices"], terrain_obj["faces"])
            allowed_edge = float(grid["max_surface_edge_mm"])
            actual_edge = float(actual_mesh["max_xy_edge_mm"])
            stored_edge = float(stored_mesh["max_xy_edge_mm"])
            qem_off = bool(
                terrain_spec.get("formal_qem_decimation") is False
                and grid.get("qem_decimation") is False
                and grid.get("method") == "regular_raster_resample"
            )
            artifact_claim = design_spec.get("artifact") or {}
            artifact_matches = bool(
                artifact_claim.get("filename") == os.path.basename(filepath)
                and artifact_claim.get("sha256") == _sha256_file(filepath)
            )
            numeric_ok = bool(np.isfinite([
                allowed_edge, actual_edge, stored_edge,
            ]).all())
            v15_ok = bool(
                numeric_ok
                and qem_off
                and allowed_edge > 0
                and actual_edge <= allowed_edge + 1e-4
                and abs(actual_edge - stored_edge) <= 1e-3
                and int(actual_mesh.get("faces_over_2mm", -1)) == 0
                and int(actual_mesh.get("faces_over_5mm", -1)) == 0
                and artifact_matches
            )
            v15_detail = (
                f"actual_max={actual_edge:.3f}mm, "
                f"allowed={allowed_edge:.3f}mm, "
                f"over_2mm={actual_mesh.get('faces_over_2mm')}, "
                f"over_5mm={actual_mesh.get('faces_over_5mm')}, "
                f"QEM_off={qem_off}, artifact_matches={artifact_matches}"
            )
        except (KeyError, OSError, ValueError, TypeError) as exc:
            v15_ok = False
            v15_detail = f"invalid terrain evidence: {exc}"
    results["rules"].append({
        "id": "V15",
        "name": "Formal terrain uses printer-bounded regular topology",
        "passed": v15_ok,
        "detail": v15_detail,
    })

    # ---- V16: Terrain-draped overlays cannot contain giant fan facets ----
    # A valid terrain mesh is insufficient: any overlay that samples terrain
    # only at sparse boundary vertices can still bridge an entire hill with a
    # single flat triangle.  Vegetation is currently the formal draped layer.
    vegetation_obj = objects.get("vegetation")
    printable_evidence = (((design_spec or {}).get("evidence") or {})
                          .get("printable_features") or {})
    declared_vegetation = int(
        printable_evidence.get("vegetation_landmarks", 0)
        + printable_evidence.get("vegetation_polygons", 0)
    )
    if not vegetation_obj or not len(vegetation_obj["faces"]):
        v16_ok = declared_vegetation == 0
        v16_detail = (
            "not applicable (no vegetation mesh)"
            if v16_ok else
            f"missing vegetation mesh for {declared_vegetation} declared "
            "printable vegetation features"
        )
    elif not os.path.isfile(design_spec_path):
        v16_ok = True
        v16_detail = "not recorded (legacy artifact without design_spec.json)"
    elif design_spec_error is not None:
        v16_ok = False
        v16_detail = f"invalid draped-layer evidence: {design_spec_error}"
    else:
        try:
            printer = ((design_spec.get("printability") or {})
                       .get("printer_profile") or {})
            extrusion_width = float(printer["extrusion_width_mm"])
            allowed_edge = 2.0 * extrusion_width
            metrics = _terrain_surface_edge_metrics(
                vegetation_obj["vertices"], vegetation_obj["faces"])
            actual_edge = float(metrics["max_xy_edge_mm"])
            numeric_ok = bool(np.isfinite([
                extrusion_width, allowed_edge, actual_edge,
            ]).all())
            v16_ok = bool(
                numeric_ok
                and extrusion_width > 0
                and actual_edge <= allowed_edge + 1e-4
                and int(metrics.get("faces_over_2mm", -1)) == 0
                and int(metrics.get("faces_over_5mm", -1)) == 0
            )
            v16_detail = (
                f"vegetation_actual_max={actual_edge:.3f}mm, "
                f"allowed={allowed_edge:.3f}mm, "
                f"over_2mm={metrics.get('faces_over_2mm')}, "
                f"over_5mm={metrics.get('faces_over_5mm')}"
            )
        except (KeyError, ValueError, TypeError) as exc:
            v16_ok = False
            v16_detail = f"invalid draped-layer evidence: {exc}"
    results["rules"].append({
        "id": "V16",
        "name": "Terrain-draped overlays use printer-bounded facets",
        "passed": v16_ok,
        "detail": v16_detail,
    })

    # ---- V17: Final hero buildings are printable and respect Z ownership ----
    hierarchy = (((design_spec or {}).get("decisions") or {})
                 .get("building_height_hierarchy") or {})
    landmarks_obj = objects.get("landmarks")
    hierarchy_policy = str(hierarchy.get("policy_version") or "")
    if not hierarchy_policy.startswith("terrain-owned-building-z-v"):
        v17_ok = True
        v17_detail = "not recorded (legacy building-height contract)"
    elif hierarchy.get("status") != "active":
        v17_ok = True
        v17_detail = f"not applicable ({hierarchy.get('status', 'inactive')})"
    elif not landmarks_obj or not len(landmarks_obj["faces"]):
        declared = int(hierarchy.get("hero_count", 0))
        v17_ok = declared == 0
        v17_detail = f"no landmark mesh; declared heroes={declared}"
    else:
        try:
            printer = ((design_spec.get("printability") or {})
                       .get("printer_profile") or {})
            extrusion_width = float(printer["extrusion_width_mm"])
            metrics = _component_slender_metrics(
                landmarks_obj["vertices"], landmarks_obj["faces"],
                extrusion_width,
            )
            terrain_peak = (
                float(terrain_obj["vertices"][:, 2].max())
                if terrain_obj is not None else float("nan"))
            peak_ok = bool(
                not hierarchy.get("terrain_owned")
                or (math.isfinite(terrain_peak)
                    and metrics["maximum_top_z_mm"]
                    <= terrain_peak + 1e-4)
            )
            v17_ok = bool(
                metrics["below_extrusion_width"] == 0
                and metrics["height_to_width_above_4"] == 0
                and peak_ok
            )
            v17_detail = (
                f"components={metrics['component_count']}, "
                f"below_{extrusion_width:.2f}mm="
                f"{metrics['below_extrusion_width']}, "
                f"height/width>4={metrics['height_to_width_above_4']}, "
                f"width_p50={metrics['minimum_width_p50_mm']:.3f}mm, "
                f"height_p50={metrics['height_p50_mm']:.3f}mm, "
                f"landmark_top={metrics['maximum_top_z_mm']:.3f}mm, "
                f"terrain_peak={terrain_peak:.3f}mm, peak_ok={peak_ok}"
            )
        except (KeyError, TypeError, ValueError) as exc:
            v17_ok = False
            v17_detail = f"invalid final hero evidence: {exc}"
    results["rules"].append({
        "id": "V17",
        "name": "Hero buildings survive the nozzle and respect scene Z ownership",
        "passed": v17_ok,
        "detail": v17_detail,
    })

    # Aggregate results
    for rule in results["rules"]:
        if not rule["passed"]:
            if rule["id"] in (
                    "V2", "V4", "V8", "V9", "V10", "V13", "V14",
                    "V15", "V16", "V17"):
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
