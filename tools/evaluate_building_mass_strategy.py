#!/usr/bin/env python3
"""Render and measure a fixed-cache building-mass A/B comparison.

This tool is deliberately diagnostic.  It consumes only explicitly supplied,
trusted project pickle caches, does not download data, and never changes the
formal 3MF mesh path.  The output is evidence for human review rather than an
automatic aesthetic acceptance decision.
"""
from __future__ import annotations

import argparse
import copy
from dataclasses import replace
import json
from pathlib import Path
import pickle
import sys
import textwrap
import time

import geopandas as gpd
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy.ndimage import label
from shapely.geometry import box
from shapely.ops import unary_union


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from _TEXTURE_STYLE_OF_DEEPSEEK.buildings import ROAD_TIERS, _build_city_blocks
from _TEXTURE_STYLE_OF_DEEPSEEK.print_profile import DEFAULT_PRINTER_PROFILE
from _TEXTURE_STYLE_OF_DEEPSEEK.road_roles import select_road_roles
from _TEXTURE_STYLE_OF_DEEPSEEK.road_roles import (
    POLICY_VERSION as ROAD_ROLE_POLICY_VERSION,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import (
    bbox_to_utm,
    project_geodataframe,
)
from aesthetic.building_mass_strategy import (
    BuildingMassPolicy,
    build_building_mass_candidate,
    compose_building_mass_with_baseline,
    resolve_component_width_target,
    resolve_building_mass_policy,
)
from aesthetic.height_emphasis_zones import (
    apply_height_emphasis_zones,
    prepare_region_first_height_roles,
)
from aesthetic.scale_aware_topology import coarsen_city_blocks_for_print
from aesthetic.scale_aware_topology import (
    POLICY_VERSION as TOPOLOGY_POLICY_VERSION,
)
from aesthetic.scene_character import analyze_scene_character
from aesthetic.scene_policy import resolve_scene_policy
from aesthetic.review_render import (
    _BLOCK_BASE,
    _PAPER,
    _Rasterizer,
    _downscale_mask,
    _rasterize_mask,
    render_review_bundle,
)


def _parse_bbox(value: str) -> tuple[float, float, float, float]:
    try:
        parts = tuple(float(part.strip()) for part in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("bbox values must be numbers") from exc
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("bbox must be south,west,north,east")
    return parts


def _parse_local_bbox(value: str) -> tuple[float, float, float, float]:
    try:
        parts = tuple(float(part.strip()) for part in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "local bbox values must be numbers") from exc
    if len(parts) != 4 or not (parts[0] < parts[2] and parts[1] < parts[3]):
        raise argparse.ArgumentTypeError(
            "local bbox must be xmin,ymin,xmax,ymax")
    return parts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate experimental two-tier building mass on fixed caches")
    parser.add_argument("--gdf-cache", required=True, type=Path)
    parser.add_argument("--layer-cache", required=True, type=Path)
    parser.add_argument("--bbox", required=True, type=_parse_bbox)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--scene-policy", type=Path)
    parser.add_argument("--road-width-multiplier", type=float, default=1.0)
    parser.add_argument("--model-span-mm", type=float, default=196.0)
    parser.add_argument(
        "--experimental-complete-source-filter", action="store_true",
        help="opt in to the unaccepted v7 bounded complete-source experiment")
    parser.add_argument(
        "--allow-baseline-scale-mismatch", action="store_true",
        help=("diagnostic only: retain an incompatible cached baseline as a "
              "qualitative reference, never as a controlled A/B comparison"))
    parser.add_argument(
        "--topology-cache", type=Path,
        help=("optional trusted, versioned local pickle for derived topology "
              "blocks; raw sources and policy signatures are verified"))
    parser.add_argument(
        "--block-source", choices=("topology", "layer"), default="topology",
        help=("topology rebuilds complete blocks from cached roads/water; "
              "layer uses the already filtered final block_base"))
    parser.add_argument(
        "--height-emphasis-zones", action="store_true", default=False,
        help=("run the region-first building-height A/B after mass aggregation; "
              "no terrain or formal mesh is built"))
    parser.add_argument(
        "--merged-final-clearance", action="store_true", default=False,
        help=("diagnose the production two-layer route: S6 reserves only "
              "shape safety and S8 owns the single final printable road cut"))
    parser.add_argument(
        "--focus-local-bbox", type=_parse_local_bbox,
        help=("optional representative local-metre window; preserves the "
              "full crop printer scale while reducing diagnostic cost"))
    return parser.parse_args()


def _load_trusted_pickle(path: Path):
    if not path.is_file():
        raise SystemExit(f"trusted cache not found: {path}")
    # Pickle execution is intentionally bounded to a path explicitly supplied
    # by the operator.  Never auto-discover or accept an uploaded cache here.
    with path.open("rb") as handle:
        return pickle.load(handle)


def _resolve_diagnostic_scale(layers, projection, model_span_mm,
                              *, allow_mismatch=False):
    """One run scale owns topology, mass, nozzle and measurement units."""
    real_span = max(float(projection["width_m"]), float(projection["height_m"]))
    if (not np.isfinite(real_span) or real_span <= 0
            or not np.isfinite(model_span_mm) or model_span_mm <= 0):
        raise ValueError("model and geographic spans must be finite and positive")
    scale = float(model_span_mm) / real_span
    expected = DEFAULT_PRINTER_PROFILE.nozzle_diameter_mm / scale
    cached = float(getattr(layers, "nozzle_real_m", 0.0) or 0.0)
    known = bool(np.isfinite(cached) and cached > 0)
    matches = known and bool(np.isclose(cached, expected, rtol=1e-5, atol=1e-6))
    if not matches and not allow_mismatch:
        raise ValueError(
            f"cached layer scale mismatch: nozzle_real_m={cached!r}; "
            f"RunSpec requires {expected:.6f} m at {model_span_mm:g} mm / "
            f"{real_span / 1000:.6f} km. Rebuild a matching layer cache, or "
            "use --allow-baseline-scale-mismatch for qualitative diagnostics only.")
    return scale, expected, {
        "authority": "requested_bbox_model_span_and_printer_profile",
        "scale_mm_per_real_m": scale,
        "nozzle_real_m": expected,
        "cached_nozzle_real_m": cached if known else None,
        "cached_scale_matches": matches,
        "comparability": "controlled" if matches else "qualitative_only",
        "cached_baseline_geometry_rescaled": False,
    }


def _evaluation_verdict(composition, scale_evidence):
    """Fallback pixels must never masquerade as an accepted candidate."""
    if composition.get("status") != "composed":
        return {"status": "rerun", "candidate_applied": False,
                "displayed_geometry": "baseline_fallback",
                "label": "未采用候选：显示原图回退"}
    if not scale_evidence["cached_scale_matches"]:
        return {"status": "not_comparable", "candidate_applied": True,
                "displayed_geometry": "composed_candidate",
                "label": "候选诊断：缓存比例不同，不可判定改善"}
    return {"status": "human_review_required", "candidate_applied": True,
            "displayed_geometry": "composed_candidate",
            "label": "候选结果：待人工审图"}


def _ensure_layer_compatibility(layers) -> None:
    """Backfill evidence-only attributes absent from old local caches."""

    defaults = {
        "road_roles": {},
        "water_roles": {},
        "building_height_evidence": {},
        "block_base_classes": [],
        "block_base_cut_lines": [],
    }
    for name, value in defaults.items():
        if not hasattr(layers, name):
            setattr(layers, name, value)


def _copy_layers_for_diagnostic(layers):
    """Copy mutable semantic channels without cloning immutable geometry."""

    result = copy.copy(layers)
    for name in (
        "BL", "BL_categories", "BL_height_roles", "BO", "BO_heights",
        "VL", "VO", "WL", "WO", "block_base", "block_base_classes",
        "block_base_cut_lines", "city_blocks", "roads_lines",
    ):
        if hasattr(layers, name):
            setattr(result, name, list(getattr(layers, name) or ()))
    for name in ("road_roles", "water_roles", "building_height_evidence"):
        if hasattr(layers, name):
            setattr(result, name, dict(getattr(layers, name) or {}))
    return result


def _legacy_topology_cache_is_compatible(
    payload,
    *,
    common_key: dict,
    target_min_model_mm: float,
    hard_floor_model_mm: float,
    boundary_inset_model_mm: float,
) -> bool:
    """Verify a v1 cache without trusting an unrelated building version.

    The old key invalidated road/water topology whenever any building-mass
    policy version changed.  Topology depends only on the explicit controls
    checked here, so a legacy cache may be re-keyed only when both provenance
    and the recorded derived measurements match exactly.
    """

    if not isinstance(payload, dict):
        return False
    key = payload.get("key")
    evidence = payload.get("evidence")
    if (not isinstance(key, dict)
            or key.get("schema") != "building-mass-topology-cache-v1"
            or not isinstance(payload.get("blocks"), list)
            or not isinstance(evidence, dict)):
        return False
    for name, expected in common_key.items():
        if key.get(name) != expected:
            return False
    checks = {
        "policy_version": TOPOLOGY_POLICY_VERSION,
        "target_min_model_mm": float(target_min_model_mm),
        "hard_floor_model_mm": float(hard_floor_model_mm),
        "boundary_inset_each_side_model_mm": float(
            boundary_inset_model_mm),
    }
    for name, expected in checks.items():
        observed = evidence.get(name)
        if isinstance(expected, float):
            try:
                if not np.isclose(float(observed), expected, atol=1e-9):
                    return False
            except (TypeError, ValueError):
                return False
        elif observed != expected:
            return False
    return True


def _subset_wgs84_frame(source: gpd.GeoDataFrame, bbox) -> gpd.GeoDataFrame:
    """Discard obviously out-of-frame cached features before projection.

    Large regional caches can contain more than a million buildings.  Their
    coordinates are still WGS84 here, so a vectorized bounds test avoids
    projecting the complete region.  The later projected clip remains the
    geometric authority and handles exact boundary intersections.
    """

    if source.empty or source.crs is None:
        return source
    try:
        if source.crs.to_epsg() != 4326:
            return source
    except (AttributeError, ValueError):
        return source
    south, west, north, east = bbox
    bounds = source.geometry.bounds
    selected = (
        (bounds["minx"] <= east)
        & (bounds["maxx"] >= west)
        & (bounds["miny"] <= north)
        & (bounds["maxy"] >= south)
    )
    return source.loc[selected].copy()


def _raster_metrics(bundle: dict, *, nozzle_real_m: float,
                    frame_width_m: float) -> dict:
    building = np.clip(bundle["building_mask"], 0.0, 1.0)
    water = np.clip(bundle["water_mask"], 0.0, 1.0)
    land = 1.0 - water
    coverage = float((building * land).sum() / max(float(land.sum()), 1.0))

    binary = (building >= 0.5) & (water < 0.5)
    labels, components = label(binary)
    areas = np.bincount(labels.ravel())[1:] if components else np.asarray([])
    meters_per_px = frame_width_m / max(building.shape[1], 1)
    nozzle_px = nozzle_real_m / max(meters_per_px, 1e-9)
    # Components smaller than one circular nozzle footprint are visually and
    # physically unstable at this raster scale.
    small_limit_px = max(1.0, np.pi * (nozzle_px * 0.5) ** 2)
    small = areas < small_limit_px if areas.size else np.asarray([], dtype=bool)

    grid_coverages = []
    row_edges = np.linspace(0, building.shape[0], 9, dtype=int)
    col_edges = np.linspace(0, building.shape[1], 9, dtype=int)
    for row in range(8):
        for column in range(8):
            rs = slice(row_edges[row], row_edges[row + 1])
            cs = slice(col_edges[column], col_edges[column + 1])
            local_land = land[rs, cs]
            if float(local_land.mean()) < 0.25:
                continue
            grid_coverages.append(float(
                (building[rs, cs] * local_land).sum()
                / max(float(local_land.sum()), 1.0)))
    grid = np.asarray(grid_coverages, dtype=float)
    positive_cells = int(np.count_nonzero(grid >= 0.01))
    mid_density_cells = int(np.count_nonzero((grid >= 0.03) & (grid <= 0.35)))
    return {
        "land_building_coverage": round(coverage, 5),
        "raster_components": int(components),
        "small_component_threshold_px": round(float(small_limit_px), 3),
        "small_component_fraction": (
            round(float(np.mean(small)), 5) if areas.size else None),
        "small_component_ink_fraction": (
            round(float(areas[small].sum() / max(areas.sum(), 1)), 5)
            if areas.size else None),
        "largest_component_ink_fraction": (
            round(float(areas.max() / max(areas.sum(), 1)), 5)
            if areas.size else None),
        "occupied_density_cells_8x8": positive_cells,
        "mid_density_cells_8x8": mid_density_cells,
        "density_cell_cv": (
            round(float(grid.std() / max(grid.mean(), 1e-9)), 5)
            if grid.size else None),
    }


def _render_roles(candidate, layers, extent, output_path: Path) -> Path:
    size = 2048
    raster = _Rasterizer(extent, size * 2, size * 2)

    def mask(polygons):
        raw = _rasterize_mask(raster, polygons)
        return _downscale_mask(raw, size) >= 0.5

    image = np.full((size, size, 3), _PAPER, dtype=np.uint8)
    image[mask(layers.block_base)] = _BLOCK_BASE
    image[mask(candidate.quiet_texture)] = (166, 194, 205)
    image[mask(candidate.urban_mass)] = (196, 112, 67)
    image[mask(candidate.sparse_printable)] = (211, 179, 93)
    image[mask([polygon for polygon, _ in layers.BL])] = (122, 60, 45)
    rendered = Image.fromarray(image)
    draw = ImageDraw.Draw(rendered)
    draw.rectangle((18, 18, 770, 70), fill=(247, 247, 245))
    draw.text(
        (28, 34),
        "quiet=blue  urban mass=orange  sparse=ochre  existing BL=dark red",
        fill=(25, 25, 25),
    )
    rendered.save(output_path)
    return output_path


def _comparison_check(
        check_id: str,
        *,
        current,
        reference,
        status: str,
        note: str,
) -> dict:
    return {
        "id": check_id,
        "current": current,
        "reference": reference,
        "status": status,
        "note": note,
    }


def _build_reference_comparison(
        candidate_evidence: dict,
        composition: dict,
        raster_comparison: dict,
) -> dict:
    """Build an explicit, machine-readable demo/effect comparison.

    The reference audit is a cross-city 3MF morphology cohort rather than a
    same-bbox pixel target. Only directly comparable measurements receive a
    pass/warn status; changes without an established acceptance bound remain
    informational so the diagnostic cannot masquerade as aesthetic proof.
    """

    silhouette = candidate_evidence.get("silhouette_regularization", {})
    reference_audit = silhouette.get("reference_audit", {})
    after = silhouette.get("after", {})
    width_target = candidate_evidence.get("component_width_target", {})
    hard_boundaries = candidate_evidence.get("hard_boundaries", {})
    representation = candidate_evidence.get("global_representation", {})
    role_metrics = candidate_evidence.get("component_metrics_by_role", {})
    urban_role_metrics = role_metrics.get("urban_mass", {})
    role_filter = candidate_evidence.get("mid_frequency_role_filter", {})
    baseline = raster_comparison.get("baseline", {})
    candidate = raster_comparison.get("candidate", {})

    width = (
        urban_role_metrics.get("short_axis_model_mm", {}).get("p50")
        if urban_role_metrics else
        candidate_evidence.get("component_min_width_p50_model_mm"))
    width_low = width_target.get("target_min_model_mm")
    width_high = width_target.get("target_max_model_mm")
    width_relation = width_target.get("status")
    if width_relation == "within_target":
        width_status = "pass"
    elif width is None or width_low is None or width_high is None:
        width_status = "unknown"
    else:
        width_status = "warn"

    solidity = (
        urban_role_metrics.get("solidity", {}).get("p50")
        if urban_role_metrics else
        after.get("solidity", {}).get("p50"))
    outline_vertices = (
        urban_role_metrics.get("outline_vertices", {}).get("p50")
        if urban_role_metrics else
        after.get("outline_vertices", {}).get("p50"))
    solidity_range = reference_audit.get("city_median_solidity_range")
    if (solidity is None or not isinstance(solidity_range, list)
            or len(solidity_range) != 2):
        solidity_status = "unknown"
    elif float(solidity_range[0]) <= float(solidity) <= float(
            solidity_range[1]):
        solidity_status = "pass"
    else:
        solidity_status = "warn"

    seam = hard_boundaries.get("observed_minimum_two_sided_seam_nozzles")
    required_seam = hard_boundaries.get(
        "required_minimum_two_sided_seam_nozzles")
    seam_passed = hard_boundaries.get("boundary_clearance_passed")
    seam_status = (
        "pass" if seam_passed is True else
        "warn" if seam_passed is False else
        "unknown"
    )

    checks = [
        _comparison_check(
            "component_short_axis_p50_model_mm",
            current=width,
            reference=[width_low, width_high],
            status=width_status,
            note=("standard-relief urban mass versus the project visual-size "
                  "band; printer floor remains a separate hard bound"),
        ),
        _comparison_check(
            "silhouette_solidity_p50",
            current=solidity,
            reference=solidity_range,
            status=solidity_status,
            note="candidate component median versus reference city medians",
        ),
        _comparison_check(
            "outline_vertices",
            current={"candidate_p50": outline_vertices},
            reference={"demo_city_p75_typical_range": reference_audit.get(
                "typical_effective_outline_vertices_p75")},
            status="info",
            note="different percentiles; context only, not a pass/fail gate",
        ),
        _comparison_check(
            "road_seam_nozzles",
            current=seam,
            reference={"required_minimum": required_seam},
            status=seam_status,
            note="project printer boundary, not a reference-demo metric",
        ),
    ]

    def _delta(before, after_value):
        if before is None or after_value is None:
            return None
        return round(float(after_value) - float(before), 5)

    return {
        "schema_version": "reference-demo-effect-comparison-v1",
        "scope": {
            "kind": "cross_city_reference_cohort",
            "comparability": "morphology_only",
            "cohort": reference_audit.get("cohort"),
            "semantic_boundary": reference_audit.get("semantic_boundary"),
            "same_bbox_reference_image": False,
        },
        "representation": {
            "mode": representation.get("mode", "unknown"),
            "reason": representation.get("reason"),
            "composition_status": composition.get("status", "unknown"),
            "measurement_role": (
                "urban_mass" if urban_role_metrics else "all_components"),
            "mid_frequency_role_filter": role_filter,
        },
        "checks": checks,
        "measured_effect": {
            "land_building_coverage": {
                "baseline": baseline.get("land_building_coverage"),
                "candidate": candidate.get("land_building_coverage"),
                "absolute_delta": _delta(
                    baseline.get("land_building_coverage"),
                    candidate.get("land_building_coverage")),
            },
            "raster_components": {
                "baseline": baseline.get("raster_components"),
                "candidate": candidate.get("raster_components"),
                "delta": _delta(
                    baseline.get("raster_components"),
                    candidate.get("raster_components")),
            },
            "small_component_ink_fraction": {
                "baseline": baseline.get("small_component_ink_fraction"),
                "candidate": candidate.get("small_component_ink_fraction"),
                "absolute_delta": _delta(
                    baseline.get("small_component_ink_fraction"),
                    candidate.get("small_component_ink_fraction")),
            },
            "largest_component_ink_fraction": {
                "baseline": baseline.get("largest_component_ink_fraction"),
                "candidate": candidate.get("largest_component_ink_fraction"),
                "absolute_delta": _delta(
                    baseline.get("largest_component_ink_fraction"),
                    candidate.get("largest_component_ink_fraction")),
            },
        },
        "interpretation": (
            "Reference bounds expose morphology gaps but do not prove beauty; "
            "baseline-to-candidate deltas show the actual effect of this run."),
    }


def _load_panel_font(size: int, *, bold: bool = False):
    names = (
        ("DejaVuSans-Bold.ttf", "Arial Bold.ttf") if bold
        else ("DejaVuSans.ttf", "Arial.ttf")
    )
    for name in names:
        try:
            return ImageFont.truetype(name, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _format_number(value, digits: int = 3) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, int):
        return f"{value:,}"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _format_range(value, digits: int = 3) -> str:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return "unknown"
    return f"{_format_number(value[0], digits)}-{_format_number(value[1], digits)}"


def _render_reference_scorecard(comparison: dict, output_path: Path) -> Path:
    """Render the sixth A/B panel: reference context plus measured deltas."""

    size = 720
    image = Image.new("RGB", (size, size), (247, 246, 242))
    draw = ImageDraw.Draw(image)
    title_font = _load_panel_font(28, bold=True)
    section_font = _load_panel_font(20, bold=True)
    body_font = _load_panel_font(18)
    small_font = _load_panel_font(15)
    status_font = _load_panel_font(15, bold=True)
    status_colors = {
        "pass": (43, 111, 77),
        "warn": (180, 92, 38),
        "info": (55, 92, 130),
        "unknown": (112, 112, 108),
    }

    draw.rectangle((0, 0, size, 78), fill=(38, 40, 38))
    draw.text((24, 18), "REFERENCE DEMO + MEASURED EFFECT",
              font=title_font, fill=(249, 248, 245))
    scope = comparison.get("scope", {})
    cohort = scope.get("cohort") or "reference cohort unavailable"
    cohort_parts = cohort.split(":", 1)
    cohort_short = cohort_parts[0]
    draw.text((24, 91), cohort_short, font=section_font,
              fill=(41, 41, 39))
    cohort_names = cohort_parts[1].strip() if len(cohort_parts) == 2 else ""
    for index, line in enumerate(textwrap.wrap(cohort_names, width=84)[:2]):
        draw.text((24, 120 + index * 18), line, font=small_font,
                  fill=(76, 74, 69))
    draw.text((24, 158), "Cross-city 3MF morphology; no same-bbox pixel target",
              font=small_font, fill=(102, 99, 91))

    checks = {check["id"]: check for check in comparison.get("checks", [])}
    width = checks.get("component_short_axis_p50_model_mm", {})
    solidity = checks.get("silhouette_solidity_p50", {})
    turns = checks.get("outline_vertices", {})
    seam = checks.get("road_seam_nozzles", {})

    def metric_line(y, label_text, current_text, reference_text, status):
        draw.text((24, y), label_text, font=body_font, fill=(45, 45, 43))
        draw.text((270, y), current_text, font=body_font, fill=(45, 45, 43))
        draw.text((414, y), reference_text, font=body_font, fill=(89, 86, 80))
        normalized = status if status in status_colors else "unknown"
        draw.rounded_rectangle(
            (627, y - 2, 699, y + 24), radius=6,
            fill=status_colors[normalized])
        draw.text((638, y + 2), normalized.upper(), font=status_font,
                  fill=(255, 255, 255))

    draw.text((24, 190), "metric", font=small_font, fill=(112, 108, 101))
    draw.text((270, 190), "current", font=small_font, fill=(112, 108, 101))
    draw.text((414, 190), "demo / requirement", font=small_font,
              fill=(112, 108, 101))
    draw.line((24, 205, 696, 205), fill=(210, 206, 197), width=2)
    metric_line(
        216, "urban short axis p50",
        f"{_format_number(width.get('current'))} mm",
        f"{_format_range(width.get('reference'))} mm", width.get("status"))
    metric_line(
        252, "solidity p50", _format_number(solidity.get("current")),
        _format_range(solidity.get("reference")), solidity.get("status"))
    turn_current = turns.get("current") or {}
    turn_reference = turns.get("reference") or {}
    metric_line(
        288, "outline vertices", f"p50 {_format_number(turn_current.get('candidate_p50'))}",
        f"p75 {_format_range(turn_reference.get('demo_city_p75_typical_range'), 0)}",
        "info")
    seam_reference = seam.get("reference") or {}
    metric_line(
        324, "road seam", f"{_format_number(seam.get('current'))} noz",
        f">= {_format_number(seam_reference.get('required_minimum'))}",
        seam.get("status"))

    draw.text((24, 374), "BASELINE -> CANDIDATE", font=section_font,
              fill=(41, 41, 39))
    effect = comparison.get("measured_effect", {})

    def effect_line(y, label_text, key, digits=4):
        values = effect.get(key, {})
        before = _format_number(values.get("baseline"), digits)
        after_value = _format_number(values.get("candidate"), digits)
        delta = (values.get("delta") if "delta" in values
                 else values.get("absolute_delta"))
        delta_text = _format_number(delta, digits)
        if delta is not None and float(delta) > 0:
            delta_text = "+" + delta_text
        draw.text((24, y), label_text, font=body_font, fill=(45, 45, 43))
        draw.text((270, y), f"{before} -> {after_value}",
                  font=body_font, fill=(45, 45, 43))
        draw.text((583, y), f"d {delta_text}", font=small_font,
                  fill=(89, 86, 80))

    effect_line(413, "land coverage", "land_building_coverage")
    effect_line(449, "raster components", "raster_components", digits=0)
    effect_line(485, "small-fragment ink", "small_component_ink_fraction", 5)
    effect_line(521, "largest-component ink", "largest_component_ink_fraction", 5)

    representation = comparison.get("representation", {})
    draw.line((24, 570, 696, 570), fill=(210, 206, 197), width=2)
    draw.text((24, 586), "ROUTING", font=small_font, fill=(112, 108, 101))
    draw.text((120, 582), str(representation.get("mode", "unknown")),
              font=body_font, fill=(45, 45, 43))
    draw.text((24, 616), "RESULT", font=small_font, fill=(112, 108, 101))
    draw.text((120, 612), str(representation.get(
        "composition_status", "unknown")), font=body_font,
        fill=(45, 45, 43))
    role_filter = representation.get("mid_frequency_role_filter", {}) or {}
    draw.text((24, 646), "ROLE FILTER", font=small_font,
              fill=(112, 108, 101))
    draw.text(
        (155, 642),
        f"{_format_number(role_filter.get('demoted_to_quiet_texture'), 0)} "
        "narrow mass -> quiet",
        font=body_font, fill=(45, 45, 43))

    interpretation = comparison.get("interpretation", "")
    wrapped = textwrap.wrap(interpretation, width=78)[:2]
    for index, line in enumerate(wrapped):
        draw.text((24, 682 + index * 18), line, font=small_font,
                  fill=(102, 99, 91))
    image.save(output_path)
    return output_path


def _contact_sheet(entries: list[tuple[str, Path]], output_path: Path) -> Path:
    thumb_size = 720
    header = 48
    columns = 3
    rows = (len(entries) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * thumb_size, rows * (thumb_size + header)),
                      (238, 236, 230))
    draw = ImageDraw.Draw(sheet)
    for index, (title, path) in enumerate(entries):
        image = Image.open(path).convert("RGB")
        image.thumbnail((thumb_size, thumb_size), Image.Resampling.LANCZOS)
        column = index % columns
        row = index // columns
        x = column * thumb_size + (thumb_size - image.width) // 2
        y = row * (thumb_size + header) + header
        sheet.paste(image, (x, y))
        draw.text((column * thumb_size + 14,
                   row * (thumb_size + header) + 15), title, fill=(25, 25, 25),
                  font=_load_panel_font(18))
    sheet.save(output_path)
    return output_path


def main() -> int:
    args = parse_args()
    started = time.perf_counter()
    payload = _load_trusted_pickle(args.gdf_cache)
    layers = _load_trusted_pickle(args.layer_cache)
    if not isinstance(payload, dict) or "buildings" not in payload:
        raise SystemExit("gdf cache must contain a buildings GeoDataFrame")
    if not hasattr(layers, "block_base"):
        raise SystemExit("layer cache does not contain LayerPolygons")
    _ensure_layer_compatibility(layers)
    baseline_layers = _copy_layers_for_diagnostic(layers)
    height_emphasis_preparation = {
        "status": "inactive",
        "reason": "region-first height diagnostic not requested",
    }
    if args.height_emphasis_zones:
        height_emphasis_preparation = prepare_region_first_height_roles(layers)

    projection = bbox_to_utm(*args.bbox)
    try:
        scale_mm_per_m, nozzle_real_m, scale_evidence = _resolve_diagnostic_scale(
            layers, projection, args.model_span_mm,
            allow_mismatch=args.allow_baseline_scale_mismatch)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    utm_bbox = projection["utm_bbox"]
    origin = projection["origin"]
    bbox_local = (
        utm_bbox[0] - origin[0], utm_bbox[1] - origin[1],
        utm_bbox[2] - origin[0], utm_bbox[3] - origin[1],
    )
    projected = {}
    for key in ("buildings", "roads", "water"):
        source = payload.get(key)
        if source is None or not isinstance(source, gpd.GeoDataFrame):
            if key == "buildings":
                raise SystemExit("buildings cache entry must be a GeoDataFrame")
            source = gpd.GeoDataFrame(
                {"geometry": []}, geometry="geometry", crs="EPSG:4326")
        scoped = _subset_wgs84_frame(source, args.bbox)
        print(
            f"[mass] projecting {len(scoped):,}/{len(source):,} "
            f"in-frame {key} features")
        projected[key] = project_geodataframe(
            scoped, projection["utm_crs"], origin, clip_bbox=utm_bbox)
    projected_buildings = projected["buildings"]

    focus_box = None
    if args.focus_local_bbox:
        focus_box = box(*args.focus_local_bbox)
        for key, source in projected.items():
            if source is not None and len(source):
                projected[key] = source.loc[
                    source.geometry.intersects(focus_box)].copy()
        projected_buildings = projected["buildings"]
        print(
            "[mass] representative focus after projection: "
            f"buildings={len(projected_buildings):,}, "
            f"roads={len(projected['roads']):,}, "
            f"water={len(projected['water']):,}")
    analysis_bbox_local = args.focus_local_bbox or bbox_local

    topology_evidence = None
    road_roles = None
    retained_surface_lines = []
    topology_cache_status = "not_requested"
    if args.block_source == "topology":
        building_mass_policy = BuildingMassPolicy()
        width_target = resolve_component_width_target(
            building_mass_policy,
            printer_profile=DEFAULT_PRINTER_PROFILE,
            scale_mm_per_m=scale_mm_per_m,
            model_span_mm=args.model_span_mm,
        )
        hard_floor_model_mm = float(
            DEFAULT_PRINTER_PROFILE.min_colored_strip_mm)
        boundary_inset_model_mm = (
            DEFAULT_PRINTER_PROFILE.surface_road_gap_mm / 2.0
            + DEFAULT_PRINTER_PROFILE.nozzle_diameter_mm * (
                building_mass_policy.simplify_nozzles
                + building_mass_policy.boundary_clearance_safety_nozzles))
        source_stat = args.gdf_cache.stat()
        topology_cache_common_key = {
            "gdf_cache": str(args.gdf_cache.resolve()),
            "gdf_cache_size": int(source_stat.st_size),
            "gdf_cache_mtime_ns": int(source_stat.st_mtime_ns),
            "bbox": list(args.bbox),
            "model_span_mm": float(args.model_span_mm),
            "road_width_multiplier": float(args.road_width_multiplier),
            "road_role_policy": ROAD_ROLE_POLICY_VERSION,
            "topology_policy": TOPOLOGY_POLICY_VERSION,
            "printer_profile": DEFAULT_PRINTER_PROFILE.to_dict(),
        }
        topology_cache_key = {
            "schema": "building-mass-topology-cache-v2",
            **topology_cache_common_key,
            "topology_shape_controls": {
                "target_min_model_mm": hard_floor_model_mm,
                "hard_floor_model_mm": hard_floor_model_mm,
                "boundary_inset_each_side_model_mm": (
                    boundary_inset_model_mm),
            },
        }
        cached = None
        rekey_legacy_cache = False
        if args.topology_cache and args.topology_cache.is_file():
            with args.topology_cache.open("rb") as handle:
                payload = pickle.load(handle)
            if (isinstance(payload, dict)
                    and payload.get("key") == topology_cache_key
                    and isinstance(payload.get("blocks"), list)
                    and isinstance(payload.get("evidence"), dict)):
                cached = payload
                topology_cache_status = "hit"
            elif _legacy_topology_cache_is_compatible(
                    payload,
                    common_key=topology_cache_common_key,
                    target_min_model_mm=hard_floor_model_mm,
                    hard_floor_model_mm=hard_floor_model_mm,
                    boundary_inset_model_mm=boundary_inset_model_mm):
                cached = payload
                topology_cache_status = "legacy_hit_verified"
                rekey_legacy_cache = True
        if cached is not None:
            candidate_blocks = cached["blocks"]
            topology_evidence = cached["evidence"]
            if rekey_legacy_cache and args.topology_cache:
                with args.topology_cache.open("wb") as handle:
                    pickle.dump({
                        "key": topology_cache_key,
                        "blocks": candidate_blocks,
                        "evidence": topology_evidence,
                    }, handle, protocol=pickle.HIGHEST_PROTOCOL)
                topology_cache_status = "legacy_hit_verified_rekeyed"
            print(f"[mass] loaded {len(candidate_blocks):,} topology blocks "
                  f"from {args.topology_cache}")
        else:
            topology_cache_status = (
                "miss_written" if args.topology_cache else "not_requested")
            block_started = time.perf_counter()
            road_roles = select_road_roles(
                projected["roads"],
                topology_tier=4,
                nozzle_real_m=nozzle_real_m,
                bbox_local=analysis_bbox_local,
                scale_mm_per_m=scale_mm_per_m,
                road_width_multiplier=args.road_width_multiplier,
                min_colored_strip_mm=(
                    DEFAULT_PRINTER_PROFILE.min_colored_strip_mm),
            )
            initial_blocks = _build_city_blocks(
                road_roles.topology, projected["water"], road_tier=4,
                bbox_local=analysis_bbox_local)
            if "highway" in road_roles.topology.columns:
                protected_major = road_roles.topology.loc[
                    road_roles.topology["highway"].isin(set(ROAD_TIERS[1]))]
            else:
                protected_major = road_roles.topology
            protected_lines = list(protected_major.geometry)
            protected_lines.extend(list(road_roles.visible.geometry))
            candidate_blocks, retained_surface_lines, topology_evidence = (
                coarsen_city_blocks_for_print(
                    initial_blocks,
                    buildings=projected_buildings,
                    cut_lines=road_roles.topology,
                    protected_cut_lines=protected_lines,
                    water=projected["water"],
                    scale_mm_per_m=scale_mm_per_m,
                    target_min_model_mm=hard_floor_model_mm,
                    hard_floor_model_mm=hard_floor_model_mm,
                    boundary_inset_model_mm=boundary_inset_model_mm,
                )
            )
            print(f"[mass] rebuilt {len(candidate_blocks):,} topology blocks "
                  f"in {time.perf_counter() - block_started:.1f}s")
            if args.topology_cache:
                args.topology_cache.parent.mkdir(parents=True, exist_ok=True)
                temporary = args.topology_cache.with_suffix(
                    args.topology_cache.suffix + ".tmp")
                with temporary.open("wb") as handle:
                    pickle.dump({
                        "key": topology_cache_key,
                        "blocks": candidate_blocks,
                        "evidence": topology_evidence,
                    }, handle, protocol=pickle.HIGHEST_PROTOCOL)
                temporary.replace(args.topology_cache)
                print(f"[mass] wrote topology cache {args.topology_cache}")
    else:
        candidate_blocks = list(layers.block_base)
    if focus_box is not None:
        focused_blocks = []
        for block in candidate_blocks:
            if block is None or block.is_empty or not block.intersects(focus_box):
                continue
            clipped = block.intersection(focus_box)
            if clipped.geom_type == "Polygon":
                focused_blocks.append(clipped)
            elif clipped.geom_type in {"MultiPolygon", "GeometryCollection"}:
                focused_blocks.extend(
                    child for child in clipped.geoms
                    if child.geom_type == "Polygon" and not child.is_empty)
        candidate_blocks = focused_blocks
        print(f"[mass] representative focus blocks={len(candidate_blocks):,}")
    scene_character = analyze_scene_character(
        projected["roads"], projected["buildings"], projected["water"],
        analysis_bbox_local,
        grid_size=8,
        nozzle_real_m=nozzle_real_m,
        model_span_mm=args.model_span_mm,
    )
    if args.scene_policy:
        scene_policy = json.loads(
            args.scene_policy.read_text(encoding="utf-8"))
        scene_policy_source = "explicit_file"
    else:
        scene_policy = resolve_scene_policy(
            scene_character, printer_profile=DEFAULT_PRINTER_PROFILE,
            activation=(
                "active" if args.height_emphasis_zones else "audit_only"))
        scene_policy_source = "measured_fixed_cache"
    resolved_policy, policy_resolution = resolve_building_mass_policy(
        projected_buildings,
        nozzle_real_m=nozzle_real_m,
        source_scene_policy=scene_policy,
    )
    resolved_policy = replace(
        resolved_policy,
        experimental_complete_source_filter=args.experimental_complete_source_filter)
    candidate = build_building_mass_candidate(
        projected_buildings,
        candidate_blocks,
        nozzle_real_m=nozzle_real_m,
        printer_profile=DEFAULT_PRINTER_PROFILE,
        model_span_mm=args.model_span_mm,
        exclusion_polys=[
            polygon for polygon, _ in layers.BL
            if focus_box is None or polygon.intersects(focus_box)],
        policy=resolved_policy,
        source_scene_policy=scene_policy,
        downstream_final_clearance=args.merged_final_clearance,
    )
    candidate.evidence["policy_resolution"] = policy_resolution
    policy_resolution["effective_policy_after_hard_bounds"] = (
        candidate.evidence["policy"])

    args.output_dir.mkdir(parents=True, exist_ok=True)
    ctx = {
        "bbox_local": analysis_bbox_local,
        "scale": scale_mm_per_m,
    }
    baseline = render_review_bundle(
        baseline_layers, ctx, args.road_width_multiplier,
        str(args.output_dir), f"{args.tag}_baseline")

    candidate_layers = copy.copy(layers)
    heights = candidate.evidence["semantic_heights"]
    baseline_bo = [
        polygon for polygon in layers.BO
        if focus_box is None or polygon.intersects(focus_box)]
    quiet, mass, composition = compose_building_mass_with_baseline(
        baseline_bo, candidate, nozzle_real_m=nozzle_real_m)
    candidate_layers.BO = quiet + mass
    candidate_layers.BO_heights = (
        [float(heights["quiet_texture_mm"])] * len(quiet)
        + [float(heights["urban_mass_mm"])] * len(mass)
    )
    candidate_layers.BL = list(layers.BL)
    candidate_layers.BL_categories = list(
        getattr(layers, "BL_categories", ()) or ())
    candidate_layers.BL_height_roles = list(
        getattr(layers, "BL_height_roles", ()) or ())
    height_emphasis = {
        **height_emphasis_preparation,
        "status": "inactive",
        "reason": "region-first height diagnostic not requested",
    }
    if args.height_emphasis_zones:
        height_emphasis = {
            **height_emphasis_preparation,
            **apply_height_emphasis_zones(
                candidate_layers,
                projected_buildings,
                scene_character,
                scene_policy,
                printer_profile=DEFAULT_PRINTER_PROFILE,
                scale_mm_per_m=scale_mm_per_m,
                building_mass_evidence={"status": "active"},
            ),
        }
        if height_emphasis.get("status") != "active":
            raise SystemExit(
                "region-first height diagnostic failed: "
                f"{height_emphasis}")
    if args.merged_final_clearance and road_roles is not None:
        # Match the formal two-layer route for the visual diagnostic: BO is
        # merged into block_base, then the complete structural road network
        # cuts one printer-owned 0.84 mm seam. Visible road material remains
        # a separate, smaller hierarchy laid over those continuous seams.
        merged = [
            polygon for polygon in list(layers.block_base) + list(candidate_layers.BO)
            if focus_box is None or polygon.intersects(focus_box)]
        # Reference urban models use a dense, continuous lower-surface road
        # texture, while reserving the strongest separation for the arterial
        # hierarchy.  Reproduce that distinction in the diagnostic instead
        # of cutting every retained road with the same two-extrusion void.
        # The local network remains bounded by the printer's coloured-strip
        # floor; major/visible roads keep the conservative structural gap.
        surface = [
            geometry for geometry in road_roles.topology.geometry
            if geometry is not None and not geometry.is_empty]
        major_gdf = road_roles.structural
        if "highway" in major_gdf.columns:
            major_gdf = major_gdf.loc[major_gdf["highway"].isin({
                "motorway", "motorway_link", "trunk", "trunk_link",
                "primary", "primary_link", "secondary", "secondary_link",
            })]
        major = [
            geometry for geometry in major_gdf.geometry
            if geometry is not None and not geometry.is_empty]
        surface_gap_mm = DEFAULT_PRINTER_PROFILE.surface_road_gap_mm
        surface_cutter = (
            unary_union(surface).buffer(
                surface_gap_mm / scale_mm_per_m / 2.0, join_style=1)
            if surface else None)
        major_cutter = (
            unary_union(major).buffer(
                DEFAULT_PRINTER_PROFILE.final_block_base_gap_mm
                / scale_mm_per_m / 2.0,
                join_style=1,
            )
            if major else None)
        cutter_parts = [
            geometry for geometry in (surface_cutter, major_cutter)
            if geometry is not None and not geometry.is_empty]
        cutter = unary_union(cutter_parts) if cutter_parts else None
        cleared = []
        for polygon in merged:
            geometry = polygon if cutter is None else polygon.difference(cutter)
            if geometry.is_empty:
                continue
            if geometry.geom_type == "Polygon":
                cleared.append(geometry)
            elif geometry.geom_type in {"MultiPolygon", "GeometryCollection"}:
                cleared.extend(
                    child for child in geometry.geoms
                    if child.geom_type == "Polygon" and not child.is_empty)
        candidate_layers.block_base = cleared
        candidate_layers.BO = []
        candidate_layers.BO_heights = []
        # The formal mesh reveals the lower terrain/material through these
        # cuts.  The lightweight polygon renderer has no terrain mesh, so draw
        # the same complete centre-lines as the visible proxy; otherwise it
        # misleadingly shows only the sparse explicit-road material layer.
        candidate_layers.roads_lines = [
            (geometry, "unclassified", False, "context")
            for geometry in surface
        ]
        candidate_layers.road_roles = dict(
            getattr(candidate_layers, "road_roles", {}) or {})
        candidate_layers.road_roles["diagnostic_render_semantics"] = (
            "topology centre-lines proxy lower-surface road reveals")
        candidate.evidence["diagnostic_final_clearance_preview"] = {
            "status": "applied",
            "surface_gap_model_mm": surface_gap_mm,
            "major_gap_model_mm": (
                DEFAULT_PRINTER_PROFILE.final_block_base_gap_mm),
            "surface_cutters": len(surface),
            "major_cutters": len(major),
            "merged_input_polygons": len(merged),
            "cleared_output_polygons": len(cleared),
            "formal_mesh_changed": False,
        }
    candidate_bundle = render_review_bundle(
        candidate_layers, ctx, args.road_width_multiplier,
        str(args.output_dir), f"{args.tag}_candidate")
    roles_path = _render_roles(
        candidate, candidate_layers, analysis_bbox_local,
        args.output_dir / f"{args.tag}_candidate_roles.png")
    raster_comparison = {
        "baseline": _raster_metrics(
            baseline, nozzle_real_m=nozzle_real_m,
            frame_width_m=projection["width_m"]),
        "candidate": _raster_metrics(
            candidate_bundle, nozzle_real_m=nozzle_real_m,
            frame_width_m=projection["width_m"]),
    }
    reference_comparison = _build_reference_comparison(
        candidate.evidence, composition, raster_comparison)
    reference_scorecard_path = _render_reference_scorecard(
        reference_comparison,
        args.output_dir / f"{args.tag}_reference_demo_scorecard.png")
    verdict = _evaluation_verdict(composition, scale_evidence)
    sheet_path = _contact_sheet([
        ("1 / 原有建筑层 · 俯视", Path(baseline["topdown"])),
        ("2 / " + verdict["label"], Path(candidate_bundle["topdown"])),
        ("3 / 原始候选角色（不代表已采用）", roles_path),
        ("4 / 原有建筑层 · 高度", Path(baseline["height"])),
        ("5 / " + verdict["label"] + " · 高度", Path(candidate_bundle["height"])),
        ("6 / 参考形态与本次测量", reference_scorecard_path),
    ], args.output_dir / f"{args.tag}_building_mass_ab.png")

    evidence = {
        "evaluation_version": "building-stage-ab-v5",
        "status": verdict["status"],
        "display_verdict": verdict,
        "formal_print_acceptance": "not_evaluated",
        "provenance": {
            "gdf_cache": str(args.gdf_cache.resolve()),
            "layer_cache": str(args.layer_cache.resolve()),
            "bbox_wgs84": list(args.bbox),
            "scale": scale_evidence,
            "tag": args.tag,
            "candidate_block_source": args.block_source,
            "candidate_blocks": len(candidate_blocks),
            "topology_cache": {
                "status": topology_cache_status,
                "path": (str(args.topology_cache.resolve())
                         if args.topology_cache else None),
            },
            "scale_aware_topology": topology_evidence,
            "scene_policy_source": scene_policy_source,
            "scene_policy_version": scene_policy.get("policy_version"),
            "building_quality_version": (
                scene_policy.get("roles", {}).get("buildings", {})
                .get("local_strategy", {}).get("version")),
            "building_quality_summary": {
                key: scene_policy.get("roles", {}).get(
                    "buildings", {}).get(key)
                for key in (
                    "distribution_profile",
                    "local_data_completeness_score",
                    "building_distribution_continuity_score",
                    "suspected_building_gap_fraction_of_urban",
                    "positive_building_coverage_cv",
                    "footprint_frame_coverage",
                    "recommended_representation",
                )
            },
            "building_stage_only": True,
            "terrain_mesh_built": False,
            "vegetation_mesh_built": False,
            "block_base_mesh_built": False,
            "formal_3mf_built": False,
        },
        "candidate": candidate.evidence,
        "composition": composition,
        "height_emphasis_zones": height_emphasis,
        "raster_comparison": raster_comparison,
        "reference_comparison": reference_comparison,
        "artifacts": {
            "contact_sheet": str(sheet_path.resolve()),
            "baseline_topdown": str(Path(baseline["topdown"]).resolve()),
            "candidate_topdown": str(Path(candidate_bundle["topdown"]).resolve()),
            "candidate_roles": str(roles_path.resolve()),
            "baseline_height": str(Path(baseline["height"]).resolve()),
            "candidate_height": str(Path(candidate_bundle["height"]).resolve()),
            "reference_demo_scorecard": str(
                reference_scorecard_path.resolve()),
        },
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "review_contract": [
            "Metrics reveal regressions; they do not prove aesthetic quality.",
            "Candidate geometry is experimental and does not affect formal 3MF.",
            "Relative building tiers are shown without terrain-owned Z capping.",
            "A print verdict requires real 3MF, design_spec, and strict 0/0 validation.",
        ],
    }
    evidence_path = args.output_dir / f"{args.tag}_building_mass_evidence.json"
    evidence_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    print(json.dumps({
        "contact_sheet": str(sheet_path),
        "evidence": str(evidence_path),
        "candidate": candidate.evidence["output_components"],
        "height_emphasis": {
            "status": height_emphasis.get("status"),
            "zones": height_emphasis.get("selected_zone_count", 0),
            "components": height_emphasis.get("promoted_component_count", 0),
        },
        "raster_comparison": evidence["raster_comparison"],
        "elapsed_seconds": evidence["elapsed_seconds"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
