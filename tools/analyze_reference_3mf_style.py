#!/usr/bin/env python3
"""Read-only geometric/material audit for reference city-texture 3MF files.

The report describes transferable print and simplification evidence.  It does
not copy source geometry into the project and does not declare aesthetic or
print acceptance for generated artifacts.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import zipfile
from xml.etree import ElementTree as ET

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _percentiles(values, points=(0, 10, 25, 50, 75, 90, 100), digits=4):
    array = np.asarray(values, dtype=float)
    if array.size == 0:
        return {f"p{point:02d}": None for point in points}
    result = np.percentile(array, points)
    return {
        f"p{point:02d}": round(float(value), digits)
        for point, value in zip(points, result)
    }


def _parse_transform(value: str | None) -> np.ndarray:
    if not value:
        return np.eye(4, dtype=float)
    numbers = np.asarray([float(part) for part in value.split()], dtype=float)
    if numbers.size != 12:
        return np.eye(4, dtype=float)
    matrix = np.eye(4, dtype=float)
    matrix[:3, :4] = numbers.reshape(4, 3).T
    return matrix


def _project_settings(archive: zipfile.ZipFile) -> dict:
    try:
        payload = json.loads(archive.read("Metadata/project_settings.config"))
    except Exception:
        payload = {}
    return {
        "nozzle_diameter_mm": (
            float(payload.get("nozzle_diameter", [0.0])[0])
            if payload.get("nozzle_diameter") else None),
        "layer_height_mm": (
            float(payload["layer_height"])
            if payload.get("layer_height") is not None else None),
        "line_width_mm": (
            float(payload["line_width"])
            if payload.get("line_width") is not None else None),
        "outer_wall_line_width_mm": (
            float(payload["outer_wall_line_width"])
            if payload.get("outer_wall_line_width") is not None else None),
        "wall_loops": (
            int(payload["wall_loops"])
            if payload.get("wall_loops") is not None else None),
        "detect_thin_wall": str(payload.get("detect_thin_wall", "")) == "1",
        "filament_colors": list(payload.get("filament_colour") or []),
    }


def _volume_settings(archive: zipfile.ZipFile) -> dict[int, dict]:
    try:
        root = ET.fromstring(archive.read("Metadata/model_settings.config"))
    except Exception:
        return {}
    # Bambu/Orca projects describe model submeshes as ``part`` while some
    # producers use ``volume``.  The printable city model is the object with
    # the greatest submesh count; the other object is normally the separate
    # labelled backing board.
    objects = []
    for object_element in root.iter():
        if _local_name(object_element.tag) != "object":
            continue
        volumes = [
            child for child in object_element
            if _local_name(child.tag) in {"part", "volume"}
        ]
        if volumes:
            objects.append((len(volumes), volumes))
    if not objects:
        return {}
    _, volumes = max(objects, key=lambda item: item[0])
    result = {}
    for index, volume in enumerate(volumes, start=1):
        metadata = {
            item.attrib.get("key"): item.attrib.get("value")
            for item in volume
            if _local_name(item.tag) == "metadata"
        }
        mesh_stat = next(
            (item for item in volume
             if _local_name(item.tag) == "mesh_stat"),
            None,
        )
        # Bambu/Orca part ids are the 3MF object ids and need not be
        # contiguous.  Positional enumeration can therefore assign the wrong
        # name/material to later submeshes (for example ids 1, 3, 4, 6, 7).
        # Retain the position only as a fallback for older metadata without
        # an explicit id.
        object_id = int(volume.attrib.get("id", index))
        result[object_id] = {
            "name": metadata.get("name", f"Object_{index}"),
            "extruder": int(metadata.get("extruder") or 1),
            "declared_face_count": (
                int(mesh_stat.attrib.get("face_count", 0))
                if mesh_stat is not None else None),
        }
    return result


def _city_model_scale(archive: zipfile.ZipFile) -> float:
    try:
        root = ET.fromstring(archive.read("3D/3dmodel.model"))
    except Exception:
        return 1.0
    # Object 5 is the city model in the inspected library; avoid relying on
    # that id by taking the smaller of the printable build scales (the backing
    # board is normally larger).
    scales = []
    for element in root.iter():
        if _local_name(element.tag) != "item":
            continue
        matrix = _parse_transform(element.attrib.get("transform"))
        scale = float(np.linalg.norm(matrix[:3, 0]))
        if scale > 0:
            scales.append(scale)
    return min(scales) if scales else 1.0


def _material_role(color: str | None) -> str:
    normalized = str(color or "").upper()
    if normalized.startswith("#FFFFFF"):
        return "white_relief"
    if normalized.startswith("#8E9089"):
        return "neutral_substrate"
    if normalized in {"#000000", "#000000FF"}:
        return "black_negative_backing"
    if normalized.endswith("00"):
        return "transparent_or_unused"
    return "other"


def _component_metrics(vertices: np.ndarray, triangles: np.ndarray,
                       model_scale: float, line_width_mm: float | None) -> dict:
    count = len(vertices)
    if count == 0 or len(triangles) == 0:
        return {
            "count": 0,
            "minimum_axis_mm": _percentiles([]),
            "height_span_mm": _percentiles([]),
        }
    rows = np.concatenate((
        triangles[:, 0], triangles[:, 1],
        triangles[:, 1], triangles[:, 2],
        triangles[:, 2], triangles[:, 0],
    ))
    columns = np.concatenate((
        triangles[:, 1], triangles[:, 0],
        triangles[:, 2], triangles[:, 1],
        triangles[:, 0], triangles[:, 2],
    ))
    graph = coo_matrix(
        (np.ones(len(rows), dtype=np.uint8), (rows, columns)),
        shape=(count, count),
    ).tocsr()
    component_count, labels = connected_components(graph, directed=False)
    minima = np.full((component_count, 3), np.inf, dtype=float)
    maxima = np.full((component_count, 3), -np.inf, dtype=float)
    for axis in range(3):
        np.minimum.at(minima[:, axis], labels, vertices[:, axis])
        np.maximum.at(maxima[:, axis], labels, vertices[:, axis])
    spans = (maxima - minima) * model_scale
    minimum_axis = np.min(spans[:, :2], axis=1)
    maximum_axis = np.max(spans[:, :2], axis=1)
    aspect = maximum_axis / np.maximum(minimum_axis, 1e-9)
    height = spans[:, 2]
    valid = np.isfinite(minimum_axis) & (height >= 0.01)
    minimum_axis = minimum_axis[valid]
    aspect = aspect[valid]
    height = height[valid]
    line_width = float(line_width_mm or 0.0)
    return {
        "count": int(valid.sum()),
        "minimum_axis_mm": _percentiles(minimum_axis),
        "height_span_mm": _percentiles(height),
        "fraction_below_line_width": (
            round(float(np.mean(minimum_axis < line_width)), 5)
            if line_width > 0 and minimum_axis.size else None),
        "fraction_aspect_above_4": (
            round(float(np.mean(aspect > 4.0)), 5)
            if aspect.size else None),
        "minimum_axis_floor_mm": (
            round(float(minimum_axis.min()), 4)
            if minimum_axis.size else None),
    }


def _mesh_metrics(vertices: np.ndarray, triangles: np.ndarray,
                  model_scale: float, line_width_mm: float | None) -> dict:
    spans = np.ptp(vertices, axis=0) * model_scale if len(vertices) else np.zeros(3)
    top_area = 0.0
    if len(triangles):
        points = vertices[triangles]
        cross_z = (
            (points[:, 1, 0] - points[:, 0, 0])
            * (points[:, 2, 1] - points[:, 0, 1])
            - (points[:, 1, 1] - points[:, 0, 1])
            * (points[:, 2, 0] - points[:, 0, 0])
        )
        top_area = float(np.maximum(cross_z, 0.0).sum() * 0.5
                         * model_scale * model_scale)
    return {
        "vertices": int(len(vertices)),
        "triangles": int(len(triangles)),
        "span_mm": [round(float(value), 4) for value in spans],
        "projected_top_area_mm2": round(top_area, 3),
        "components": _component_metrics(
            vertices, triangles, model_scale, line_width_mm),
    }


def analyze_reference_3mf(path: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        settings = _project_settings(archive)
        volumes = _volume_settings(archive)
        scale = _city_model_scale(archive)
        colors = settings["filament_colors"]
        source_name = "3D/Objects/object_1.model"
        results = []
        with archive.open(source_name) as stream:
            current_id = None
            vertices = []
            triangles = []
            for event, element in ET.iterparse(stream, events=("start", "end")):
                kind = _local_name(element.tag)
                if event == "start" and kind == "object":
                    current_id = int(element.attrib.get("id", len(results) + 1))
                    vertices = []
                    triangles = []
                elif event == "end" and kind == "vertex":
                    vertices.append(tuple(
                        float(element.attrib[axis]) for axis in ("x", "y", "z")))
                    element.clear()
                elif event == "end" and kind == "triangle":
                    triangles.append(tuple(
                        int(element.attrib[axis])
                        for axis in ("v1", "v2", "v3")))
                    element.clear()
                elif event == "end" and kind == "object":
                    volume = volumes.get(current_id, {})
                    extruder = int(volume.get("extruder") or 1)
                    color = colors[extruder - 1] if extruder <= len(colors) else None
                    metrics = _mesh_metrics(
                        np.asarray(vertices, dtype=float),
                        np.asarray(triangles, dtype=np.int32),
                        scale,
                        settings["line_width_mm"],
                    )
                    results.append({
                        "object_id": current_id,
                        "name": volume.get("name", f"Object_{current_id}"),
                        "extruder": extruder,
                        "color": color,
                        "material_role": _material_role(color),
                        **metrics,
                    })
                    current_id = None
                    vertices = []
                    triangles = []
                    element.clear()

    white = [item for item in results
             if item["material_role"] == "white_relief"]
    white_components = sum(item["components"]["count"] for item in white)
    below = [
        (item["components"]["fraction_below_line_width"],
         item["components"]["count"])
        for item in white
        if item["components"]["fraction_below_line_width"] is not None
    ]
    weighted_below = (
        sum(fraction * count for fraction, count in below)
        / max(sum(count for _, count in below), 1)
        if below else None)
    white_height_tiers = sorted(
        {
            item["components"]["height_span_mm"]["p50"]
            for item in white
            if item["components"]["height_span_mm"]["p50"] is not None
        }
    )
    white_width_medians = [
        (item["components"]["minimum_axis_mm"]["p50"],
         item["components"]["count"])
        for item in white
        if item["components"]["minimum_axis_mm"]["p50"] is not None
    ]
    weighted_white_width = (
        sum(width * count for width, count in white_width_medians)
        / max(sum(count for _, count in white_width_medians), 1)
        if white_width_medians else None
    )
    return {
        "schema_version": "reference-3mf-style-v1",
        "city": path.parent.name,
        "source_filename": path.name,
        "archive_bytes": path.stat().st_size,
        "model_scale": round(scale, 8),
        "printer": settings,
        "volumes": results,
        "summary": {
            "volume_count": len(results),
            "white_relief_component_count": white_components,
            "white_relief_fraction_below_line_width": (
                round(weighted_below, 5) if weighted_below is not None else None),
            "white_relief_component_width_p50_mm": (
                round(weighted_white_width, 4)
                if weighted_white_width is not None else None),
            "white_relief_object_height_p50_tiers_mm": white_height_tiers,
            "thin_wall_rescue_disabled": not settings["detect_thin_wall"],
            "black_full_frame_backing_detected": any(
                item["material_role"] == "black_negative_backing"
                and item["components"]["count"] == 1
                and min(item["span_mm"][:2]) >= 195.0
                for item in results),
            "interpretation_boundary": (
                "material volumes are measurable; semantic building/road/water "
                "ownership remains an inference requiring image review"),
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only material/component audit of reference city 3MFs")
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    paths = []
    for source in args.inputs:
        if source.is_dir():
            paths.extend(sorted(source.glob("*/*.3mf")))
        elif source.is_file():
            paths.append(source)
        else:
            raise SystemExit(f"input not found: {source}")
    reports = [analyze_reference_3mf(path) for path in paths]
    payload = json.dumps(reports, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
        print(args.output)
    else:
        print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
