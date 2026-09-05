#!/usr/bin/env python3
"""Measure the printable silhouette grammar of reference city 3MFs.

This is a read-only audit.  It measures a deterministic sample of compact
white-relief components and reports distributions; it never exports or copies
their geometry into generated models.  The semantic ownership of a white
component remains an inference, so elongated components and full-frame
carriers are deliberately excluded from the building-mass sample.
"""

from __future__ import annotations

import argparse
from io import BytesIO
import json
import math
from pathlib import Path
import zipfile
from xml.etree import ElementTree as ET

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from shapely.geometry import Polygon
from shapely.geometry.polygon import orient
from shapely.ops import unary_union

from tools.analyze_reference_3mf_style import (
    _city_model_scale,
    _local_name,
    _material_role,
    _percentiles,
    _project_settings,
    _volume_settings,
)


SCHEMA_VERSION = "reference-3mf-silhouette-v1"


def _component_labels(vertex_count: int, triangles: np.ndarray) -> np.ndarray:
    if vertex_count == 0:
        return np.asarray([], dtype=np.int32)
    if len(triangles) == 0:
        return np.arange(vertex_count, dtype=np.int32)
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
        shape=(vertex_count, vertex_count),
    ).tocsr()
    _, labels = connected_components(graph, directed=False)
    return labels.astype(np.int32, copy=False)


def _largest_polygon(geometry):
    if isinstance(geometry, Polygon):
        return geometry
    parts = [part for part in getattr(geometry, "geoms", ())
             if isinstance(part, Polygon) and not part.is_empty]
    return max(parts, key=lambda item: item.area) if parts else None


def _turn_metrics(polygon: Polygon, simplify_mm: float) -> dict:
    boundary = polygon.simplify(
        max(float(simplify_mm), 0.0), preserve_topology=True)
    boundary = _largest_polygon(boundary) or polygon
    boundary = orient(boundary, sign=1.0)
    coords = np.asarray(boundary.exterior.coords[:-1], dtype=float)
    if len(coords) < 3:
        return {
            "outline_vertices": int(len(coords)),
            "reflex_vertex_fraction": 0.0,
            "sharp_convex_vertex_fraction": 0.0,
        }
    previous = np.roll(coords, 1, axis=0) - coords
    following = np.roll(coords, -1, axis=0) - coords
    previous_norm = np.linalg.norm(previous, axis=1)
    following_norm = np.linalg.norm(following, axis=1)
    valid = (previous_norm > 1e-9) & (following_norm > 1e-9)
    if not np.any(valid):
        return {
            "outline_vertices": int(len(coords)),
            "reflex_vertex_fraction": 0.0,
            "sharp_convex_vertex_fraction": 0.0,
        }
    previous = previous[valid]
    following = following[valid]
    dot = np.sum(previous * following, axis=1)
    denominator = (
        np.linalg.norm(previous, axis=1) * np.linalg.norm(following, axis=1))
    smaller_angle = np.arccos(np.clip(dot / denominator, -1.0, 1.0))
    # A CCW exterior has a reflex vertex when the incoming-to-outgoing turn
    # is clockwise.  ``previous`` points from the vertex to its predecessor,
    # hence the sign is inverted relative to the usual edge cross product.
    cross = previous[:, 0] * following[:, 1] - previous[:, 1] * following[:, 0]
    reflex = cross > 1e-9
    sharp_convex = (~reflex) & (smaller_angle < math.radians(70.0))
    return {
        "outline_vertices": int(valid.sum()),
        "reflex_vertex_fraction": float(np.mean(reflex)),
        "sharp_convex_vertex_fraction": float(np.mean(sharp_convex)),
    }


