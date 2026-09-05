#!/usr/bin/env python3
"""Compare project building evidence with a reference-demo block grammar.

This is a read-only diagnostic.  It measures raw OSM building footprints in
model millimetres, adapts an existing building-mass evidence JSON when one is
available, and compares both with an observation produced by
``observe_reference_block_grammar.py``.  No measurement is consumed by the
generation pipeline and no source geometry is modified.

The raw source is reported twice:

* ``all_source`` describes the complete cached building population;
* ``reference_comparable_sample`` applies the reference observer's compact
  component bounds to a deterministic shape sample.

This distinction is important: an OSM city can contain hundreds of thousands
of real footprints that are far below one printable line at 25 km scale, while
the reference 3MF contains an already designed and simplified relief.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
import pickle
import sys
from typing import Iterable, Mapping, Sequence

import geopandas as gpd
import numpy as np
import pandas as pd
from PIL import Image, ImageDraw, ImageFont
from scipy.spatial import cKDTree
import shapely
from shapely import affinity
from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import unary_union


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm
from tools.analyze_reference_3mf_shapes import silhouette_metrics
from tools.observe_reference_block_grammar import (
    _rotated_geometry_metrics,
    orientation_metrics,
)


SCHEMA_VERSION = "project-reference-block-grammar-comparison-v1"
DEFAULT_MODEL_SPAN_MM = 196.0
DEFAULT_SAMPLE_LIMIT = 12_000


def _round(value, digits: int = 5):
    if value is None:
        return None
    return round(float(value), digits)


def _percentiles(values: Iterable[float]) -> dict:
    array = np.asarray([
        float(value) for value in values if math.isfinite(float(value))
    ], dtype=float)
    if not len(array):
        return {key: None for key in (
            "p00", "p10", "p25", "p50", "p75", "p90", "p100")}
    quantiles = np.percentile(array, [0, 10, 25, 50, 75, 90, 100])
    return {
        key: _round(value)
        for key, value in zip(
            ("p00", "p10", "p25", "p50", "p75", "p90", "p100"),
            quantiles,
        )
    }


def _font(size: int):
    candidates = (
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _load_trusted_pickle(path: Path):
    """Load only an operator-selected local cache, never an uploaded pickle."""

    if not path.is_file():
        raise FileNotFoundError(path)
    with path.open("rb") as handle:
        return pickle.load(handle)


def _load_buildings(case: Mapping) -> tuple[gpd.GeoDataFrame, list[str]]:
    provenance = []
    cache_path = case.get("gdf_cache")
    tile_dir = case.get("building_geojson_dir")
    if cache_path:
        path = PROJECT_ROOT / str(cache_path)
        payload = _load_trusted_pickle(path)
        buildings = payload.get("buildings") if isinstance(payload, dict) else None
        if not isinstance(buildings, gpd.GeoDataFrame):
            raise ValueError(f"building GeoDataFrame missing in {path}")
        provenance.append(str(path.resolve()))
        return buildings, provenance
    if tile_dir:
        directory = PROJECT_ROOT / str(tile_dir)
        paths = sorted(directory.glob("*.geojson"))
        if not paths:
            raise FileNotFoundError(f"no building tiles: {directory}")
        bbox = tuple(float(value) for value in case["bbox_wgs84"])
        south, west, north, east = bbox
        frames = []
        for path in paths:
            frame = gpd.read_file(path)
            if frame.empty:
                continue
            minx, miny, maxx, maxy = frame.total_bounds
            if maxx < west or minx > east or maxy < south or miny > north:
                continue
            frames.append(frame)
            provenance.append(str(path.resolve()))
        if not frames:
            return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326"), provenance
        buildings = gpd.GeoDataFrame(
            pd.concat(frames, ignore_index=True), crs=frames[0].crs)
        return buildings, provenance
    raise ValueError("case requires gdf_cache or building_geojson_dir")


def _subset_wgs84(buildings: gpd.GeoDataFrame, bbox: Sequence[float]):
    if buildings.empty:
        return buildings.copy()
    if buildings.crs is None:
        raise ValueError("building source has no CRS")
    source = buildings
    if source.crs.to_epsg() != 4326:
        source = source.to_crs("EPSG:4326")
    south, west, north, east = [float(value) for value in bbox]
    bounds = source.geometry.bounds
    selected = (
        (bounds.minx <= east) & (bounds.maxx >= west)
        & (bounds.miny <= north) & (bounds.maxy >= south)
    )
    return source.loc[selected].copy()


def _deterministic_indices(count: int, limit: int) -> np.ndarray:
    if count <= limit:
        return np.arange(count, dtype=int)
    rng = np.random.default_rng(20260830)
    return np.sort(rng.choice(count, size=limit, replace=False))


def _polygonal_geometry(geometry):
    """Return only valid polygonal parts from heterogeneous OSM geometry."""

    if geometry is None or geometry.is_empty:
        return None
    try:
        repaired = geometry if geometry.is_valid else shapely.make_valid(geometry)
    except Exception:
        return None
    if isinstance(repaired, (Polygon, MultiPolygon)):
        return repaired
    parts = [part for part in getattr(repaired, "geoms", ())
             if isinstance(part, (Polygon, MultiPolygon)) and not part.is_empty]
    if not parts:
        return None
    merged = unary_union(parts)
    return merged if not merged.is_empty else None


def _grid_metrics(
    centers_x: np.ndarray,
    centers_y: np.ndarray,
    areas_mm2: np.ndarray,
    frame_bounds_mm: Sequence[float],
    *,
    grid_size: int = 8,
) -> tuple[dict, np.ndarray]:
    minx, miny, maxx, maxy = [float(value) for value in frame_bounds_mm]
    span_x = max(maxx - minx, 1e-9)
    span_y = max(maxy - miny, 1e-9)
    columns = np.clip(
        ((centers_x - minx) / span_x * grid_size).astype(int),
        0, grid_size - 1)
    rows = np.clip(
        ((centers_y - miny) / span_y * grid_size).astype(int),
        0, grid_size - 1)
    grid = np.zeros((grid_size, grid_size), dtype=float)
    np.add.at(grid, (rows, columns), areas_mm2)
    occupied = grid > 1e-9
    occupied_count = int(occupied.sum())
    probabilities = grid.ravel() / max(float(grid.sum()), 1e-9)
    positive = probabilities[probabilities > 0]
    entropy = (
        -float(np.sum(positive * np.log(positive))) / math.log(grid.size)
        if len(positive) else None)
    loads = grid[occupied]
    return {
        "grid_size": grid_size,
        "occupied_cell_fraction": _round(occupied_count / grid.size),
        "cell_load_entropy": _round(entropy),
        "occupied_cell_load_cv": _round(
            float(loads.std() / max(loads.mean(), 1e-9))) if len(loads) else None,
        "footprint_area_sum_fraction_of_frame": _round(
            float(areas_mm2.sum()) / (span_x * span_y)),
        "interpretation_boundary": (
            "area is the sum of source footprints and can double-count overlapping "
            "building parts; grid assignment uses footprint centroids"),
    }, grid


def measure_source_buildings(
    buildings: gpd.GeoDataFrame,
    bbox_wgs84: Sequence[float],
    *,
    model_span_mm: float = DEFAULT_MODEL_SPAN_MM,
    sample_limit: int = DEFAULT_SAMPLE_LIMIT,
    compact_floor_mm: float = 0.21,
    maximum_short_axis_mm: float = 16.0,
    maximum_long_axis_mm: float = 24.0,
    maximum_aspect: float = 4.0,
    minimum_area_mm2: float = 0.04,
) -> dict:
    """Measure raw source footprints in the physical model coordinate system."""

    subset = _subset_wgs84(buildings, bbox_wgs84)
    if subset.empty:
        return {
            "source_footprint_count": 0,
            "error": "no building footprints in frame",
        }
    projection = bbox_to_utm(*bbox_wgs84)
    projected = subset.to_crs(projection["utm_crs"])
    valid = projected.geometry.notna() & ~projected.geometry.is_empty
    projected = projected.loc[valid].reset_index(drop=True)
    minx_m, miny_m, maxx_m, maxy_m = projection["utm_bbox"]
    frame_width_m = maxx_m - minx_m
    frame_height_m = maxy_m - miny_m
    scale = float(model_span_mm) / max(frame_width_m, frame_height_m)
    frame_bounds_mm = [
        0.0, 0.0, frame_width_m * scale, frame_height_m * scale]

    areas_m2 = projected.geometry.area.to_numpy(dtype=float)
    centroids = projected.geometry.centroid
    center_x = (centroids.x.to_numpy(dtype=float) - minx_m) * scale
    center_y = (centroids.y.to_numpy(dtype=float) - miny_m) * scale
    areas_mm2 = areas_m2 * scale * scale
    spatial, grid = _grid_metrics(
        center_x, center_y, areas_mm2, frame_bounds_mm)

    shape_indices = _deterministic_indices(len(projected), sample_limit)
    shape_records = []
    skipped_non_polygonal = 0
    skipped_measurement_error = 0
    for index in shape_indices:
        geometry = _polygonal_geometry(projected.geometry.iloc[int(index)])
        if geometry is None:
            skipped_non_polygonal += 1
            continue
        geometry_mm = affinity.scale(
            geometry, xfact=scale, yfact=scale, origin=(minx_m, miny_m))
        try:
            rotated = _rotated_geometry_metrics(geometry_mm)
            silhouette = silhouette_metrics(
                geometry_mm, simplify_mm=max(0.035, compact_floor_mm * 0.18))
        except (AttributeError, TypeError, ValueError):
            skipped_measurement_error += 1
            continue
        if rotated is None or silhouette is None:
            continue
        record = {
            **rotated,
            **silhouette,
            "area_mm2": float(geometry_mm.area),
        }
        shape_records.append(record)

    comparable = [record for record in shape_records if (
        record["short_axis_mm"] >= compact_floor_mm
        and record["short_axis_mm"] <= maximum_short_axis_mm
        and record["long_axis_mm"] <= maximum_long_axis_mm
        and record["aspect_ratio"] <= maximum_aspect
        and record["area_mm2"] >= minimum_area_mm2
    )]

    def summarize(records: Sequence[Mapping]) -> dict:
        names = (
            "area_mm2", "short_axis_mm", "long_axis_mm", "aspect_ratio",
            "solidity", "rectangularity", "perimeter_excess",
            "concavity_depth_fraction", "outline_vertices",
        )
        return {
            "sample_count": len(records),
            "metrics": {
                name: _percentiles(record[name] for record in records)
                for name in names
            },
            "orientation": orientation_metrics(records),
        }

    # A nearest-neighbour proxy on at most 20k raw centers keeps Chicago's
    # million-footprint cache bounded while preserving a deterministic sample.
    gap_indices = _deterministic_indices(len(projected), 20_000)
    gap_centers = np.column_stack((center_x[gap_indices], center_y[gap_indices]))
    gaps = []
    if len(gap_centers) >= 2:
        tree = cKDTree(gap_centers)
        distances, _ = tree.query(gap_centers, k=2)
        gaps = distances[:, 1]
    spatial["sampled_centroid_nearest_distance_mm"] = _percentiles(gaps)
    spatial["sampled_centroid_count"] = len(gap_indices)

    comparable_fraction = len(comparable) / max(len(shape_records), 1)
    return {
        "source_footprint_count": int(len(projected)),
        "projection": str(projection["utm_crs"]),
        "model_span_mm": float(model_span_mm),
        "scale_mm_per_m": _round(scale, 9),
        "frame_bounds_mm": [_round(value) for value in frame_bounds_mm],
        "all_source": {
            "spatial_fullness": spatial,
            "shape_sample": summarize(shape_records),
            "shape_sample_skipped_non_polygonal": skipped_non_polygonal,
            "shape_sample_skipped_measurement_error": skipped_measurement_error,
        },
        "reference_comparable_sample": {
            "selection": {
                "compact_floor_mm": compact_floor_mm,
                "maximum_short_axis_mm": maximum_short_axis_mm,
                "maximum_long_axis_mm": maximum_long_axis_mm,
                "maximum_aspect": maximum_aspect,
                "minimum_area_mm2": minimum_area_mm2,
            },
            "sample_fraction": _round(comparable_fraction),
            "estimated_population_count": int(round(
                comparable_fraction * len(projected))),
            "shape": summarize(comparable),
            "interpretation_boundary": (
                "population count is extrapolated from the deterministic shape "
                "sample; it is not an exact filtered count"),
        },
        "grid_area_mm2": grid.round(5).tolist(),
    }


def adapt_candidate_evidence(path: Path | None) -> dict | None:
    if path is None or not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    candidate = payload.get("candidate") or {}
    raster = (payload.get("raster_comparison") or {}).get("candidate") or {}
    roles = candidate.get("component_metrics_by_role") or {}
    return {
        "source": str(path.resolve()),
        "comparability": "partial_existing_pipeline_evidence",
        "source_footprints": candidate.get("source_footprints"),
        "output_components": candidate.get("output_components"),
        "role_shape_metrics": roles,
        "raster_spatial_metrics": raster,
        "composition": payload.get("composition"),
        "topology": (payload.get("provenance") or {}).get(
            "scale_aware_topology"),
        "limitations": (
            "existing evidence stores role-level percentiles, not candidate "
            "polygons; rectangularity and orientation cannot be reconstructed"),
    }


def _reference_summary(reference: Mapping) -> dict:
    grammar = reference["block_grammar"]
    return {
        "eligible_component_count": reference["population"][
            "eligible_component_count"],
        "spatial_fullness": grammar["spatial_fullness"],
        "component_shape": grammar["component_shape"],
        "printer": reference.get("printer"),
        "source_3mf": (reference.get("scope") or {}).get("source_3mf"),
    }


def _p50(container: Mapping, metric: str):
    try:
        return container["metrics"][metric]["p50"]
    except (KeyError, TypeError):
        return None


def compare_source_to_reference(source: Mapping, reference: Mapping) -> dict:
    raw = source["reference_comparable_sample"]["shape"]
    ref = reference["block_grammar"]["component_shape"]
    estimated = source["reference_comparable_sample"][
        "estimated_population_count"]
    reference_count = reference["population"]["eligible_component_count"]
    source_spatial = source["all_source"]["spatial_fullness"]
    ref_spatial = reference["block_grammar"]["spatial_fullness"]

    raw_solidity = _p50(raw, "solidity")
    ref_solidity = _p50(ref, "solidity")
    raw_rectangularity = _p50(raw, "rectangularity")
    ref_rectangularity = _p50(ref, "rectangularity")
    raw_perimeter = _p50(raw, "perimeter_excess")
    ref_perimeter = _p50(ref, "perimeter_excess")
    raw_width = _p50(raw, "short_axis_mm")
    ref_width = _p50(ref, "short_axis_mm")

    observations = {
        "reference_to_estimated_comparable_count_ratio": _round(
            reference_count / max(estimated, 1)),
        "reference_to_raw_short_axis_p50_ratio": _round(
            ref_width / max(raw_width, 1e-9))
        if raw_width is not None and ref_width is not None else None,
        "solidity_p50_delta_reference_minus_raw": _round(
            ref_solidity - raw_solidity)
        if raw_solidity is not None and ref_solidity is not None else None,
        "rectangularity_p50_delta_reference_minus_raw": _round(
            ref_rectangularity - raw_rectangularity)
        if raw_rectangularity is not None and ref_rectangularity is not None
        else None,
        "perimeter_excess_p50_delta_reference_minus_raw": _round(
            ref_perimeter - raw_perimeter)
        if raw_perimeter is not None and ref_perimeter is not None else None,
        "occupied_cell_fraction_delta_reference_minus_raw": _round(
            ref_spatial["occupied_cell_fraction"]
            - source_spatial["occupied_cell_fraction"]),
        "orthogonal_coherence_delta_reference_minus_raw": _round(
            ref["orientation"]["orthogonal_coherence"]
            - raw["orientation"]["orthogonal_coherence"])
        if (ref["orientation"]["orthogonal_coherence"] is not None
            and raw["orientation"]["orthogonal_coherence"] is not None)
        else None,
    }
    inferences = []
    if (observations["reference_to_estimated_comparable_count_ratio"] is not None
            and observations[
                "reference_to_estimated_comparable_count_ratio"] < 0.65):
        inferences.append({
            "hypothesis": "reference_demo_uses_component_aggregation_or_selection",
            "basis": "reference compact-component count is substantially below the extrapolated source-compatible population",
            "confidence": "medium",
        })
    if ((observations["rectangularity_p50_delta_reference_minus_raw"] or 0) > 0.06
            and (observations[
                "perimeter_excess_p50_delta_reference_minus_raw"] or 0) < -0.015):
        inferences.append({
            "hypothesis": "reference_demo_uses_outline_regularization",
            "basis": "reference components are more rectangular and have less excess perimeter than raw footprints",
            "confidence": "medium-high",
        })
    if ((observations["reference_to_raw_short_axis_p50_ratio"] or 0) > 1.35):
        inferences.append({
            "hypothesis": "reference_demo_coarsens_the_print_scale_granularity",
            "basis": "reference median short axis is substantially wider than the source-compatible sample",
            "confidence": "medium",
        })
    if (observations["reference_to_estimated_comparable_count_ratio"] is not None
            and observations[
                "reference_to_estimated_comparable_count_ratio"] > 1.5):
        inferences.append({
            "hypothesis": "reference_demo_promotes_sub_threshold_texture_into_printable_blocks",
            "basis": "reference contains substantially more compact components than the source sample predicts above the same printable-size bounds",
            "confidence": "medium",
        })
    return {
        "observations": observations,
        "inferences": inferences,
        "decision_boundary": (
            "inferences indicate geometric design pressure; they do not prove "
            "the reference author's exact algorithm or semantic intent"),
    }


def _metric_rows(source: Mapping, candidate: Mapping | None,
                 reference: Mapping) -> list[tuple[str, object, object, object]]:
    raw_shape = source["reference_comparable_sample"]["shape"]
    ref_shape = reference["block_grammar"]["component_shape"]
    raw_spatial = source["all_source"]["spatial_fullness"]
    ref_spatial = reference["block_grammar"]["spatial_fullness"]
    candidate_count = None
    candidate_status = None
    candidate_raster_components = None
    candidate_coverage = None
    candidate_width = None
    if candidate:
        candidate_count = (candidate.get("output_components") or {}).get("total")
        candidate_status = (candidate.get("composition") or {}).get("status")
        candidate_coverage = (candidate.get("raster_spatial_metrics") or {}).get(
            "land_building_coverage")
        candidate_raster_components = (
            candidate.get("raster_spatial_metrics") or {}).get(
                "raster_components")
        roles = candidate.get("role_shape_metrics") or {}
        nonempty = [value for value in roles.values()
                    if (value.get("components") or 0) > 0]
        if nonempty:
            dominant = max(nonempty, key=lambda value: value["components"])
            candidate_width = (dominant.get("short_axis_model_mm") or {}).get(
                "p50")
    return [
        ("源/策略/参考组件", source["source_footprint_count"], candidate_count,
         reference["population"]["eligible_component_count"]),
        ("候选合成状态", "—", candidate_status, "—"),
        ("候选栅格连通域", "—", candidate_raster_components, "—"),
        ("可比组件估算", source["reference_comparable_sample"][
            "estimated_population_count"], "—", "—"),
        ("面积覆盖", raw_spatial["footprint_area_sum_fraction_of_frame"],
         candidate_coverage,
         ref_spatial["white_top_area_proxy_fraction_of_frame"]),
        ("8×8 占格率", raw_spatial["occupied_cell_fraction"], "—",
         ref_spatial["occupied_cell_fraction"]),
        ("短轴 P50 / mm", _p50(raw_shape, "short_axis_mm"), candidate_width,
         _p50(ref_shape, "short_axis_mm")),
        ("实体度 P50", _p50(raw_shape, "solidity"), "—",
         _p50(ref_shape, "solidity")),
        ("矩形度 P50", _p50(raw_shape, "rectangularity"), "—",
         _p50(ref_shape, "rectangularity")),
        ("周长冗余 P50", _p50(raw_shape, "perimeter_excess"), "—",
         _p50(ref_shape, "perimeter_excess")),
        ("正交一致性", raw_shape["orientation"]["orthogonal_coherence"], "—",
         ref_shape["orientation"]["orthogonal_coherence"]),
    ]


def render_comparison(city: str, source: Mapping, candidate: Mapping | None,
                      reference: Mapping, comparison: Mapping, output: Path):
    width, height = 1500, 1300
    image = Image.new("RGB", (width, height), "#f4f1ea")
    draw = ImageDraw.Draw(image)
    title_font = _font(42)
    head_font = _font(27)
    body_font = _font(23)
    small_font = _font(19)
    ink, muted, accent = "#1e1d1a", "#6f6a62", "#bd4b2d"
    draw.text((58, 44), f"{city}：项目数据 × 当前候选 × 参考 demo",
              fill=ink, font=title_font)
    draw.text((58, 102), "只读几何观测；不是审美自动验收，也不改变生成参数",
              fill=muted, font=small_font)
    columns = [58, 410, 760, 1110]
    headers = ("指标", "原始 OSM", "当前候选", "参考 demo")
    for x, header in zip(columns, headers):
        draw.text((x, 164), header, fill=accent if header == "指标" else ink,
                  font=head_font)
    y = 216
    for label, raw, current, ref in _metric_rows(source, candidate, reference):
        values = (label, raw, current, ref)
        for x, value in zip(columns, values):
            if isinstance(value, float):
                text = f"{value:.4f}"
            elif value is None:
                text = "缺少证据"
            else:
                text = str(value)
            draw.text((x, y), text, fill=ink if x == columns[0] else muted,
                      font=body_font)
        draw.line((58, y + 40, width - 58, y + 40), fill="#d9d2c7", width=1)
        y += 58

    y += 14
    draw.text((58, y), "基于观测的假设", fill=ink, font=head_font)
    y += 48
    labels = {
        "reference_demo_uses_component_aggregation_or_selection":
            "参考 demo 很可能进行了组件聚合或选择",
        "reference_demo_uses_outline_regularization":
            "参考 demo 很可能进行了轮廓规整",
        "reference_demo_coarsens_the_print_scale_granularity":
            "参考 demo 很可能主动放粗了打印尺度粒度",
        "reference_demo_promotes_sub_threshold_texture_into_printable_blocks":
            "参考 demo 很可能把喷嘴以下纹理重组为可打印 block",
    }
    bases = {
        "reference compact-component count is substantially below the extrapolated source-compatible population":
            "参考紧凑组件数显著低于原始数据可比组件估算",
        "reference components are more rectangular and have less excess perimeter than raw footprints":
            "参考组件更接近矩形，且相对凸包的冗余周长更低",
        "reference median short axis is substantially wider than the source-compatible sample":
            "参考组件短轴中位数显著大于原始数据可比样本",
        "reference contains substantially more compact components than the source sample predicts above the same printable-size bounds":
            "同一可打印尺寸口径下，参考组件显著多于原始数据的抽样估算",
    }
    inferences = comparison.get("inferences") or []
    if not inferences:
        draw.text((58, y), "现有指标不足以支持明确的艺术加工假设",
                  fill=muted, font=body_font)
    for inference in inferences:
        text = "• " + labels.get(inference["hypothesis"], inference["hypothesis"])
        draw.text((58, y), text, fill=accent, font=body_font)
        draw.text((80, y + 32),
                  f"置信度：{inference['confidence']}；"
                  f"{bases.get(inference['basis'], inference['basis'])}",
                  fill=muted, font=small_font)
        y += 76
    draw.text((58, height - 78),
              "注意：参考 3MF 的白色紧凑实体只是建筑/街区代理，不保证语义上全是建筑。",
              fill=muted, font=small_font)
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output)


def observe_case(case: Mapping, output_root: Path) -> dict:
    city = str(case["city"])
    buildings, sources = _load_buildings(case)
    reference_path = PROJECT_ROOT / str(case["reference_observation"])
    reference = json.loads(reference_path.read_text(encoding="utf-8"))
    source = measure_source_buildings(
        buildings,
        case["bbox_wgs84"],
        model_span_mm=float(case.get("model_span_mm", DEFAULT_MODEL_SPAN_MM)),
        sample_limit=int(case.get("sample_limit", DEFAULT_SAMPLE_LIMIT)),
        compact_floor_mm=float(
            reference["selection"]["compact_component_floor_mm"]),
        maximum_short_axis_mm=float(
            reference["selection"]["maximum_short_axis_mm"]),
        maximum_long_axis_mm=float(
            reference["selection"]["maximum_long_axis_mm"]),
        maximum_aspect=float(reference["selection"]["maximum_aabb_aspect"]),
        minimum_area_mm2=float(
            reference["selection"]["minimum_projected_top_area_mm2"]),
    )
    candidate_value = case.get("candidate_evidence")
    candidate_path = PROJECT_ROOT / str(candidate_value) if candidate_value else None
    candidate = adapt_candidate_evidence(candidate_path)
    comparison = compare_source_to_reference(source, reference)
    result = {
        "schema_version": SCHEMA_VERSION,
        "purpose": "diagnostic",
        "city": city,
        "scope": {
            "bbox_wgs84": case["bbox_wgs84"],
            "raw_sources": sources,
            "reference_observation": str(reference_path.resolve()),
            "candidate_evidence": (
                str(candidate_path.resolve()) if candidate_path else None),
        },
        "raw_source": source,
        "current_candidate": candidate,
        "reference_demo": _reference_summary(reference),
        "comparison": comparison,
        "limitations": [
            "raw footprint overlap can double-count building parts",
            "source-compatible population count is sample-extrapolated",
            "candidate metrics are partial unless candidate polygons are persisted",
            "reference white-relief material is a morphology proxy, not guaranteed building semantics",
            "all aesthetic inferences require human visual review",
        ],
    }
    observations = output_root / "observations"
    diagnostics = output_root / "diagnostics"
    observations.mkdir(parents=True, exist_ok=True)
    path = observations / f"{city}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    render_comparison(city, source, candidate, reference, comparison,
                      diagnostics / f"{city}.png")
    return result


def write_summary(results: Sequence[Mapping], output_root: Path):
    rows = []
    for result in results:
        source = result["raw_source"]
        reference = result["reference_demo"]
        observations = result["comparison"]["observations"]
        rows.append({
            "city": result["city"],
            "raw_source_count": source["source_footprint_count"],
            "estimated_comparable_source_count": source[
                "reference_comparable_sample"]["estimated_population_count"],
            "reference_component_count": reference[
                "eligible_component_count"],
            **observations,
        })
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "summary.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    with (output_root / "summary.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else ["city"])
        writer.writeheader()
        writer.writerows(rows)
    lines = [
        "# 项目数据与参考 demo 的 block grammar 对比", "",
        "本报告只做只读几何观测。原始 OSM、当前候选和参考 3MF 的语义边界不同，",
        "因此结论以观测和可证伪假设分开记录。", "",
        "| 城市 | 原始足迹 | 可比足迹估算 | 参考组件 | 参考/可比数量 | 短轴倍率 | 矩形度差 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row['city']} | {row['raw_source_count']} | "
            f"{row['estimated_comparable_source_count']} | "
            f"{row['reference_component_count']} | "
            f"{row['reference_to_estimated_comparable_count_ratio']} | "
            f"{row['reference_to_raw_short_axis_p50_ratio']} | "
            f"{row['rectangularity_p50_delta_reference_minus_raw']} |")
    (output_root / "summary.md").write_text("\n".join(lines) + "\n",
                                             encoding="utf-8")


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, required=True,
                        help="JSON array of trusted local observation cases")
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def main():
    args = parse_args()
    cases_path = args.cases if args.cases.is_absolute() else PROJECT_ROOT / args.cases
    output_root = (args.output_dir if args.output_dir.is_absolute()
                   else PROJECT_ROOT / args.output_dir)
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    results = []
    for case in cases:
        print(f"观察 {case['city']} …", flush=True)
        results.append(observe_case(case, output_root))
    write_summary(results, output_root)
    print(output_root.resolve())


if __name__ == "__main__":
    main()