def silhouette_metrics(geometry, *, simplify_mm: float) -> dict | None:
    """Return scale-independent regularity measures for one XY silhouette."""

    if geometry is None or geometry.is_empty:
        return None
    if not geometry.is_valid:
        geometry = geometry.buffer(0)
    if geometry.is_empty or geometry.area <= 1e-9:
        return None
    hull = geometry.convex_hull
    if hull.is_empty or hull.area <= 1e-9 or hull.length <= 1e-9:
        return None
    main = _largest_polygon(geometry)
    if main is None:
        return None
    turns = _turn_metrics(main, simplify_mm)
    minx, miny, maxx, maxy = geometry.bounds
    minimum_axis = min(maxx - minx, maxy - miny)
    concavity_depth = geometry.boundary.hausdorff_distance(hull.boundary)
    holes = sum(len(part.interiors) for part in (
        [geometry] if isinstance(geometry, Polygon)
        else [item for item in getattr(geometry, "geoms", ())
              if isinstance(item, Polygon)]
    ))
    return {
        "area_mm2": float(geometry.area),
        "minimum_axis_mm": float(minimum_axis),
        "solidity": float(geometry.area / hull.area),
        "perimeter_excess": float(geometry.length / hull.length),
        "compactness": float(
            4.0 * math.pi * geometry.area / (geometry.length ** 2)),
        "concavity_depth_fraction": float(
            concavity_depth / max(minimum_axis, 1e-9)),
        "hole_count": int(holes),
        **turns,
    }


def _mesh_silhouettes(
    vertices: np.ndarray,
    triangles: np.ndarray,
    *,
    model_scale: float,
    line_width_mm: float,
    sample_limit: int,
) -> list[dict]:
    if len(vertices) == 0 or len(triangles) == 0:
        return []
    labels = _component_labels(len(vertices), triangles)
    component_count = int(labels.max()) + 1 if len(labels) else 0
    minima = np.full((component_count, 3), np.inf, dtype=float)
    maxima = np.full((component_count, 3), -np.inf, dtype=float)
    for axis in range(3):
        np.minimum.at(minima[:, axis], labels, vertices[:, axis])
        np.maximum.at(maxima[:, axis], labels, vertices[:, axis])
    spans = (maxima - minima) * float(model_scale)

    points = vertices[triangles]
    cross_z = (
        (points[:, 1, 0] - points[:, 0, 0])
        * (points[:, 2, 1] - points[:, 0, 1])
        - (points[:, 1, 1] - points[:, 0, 1])
        * (points[:, 2, 0] - points[:, 0, 0])
    )
    top_mask = cross_z > 1e-10
    triangle_labels = labels[triangles[:, 0]]
    top_area = np.bincount(
        triangle_labels[top_mask],
        weights=cross_z[top_mask] * 0.5 * model_scale * model_scale,
        minlength=component_count,
    )

    minimum_axis = np.min(spans[:, :2], axis=1)
    maximum_axis = np.max(spans[:, :2], axis=1)
    aspect = maximum_axis / np.maximum(minimum_axis, 1e-9)
    floor = max(0.15, float(line_width_mm) * 0.5)
    eligible = np.flatnonzero(
        (minimum_axis >= floor)
        & (minimum_axis <= 16.0)
        & (maximum_axis <= 24.0)
        & (aspect <= 4.0)
        & (spans[:, 2] >= 0.01)
        & (top_area >= 0.04)
    )
    if len(eligible) > sample_limit:
        ordered = eligible[np.lexsort((eligible, top_area[eligible],
                                       minimum_axis[eligible]))]
        positions = np.linspace(0, len(ordered) - 1, sample_limit)
        eligible = ordered[np.rint(positions).astype(int)]

    result = []
    for component in eligible:
        selected = np.flatnonzero(top_mask & (triangle_labels == component))
        polygons = []
        for triangle_index in selected:
            xy = points[triangle_index, :, :2] * float(model_scale)
            polygon = Polygon(xy)
            if polygon.area > 1e-10:
                polygons.append(polygon)
        if not polygons:
            continue
        footprint = unary_union(polygons)
        metrics = silhouette_metrics(
            footprint, simplify_mm=max(0.02, line_width_mm * 0.12))
        if metrics is not None:
            result.append(metrics)
    return result


def _aggregate(samples: list[dict]) -> dict:
    fields = (
        "area_mm2", "minimum_axis_mm", "solidity", "perimeter_excess",
        "compactness", "concavity_depth_fraction", "outline_vertices",
        "reflex_vertex_fraction", "sharp_convex_vertex_fraction",
    )
    return {
        "sample_count": len(samples),
        "metrics": {
            field: _percentiles([sample[field] for sample in samples])
            for field in fields
        },
        "fraction_with_holes": (
            round(float(np.mean([sample["hole_count"] > 0
                                 for sample in samples])), 5)
            if samples else None),
        "selection_boundary": (
            "compact white-relief components only: 0.5 line-width to 16 mm "
            "minimum axis, <=24 mm maximum axis, aspect <=4; semantic "
            "building ownership remains an inference"),
    }


def analyze_archive(
    archive: zipfile.ZipFile,
    *,
    city: str,
    source_filename: str,
    archive_bytes: int,
    sample_limit: int = 300,
) -> dict:
    settings = _project_settings(archive)
    volumes = _volume_settings(archive)
    scale = _city_model_scale(archive)
    colors = settings["filament_colors"]
    line_width = float(settings["line_width_mm"] or 0.42)
    samples = []
    with archive.open("3D/Objects/object_1.model") as stream:
        current_id = None
        vertices = []
        triangles = []
        for event, element in ET.iterparse(stream, events=("start", "end")):
            kind = _local_name(element.tag)
            if event == "start" and kind == "object":
                current_id = int(element.attrib.get("id", 0))
                vertices = []
                triangles = []
            elif event == "end" and kind == "vertex":
                vertices.append(tuple(
                    float(element.attrib[axis]) for axis in ("x", "y", "z")))
                element.clear()
            elif event == "end" and kind == "triangle":
                triangles.append(tuple(
                    int(element.attrib[axis]) for axis in ("v1", "v2", "v3")))
                element.clear()
            elif event == "end" and kind == "object":
                volume = volumes.get(current_id, {})
                extruder = int(volume.get("extruder") or 1)
                color = colors[extruder - 1] if extruder <= len(colors) else None
                if _material_role(color) == "white_relief":
                    samples.extend(_mesh_silhouettes(
                        np.asarray(vertices, dtype=float),
                        np.asarray(triangles, dtype=np.int32),
                        model_scale=scale,
                        line_width_mm=line_width,
                        sample_limit=sample_limit,
                    ))
                current_id = None
                vertices = []
                triangles = []
                element.clear()
    return {
        "schema_version": SCHEMA_VERSION,
        "city": city,
        "source_filename": source_filename,
        "archive_bytes": int(archive_bytes),
        "model_scale": round(float(scale), 8),
        "printer": settings,
        "compact_white_relief_silhouettes": _aggregate(samples),
    }


def analyze_path(path: Path, *, sample_limit: int = 300) -> dict:
    with zipfile.ZipFile(path) as archive:
        return analyze_archive(
            archive,
            city=path.parent.name,
            source_filename=path.name,
            archive_bytes=path.stat().st_size,
            sample_limit=sample_limit,
        )


def analyze_bundle(
    path: Path,
    *,
    cities: list[str],
    sample_limit: int = 300,
) -> list[dict]:
    reports = []
    with zipfile.ZipFile(path) as bundle:
        entries = [item for item in bundle.infolist()
                   if item.filename.lower().endswith(".3mf")]
        if len(entries) != len(cities):
            raise ValueError(
                f"bundle has {len(entries)} 3MFs but {len(cities)} city labels")
        for city, entry in zip(cities, entries):
            payload = bundle.read(entry)
            with zipfile.ZipFile(BytesIO(payload)) as archive:
                reports.append(analyze_archive(
                    archive,
                    city=city,
                    source_filename=entry.filename,
                    archive_bytes=len(payload),
                    sample_limit=sample_limit,
                ))
    return reports


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only silhouette audit of reference city 3MFs")
    parser.add_argument("input", type=Path)
    parser.add_argument("--bundle-cities", help="comma-separated entry labels")
    parser.add_argument("--sample-limit", type=int, default=300)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.sample_limit < 1:
        raise SystemExit("--sample-limit must be positive")
    if args.bundle_cities:
        cities = [item.strip() for item in args.bundle_cities.split(",")
                  if item.strip()]
        payload = analyze_bundle(
            args.input, cities=cities, sample_limit=args.sample_limit)
    else:
        payload = [analyze_path(args.input, sample_limit=args.sample_limit)]
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print(args.output)
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
