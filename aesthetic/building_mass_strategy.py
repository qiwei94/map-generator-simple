"""Deterministic, printer-scaled building-mass strategy.

The strategy turns raw anonymous footprints into bounded candidates inside
existing road/water-separated blocks.  Candidate construction remains usable
for isolated A/B review; :func:`apply_building_mass_to_layers` is the bounded
production adapter for an explicitly active, versioned scene policy.

No city-name lookup, generative geometry, global Z position, or Boolean mesh
instruction is accepted here.  XY distances derive from the selected printer
profile and the map scale; relief is expressed as printable layer counts.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import math
from typing import Iterable, Mapping, Sequence

import numpy as np
from shapely.errors import GEOSException
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon
from shapely.ops import unary_union
from shapely.strtree import STRtree

from _TEXTURE_STYLE_OF_DEEPSEEK.print_profile import PrinterProfile


POLICY_VERSION = "building-mass-candidate-v9"
ACTIVATION_VERSION = "building-mass-activation-v9"
REPRESENTATION_ROUTER_VERSION = "building-representation-router-v1"


@dataclass(frozen=True)
class BuildingMassPolicy:
    """Versioned semantic controls for an A/B candidate.

    Quantiles choose relative urban intensity inside one crop.  Multipliers
    are expressed in real-world nozzle footprints, so changing crop size or
    printer profile changes the geometry floor without a city-specific table.
    """

    policy_version: str = POLICY_VERSION
    # Rejected by the Chicago 25 km fixed-cache gate. Diagnostic opt-in only;
    # production callers retain the established aggregation path by default.
    experimental_complete_source_filter: bool = False
    quiet_score_quantile: float = 0.20
    urban_mass_score_quantile: float = 0.65
    quiet_merge_radius_nozzles: float = 0.30
    urban_merge_radius_nozzles: float = 0.50
    # Morphological closing is scale-aware in model millimetres.  It connects
    # nearby source-supported footprints inside an eligible block, then
    # shrinks by the same radius minus the small growth slack; it does not
    # stretch every building to the target width.
    # The initial 0.35/0.50 fractions left the fixed West Lake 25 km sample
    # at 1.232 mm p50.  The small increase below follows the provisional
    # 1.30 mm aggregation prior while staying far below a per-footprint
    # minimum-width expansion.  The printer profile, not 1.30 mm, owns the
    # hard survival floor.
    quiet_closing_radius_target_fraction: float = 0.40
    urban_closing_radius_target_fraction: float = 0.55
    urban_block_fill_min_density: float = 0.12
    # Cross-source scene evidence can distinguish a genuinely urban block
    # whose OSM footprints are incomplete from a naturally sparse block.  It
    # may use a lower fill gate, but never bypasses the building-count guard.
    urban_block_fill_supported_min_density: float = 0.08
    urban_block_fill_min_count: int = 6
    # A dense, evidenced block receives stronger source-seed closing rather
    # than being replaced by its complete road-bounded interior.  This keeps
    # mid-frequency fullness while letting the final mass resolve into calm,
    # compact components instead of one deeply concave block silhouette.
    urban_infill_closing_multiplier: float = 1.35
    urban_infill_growth_slack_nozzles: float = 0.30
    # Complete building datasets still need aggregation at print scale, but
    # their geometry should be trusted.  Compact-union clusters therefore use
    # only mild closing and may accept a hull only when it adds at most 12%
    # area over its source group.  The incomplete-data path retains the
    # stronger, separately evidenced source-seeded abstraction above.
    complete_cluster_closing_multiplier: float = 1.0
    complete_cluster_growth_slack_nozzles: float = 0.05
    complete_cluster_max_hull_area_growth_fraction: float = 0.12
    # The 12% hull cap remains the authority for shaping one source group.
    # A second-stage merge may join two already compact, nearby groups inside
    # the same road/water block with a slightly larger cumulative budget. This
    # is the only route by which a complete source becomes coarser at 25 km;
    # it never widens individual footprints or crosses the topology clip.
    complete_cluster_merge_max_area_growth_fraction: float = 0.35
    urban_cluster_max_axis_target_multiple: float = 1.45
    urban_cluster_min_local_density: float = 0.10
    urban_cluster_max_split_depth: int = 6
    # A topology block can still contain tens of thousands of source
    # footprints (large campuses, rail yards, or an imperfect outer face).
    # Never construct or morph one monolithic GEOS union from such a group.
    # Deterministic spatial bisection first creates bounded local work units;
    # this is both the performance guard and the desired fine-grained visual
    # language for dense cities.
    urban_cluster_max_source_members: int = 32
    urban_cluster_merge_distance_target_fraction: float = 1.00
    urban_cluster_merge_neighbor_candidates: int = 12
    source_clearance_exact_union_limit: int = 256
    urban_cluster_merge_max_area_growth_fraction: float = 0.75
    # Standard-relief urban mass should carry the mid-frequency composition.
    # A source-supported component that remains narrower after all safe merge
    # attempts is retained as low-relief quiet texture, not deleted or forced
    # across a road. 0.60 * the soft 1.30 mm prior is 0.78 mm on the reference
    # model, above the 0.63 mm printable-strip floor but below a hard clamp.
    urban_mass_min_short_axis_target_fraction: float = 0.60
    # Disabled by default after the West Lake fixed-cache A/B: 0.35 nozzle
    # only moved p50 width 0.81 -> 0.83 mm while inferred area rose 67.4% ->
    # 71.3%.  Keep the bounded control explicit for future profiles, but do
    # not use white-area inflation to disguise an undersized clustering issue.
    urban_cluster_hull_growth_nozzles: float = 0.0
    growth_slack_nozzles: float = 0.12
    # One side of the default two-extrusion final road seam.  The build entry
    # point raises this further when a custom profile requires a wider gap.
    block_inset_nozzles: float = 1.05
    simplify_nozzles: float = 0.08
    boundary_clearance_safety_nozzles: float = 0.02
    maximum_independent_aspect: float = 4.0
    # The 10-city reference audit found that the median compact white-relief
    # component is effectively convex (city medians 0.9904--1.000 solidity),
    # while its useful outline normally has only 6--11 turns.  These are soft
    # silhouette targets for anonymous aggregated mass, never permission to
    # cross a road/water block or alter a trusted landmark footprint.
    silhouette_target_solidity: float = 0.97
    silhouette_max_perimeter_excess: float = 1.05
    silhouette_rounding_nozzles: float = 0.22
    silhouette_outline_simplify_nozzles: float = 0.10
    silhouette_max_area_growth_fraction: float = 0.12
    silhouette_max_area_loss_fraction: float = 0.06
    # Early reference city-demo audits suggested a 1.3--2.3 mm aggregation
    # band for a 25 km composition.  The later ten-city silhouette audit found
    # meaningful city-to-city width variation, so this remains a soft prior,
    # not a hard acceptance gate or per-footprint clamp.  Quiet texture and
    # hero identities may legitimately fall outside it.
    component_short_axis_p50_target_min_mm: float = 1.30
    component_short_axis_p50_target_max_mm: float = 2.30
    component_width_measurement_tolerance_mm: float = 0.01
    component_width_reference_crop_km: float = 25.0
    component_width_reference_model_span_mm: float = 196.0
    quiet_relief_layers: int = 2
    # The inspected reference family clusters its standard low-rise mass near
    # 0.8 mm. Seven 0.12 mm layers produce 0.84 mm while remaining an integer
    # multiple of the selected printer profile; no absolute global Z is set.
    urban_mass_relief_layers: int = 7

    def __post_init__(self) -> None:
        if not 0 <= self.quiet_score_quantile < self.urban_mass_score_quantile <= 1:
            raise ValueError("building-mass score quantiles are invalid")
        for name in (
            "quiet_merge_radius_nozzles", "urban_merge_radius_nozzles",
            "quiet_closing_radius_target_fraction",
            "urban_closing_radius_target_fraction",
            "urban_block_fill_min_density",
            "urban_block_fill_supported_min_density",
            "urban_infill_closing_multiplier", "growth_slack_nozzles",
            "urban_infill_growth_slack_nozzles", "block_inset_nozzles",
            "complete_cluster_closing_multiplier",
            "complete_cluster_growth_slack_nozzles",
            "complete_cluster_max_hull_area_growth_fraction",
            "complete_cluster_merge_max_area_growth_fraction",
            "urban_cluster_max_axis_target_multiple",
            "urban_cluster_min_local_density",
            "urban_cluster_merge_distance_target_fraction",
            "urban_cluster_merge_max_area_growth_fraction",
            "urban_mass_min_short_axis_target_fraction",
            "urban_cluster_hull_growth_nozzles",
            "simplify_nozzles", "boundary_clearance_safety_nozzles",
            "maximum_independent_aspect", "silhouette_rounding_nozzles",
            "silhouette_outline_simplify_nozzles",
            "silhouette_max_area_growth_fraction",
            "silhouette_max_area_loss_fraction",
            "silhouette_target_solidity",
            "silhouette_max_perimeter_excess",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.urban_merge_radius_nozzles < self.quiet_merge_radius_nozzles:
            raise ValueError("urban merge radius must be at least quiet radius")
        if (self.urban_closing_radius_target_fraction
                < self.quiet_closing_radius_target_fraction):
            raise ValueError(
                "urban closing target fraction must be at least quiet fraction")
        if self.urban_block_fill_min_density > 1:
            raise ValueError("urban block fill density must not exceed one")
        if self.urban_block_fill_supported_min_density > 1:
            raise ValueError(
                "supported urban block fill density must not exceed one")
        if (self.urban_block_fill_supported_min_density
                > self.urban_block_fill_min_density):
            raise ValueError(
                "supported urban fill density must not exceed the default")
        if not 0 < self.silhouette_target_solidity <= 1:
            raise ValueError("silhouette target solidity must be in (0, 1]")
        if self.silhouette_max_perimeter_excess < 1:
            raise ValueError(
                "silhouette perimeter excess must be at least one")
        if self.silhouette_max_area_growth_fraction > 1:
            raise ValueError(
                "silhouette area-growth fraction must not exceed one")
        if self.silhouette_max_area_loss_fraction >= 1:
            raise ValueError(
                "silhouette area-loss fraction must be less than one")
        for name in (
            "component_short_axis_p50_target_min_mm",
            "component_short_axis_p50_target_max_mm",
            "component_width_reference_crop_km",
            "component_width_reference_model_span_mm",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        tolerance = float(self.component_width_measurement_tolerance_mm)
        if not math.isfinite(tolerance) or tolerance < 0:
            raise ValueError(
                "component width measurement tolerance must be finite and "
                "non-negative")
        if (self.component_short_axis_p50_target_max_mm
                < self.component_short_axis_p50_target_min_mm):
            raise ValueError(
                "component short-axis target maximum must be at least minimum")
        if (isinstance(self.urban_block_fill_min_count, bool)
                or self.urban_block_fill_min_count < 2
                or int(self.urban_block_fill_min_count)
                != self.urban_block_fill_min_count):
            raise ValueError("urban block fill count must be an integer >= 2")
        if (isinstance(self.urban_cluster_max_split_depth, bool)
                or self.urban_cluster_max_split_depth < 1
                or int(self.urban_cluster_max_split_depth)
                != self.urban_cluster_max_split_depth):
            raise ValueError(
                "urban cluster max split depth must be a positive integer")
        if (isinstance(self.urban_cluster_max_source_members, bool)
                or self.urban_cluster_max_source_members < 2
                or int(self.urban_cluster_max_source_members)
                != self.urban_cluster_max_source_members):
            raise ValueError(
                "urban cluster max source members must be an integer >= 2")
        if self.urban_cluster_min_local_density > 1:
            raise ValueError(
                "urban cluster minimum local density must not exceed one")
        if self.urban_cluster_merge_max_area_growth_fraction > 1:
            raise ValueError(
                "urban cluster merge area growth must not exceed one")
        if self.complete_cluster_max_hull_area_growth_fraction > 1:
            raise ValueError(
                "complete cluster hull area growth must not exceed one")
        if self.complete_cluster_merge_max_area_growth_fraction > 1:
            raise ValueError(
                "complete cluster merge area growth must not exceed one")
        if self.urban_mass_min_short_axis_target_fraction > 1:
            raise ValueError(
                "urban mass short-axis target fraction must not exceed one")
        if self.quiet_relief_layers < 1:
            raise ValueError("quiet_relief_layers must be positive")
        if self.urban_mass_relief_layers <= self.quiet_relief_layers:
            raise ValueError("urban mass must be higher than quiet relief")


def resolve_component_width_target(
    policy: BuildingMassPolicy,
    *,
    printer_profile: PrinterProfile,
    scale_mm_per_m: float,
    model_span_mm: float | None = None,
) -> dict:
    """Resolve visual size and scale-aware aggregation as separate controls.

    For the same physical model, the visible component band stays in model
    millimetres.  A larger geographic crop instead increases the real-world
    distance represented by every printable millimetre: more close buildings
    merge and fewer sub-print road intervals remain eligible as independent
    cuts.  A larger physical model scales the visible band proportionally.
    """

    if not math.isfinite(scale_mm_per_m) or scale_mm_per_m <= 0:
        raise ValueError("scale_mm_per_m must be finite and positive")
    current_model_span = float(
        model_span_mm
        if model_span_mm is not None
        else policy.component_width_reference_model_span_mm)
    if not math.isfinite(current_model_span) or current_model_span <= 0:
        raise ValueError("model_span_mm must be finite and positive")
    current_crop_km = current_model_span / float(scale_mm_per_m) / 1000.0
    reference_scale = (
        float(policy.component_width_reference_model_span_mm)
        / (float(policy.component_width_reference_crop_km) * 1000.0))
    model_span_factor = (
        current_model_span
        / float(policy.component_width_reference_model_span_mm))
    raw_min = (
        float(policy.component_short_axis_p50_target_min_mm)
        * model_span_factor)
    raw_max = (
        float(policy.component_short_axis_p50_target_max_mm)
        * model_span_factor)
    print_floor = max(
        float(printer_profile.extrusion_width_mm),
        float(printer_profile.min_colored_strip_mm),
    )
    resolved_min = max(raw_min, print_floor)
    resolved_max = max(raw_max, resolved_min)
    real_m_per_model_mm = 1.0 / float(scale_mm_per_m)
    aggregation_real_scale_factor = reference_scale / float(scale_mm_per_m)
    quiet_merge_model_mm = (
        float(printer_profile.nozzle_diameter_mm)
        * float(policy.quiet_merge_radius_nozzles))
    urban_merge_model_mm = (
        float(printer_profile.nozzle_diameter_mm)
        * float(policy.urban_merge_radius_nozzles))
    road_seam_model_mm = float(printer_profile.final_block_base_gap_mm)
    return {
        "metric": "p50_minimum_rotated_xy_axis",
        "reference_crop_km": round(
            float(policy.component_width_reference_crop_km), 3),
        "reference_model_span_mm": round(
            float(policy.component_width_reference_model_span_mm), 3),
        "reference_target_min_model_mm": round(
            float(policy.component_short_axis_p50_target_min_mm), 3),
        "reference_target_max_model_mm": round(
            float(policy.component_short_axis_p50_target_max_mm), 3),
        "current_crop_km": round(float(current_crop_km), 5),
        "current_model_span_mm": round(float(current_model_span), 5),
        "current_scale_mm_per_m": round(float(scale_mm_per_m), 9),
        "reference_scale_mm_per_m": round(float(reference_scale), 9),
        "model_span_factor": round(float(model_span_factor), 6),
        "real_m_per_model_mm": round(float(real_m_per_model_mm), 6),
        "aggregation_real_scale_factor": round(
            float(aggregation_real_scale_factor), 6),
        "raw_model_target_min_mm": round(float(raw_min), 5),
        "raw_model_target_max_mm": round(float(raw_max), 5),
        "print_floor_model_mm": round(float(print_floor), 5),
        "target_min_model_mm": round(float(resolved_min), 5),
        "target_max_model_mm": round(float(resolved_max), 5),
        "target_min_real_m": round(
            float(resolved_min * real_m_per_model_mm), 5),
        "target_max_real_m": round(
            float(resolved_max * real_m_per_model_mm), 5),
        "scale_aware_aggregation": {
            "quiet_merge_radius_model_mm": round(
                quiet_merge_model_mm, 5),
            "quiet_merge_radius_real_m": round(
                quiet_merge_model_mm * real_m_per_model_mm, 5),
            "urban_merge_radius_model_mm": round(
                urban_merge_model_mm, 5),
            "urban_merge_radius_real_m": round(
                urban_merge_model_mm * real_m_per_model_mm, 5),
            "minimum_road_seam_model_mm": round(
                road_seam_model_mm, 5),
            "minimum_road_seam_real_m": round(
                road_seam_model_mm * real_m_per_model_mm, 5),
            "interpretation": (
                "larger crops keep the model-mm visual band but increase "
                "real merge and road-seam distances, producing coarser "
                "topology and fewer independent components"),
        },
        "print_floor_limited": bool(
            resolved_min > raw_min + 1e-9
            or resolved_max > raw_max + 1e-9),
        "enforcement": "soft_aggregation_prior",
        "hard_acceptance_gate": False,
    }


@dataclass
class BuildingMassCandidate:
    quiet_texture: list[Polygon]
    urban_mass: list[Polygon]
    sparse_printable: list[Polygon]
    evidence: dict

    @property
    def all_polygons(self) -> list[Polygon]:
        return self.quiet_texture + self.urban_mass + self.sparse_printable


def compose_building_mass_with_baseline(
    baseline: Sequence[Polygon],
    candidate: BuildingMassCandidate,
    *,
    nozzle_real_m: float,
    minimum_area_gain: float = 0.01,
) -> tuple[list[Polygon], list[Polygon], dict]:
    """Compose a candidate with the accepted low-frequency city carrier.

    The experimental A/B renderer and the formal adapter must evaluate the
    same object.  Earlier diagnostics replaced the baseline with candidate
    components, while the formal adapter retained it; that made sparse-data
    scenes look artificially empty and encouraged the wrong tuning response.

    Returns ``(quiet, mass, evidence)``.  A guarded fallback returns the
    original baseline as quiet geometry when the composition does not retain
    the configured minimum area.  This helper only combines already bounded
    polygons and does not decide global Z, mesh booleans, materials, or source
    geometry.
    """

    if not math.isfinite(nozzle_real_m) or nozzle_real_m <= 0:
        raise ValueError("nozzle_real_m must be finite and positive")
    if (not math.isfinite(minimum_area_gain)
            or minimum_area_gain < 0):
        raise ValueError("minimum_area_gain must be finite and non-negative")

    baseline = [
        polygon for item in baseline for polygon in _parts(item)
        if not polygon.is_empty
    ]
    baseline_union = _safe_union(baseline)
    baseline_area = float(baseline_union.area) if baseline else 0.0
    representation_mode = (
        candidate.evidence.get("global_representation", {}).get("mode")
        if isinstance(candidate.evidence, Mapping) else None)
    if baseline_area > 0 and representation_mode == "sparse_local_preserve":
        return list(baseline), [], {
            "status": "policy_preserved_baseline",
            "reason": (
                "sparse/low-confidence scene prohibits inferred urban mass "
                "from replacing the accepted carrier"),
            "representation_mode": representation_mode,
            "baseline_area_m2": round(baseline_area, 3),
            "candidate_area_m2": None,
        }
    mass = list(candidate.urban_mass)
    mass_union = _safe_union(mass)
    quiet_sources = (
        baseline
        + list(candidate.quiet_texture)
        + list(candidate.sparse_printable)
    )
    quiet = []
    for polygon in quiet_sources:
        for part in _parts(_safe_difference(polygon, mass_union)):
            if _has_printable_core(part, nozzle_real_m):
                quiet.append(part)

    combined = quiet + mass
    combined_union = _safe_union(combined)
    combined_area = float(combined_union.area) if combined else 0.0
    minimum_area = baseline_area * (1.0 + minimum_area_gain)
    if baseline_area > 0 and combined_area + 1e-6 < minimum_area:
        return list(baseline), [], {
            "status": "guarded_fallback",
            "reason": "candidate did not preserve baseline urban mass area",
            "baseline_area_m2": round(baseline_area, 3),
            "candidate_area_m2": round(combined_area, 3),
            "minimum_area_m2": round(minimum_area, 3),
        }
    return quiet, mass, {
        "status": "composed",
        "baseline_area_m2": round(baseline_area, 3),
        "candidate_area_m2": round(combined_area, 3),
        "area_gain_ratio": round(
            combined_area / max(baseline_area, 1.0), 5),
    }


def apply_building_mass_to_layers(
    layers,
    buildings,
    roads,
    water,
    bbox_local,
    *,
    printer_profile: PrinterProfile,
    scene_policy: Mapping,
    topology_tier: int = 2,
    topology_blocks: Sequence[Polygon] | None = None,
    topology_evidence: Mapping | None = None,
    model_span_mm: float | None = None,
    preparation_cache: dict | None = None,
    downstream_final_clearance: bool = False,
) -> dict:
    """Activate a two-tier printable mass without inventing source geometry.

    Existing low-frequency BO polygons remain as quiet relief.  Dense,
    source-supported neighbourhoods are regularized inside road/water-bounded
    topology blocks and raised to the standard mass tier.  The higher tier is
    subtracted from the quiet tier so the formal 3MF contains no overlapping
    building bodies.

    This adapter deliberately does not touch terrain, water, roads, global Z,
    Boolean meshes or material assignment.  It only replaces the BO polygon
    list and records layer-quantized relative relief heights.
    """

    policy_version = str(scene_policy.get("policy_version") or "")
    garden = scene_policy.get("garden_city_strategy", {}) or {}
    landscape = scene_policy.get("landscape_strategy", {}) or {}
    building_roles = (
        (scene_policy.get("roles", {}) or {}).get("buildings", {}) or {})
    local_strategy = building_roles.get("local_strategy", {}) or {}
    local_cells = local_strategy.get("cells", ()) or ()
    local_mass_requested = bool(
        local_strategy.get("status") == "ready"
        and any(
            cell.get("strategy") in {"neighborhood_mass", "hybrid_mass"}
            for cell in local_cells
            if isinstance(cell, Mapping)
        )
    )
    if scene_policy.get("activation") != "active":
        return {
            "activation_version": ACTIVATION_VERSION,
            "status": "inactive",
            "reason": "scene policy is audit-only",
        }
    if not policy_version.startswith("scene-policy-v"):
        return {
            "activation_version": ACTIVATION_VERSION,
            "status": "inactive",
            "reason": "scene policy is missing a supported version",
        }
    if landscape.get("enabled") or scene_policy.get("scene_class") == "landscape":
        return {
            "activation_version": ACTIVATION_VERSION,
            "status": "inactive",
            "reason": "landscape policy suppresses urban building mass",
        }
    if garden.get("enabled"):
        activation_basis = "garden_city"
    elif local_mass_requested:
        activation_basis = "local_building_quality"
    else:
        return {
            "activation_version": ACTIVATION_VERSION,
            "status": "inactive",
            "reason": (
                "scene has no measured local neighborhood-mass support"),
        }
    if topology_tier not in (1, 2, 3):
        raise ValueError("building-mass topology_tier must be 1, 2 or 3")
    if buildings is None or len(buildings) == 0:
        return {
            "activation_version": ACTIVATION_VERSION,
            "status": "inactive",
            "reason": "building source is empty",
        }
    if roads is None or len(roads) == 0:
        return {
            "activation_version": ACTIVATION_VERSION,
            "status": "inactive",
            "reason": "road source is empty",
        }

    # Local import avoids making the review-only candidate API depend on the
    # legacy preprocessing module at import time.
    from _TEXTURE_STYLE_OF_DEEPSEEK.buildings import _build_city_blocks

    baseline = list(getattr(layers, "BO", ()) or ())
    exclusion_polys = [polygon for polygon, _height in layers.BL]
    digest = hashlib.blake2b(digest_size=12)
    for polygon in exclusion_polys:
        digest.update(polygon.wkb)
    preparation_key = (
        policy_version,
        int(topology_tier),
        "preprocessed" if topology_blocks is not None else "rebuilt",
        len(topology_blocks) if topology_blocks is not None else None,
        round(float(model_span_mm), 5) if model_span_mm is not None else None,
        round(float(layers.nozzle_real_m), 6),
        getattr(printer_profile, "profile_id", "unknown"),
        bool(downstream_final_clearance),
        digest.hexdigest(),
    )
    prepared = (
        preparation_cache.get(preparation_key)
        if preparation_cache is not None else None)
    preparation_cache_hit = prepared is not None
    if prepared is None:
        candidate_blocks = (
            list(topology_blocks)
            if topology_blocks is not None
            else _build_city_blocks(
                roads, water, road_tier=topology_tier,
                bbox_local=bbox_local)
        )
        resolved_policy, resolution = resolve_building_mass_policy(
            buildings,
            nozzle_real_m=float(layers.nozzle_real_m),
            source_scene_policy=scene_policy,
        )
        candidate = build_building_mass_candidate(
            buildings,
            candidate_blocks,
            nozzle_real_m=float(layers.nozzle_real_m),
            printer_profile=printer_profile,
            exclusion_polys=exclusion_polys,
            policy=resolved_policy,
            source_scene_policy=scene_policy,
            model_span_mm=model_span_mm,
            downstream_final_clearance=downstream_final_clearance,
        )
        prepared = (candidate, len(candidate_blocks), resolution)
        if preparation_cache is not None:
            preparation_cache[preparation_key] = prepared
    candidate, topology_block_count, resolution = prepared

    quiet, mass, composition = compose_building_mass_with_baseline(
        baseline, candidate, nozzle_real_m=float(layers.nozzle_real_m))
    if composition["status"] != "composed":
        return {
            "activation_version": ACTIVATION_VERSION,
            "status": "guarded_fallback",
            "reason": composition["reason"],
            "topology_tier": topology_tier,
            "baseline_area_m2": composition["baseline_area_m2"],
            "candidate_area_m2": composition["candidate_area_m2"],
            "topology_source": (
                "preprocessed_scale_aware"
                if topology_blocks is not None else "fixed_tier_rebuild"),
            "topology_evidence": dict(topology_evidence or {}),
            "candidate": candidate.evidence,
            "policy_resolution": resolution,
        }

    combined = quiet + mass

    heights = candidate.evidence["semantic_heights"]
    layers.BO = combined
    layers.BO_heights = (
        [float(heights["quiet_texture_mm"])] * len(quiet)
        + [float(heights["urban_mass_mm"])] * len(mass)
    )
    candidate.evidence["formal_mesh_affected"] = True
    if isinstance(candidate.evidence.get("hard_boundaries"), dict):
        candidate.evidence["hard_boundaries"]["formal_mesh_affected"] = True
    candidate.evidence["activation"] = (
        "active_garden_city"
        if activation_basis == "garden_city"
        else "active_local_building_quality"
    )
    return {
        "activation_version": ACTIVATION_VERSION,
        "status": "active",
        "activation_basis": activation_basis,
        "source_scene_policy_version": policy_version,
        "topology_tier": topology_tier,
        "topology_blocks": topology_block_count,
        "topology_source": (
            "preprocessed_scale_aware"
            if topology_blocks is not None else "fixed_tier_rebuild"),
        "topology_evidence": dict(topology_evidence or {}),
        "preparation_cache_hit": preparation_cache_hit,
        "baseline_components": len(baseline),
        "quiet_components": len(quiet),
        "urban_mass_components": len(mass),
        "output_components": len(combined),
        "baseline_area_m2": composition["baseline_area_m2"],
        "output_area_m2": composition["candidate_area_m2"],
        "area_gain_ratio": composition["area_gain_ratio"],
        "semantic_heights": heights,
        "candidate": candidate.evidence,
        "policy_resolution": resolution,
        "geometry_authority": "OSM buildings inside OSM road/water blocks",
        "forbidden_controls_untouched": [
            "terrain mesh", "global Z", "mesh booleans",
            "replacement road geometry", "replacement water geometry",
        ],
    }


def measure_building_print_pressure(
    buildings,
    *,
    nozzle_real_m: float,
    maximum_samples: int = 12_000,
) -> dict:
    """Measure how strongly literal footprints conflict with printer scale."""

    if not math.isfinite(nozzle_real_m) or nozzle_real_m <= 0:
        raise ValueError("nozzle_real_m must be finite and positive")
    if (isinstance(maximum_samples, bool) or maximum_samples < 1
            or int(maximum_samples) != maximum_samples):
        raise ValueError("maximum_samples must be a positive integer")
    polygons = _flatten_buildings(buildings)
    if not polygons:
        return {
            "sampled_footprints": 0,
            "sub_nozzle_fraction": 0.0,
            "sub_nozzle_area_fraction": 0.0,
            "regularization_pressure": 0.0,
        }
    if len(polygons) > maximum_samples:
        indexes = np.linspace(
            0, len(polygons) - 1, maximum_samples, dtype=int)
        sampled = [polygons[int(index)] for index in indexes]
    else:
        sampled = polygons
    widths = np.asarray([_rotated_axes(polygon)[0] for polygon in sampled])
    areas = np.asarray([float(polygon.area) for polygon in sampled])
    below = widths < nozzle_real_m
    count_fraction = float(np.mean(below))
    area_fraction = float(
        areas[below].sum() / max(float(areas.sum()), 1.0))
    pressure = 0.55 * count_fraction + 0.45 * area_fraction
    return {
        "sampled_footprints": len(sampled),
        "sub_nozzle_fraction": round(count_fraction, 5),
        "sub_nozzle_area_fraction": round(area_fraction, 5),
        "regularization_pressure": round(float(pressure), 5),
    }


def resolve_building_mass_policy(
    buildings,
    *,
    nozzle_real_m: float,
    base_policy: BuildingMassPolicy | None = None,
    source_scene_policy: Mapping | None = None,
) -> tuple[BuildingMassPolicy, dict]:
    """Resolve printer-pressure adaptations without city-name lookup.

    High pressure broadens the coherent urban-mass tier and merge radius.
    Low pressure keeps the conservative base policy so printable footprints
    are not needlessly replaced.  A scene-policy measurement is preferred;
    otherwise the same pressure is measured from a deterministic sample.
    """

    base = base_policy or BuildingMassPolicy()
    source = "measured_footprints"
    measurements = None
    pressure = None
    if source_scene_policy:
        building_role = (
            source_scene_policy.get("roles", {}).get("buildings", {}) or {})
        raw_pressure = building_role.get("regularization_pressure")
        if raw_pressure is not None:
            try:
                pressure = float(raw_pressure)
                source = "scene_policy"
            except (TypeError, ValueError):
                pressure = None
    if pressure is None:
        measurements = measure_building_print_pressure(
            buildings, nozzle_real_m=nozzle_real_m)
        pressure = float(measurements["regularization_pressure"])
    pressure = float(np.clip(pressure, 0.0, 1.0))
    intensity = float(np.clip((pressure - 0.30) / 0.50, 0.0, 1.0))
    grammar_strategy = {}
    if source_scene_policy:
        building_role = (
            source_scene_policy.get("roles", {}).get("buildings", {}) or {})
        candidate_strategy = building_role.get(
            "block_grammar_strategy", {}) or {}
        if (isinstance(candidate_strategy, Mapping)
                and candidate_strategy.get("status") == "ready"):
            grammar_strategy = dict(candidate_strategy)

    def bounded_signal(name: str) -> float:
        try:
            value = float(grammar_strategy.get(name) or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return float(np.clip(value, 0.0, 1.0)) if math.isfinite(value) else 0.0

    aggregation = bounded_signal("aggregation_pressure")
    outline = bounded_signal("outline_regularization_pressure")
    orientation = bounded_signal("orientation_preservation_pressure")
    texture = bounded_signal("texture_promotion_pressure")
    selection = bounded_signal("selection_pressure")
    target_band = grammar_strategy.get(
        "soft_target_short_axis_band_mm", ()) or ()
    try:
        target_min = float(target_band[0])
        target_max = float(target_band[1])
        if not (math.isfinite(target_min) and math.isfinite(target_max)
                and 0 < target_min <= target_max):
            raise ValueError
    except (IndexError, TypeError, ValueError):
        target_min = base.component_short_axis_p50_target_min_mm
        target_max = base.component_short_axis_p50_target_max_mm

    urban_quantile = float(np.clip(
        base.urban_mass_score_quantile
        - 0.35 * intensity
        - 0.10 * texture
        + 0.08 * selection,
        base.quiet_score_quantile + 0.10,
        0.88,
    ))
    resolved = replace(
        base,
        urban_mass_score_quantile=urban_quantile,
        quiet_merge_radius_nozzles=(
            base.quiet_merge_radius_nozzles
            + 0.25 * intensity + 0.12 * aggregation + 0.08 * texture),
        urban_merge_radius_nozzles=(
            base.urban_merge_radius_nozzles
            + 0.30 * intensity + 0.20 * aggregation + 0.10 * texture),
        quiet_closing_radius_target_fraction=(
            base.quiet_closing_radius_target_fraction
            + 0.12 * aggregation + 0.08 * texture),
        urban_closing_radius_target_fraction=(
            base.urban_closing_radius_target_fraction
            + 0.18 * aggregation + 0.10 * texture),
        urban_block_fill_supported_min_density=max(
            0.05,
            base.urban_block_fill_supported_min_density - 0.02 * texture),
        urban_cluster_merge_distance_target_fraction=(
            base.urban_cluster_merge_distance_target_fraction
            + 0.25 * aggregation),
        complete_cluster_merge_max_area_growth_fraction=min(
            0.75,
            base.complete_cluster_merge_max_area_growth_fraction
            + 0.35 * aggregation),
        silhouette_target_solidity=min(
            0.995,
            base.silhouette_target_solidity + 0.02 * outline),
        silhouette_max_perimeter_excess=max(
            1.015,
            base.silhouette_max_perimeter_excess - 0.02 * outline),
        silhouette_rounding_nozzles=max(
            0.10,
            base.silhouette_rounding_nozzles
            * (1.0 - 0.35 * orientation)),
        silhouette_outline_simplify_nozzles=max(
            0.05,
            base.silhouette_outline_simplify_nozzles
            * (1.0 - 0.25 * orientation)),
        component_short_axis_p50_target_min_mm=target_min,
        component_short_axis_p50_target_max_mm=target_max,
        # Printer pressure may merge source-supported clusters, but must not
        # weaken the evidence gate for inferring an entire occupied block.
        # Otherwise sparse garden-city footprints turn into broad white slabs.
        urban_block_fill_min_density=base.urban_block_fill_min_density,
        urban_block_fill_min_count=base.urban_block_fill_min_count,
    )
    return resolved, {
        "resolver_version": "building-mass-pressure-resolver-v2",
        "source": source,
        "regularization_pressure": round(pressure, 5),
        "adaptation_intensity": round(intensity, 5),
        "measurements": measurements,
        "block_grammar_strategy": grammar_strategy,
        "block_grammar_adaptation": {
            "aggregation_pressure": round(aggregation, 5),
            "outline_regularization_pressure": round(outline, 5),
            "orientation_preservation_pressure": round(orientation, 5),
            "texture_promotion_pressure": round(texture, 5),
            "selection_pressure": round(selection, 5),
            "soft_target_short_axis_band_mm": [
                round(target_min, 5), round(target_max, 5)],
        },
        "base_policy": asdict(base),
        "resolved_policy": asdict(resolved),
        "city_name_lookup": False,
    }


def resolve_building_representation_mode(
    source_scene_policy: Mapping | None,
) -> dict:
    """Choose a global carrier without city-name or hand-authored presets.

    Local cells still decide *where* mass, support, or open space belongs.
    This resolver decides how much geometry may be inferred inside those
    cells.  Complete urban data receives source-supported union/closing;
    heterogeneous or incomplete but urban-supported data may use bounded
    cluster infill; a genuinely sparse/low-confidence frame preserves local
    source settlements without broad inferred mass.
    """

    thresholds = {
        "mixed_complete_min_completeness": 0.76,
        "mixed_complete_min_continuity": 0.70,
        "mixed_complete_max_gap_fraction": 0.05,
        "sparse_max_footprint_frame_coverage": 0.035,
        "sparse_max_completeness": 0.68,
        "sparse_max_continuity": 0.58,
    }
    building_role = {}
    if isinstance(source_scene_policy, Mapping):
        roles = source_scene_policy.get("roles", {}) or {}
        if isinstance(roles, Mapping):
            candidate = roles.get("buildings", {}) or {}
            if isinstance(candidate, Mapping):
                building_role = candidate

    def number(key: str) -> float | None:
        raw = building_role.get(key)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

    profile = str(building_role.get("distribution_profile") or "unknown")
    completeness = number("local_data_completeness_score")
    continuity = number("building_distribution_continuity_score")
    gap_fraction = number("suspected_building_gap_fraction_of_urban")
    footprint_coverage = number("footprint_frame_coverage")
    local = building_role.get("local_strategy", {}) or {}
    local_ready = bool(
        isinstance(local, Mapping) and local.get("status") == "ready")

    complete_profiles = {
        "continuous_urban_fabric",
        "continuous_urban_with_large_open_space",
    }
    mixed_complete = bool(
        profile == "mixed_complete_urban_fabric"
        and completeness is not None
        and completeness >= thresholds["mixed_complete_min_completeness"]
        and continuity is not None
        and continuity >= thresholds["mixed_complete_min_continuity"]
        and gap_fraction is not None
        and gap_fraction <= thresholds["mixed_complete_max_gap_fraction"]
    )
    sparse_low_confidence = bool(
        profile == "fragmented_or_incomplete_urban_evidence"
        and footprint_coverage is not None
        and footprint_coverage < thresholds["sparse_max_footprint_frame_coverage"]
        and completeness is not None
        and completeness < thresholds["sparse_max_completeness"]
        and continuity is not None
        and continuity < thresholds["sparse_max_continuity"]
    )

    if building_role.get("urban_mass") == "suppress":
        mode = "sparse_local_preserve"
        reason = "scene policy suppresses urban mass"
    elif local_ready and (profile in complete_profiles or mixed_complete):
        mode = "complete_source_union"
        reason = (
            "building evidence is sufficiently complete and continuous; "
            "do not infer broad cluster fill")
    elif local_ready and sparse_low_confidence:
        mode = "sparse_local_preserve"
        reason = (
            "low footprint coverage and weak continuity indicate a sparse "
            "or low-confidence frame")
    else:
        # Compatibility is intentionally conservative: older scene reports
        # lack the required measurements, so retain the already-reviewed
        # bounded cluster path rather than silently changing output.
        mode = "supported_cluster_infill"
        reason = (
            "heterogeneous/incomplete urban evidence, or legacy evidence "
            "without enough fields for a safer classification")

    return {
        "router_version": REPRESENTATION_ROUTER_VERSION,
        "mode": mode,
        "reason": reason,
        "local_quality_ready": local_ready,
        "distribution_profile": profile,
        "measurements": {
            "local_data_completeness_score": completeness,
            "building_distribution_continuity_score": continuity,
            "suspected_building_gap_fraction_of_urban": gap_fraction,
            "footprint_frame_coverage": footprint_coverage,
            "positive_building_coverage_cv": number(
                "positive_building_coverage_cv"),
        },
        "thresholds": thresholds,
        "city_name_lookup": False,
        "geometry_authority": "source_vectors_and_existing_topology_only",
    }


def _parts(geometry) -> list[Polygon]:
    if geometry is None or geometry.is_empty:
        return []
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, (MultiPolygon, GeometryCollection)):
        result = []
        for child in geometry.geoms:
            result.extend(_parts(child))
        return result
    return []


def _safe_union(polygons: Sequence[Polygon]):
    try:
        return unary_union(polygons)
    except GEOSException:
        repaired = []
        for polygon in polygons:
            try:
                candidate = polygon if polygon.is_valid else polygon.buffer(0)
            except GEOSException:
                continue
            if candidate is not None and not candidate.is_empty:
                repaired.append(candidate)
        try:
            return unary_union(repaired) if repaired else GeometryCollection()
        except GEOSException:
            return GeometryCollection()


def _safe_intersection(left, right):
    try:
        return left.intersection(right)
    except GEOSException:
        try:
            return left.buffer(0).intersection(right.buffer(0))
        except GEOSException:
            return GeometryCollection()


def _safe_difference(left, right):
    if right is None or right.is_empty:
        return left
    try:
        return left.difference(right)
    except GEOSException:
        try:
            return left.buffer(0).difference(right.buffer(0))
        except GEOSException:
            return GeometryCollection()


def _rotated_axes(polygon: Polygon) -> tuple[float, float]:
    try:
        hull = polygon.convex_hull
        hull_coordinates = np.asarray(hull.exterior.coords[:-1], dtype=float)
        if len(hull_coordinates) < 2:
            return 0.0, 0.0
        # Shapely 2.0's Python fallback for oriented_envelope is quadratic in
        # hull vertices and dominates dense-city runs.  A PCA-aligned envelope
        # is linear, deterministic and conservative enough for this size gate.
        centered = hull_coordinates - hull_coordinates.mean(axis=0)
        covariance = centered.T @ centered
        values, vectors = np.linalg.eigh(covariance)
        axis = vectors[:, int(np.argmax(values))]
        normal = np.asarray((-axis[1], axis[0]), dtype=float)
        along = centered @ axis
        across = centered @ normal
        spans = sorted((
            float(along.max() - along.min()),
            float(across.max() - across.min()),
        ))
        return spans[0], spans[1]
    except (AttributeError, GEOSException, ValueError, np.linalg.LinAlgError):
        return 0.0, 0.0


def _printable_independent(polygon: Polygon, nozzle_real_m: float,
                           maximum_aspect: float) -> bool:
    short, long = _rotated_axes(polygon)
    if short < nozzle_real_m or short <= 0:
        return False
    return long / short <= maximum_aspect


def _has_printable_core(polygon: Polygon, nozzle_real_m: float) -> bool:
    if polygon.is_empty or polygon.area <= 0:
        return False
    try:
        return not polygon.buffer(-nozzle_real_m * 0.5).is_empty
    except GEOSException:
        short, _ = _rotated_axes(polygon)
        return short >= nozzle_real_m


def _silhouette_metrics(polygon: Polygon, *, simplify: float) -> dict:
    """Measure regularity without mistaking size for shape quality."""

    if polygon is None or polygon.is_empty or polygon.area <= 0:
        return {
            "solidity": 0.0,
            "perimeter_excess": float("inf"),
            "outline_vertices": 0,
            "hole_count": 0,
        }
    try:
        hull = polygon.convex_hull
        solidity = float(polygon.area / max(float(hull.area), 1e-9))
        perimeter_excess = float(
            polygon.length / max(float(hull.length), 1e-9))
        outline = (
            polygon.simplify(simplify, preserve_topology=True)
            if simplify > 0 else polygon)
        outline_vertices = sum(
            max(0, len(part.exterior.coords) - 1)
            for part in _parts(outline))
        hole_count = sum(len(part.interiors) for part in _parts(polygon))
    except (AttributeError, GEOSException, ValueError, ZeroDivisionError):
        return {
            "solidity": 0.0,
            "perimeter_excess": float("inf"),
            "outline_vertices": 0,
            "hole_count": 0,
        }
    return {
        "solidity": solidity,
        "perimeter_excess": perimeter_excess,
        "outline_vertices": int(outline_vertices),
        "hole_count": int(hole_count),
    }


def _silhouette_target_met(metrics: Mapping, policy: BuildingMassPolicy) -> bool:
    return bool(
        float(metrics.get("solidity", 0.0)) + 1e-9
        >= float(policy.silhouette_target_solidity)
        and float(metrics.get("perimeter_excess", float("inf")))
        <= float(policy.silhouette_max_perimeter_excess) + 1e-9
    )


def _regularize_silhouette(
    polygon: Polygon,
    *,
    safe_clip,
    nozzle_real_m: float,
    policy: BuildingMassPolicy,
) -> tuple[Polygon, dict]:
    """Regularize one anonymous mass without crossing semantic boundaries.

    A global convex hull is considered only when it already fits inside the
    road/water/exclusion-safe clip and its added area stays within a small
    budget.  Deep concavities made by real topology therefore remain intact;
    shallow source-boundary bites and needless vertices may be removed.
    """

    simplify = nozzle_real_m * policy.silhouette_outline_simplify_nozzles
    rounding = nozzle_real_m * policy.silhouette_rounding_nozzles
    original = _safe_intersection(polygon, safe_clip)
    original_parts = _parts(original)
    if len(original_parts) != 1:
        return polygon, {
            "method": "unchanged_split_clip",
            "target_met_before": False,
            "target_met_after": False,
            "boundary_limited": True,
            "before": _silhouette_metrics(polygon, simplify=simplify),
            "after": _silhouette_metrics(polygon, simplify=simplify),
        }
    original = original_parts[0]
    before = _silhouette_metrics(original, simplify=simplify)
    minimum_area = (
        float(original.area)
        * (1.0 - policy.silhouette_max_area_loss_fraction))
    maximum_area = (
        float(original.area)
        * (1.0 + policy.silhouette_max_area_growth_fraction))

    def accepted(candidate) -> Polygon | None:
        parts = _parts(_safe_intersection(candidate, safe_clip))
        if len(parts) != 1:
            return None
        part = parts[0]
        if not (minimum_area <= float(part.area) <= maximum_area):
            return None
        if not _has_printable_core(part, nozzle_real_m):
            return None
        try:
            if part.difference(safe_clip).area > 1e-6:
                return None
        except GEOSException:
            return None
        return part

    candidates: list[tuple[str, Polygon]] = [("unchanged", original)]
    if simplify > 0:
        try:
            simplified = accepted(original.simplify(
                simplify, preserve_topology=True))
            if simplified is not None:
                candidates.append(("simplified", simplified))
        except GEOSException:
            pass

    # A close/open round trip fills sub-nozzle bites and removes thin spikes.
    # Low quad segmentation avoids replacing one jagged outline with hundreds
    # of tiny arc edges that would merely bloat the 3MF.
    seed = candidates[-1][1]
    if rounding > 0:
        try:
            softened = seed.buffer(
                rounding, join_style=1, quad_segs=2).buffer(
                    -rounding, join_style=1, quad_segs=2)
            inner = softened.buffer(
                -rounding * 0.5, join_style=1, quad_segs=2)
            if not inner.is_empty:
                softened = inner.buffer(
                    rounding * 0.5, join_style=1, quad_segs=2)
            softened = softened.simplify(
                simplify, preserve_topology=True)
            softened = accepted(softened)
            if softened is not None:
                candidates.append(("softened", softened))
        except GEOSException:
            pass

    # Only shallow, bounded concavities may be convexified.  A hull that
    # would enter a road, water body or protected exclusion cannot pass
    # ``accepted`` because it is clipped/split or exceeds the area budget.
    if not any(_silhouette_target_met(
            _silhouette_metrics(item, simplify=simplify), policy)
            for _name, item in candidates):
        try:
            hull = accepted(seed.convex_hull)
            if hull is not None:
                candidates.append(("bounded_convex_hull", hull))
        except GEOSException:
            pass

    evaluated = [
        (name, item, _silhouette_metrics(item, simplify=simplify))
        for name, item in candidates
    ]
    meeting = [entry for entry in evaluated
               if _silhouette_target_met(entry[2], policy)]
    if _silhouette_target_met(before, policy):
        # Do not spend area merely to make an already regular component more
        # convex.  Prefer fewer meaningful turns, then the smaller area delta.
        pool = [entry for entry in meeting
                if entry[0] != "bounded_convex_hull"] or meeting
    elif meeting:
        # For a failing shape, prefer the least geometric change that reaches
        # the measured reference band.
        pool = meeting
    else:
        pool = evaluated
    chosen_name, chosen, after = min(
        pool,
        key=lambda entry: (
            0 if _silhouette_target_met(entry[2], policy) else 1,
            abs(float(entry[1].area) / max(float(original.area), 1e-9) - 1.0),
            entry[2]["outline_vertices"],
            entry[2]["perimeter_excess"],
            -entry[2]["solidity"],
        ),
    )
    target_after = _silhouette_target_met(after, policy)
    return chosen, {
        "method": chosen_name,
        "target_met_before": _silhouette_target_met(before, policy),
        "target_met_after": target_after,
        "boundary_limited": not target_after,
        "area_change_fraction": float(
            chosen.area / max(float(original.area), 1e-9) - 1.0),
        "before": before,
        "after": after,
    }


def _split_source_group(polygons: Sequence[Polygon]) -> tuple[list, list]:
    """Deterministically bisect a source group along its dominant axis."""

    if len(polygons) < 2:
        return list(polygons), []
    centers = np.asarray([
        (float(polygon.centroid.x), float(polygon.centroid.y))
        for polygon in polygons
    ], dtype=float)
    centered = centers - centers.mean(axis=0)
    try:
        covariance = centered.T @ centered
        values, vectors = np.linalg.eigh(covariance)
        axis = vectors[:, int(np.argmax(values))]
    except (ValueError, np.linalg.LinAlgError):
        axis = np.asarray((1.0, 0.0), dtype=float)
    projection = centered @ axis
    # Lexical XY fallbacks make equal projections stable across GEOS runs.
    order = np.lexsort((centers[:, 1], centers[:, 0], projection))
    split = max(1, min(len(order) - 1, len(order) // 2))
    return (
        [polygons[int(index)] for index in order[:split]],
        [polygons[int(index)] for index in order[split:]],
    )


def _merge_small_clusters(
    clusters: Sequence[Polygon],
    *,
    clip,
    target_min_real_m: float,
    maximum_axis_real_m: float,
    policy: BuildingMassPolicy,
    maximum_area_growth_fraction: float | None = None,
) -> tuple[list[Polygon], dict]:
    """Agglomerate nearby regular leaves without recreating a gnawed block."""

    result = list(clusters)
    merge_limit = (
        float(target_min_real_m)
        * float(policy.urban_cluster_merge_distance_target_fraction))
    merges = 0
    rejection_counts = {
        "distance": 0,
        "split_or_outside_clip": 0,
        "area_growth": 0,
        "maximum_axis": 0,
        "regularity": 0,
    }
    initial_undersized = sum(
        _rotated_axes(item)[0] < target_min_real_m for item in result)
    pair_attempts = 0
    spatial_index_queries = 0
    # Minimum rotated rectangles are comparatively expensive.  Cache the
    # axes and invalidate only the one component changed by an accepted merge;
    # recomputing every envelope on every greedy iteration made the 25 km
    # Chicago quality baseline spend 4,650 s in this stage.
    axes = [_rotated_axes(item) for item in result]
    while len(result) > 1:
        undersized = sorted(
            (index for index, (short, _long) in enumerate(axes)
             if short < target_min_real_m),
            key=lambda index: (axes[index][0], result[index].area, index),
        )
        # Query only geometries whose envelopes are within the admissible
        # merge radius.  The previous implementation sorted every other
        # cluster for every undersized leaf, which became the dominant cost
        # once sub-nozzle Chicago footprints were correctly retained until
        # agglomeration.  Rebuilding this small per-block STRtree after an
        # accepted merge preserves the deterministic greedy semantics while
        # avoiding all-pairs scans across unrelated leaves.
        tree = STRtree(result)
        changed = False
        for first_index in undersized:
            spatial_index_queries += 1
            try:
                search = result[first_index].buffer(merge_limit)
                neighbor_indexes = [
                    int(index) for index in tree.query(search)
                    if int(index) != first_index
                ]
            except (GEOSException, TypeError, ValueError):
                neighbor_indexes = [
                    index for index in range(len(result))
                    if index != first_index
                ]
            neighbors = sorted(
                neighbor_indexes,
                key=lambda index: (
                    result[first_index].distance(result[index]), index),
            )
            for second_index in neighbors:
                distance = float(
                    result[first_index].distance(result[second_index]))
                if distance > merge_limit:
                    rejection_counts["distance"] += 1
                    break
                pair_attempts += 1
                combined = _safe_union([
                    result[first_index], result[second_index]])
                try:
                    hull = combined.convex_hull
                except GEOSException:
                    continue
                clipped = _parts(_safe_intersection(hull, clip))
                if len(clipped) != 1:
                    rejection_counts["split_or_outside_clip"] += 1
                    continue
                candidate = clipped[0]
                area_growth = (
                    float(candidate.area) / max(float(combined.area), 1.0) - 1.0)
                _short, long = _rotated_axes(candidate)
                metrics = _silhouette_metrics(
                    candidate,
                    simplify=0.0,
                )
                area_growth_limit = (
                    float(maximum_area_growth_fraction)
                    if maximum_area_growth_fraction is not None
                    else float(
                        policy.urban_cluster_merge_max_area_growth_fraction))
                if area_growth > area_growth_limit:
                    rejection_counts["area_growth"] += 1
                    continue
                if long > maximum_axis_real_m:
                    rejection_counts["maximum_axis"] += 1
                    continue
                if not _silhouette_target_met(metrics, policy):
                    rejection_counts["regularity"] += 1
                    continue
                low, high = sorted((first_index, second_index))
                result[low] = candidate
                axes[low] = (_short, long)
                del result[high]
                del axes[high]
                merges += 1
                changed = True
                break
            if changed:
                break
        if not changed:
            break
    remaining_undersized = sum(
        _rotated_axes(item)[0] < target_min_real_m for item in result)
    return result, {
        "initial_undersized": int(initial_undersized),
        "pair_attempts": int(pair_attempts),
        "spatial_index_queries": int(spatial_index_queries),
        "neighbor_search": "per_block_strtree_radius_v1",
        "merges": int(merges),
        "remaining_undersized": int(remaining_undersized),
        "rejections": rejection_counts,
        "maximum_area_growth_fraction": (
            float(maximum_area_growth_fraction)
            if maximum_area_growth_fraction is not None
            else float(policy.urban_cluster_merge_max_area_growth_fraction)),
    }


def _merge_complete_clusters_bounded(
    clusters: Sequence[Polygon],
    *,
    clip,
    target_min_real_m: float,
    maximum_axis_real_m: float,
    policy: BuildingMassPolicy,
    maximum_area_growth_fraction: float,
    maximum_passes: int = 3,
) -> tuple[list[Polygon], dict]:
    """Batch-merge only sub-floor leaves from a complete city source.

    The general incomplete-data path intentionally performs a greedy search
    because it is trying to construct a stronger carrier.  A complete source
    has a different job: retain every already printable grain and make only a
    bounded number of attempts to rescue nearby sub-floor leaves.  Settled
    components never re-enter the neighbor index, preventing both visual
    over-merging and an unbounded series of STRtree rebuilds.
    """

    if maximum_passes < 1:
        raise ValueError("maximum_passes must be positive")
    merge_limit = (
        float(target_min_real_m)
        * float(policy.urban_cluster_merge_distance_target_fraction))
    rejection_counts = {
        "distance": 0,
        "split_or_outside_clip": 0,
        "area_growth": 0,
        "maximum_axis": 0,
        "regularity": 0,
    }
    measured = [(item, _rotated_axes(item)) for item in clusters]
    settled = [item for item, axes in measured
               if axes[0] + 1e-9 >= target_min_real_m]
    active = [item for item, axes in measured
              if axes[0] + 1e-9 < target_min_real_m]
    initial_undersized = len(active)
    merges = 0
    pair_attempts = 0
    spatial_index_queries = 0
    passes_run = 0
    for _pass in range(maximum_passes):
        if len(active) < 2:
            break
        passes_run += 1
        axes = [_rotated_axes(item) for item in active]
        centers = np.asarray([
            (float(item.centroid.x), float(item.centroid.y))
            for item in active
        ], dtype=float)
        order = sorted(
            range(len(active)),
            key=lambda index: (axes[index][0], active[index].area, index),
        )
        tree = STRtree(active)
        consumed = set()
        next_active = []
        pass_merges = 0
        for first_index in order:
            if first_index in consumed:
                continue
            first = active[first_index]
            spatial_index_queries += 1
            try:
                neighbor_indexes = [
                    int(index) for index in tree.query(
                        first.buffer(merge_limit))
                    if int(index) != first_index
                    and int(index) not in consumed
                ]
            except (GEOSException, TypeError, ValueError):
                neighbor_indexes = [
                    index for index in range(len(active))
                    if index != first_index and index not in consumed
                ]
            max_candidates = max(
                1, int(policy.urban_cluster_merge_neighbor_candidates))
            if len(neighbor_indexes) > max_candidates:
                first_center = centers[first_index]
                neighbor_indexes = sorted(
                    neighbor_indexes,
                    key=lambda index: (
                        float(np.sum((centers[index] - first_center) ** 2)),
                        index),
                )[:max_candidates]
            neighbors = sorted(
                neighbor_indexes,
                key=lambda index: (first.distance(active[index]), index),
            )
            accepted = None
            for second_index in neighbors:
                distance = float(first.distance(active[second_index]))
                if distance > merge_limit:
                    rejection_counts["distance"] += 1
                    break
                pair_attempts += 1
                combined = _safe_union([first, active[second_index]])
                try:
                    hull = combined.convex_hull
                except GEOSException:
                    continue
                clipped = _parts(_safe_intersection(hull, clip))
                if len(clipped) != 1:
                    rejection_counts["split_or_outside_clip"] += 1
                    continue
                candidate = clipped[0]
                area_growth = (
                    float(candidate.area)
                    / max(float(combined.area), 1.0) - 1.0)
                short, long = _rotated_axes(candidate)
                if area_growth > float(maximum_area_growth_fraction):
                    rejection_counts["area_growth"] += 1
                    continue
                if long > maximum_axis_real_m:
                    rejection_counts["maximum_axis"] += 1
                    continue
                if not _silhouette_target_met(
                        _silhouette_metrics(candidate, simplify=0.0), policy):
                    rejection_counts["regularity"] += 1
                    continue
                accepted = (second_index, candidate, short)
                break
            consumed.add(first_index)
            if accepted is None:
                next_active.append(first)
                continue
            second_index, candidate, short = accepted
            consumed.add(second_index)
            merges += 1
            pass_merges += 1
            if short + 1e-9 >= target_min_real_m:
                settled.append(candidate)
            else:
                next_active.append(candidate)
        active = next_active
        if pass_merges == 0:
            break
    result = settled + active
    return result, {
        "initial_undersized": int(initial_undersized),
        "pair_attempts": int(pair_attempts),
        "spatial_index_queries": int(spatial_index_queries),
        "neighbor_search": "per_block_strtree_bounded_passes_v1",
        "maximum_passes": int(maximum_passes),
        "passes_run": int(passes_run),
        "merges": int(merges),
        "remaining_undersized": int(len(active)),
        "settled_components_excluded_from_neighbor_search": int(
            len(settled)),
        "rejections": rejection_counts,
        "maximum_area_growth_fraction": float(
            maximum_area_growth_fraction),
    }


def _source_seed_cluster_infill(
    polygons: Sequence[Polygon],
    *,
    clip,
    nozzle_real_m: float,
    target_min_real_m: float,
    target_max_real_m: float,
    policy: BuildingMassPolicy,
    representation_mode: str = "supported_cluster_infill",
    complete_preserve_min_real_m: float | None = None,
) -> tuple[list[Polygon], dict]:
    """Build several compact masses inside one evidenced urban block.

    The split decision uses geometry and printer scale only.  It prevents a
    transitive chain of nearby source buildings from becoming one U-shaped or
    perimeter-hugging body, while preserving the source buildings and the
    enclosing road/water topology as the sole geometric authority.
    """

    if representation_mode not in {
            "complete_source_union", "supported_cluster_infill"}:
        raise ValueError("unsupported cluster representation mode")
    complete_source = representation_mode == "complete_source_union"
    maximum_hull_growth = (
        float(policy.complete_cluster_max_hull_area_growth_fraction)
        if complete_source else None)
    maximum_merge_growth = (
        float(policy.complete_cluster_merge_max_area_growth_fraction)
        if complete_source
        else float(policy.urban_cluster_merge_max_area_growth_fraction))
    maximum_axis = (
        float(target_max_real_m)
        * float(policy.urban_cluster_max_axis_target_multiple))
    stack = [(list(polygons), 0)]
    clusters = []
    split_count = 0
    hard_source_splits = 0
    maximum_source_members_observed = 0
    boundary_limited_leaves = 0
    while stack:
        group, depth = stack.pop()
        # Enforce the complexity ceiling before unary union or buffering.
        # The aesthetic split below historically ran only after constructing
        # a union and stopped at ``urban_cluster_max_split_depth``. A large
        # Paris block could therefore reach GEOS buffer with many thousands
        # of components and spend more than twenty minutes in one call.
        # Both children remain clipped to the same authoritative road/water
        # block, so this changes work granularity without crossing topology.
        if len(group) > policy.urban_cluster_max_source_members:
            left, right = _split_source_group(group)
            if left and right:
                stack.append((right, depth + 1))
                stack.append((left, depth + 1))
                split_count += 1
                hard_source_splits += 1
                continue
        maximum_source_members_observed = max(
            maximum_source_members_observed, len(group))
        source_union = _safe_union(group)
        if source_union.is_empty:
            continue
        try:
            hull = source_union.convex_hull
        except GEOSException:
            hull = source_union
        clipped = _safe_intersection(hull, clip)
        parts = _parts(clipped)
        hull_area = max(float(getattr(hull, "area", 0.0)), 1.0)
        local_density = float(source_union.area) / hull_area
        hull_area_growth = max(
            0.0, hull_area / max(float(source_union.area), 1.0) - 1.0)
        largest = max(parts, key=lambda item: item.area) if parts else None
        metrics = (
            _silhouette_metrics(
                largest,
                simplify=(
                    nozzle_real_m
                    * policy.silhouette_outline_simplify_nozzles),
            ) if largest is not None else {
                "solidity": 0.0, "perimeter_excess": float("inf")})
        _short, long = _rotated_axes(largest) if largest is not None else (0, 0)
        target_met = _silhouette_target_met(metrics, policy)
        should_split = bool(
            len(group) >= 4
            and depth < policy.urban_cluster_max_split_depth
            and (
                long > maximum_axis
                or local_density < policy.urban_cluster_min_local_density
                or not target_met
                or (maximum_hull_growth is not None
                    and hull_area_growth > maximum_hull_growth)
            )
        )
        if should_split:
            left, right = _split_source_group(group)
            if left and right:
                # Reverse push preserves the lexical first half in output.
                stack.append((right, depth + 1))
                stack.append((left, depth + 1))
                split_count += 1
                continue

        use_hull = bool(
            largest is not None
            and len(parts) == 1
            and local_density >= policy.urban_cluster_min_local_density
            and target_met
            and (maximum_hull_growth is None
                 or hull_area_growth <= maximum_hull_growth))
        if use_hull:
            try:
                grown = largest.buffer(
                    nozzle_real_m * policy.urban_cluster_hull_growth_nozzles,
                    join_style=1, quad_segs=2)
                grown = _safe_intersection(grown, clip)
                grown_parts = _parts(grown)
                grown_candidate = (
                    grown_parts[0] if len(grown_parts) == 1 else None)
                grown_metrics = (
                    _silhouette_metrics(
                        grown_candidate,
                        simplify=(
                            nozzle_real_m
                            * policy.silhouette_outline_simplify_nozzles),
                    ) if grown_candidate is not None else {})
                candidate = (
                    grown_candidate
                    if grown_candidate is not None
                    and _silhouette_target_met(grown_metrics, policy)
                    else largest)
            except GEOSException:
                candidate = largest
        else:
            boundary_limited_leaves += 1
            radius = (
                nozzle_real_m * policy.urban_merge_radius_nozzles
                * (policy.complete_cluster_closing_multiplier
                   if complete_source
                   else policy.urban_infill_closing_multiplier))
            growth = (
                nozzle_real_m
                * (policy.complete_cluster_growth_slack_nozzles
                   if complete_source
                   else policy.urban_infill_growth_slack_nozzles))
            try:
                candidate = source_union.buffer(
                    radius, join_style=1).buffer(
                        -max(0.0, radius - growth), join_style=1)
            except GEOSException:
                candidate = source_union
            candidate = _safe_intersection(candidate, clip)
        # Do not discard a sub-nozzle leaf before the agglomeration pass.
        # Dense, complete cities are commonly made from hundreds of thousands
        # of individually valid OSM footprints that are too small to print at
        # a 25 km crop.  The previous ordering removed those leaves here, so
        # `_merge_small_clusters` only ever saw the already-printable minority
        # and Chicago lost its characteristic fine building grid.  Retaining
        # the bounded leaves until the local merge is safe: every candidate is
        # still clipped to the existing road/water block and the final result
        # is subjected to the physical core test below.
        clusters.extend(_parts(candidate))
    clusters_before_merge = len(clusters)
    # A complete, continuous source already carries the city's real grain.
    # Once one bounded component reaches the selected printer profile's
    # reliable strip floor, merging it further toward the softer aesthetic
    # target destroys useful structure without improving printability.  The
    # incomplete-data path still merges toward ``target_min_real_m`` because
    # it needs a stronger mid-frequency carrier.
    agglomeration_target_real_m = float(target_min_real_m)
    agglomeration_target_origin = "soft_component_target"
    if (complete_source and policy.experimental_complete_source_filter
            and complete_preserve_min_real_m is not None):
        try:
            preserve_floor = float(complete_preserve_min_real_m)
        except (TypeError, ValueError):
            preserve_floor = float("nan")
        if math.isfinite(preserve_floor) and preserve_floor > 0:
            agglomeration_target_real_m = max(
                float(nozzle_real_m),
                min(float(target_min_real_m), preserve_floor),
            )
            agglomeration_target_origin = "printer_reliable_strip_floor"
    if complete_source and policy.experimental_complete_source_filter:
        clusters, merge_evidence = _merge_complete_clusters_bounded(
            clusters,
            clip=clip,
            target_min_real_m=agglomeration_target_real_m,
            maximum_axis_real_m=maximum_axis,
            policy=policy,
            maximum_area_growth_fraction=maximum_merge_growth,
        )
    else:
        # The historical greedy path rebuilt an STRtree and recomputed an
        # oriented envelope after every accepted pair.  On Paris this became
        # effectively quadratic and spent more than twenty minutes inside
        # minimum_rotated_rectangle.  The bounded batch pass applies the same
        # distance, topology, area-growth, axis and regularity gates while
        # consuming disjoint pairs per pass.  It also intentionally stops
        # short of turning dense texture into a few oversized white masses.
        clusters, merge_evidence = _merge_complete_clusters_bounded(
            clusters,
            clip=clip,
            target_min_real_m=agglomeration_target_real_m,
            maximum_axis_real_m=maximum_axis,
            policy=policy,
            maximum_area_growth_fraction=maximum_merge_growth,
            maximum_passes=3,
        )
    clusters_after_merge = len(clusters)
    final_core_real_m = (
        agglomeration_target_real_m
        if agglomeration_target_origin == "printer_reliable_strip_floor"
        else float(nozzle_real_m))
    clusters = [
        cluster for cluster in clusters
        if _has_printable_core(cluster, final_core_real_m)
    ]
    return clusters, {
        "representation_mode": representation_mode,
        "source_groups": len(polygons),
        "output_clusters": len(clusters),
        "recursive_splits": split_count,
        "hard_source_splits": hard_source_splits,
        "maximum_source_members_per_leaf": (
            maximum_source_members_observed),
        "maximum_source_members_policy": (
            policy.urban_cluster_max_source_members),
        "clusters_before_agglomeration": clusters_before_merge,
        "clusters_after_agglomeration": clusters_after_merge,
        "sub_printable_leaves_preserved_for_agglomeration": True,
        "post_agglomeration_physical_core_rejections": (
            clusters_after_merge - len(clusters)),
        "agglomerative_merges": merge_evidence["merges"],
        "agglomeration": merge_evidence,
        "boundary_limited_leaves": boundary_limited_leaves,
        "minimum_cluster_axis_stop_real_m": round(
            float(agglomeration_target_real_m), 5),
        "minimum_cluster_axis_stop_origin": agglomeration_target_origin,
        "final_core_floor_real_m": round(final_core_real_m, 5),
        "soft_component_target_min_real_m": round(
            float(target_min_real_m), 5),
        "maximum_cluster_axis_real_m": round(maximum_axis, 5),
        "maximum_hull_area_growth_fraction": maximum_hull_growth,
        "maximum_merge_area_growth_fraction": maximum_merge_growth,
    }


def _rank(values: np.ndarray) -> np.ndarray:
    """Return deterministic empirical CDF ranks in ``(0, 1]``."""

    if values.size == 0:
        return values.copy()
    ordered = np.sort(values, kind="mergesort")
    return np.searchsorted(ordered, values, side="right") / len(values)


def _distribution(values: Sequence[float]) -> dict:
    array = np.asarray(values, dtype=float)
    array = array[np.isfinite(array)]
    if not array.size:
        return {"p10": None, "p50": None, "p90": None}
    p10, p50, p90 = np.percentile(array, (10, 50, 90))
    return {
        "p10": round(float(p10), 5),
        "p50": round(float(p50), 5),
        "p90": round(float(p90), 5),
    }


def _component_role_metrics(
        polygons: Sequence[Polygon],
        *,
        model_mm_per_real_m: float,
        nozzle_real_m: float,
        policy: BuildingMassPolicy,
) -> dict:
    """Measure one final semantic role without changing its geometry."""

    widths = [_rotated_axes(polygon)[0] for polygon in polygons]
    simplify = (
        nozzle_real_m * policy.silhouette_outline_simplify_nozzles)
    silhouettes = [
        _silhouette_metrics(polygon, simplify=simplify)
        for polygon in polygons
    ]
    return {
        "components": len(polygons),
        "area_m2": round(float(sum(polygon.area for polygon in polygons)), 3),
        "short_axis_model_mm": _distribution([
            width * model_mm_per_real_m for width in widths]),
        "solidity": _distribution([
            metrics["solidity"] for metrics in silhouettes]),
        "perimeter_excess": _distribution([
            metrics["perimeter_excess"] for metrics in silhouettes]),
        "outline_vertices": _distribution([
            metrics["outline_vertices"] for metrics in silhouettes]),
    }


def _flatten_buildings(buildings) -> list[Polygon]:
    geometry = buildings.geometry if hasattr(buildings, "geometry") else buildings
    if geometry is None:
        return []
    result = []
    # GeoSeries deliberately rejects truth-value testing, so do not use
    # ``geometry or ()`` here.  The strategy accepts both a GeoDataFrame and
    # a plain geometry iterable without materialising a second tabular copy.
    for item in geometry:
        result.extend(
            part for part in _parts(item)
            if not part.is_empty and part.area > 0)
    return result


def _assign_to_blocks(buildings: Sequence[Polygon],
                      blocks: Sequence[Polygon], *,
                      batch_size: int = 50_000) -> dict[int, list[int]]:
    if not buildings or not blocks:
        return {}
    if isinstance(batch_size, bool) or int(batch_size) != batch_size:
        raise ValueError("batch_size must be a positive integer")
    batch_size = int(batch_size)
    if batch_size <= 0:
        raise ValueError("batch_size must be a positive integer")
    centroids = np.asarray(
        [building.centroid for building in buildings], dtype=object)
    tree = STRtree(centroids)
    assignments: dict[int, list[int]] = {}
    # Query in the polygon -> point direction.  ``STRtree.query`` prepares the
    # input geometry for a predicate.  The former point.within(block) direction
    # therefore prepared millions of trivial Points and repeatedly evaluated
    # the expensive block topology.  Querying block.contains(point) prepares
    # each complex block once and keeps the indexed side cheap.  A Paris-sized
    # crop otherwise spends tens of minutes in GEOSPreparedWithin.
    #
    # Batch the block inputs so the pair matrix stays bounded.  The centroid
    # tree is intentionally shared across batches; rebuilding it would trade
    # the fixed memory saving for repeated million-point indexing work.
    for start in range(0, len(blocks), batch_size):
        stop = min(len(blocks), start + batch_size)
        block_batch = np.asarray(blocks[start:stop], dtype=object)
        try:
            pairs = tree.query(block_batch, predicate="contains")
            if pairs.size:
                for local_block_index, building_index in zip(
                        pairs[0], pairs[1]):
                    assignments.setdefault(
                        start + int(local_block_index), []).append(
                            int(building_index))
        except (TypeError, ValueError, GEOSException):
            # Compatibility path for older Shapely: the spatial index still
            # narrows candidates, then the block is the predicate authority.
            for local_block_index, block in enumerate(block_batch):
                block_index = start + local_block_index
                for building_index in tree.query(block):
                    if block.contains(centroids[int(building_index)]):
                        assignments.setdefault(block_index, []).append(
                            int(building_index))
    for indexes in assignments.values():
        indexes.sort()
    return assignments


def _local_strategy_cells(source_scene_policy: Mapping | None) -> list[dict]:
    if not isinstance(source_scene_policy, Mapping):
        return []
    building_role = (
        source_scene_policy.get("roles", {}).get("buildings", {}) or {})
    local = building_role.get("local_strategy", {}) or {}
    if local.get("status") != "ready":
        return []
    result = []
    for cell in local.get("cells", ()):
        bounds = cell.get("bounds") if isinstance(cell, Mapping) else None
        strategy = cell.get("strategy") if isinstance(cell, Mapping) else None
        if (not isinstance(bounds, Sequence) or len(bounds) != 4
                or not isinstance(strategy, str)):
            continue
        try:
            values = tuple(float(value) for value in bounds)
        except (TypeError, ValueError):
            continue
        if all(math.isfinite(value) for value in values):
            result.append({"bounds": values, "strategy": strategy})
    return result


def _strategy_for_block(block: Polygon, cells: Sequence[Mapping]) -> str | None:
    if not cells:
        return None
    try:
        point = block.representative_point()
        x, y = float(point.x), float(point.y)
    except (AttributeError, GEOSException, ValueError):
        return None
    for cell in cells:
        xmin, ymin, xmax, ymax = cell["bounds"]
        if xmin <= x <= xmax and ymin <= y <= ymax:
            return str(cell["strategy"])
    return None


def _clip_sources_for_mass(polygons, block, *, inset,
                           exact_union_limit: int = 2048):
    """Shared clearance observation for S5 and the cheap source-only audit."""
    # Count source lineage separately from synthesized output area. A baseline
    # BO layer already contains abstraction and is not an input-area metric.
    exact_union = len(polygons) <= int(exact_union_limit)
    if exact_union:
        source_in_block_area = float(
            _safe_intersection(_safe_union(polygons), block).area)
    else:
        source_in_block_area = float(sum(
            _safe_intersection(polygon, block).area
            for polygon in polygons))
    clearance_audit = {
        "input_footprints": len(polygons),
        "source_inside_block_area_m2": source_in_block_area,
        "source_after_clearance_area_m2": 0.0,
        "footprints_surviving_clearance": 0,
        "output_supported_source_area_m2": 0.0,
        "exact_union": bool(exact_union),
    }
    try:
        clip = block.buffer(-inset, join_style=1) if inset > 0 else block
    except GEOSException:
        clip = block
    clipped_sources = []
    if not clip.is_empty:
        for polygon in polygons:
            retained = _parts(_safe_intersection(polygon, clip))
            clipped_sources.extend(retained)
            clearance_audit["footprints_surviving_clearance"] += int(bool(retained))
    if exact_union or len(clipped_sources) <= int(exact_union_limit):
        source_union = _safe_union(clipped_sources)
        clearance_audit["source_after_clearance_area_m2"] = float(
            source_union.area)
        clearance_audit["exact_union_after_clearance"] = True
    else:
        source_union = GeometryCollection(clipped_sources)
        clearance_audit["source_after_clearance_area_m2"] = float(sum(
            polygon.area for polygon in clipped_sources))
        clearance_audit["exact_union_after_clearance"] = False
    return clip, clipped_sources, source_union, clearance_audit


def _candidate_shape(polygons: Sequence[Polygon], block: Polygon,
                     *, role: str, nozzle_real_m: float,
                     policy: BuildingMassPolicy, exclusion,
                     target_min_real_m: float,
                     target_max_real_m: float,
                     complete_preserve_min_real_m: float | None = None,
                     fill_urban_block: bool = False,
                     compact_complete_block: bool = False,
                     representation_mode: str = (
                         "supported_cluster_infill")) -> tuple[list[Polygon], dict]:
    radius_ratio = (
        policy.urban_merge_radius_nozzles
        if role == "urban_mass" else policy.quiet_merge_radius_nozzles)
    radius = nozzle_real_m * radius_ratio
    growth = nozzle_real_m * policy.growth_slack_nozzles
    simplify = nozzle_real_m * policy.simplify_nozzles
    clip, clipped_sources, source_union, clearance_audit = _clip_sources_for_mass(
        polygons, block, inset=nozzle_real_m * policy.block_inset_nozzles,
        exact_union_limit=policy.source_clearance_exact_union_limit)
    if not clipped_sources:
        return [], {"source_area_m2": 0.0, "output_area_m2": 0.0,
                    "invented_area_m2": 0.0,
                    "source_clearance_audit": clearance_audit}

    cluster_evidence = None
    pre_shapes = None
    if role == "urban_mass" and (fill_urban_block or compact_complete_block):
        pre_shapes, cluster_evidence = _source_seed_cluster_infill(
            clipped_sources,
            clip=clip,
            nozzle_real_m=nozzle_real_m,
            target_min_real_m=target_min_real_m,
            target_max_real_m=target_max_real_m,
            policy=policy,
            representation_mode=representation_mode,
            complete_preserve_min_real_m=complete_preserve_min_real_m,
        )
        shaped = GeometryCollection()
    elif role == "sparse_printable":
        shaped = _safe_union([
            polygon.simplify(simplify, preserve_topology=True)
            if simplify > 0 else polygon
            for polygon in clipped_sources
            if _printable_independent(
                polygon, nozzle_real_m, policy.maximum_independent_aspect)
        ])
    else:
        effective_radius = radius
        effective_growth = (
            0.0
            if representation_mode == "sparse_local_preserve"
            else growth)
        buffered = []
        for polygon in clipped_sources:
            try:
                buffered.append(polygon.buffer(
                    effective_radius, join_style=1))
            except GEOSException:
                continue
        shaped = _safe_union(buffered)
        shrink = max(0.0, effective_radius - effective_growth)
        if shrink > 0 and not shaped.is_empty:
            try:
                shaped = shaped.buffer(-shrink, join_style=1)
            except GEOSException:
                pass
        shaped = _safe_intersection(shaped, clip)
        if simplify > 0 and not shaped.is_empty:
            try:
                shaped = shaped.simplify(simplify, preserve_topology=True)
            except GEOSException:
                pass

    safe_clip = _safe_difference(clip, exclusion)
    if pre_shapes is None:
        shaped = _safe_difference(shaped, exclusion)
        raw_result = [
            polygon for polygon in _parts(shaped)
            if _has_printable_core(polygon, nozzle_real_m)
        ]
    else:
        raw_result = []
        for item in pre_shapes:
            raw_result.extend(
                polygon for polygon in _parts(
                    _safe_difference(item, exclusion))
                if _has_printable_core(polygon, nozzle_real_m))
    silhouette_records = []
    result = []
    for polygon in raw_result:
        if role == "sparse_printable":
            result.append(polygon)
            metrics = _silhouette_metrics(
                polygon,
                simplify=(
                    nozzle_real_m
                    * policy.silhouette_outline_simplify_nozzles),
            )
            silhouette_records.append({
                "method": "preserved_sparse_identity",
                "target_met_before": _silhouette_target_met(metrics, policy),
                "target_met_after": _silhouette_target_met(metrics, policy),
                "boundary_limited": False,
                "area_change_fraction": 0.0,
                "before": metrics,
                "after": metrics,
            })
            continue
        regularized, record = _regularize_silhouette(
            polygon,
            safe_clip=safe_clip,
            nozzle_real_m=nozzle_real_m,
            policy=policy,
        )
        if _has_printable_core(regularized, nozzle_real_m):
            result.append(regularized)
            silhouette_records.append(record)
    boundary_clearances = []
    for polygon in result:
        try:
            boundary_clearances.append(float(polygon.distance(block.boundary)))
        except GEOSException:
            continue
    output_area = float(sum(polygon.area for polygon in result))
    output_union = _safe_union(result)
    if result and not source_union.is_empty:
        if clearance_audit.get("exact_union_after_clearance", True):
            supported_area = float(_safe_intersection(
                output_union, source_union).area)
        else:
            supported_area = min(
                output_area,
                float(clearance_audit["source_after_clearance_area_m2"]))
    else:
        supported_area = 0.0
    clearance_audit["output_supported_source_area_m2"] = supported_area
    return result, {
        "source_clearance_audit": clearance_audit,
        "source_area_m2": round(float(source_union.area), 3),
        "output_area_m2": round(output_area, 3),
        "invented_area_m2": round(max(0.0, output_area - supported_area), 3),
        "density_guarded_block_fill": bool(
            role == "urban_mass" and fill_urban_block),
        "density_abstraction_method": (
            "source_seed_cluster_infill"
            if role == "urban_mass" and fill_urban_block
            else "source_seed_compact_union"
            if role == "urban_mass" and compact_complete_block
            else "source_supported_closing"
            if role != "sparse_printable"
            else "literal_printable_footprint"),
        "cluster_infill": cluster_evidence,
        "silhouette_records": silhouette_records,
        "minimum_block_boundary_clearance_m": (
            min(boundary_clearances) if boundary_clearances else None),
    }


def build_building_mass_candidate(
    buildings,
    blocks: Sequence[Polygon],
    *,
    nozzle_real_m: float,
    printer_profile: PrinterProfile,
    exclusion_polys: Iterable[Polygon] = (),
    policy: BuildingMassPolicy | None = None,
    source_scene_policy: Mapping | None = None,
    model_span_mm: float | None = None,
    downstream_final_clearance: bool = False,
) -> BuildingMassCandidate:
    """Build an experimental, printable-scale building-mass candidate."""

    policy = policy or BuildingMassPolicy()
    if not math.isfinite(nozzle_real_m) or nozzle_real_m <= 0:
        raise ValueError("nozzle_real_m must be finite and positive")
    global_representation = resolve_building_representation_mode(
        source_scene_policy)
    representation_mode = global_representation["mode"]
    model_mm_per_real_m = (
        float(printer_profile.nozzle_diameter_mm) / float(nozzle_real_m))
    component_width_target = resolve_component_width_target(
        policy,
        printer_profile=printer_profile,
        scale_mm_per_m=model_mm_per_real_m,
        model_span_mm=model_span_mm,
    )
    target_min_model_mm = float(
        component_width_target["target_min_model_mm"])
    complete_preserve_min_real_m = (
        float(component_width_target["print_floor_model_mm"])
        / model_mm_per_real_m)
    nozzle_model_mm = float(printer_profile.nozzle_diameter_mm)
    quiet_target_radius_nozzles = (
        target_min_model_mm
        * float(policy.quiet_closing_radius_target_fraction)
        / nozzle_model_mm)
    urban_target_radius_nozzles = (
        target_min_model_mm
        * float(policy.urban_closing_radius_target_fraction)
        / nozzle_model_mm)
    if representation_mode == "supported_cluster_infill":
        policy = replace(
            policy,
            quiet_merge_radius_nozzles=max(
                float(policy.quiet_merge_radius_nozzles),
                quiet_target_radius_nozzles),
            urban_merge_radius_nozzles=max(
                float(policy.urban_merge_radius_nozzles),
                urban_target_radius_nozzles),
        )
    physical_half_seam_nozzles = (
        printer_profile.final_block_base_gap_mm
        / printer_profile.nozzle_diameter_mm / 2.0)
    # Topology-preserving simplification may move the final boundary outward
    # by up to its tolerance.  Reserve that displacement before shaping; the
    # observed post-shape clearance remains the actual acceptance authority.
    shape_reserve_nozzles = (
        policy.simplify_nozzles
        + policy.boundary_clearance_safety_nozzles)
    if downstream_final_clearance:
        # In the high-density two-layer route these polygons are merged into
        # block_base through a final surface plan. Do not
        # erase the same street frontage a second time here: retain only the
        # reserve needed for silhouette simplification. Canonical S6 now
        # resolves the final plan before S7; S8 only materializes its proof.
        required_inset_nozzles = shape_reserve_nozzles
        policy = replace(
            policy, block_inset_nozzles=required_inset_nozzles)
    else:
        required_inset_nozzles = (
            physical_half_seam_nozzles + shape_reserve_nozzles)
        if policy.block_inset_nozzles < required_inset_nozzles:
            policy = replace(
                policy, block_inset_nozzles=required_inset_nozzles)
    source_polygons = _flatten_buildings(buildings)
    clean_blocks = [
        block for block in blocks
        if isinstance(block, Polygon) and not block.is_empty and block.area > 0
    ]
    assignments = _assign_to_blocks(source_polygons, clean_blocks)
    local_cells = _local_strategy_cells(source_scene_policy)
    exclusion = _safe_union([
        polygon for item in exclusion_polys for polygon in _parts(item)
    ])

    records = []
    for block_index, building_indexes in assignments.items():
        block = clean_blocks[block_index]
        selected = [source_polygons[index] for index in building_indexes]
        source_area = float(sum(polygon.area for polygon in selected))
        records.append({
            "block_index": block_index,
            "building_indexes": building_indexes,
            "count": len(selected),
            "density": source_area / max(float(block.area), 1.0),
            "source_area_m2": source_area,
            "local_strategy": _strategy_for_block(block, local_cells),
        })

    if records:
        density = np.asarray([record["density"] for record in records], dtype=float)
        count = np.log1p(np.asarray(
            [record["count"] for record in records], dtype=float))
        score = 0.60 * _rank(density) + 0.40 * _rank(count)
        quiet_threshold = float(np.quantile(
            score, policy.quiet_score_quantile))
        mass_threshold = float(np.quantile(
            score, policy.urban_mass_score_quantile))
    else:
        score = np.asarray([], dtype=float)
        quiet_threshold = 1.0
        mass_threshold = 1.0

    quiet_texture = []
    urban_mass = []
    sparse_printable = []
    role_counts = {"quiet_texture": 0, "urban_mass": 0,
                   "sparse_printable": 0}
    totals = {"source_area_m2": 0.0, "output_area_m2": 0.0,
              "invented_area_m2": 0.0}
    clearance_totals = {
        "input_footprints": 0,
        "footprints_surviving_clearance": 0,
        "source_inside_block_area_m2": 0.0,
        "source_after_clearance_area_m2": 0.0,
        "output_supported_source_area_m2": 0.0,
    }
    urban_block_fill_count = 0
    local_strategy_counts = {}
    observed_boundary_clearances = []
    silhouette_records = []
    cluster_infill_totals = {
        "source_groups": 0,
        "output_clusters": 0,
        "recursive_splits": 0,
        "clusters_before_agglomeration": 0,
        "agglomerative_merges": 0,
        "boundary_limited_leaves": 0,
        "post_agglomeration_physical_core_rejections": 0,
    }
    cluster_merge_rejections = {
        "distance": 0,
        "split_or_outside_clip": 0,
        "area_growth": 0,
        "maximum_axis": 0,
        "regularity": 0,
    }
    cluster_merge_pair_attempts = 0
    cluster_merge_remaining_undersized = 0
    cluster_settled_components = 0
    shaping_method_counts = {}
    for record, evidence_score in zip(records, score):
        local_strategy = record.get("local_strategy")
        if local_strategy:
            local_strategy_counts[local_strategy] = (
                local_strategy_counts.get(local_strategy, 0) + 1)
        if (local_strategy == "neighborhood_mass"
                and record["count"] >= 2):
            role = (
                "quiet_texture"
                if representation_mode == "sparse_local_preserve"
                else "urban_mass")
        elif local_strategy in {"block_base_support", "hybrid_mass"}:
            role = "quiet_texture"
        elif local_strategy in {
                "preserve_printable_footprints", "open_space_preserve",
                "water"}:
            role = "sparse_printable"
        elif evidence_score >= mass_threshold and record["count"] >= 2:
            role = "urban_mass"
        elif evidence_score >= quiet_threshold and record["count"] >= 2:
            role = "quiet_texture"
        else:
            role = "sparse_printable"
        selected = [source_polygons[index]
                    for index in record["building_indexes"]]
        fill_density_floor = (
            policy.urban_block_fill_supported_min_density
            if local_strategy == "neighborhood_mass"
            else policy.urban_block_fill_min_density)
        polygons, block_evidence = _candidate_shape(
            selected, clean_blocks[record["block_index"]], role=role,
            nozzle_real_m=nozzle_real_m, policy=policy,
            exclusion=exclusion,
            target_min_real_m=float(
                component_width_target["target_min_real_m"]),
            target_max_real_m=float(
                component_width_target["target_max_real_m"]),
            complete_preserve_min_real_m=complete_preserve_min_real_m,
            fill_urban_block=(
                role == "urban_mass"
                and representation_mode == "supported_cluster_infill"
                and local_strategy in (None, "neighborhood_mass")
                and record["density"] >= fill_density_floor
                and record["count"] >= policy.urban_block_fill_min_count),
            compact_complete_block=(
                role == "urban_mass"
                and representation_mode == "complete_source_union"),
            representation_mode=representation_mode)
        shaping_method = str(
            block_evidence.get("density_abstraction_method") or "unknown")
        shaping_method_counts[shaping_method] = (
            shaping_method_counts.get(shaping_method, 0) + 1)
        if role == "urban_mass":
            urban_mass.extend(polygons)
        elif role == "quiet_texture":
            quiet_texture.extend(polygons)
        else:
            sparse_printable.extend(polygons)
        if polygons:
            role_counts[role] += 1
            if block_evidence["density_guarded_block_fill"]:
                urban_block_fill_count += 1
            clearance = block_evidence["minimum_block_boundary_clearance_m"]
            if clearance is not None:
                observed_boundary_clearances.append(float(clearance))
        for key in totals:
            totals[key] += float(block_evidence[key])
        for key in clearance_totals:
            clearance_totals[key] += block_evidence[
                "source_clearance_audit"][key]
        silhouette_records.extend(
            block_evidence.get("silhouette_records", ()))
        cluster_evidence = block_evidence.get("cluster_infill")
        if cluster_evidence:
            for key in cluster_infill_totals:
                cluster_infill_totals[key] += int(
                    cluster_evidence.get(key, 0))
            agglomeration = cluster_evidence.get("agglomeration", {}) or {}
            cluster_merge_pair_attempts += int(
                agglomeration.get("pair_attempts", 0))
            cluster_merge_remaining_undersized += int(
                agglomeration.get("remaining_undersized", 0))
            cluster_settled_components += int(agglomeration.get(
                "settled_components_excluded_from_neighbor_search", 0))
            rejections = agglomeration.get("rejections", {}) or {}
            for key in cluster_merge_rejections:
                cluster_merge_rejections[key] += int(rejections.get(key, 0))

    # Safe merging owns geometry.  Any standard-relief leaf that remains too
    # narrow after those attempts is still legitimate source-supported city
    # texture, but it should not become a tall visual spike. Reclassify it to
    # quiet relief without deleting area, widening the polygon, or changing
    # its road/water block membership.
    urban_mass_before_role_filter = list(urban_mass)
    minimum_urban_mass_axis_real_m = (
        float(component_width_target["target_min_real_m"])
        * float(policy.urban_mass_min_short_axis_target_fraction))
    demoted_urban_mass = []
    demoted_narrow = 0
    demoted_irregular = 0
    demoted_narrow_and_irregular = 0
    if representation_mode != "sparse_local_preserve":
        retained_urban_mass = []
        for polygon in urban_mass:
            short_axis, _long_axis = _rotated_axes(polygon)
            metrics = _silhouette_metrics(
                polygon,
                simplify=(
                    nozzle_real_m
                    * policy.silhouette_outline_simplify_nozzles),
            )
            too_narrow = bool(
                short_axis + 1e-9 < minimum_urban_mass_axis_real_m)
            irregular = not _silhouette_target_met(metrics, policy)
            if too_narrow or irregular:
                demoted_urban_mass.append(polygon)
                demoted_narrow += int(too_narrow)
                demoted_irregular += int(irregular)
                demoted_narrow_and_irregular += int(
                    too_narrow and irregular)
            else:
                retained_urban_mass.append(polygon)
        urban_mass = retained_urban_mass
        quiet_texture.extend(demoted_urban_mass)

    all_output = quiet_texture + urban_mass + sparse_printable
    widths = [_rotated_axes(polygon)[0] for polygon in all_output]
    aspects = [
        long / short for short, long in
        (_rotated_axes(polygon) for polygon in all_output)
        if short > 0
    ]
    source_policy_version = None
    if source_scene_policy:
        source_policy_version = source_scene_policy.get("policy_version")
    output_area = totals["output_area_m2"]
    minimum_clearance = (
        min(observed_boundary_clearances)
        if observed_boundary_clearances else None)
    minimum_two_sided_nozzles = (
        2.0 * minimum_clearance / nozzle_real_m
        if minimum_clearance is not None else None)
    quiet_height = (
        printer_profile.layer_height_mm * policy.quiet_relief_layers)
    mass_height = (
        printer_profile.layer_height_mm * policy.urban_mass_relief_layers)
    component_width_p50_m = (
        float(np.percentile(widths, 50)) if widths else None)
    component_width_p50_mm = (
        component_width_p50_m * model_mm_per_real_m
        if component_width_p50_m is not None else None)
    role_component_metrics = {
        "quiet_texture": _component_role_metrics(
            quiet_texture,
            model_mm_per_real_m=model_mm_per_real_m,
            nozzle_real_m=nozzle_real_m,
            policy=policy,
        ),
        "urban_mass": _component_role_metrics(
            urban_mass,
            model_mm_per_real_m=model_mm_per_real_m,
            nozzle_real_m=nozzle_real_m,
            policy=policy,
        ),
        "sparse_printable": _component_role_metrics(
            sparse_printable,
            model_mm_per_real_m=model_mm_per_real_m,
            nozzle_real_m=nozzle_real_m,
            policy=policy,
        ),
    }
    target_min_mm = float(component_width_target["target_min_model_mm"])
    target_max_mm = float(component_width_target["target_max_model_mm"])
    target_tolerance_mm = float(
        policy.component_width_measurement_tolerance_mm)
    exact_component_width_relation = "not_measured"
    if component_width_p50_mm is None:
        component_width_status = "not_measured"
    elif component_width_p50_mm < target_min_mm - target_tolerance_mm:
        component_width_status = "below_target"
        exact_component_width_relation = "below_target"
    elif component_width_p50_mm > target_max_mm + target_tolerance_mm:
        component_width_status = "above_target"
        exact_component_width_relation = "above_target"
    else:
        exact_component_width_relation = (
            "below_target" if component_width_p50_mm < target_min_mm
            else "above_target" if component_width_p50_mm > target_max_mm
            else "within_target")
        component_width_status = (
            "within_target" if exact_component_width_relation == "within_target"
            else "within_target_tolerance")
    anonymous_shape_records = [
        record for record in silhouette_records
        if record.get("method") != "preserved_sparse_identity"
    ]
    silhouette_method_counts = {}
    for record in anonymous_shape_records:
        method = str(record.get("method") or "unknown")
        silhouette_method_counts[method] = (
            silhouette_method_counts.get(method, 0) + 1)
    silhouette_pre = [record["before"] for record in anonymous_shape_records]
    silhouette_post = [record["after"] for record in anonymous_shape_records]
    evidence = {
        "policy_version": policy.policy_version,
        "source_scene_policy_version": source_policy_version,
        "activation": "experimental_ab_only",
        "printer_profile": printer_profile.to_dict(),
        "nozzle_real_m": round(float(nozzle_real_m), 5),
        "required_road_seam": {
            "final_gap_model_mm": round(
                printer_profile.final_block_base_gap_mm, 5),
            "physical_half_seam_nozzles": round(
                physical_half_seam_nozzles, 5),
            "pre_shape_block_inset_each_side_real_m": round(
                nozzle_real_m * policy.block_inset_nozzles, 5),
            "pre_shape_block_inset_each_side_nozzles": round(
                policy.block_inset_nozzles, 5),
            "simplification_reserve_nozzles": round(
                policy.simplify_nozzles, 5),
            "safety_reserve_nozzles": round(
                policy.boundary_clearance_safety_nozzles, 5),
            "clearance_owner": (
                "post_aggregation_surface_plan"
                if downstream_final_clearance else "S6_building_mass"),
            "downstream_final_clearance": bool(
                downstream_final_clearance),
        },
        "scale_aware_closing": {
            "target_min_model_mm": round(target_min_model_mm, 5),
            "quiet_radius_model_mm": round(
                policy.quiet_merge_radius_nozzles * nozzle_model_mm, 5),
            "urban_radius_model_mm": round(
                policy.urban_merge_radius_nozzles * nozzle_model_mm, 5),
            "method": (
                "bounded morphological closing inside eligible topology "
                "blocks; no per-footprint stretching"),
        },
        "semantic_heights": {
            "quiet_texture_layers": policy.quiet_relief_layers,
            "quiet_texture_mm": round(quiet_height, 5),
            "urban_mass_layers": policy.urban_mass_relief_layers,
            "urban_mass_mm": round(mass_height, 5),
            "hero_identity": "preserve_existing_trusted_bounded_role",
        },
        "policy": asdict(policy),
        "source_footprints": len(source_polygons),
        "input_blocks": len(clean_blocks),
        "occupied_blocks": len(records),
        "global_representation": global_representation,
        "source_clearance_audit": {
            **clearance_totals,
            "unassigned_source_footprints": (
                len(source_polygons) - clearance_totals["input_footprints"]),
            "clearance_source_area_retention": (
                clearance_totals["source_after_clearance_area_m2"]
                / clearance_totals["source_inside_block_area_m2"]
                if clearance_totals["source_inside_block_area_m2"] > 0 else None),
            "shaping_source_area_retention": (
                clearance_totals["output_supported_source_area_m2"]
                / clearance_totals["source_after_clearance_area_m2"]
                if clearance_totals["source_after_clearance_area_m2"] > 0 else None),
            "area_definition": (
                "union per assigned topology block; input and synthesized "
                "output are separate; not a comparison with legacy BO area"),
        },
        "source_fidelity_filter": {
            "experimental_filter_enabled": policy.experimental_complete_source_filter,
            "mode": (
                "preserve_printable_grain_with_bounded_rescue"
                if representation_mode == "complete_source_union"
                and policy.experimental_complete_source_filter
                else "established_complete_source_agglomeration"
                if representation_mode == "complete_source_union"
                else "construct_supported_mid_frequency_carrier"
                if representation_mode == "supported_cluster_infill"
                else "preserve_sparse_local_sources"),
            "complete_source": representation_mode == "complete_source_union",
            "reliable_strip_floor_model_mm": round(
                float(component_width_target["print_floor_model_mm"]), 5),
            "maximum_rescue_passes": (
                3 if representation_mode == "complete_source_union"
                and policy.experimental_complete_source_filter else None),
            "settled_components_excluded_from_neighbor_search": (
                cluster_settled_components),
            "post_rescue_core_rejections": cluster_infill_totals[
                "post_agglomeration_physical_core_rejections"],
            "authority": "measured_source_quality_then_selected_printer_profile",
            "visual_acceptance": "human_review_required",
        },
        "local_strategy_cells_available": len(local_cells),
        "local_strategy_block_counts": dict(sorted(
            local_strategy_counts.items())),
        "role_block_counts": role_counts,
        "density_guarded_urban_cluster_infills": urban_block_fill_count,
        # Compatibility key for existing evidence readers.  Version v4 no
        # longer fills the complete block; consumers should migrate to the
        # cluster-infill key above.
        "density_guarded_urban_block_fills": urban_block_fill_count,
        "urban_density_carrier": {
            "method": (
                "source_seed_cluster_infill"
                if representation_mode == "supported_cluster_infill"
                else "source_supported_compact_union"
                if representation_mode == "complete_source_union"
                else "local_source_preserve"),
            "representation_mode": representation_mode,
            "shaping_method_counts": dict(sorted(
                shaping_method_counts.items())),
            "complete_block_replacement": False,
            "closing_multiplier": round(
                (policy.complete_cluster_closing_multiplier
                 if representation_mode == "complete_source_union"
                 else 1.0
                 if representation_mode == "sparse_local_preserve"
                 else policy.urban_infill_closing_multiplier), 5),
            "growth_slack_nozzles": round(
                (policy.complete_cluster_growth_slack_nozzles
                 if representation_mode == "complete_source_union"
                 else 0.0
                 if representation_mode == "sparse_local_preserve"
                 else policy.urban_infill_growth_slack_nozzles), 5),
            "maximum_hull_area_growth_fraction": (
                policy.complete_cluster_max_hull_area_growth_fraction
                if representation_mode == "complete_source_union"
                else None),
            "maximum_merge_area_growth_fraction": (
                policy.complete_cluster_merge_max_area_growth_fraction
                if representation_mode == "complete_source_union"
                else policy.urban_cluster_merge_max_area_growth_fraction),
            "complete_source_preserve_floor": {
                "enabled": (representation_mode == "complete_source_union"
                            and policy.experimental_complete_source_filter),
                "model_mm": round(
                    float(component_width_target["print_floor_model_mm"]), 5),
                "real_m": round(complete_preserve_min_real_m, 5),
                "authority": "selected_printer_profile",
                "effect": (
                    "complete source components stop agglomerating once the "
                    "reliable printable strip floor is reached"),
            },
            "regular_cluster_growth_nozzles": round(
                policy.urban_cluster_hull_growth_nozzles, 5),
            "guarded_blocks": urban_block_fill_count,
            "source_footprints_in_guarded_blocks": (
                cluster_infill_totals["source_groups"]),
            "output_clusters": cluster_infill_totals["output_clusters"],
            "recursive_splits": cluster_infill_totals["recursive_splits"],
            "clusters_before_agglomeration": (
                cluster_infill_totals["clusters_before_agglomeration"]),
            "agglomerative_merges": (
                cluster_infill_totals["agglomerative_merges"]),
            "agglomerative_pair_attempts": cluster_merge_pair_attempts,
            "remaining_undersized_clusters": (
                cluster_merge_remaining_undersized),
            "merge_rejections": dict(sorted(
                cluster_merge_rejections.items())),
            "boundary_limited_leaves": (
                cluster_infill_totals["boundary_limited_leaves"]),
        },
        "mid_frequency_role_filter": {
            "method": (
                "demote_after_safe_merge_and_silhouette_review_without_"
                "geometry_change"),
            "target_fraction": round(
                policy.urban_mass_min_short_axis_target_fraction, 5),
            "minimum_standard_relief_short_axis_model_mm": round(
                minimum_urban_mass_axis_real_m * model_mm_per_real_m, 5),
            "minimum_standard_relief_short_axis_real_m": round(
                minimum_urban_mass_axis_real_m, 5),
            "urban_mass_components_before": len(
                urban_mass_before_role_filter),
            "urban_mass_components_after": len(urban_mass),
            "demoted_to_quiet_texture": len(demoted_urban_mass),
            "demoted_for_narrow_short_axis": demoted_narrow,
            "demoted_for_irregular_silhouette": demoted_irregular,
            "demoted_for_both": demoted_narrow_and_irregular,
            "demoted_area_m2": round(float(sum(
                polygon.area for polygon in demoted_urban_mass)), 3),
            "geometry_deleted": False,
            "geometry_widened": False,
            "road_or_water_topology_changed": False,
        },
        "output_components": {
            "quiet_texture": len(quiet_texture),
            "urban_mass": len(urban_mass),
            "sparse_printable": len(sparse_printable),
            "total": len(all_output),
        },
        "component_metrics_by_role": role_component_metrics,
        "score_thresholds": {
            "quiet_texture": round(quiet_threshold, 5),
            "urban_mass": round(mass_threshold, 5),
        },
        "source_area_m2": round(totals["source_area_m2"], 3),
        "output_area_m2": round(output_area, 3),
        "invented_area_m2": round(totals["invented_area_m2"], 3),
        "invented_output_area_fraction": round(
            totals["invented_area_m2"] / max(output_area, 1.0), 5),
        "block_abstraction": {
            "inferred_fill_area_m2": round(totals["invented_area_m2"], 3),
            "inferred_fill_output_fraction": round(
                totals["invented_area_m2"] / max(output_area, 1.0), 5),
            "meaning": (
                "deterministic source-seed cluster area outside literal "
                "footprints but inside road/water bounds; measure as "
                "abstraction, not source geometry"),
        },
        "component_min_width_p50_m": (
            round(component_width_p50_m, 3)
            if component_width_p50_m is not None else None),
        "component_min_width_p50_model_mm": (
            round(component_width_p50_mm, 5)
            if component_width_p50_mm is not None else None),
        "component_width_target": {
            **component_width_target,
            "observed_model_mm": (
                round(component_width_p50_mm, 5)
                if component_width_p50_mm is not None else None),
            "status": component_width_status,
            "exact_relation": exact_component_width_relation,
            "measurement_tolerance_model_mm": round(
                target_tolerance_mm, 5),
            "accepted_min_model_mm": round(
                target_min_mm - target_tolerance_mm, 5),
            "accepted_max_model_mm": round(
                target_max_mm + target_tolerance_mm, 5),
            "interpretation": (
                "soft reference aggregation prior at a fixed model size; "
                "the printer floor is hard, while geographic granularity "
                "changes through scale-aware merging and road-cut eligibility, "
                "not polygon stretching"),
        },
        "silhouette_regularization": {
            "reference_audit": {
                "cohort": (
                    "10-city compact white-relief sample: Hangzhou, Beijing, "
                    "Shanghai, Guangzhou, Suzhou, Chicago, Tokyo, London, "
                    "Singapore, San Francisco"),
                "city_median_solidity_range": [0.9904, 1.0],
                "typical_effective_outline_vertices_p75": [6, 11],
                "semantic_boundary": (
                    "reference material role measured; exact building "
                    "ownership remains a reviewed inference"),
            },
            "target_solidity": round(
                policy.silhouette_target_solidity, 5),
            "maximum_perimeter_excess": round(
                policy.silhouette_max_perimeter_excess, 5),
            "rounding_radius_nozzles": round(
                policy.silhouette_rounding_nozzles, 5),
            "maximum_area_growth_fraction": round(
                policy.silhouette_max_area_growth_fraction, 5),
            "maximum_area_loss_fraction": round(
                policy.silhouette_max_area_loss_fraction, 5),
            "anonymous_components_measured": len(anonymous_shape_records),
            "method_counts": dict(sorted(silhouette_method_counts.items())),
            "target_met_before_fraction": (
                round(float(np.mean([
                    bool(record["target_met_before"])
                    for record in anonymous_shape_records
                ])), 5) if anonymous_shape_records else None),
            "target_met_after_fraction": (
                round(float(np.mean([
                    bool(record["target_met_after"])
                    for record in anonymous_shape_records
                ])), 5) if anonymous_shape_records else None),
            "boundary_limited_components": sum(
                bool(record.get("boundary_limited"))
                for record in anonymous_shape_records),
            "before": {
                "solidity": _distribution([
                    item["solidity"] for item in silhouette_pre]),
                "perimeter_excess": _distribution([
                    item["perimeter_excess"] for item in silhouette_pre]),
                "outline_vertices": _distribution([
                    item["outline_vertices"] for item in silhouette_pre]),
            },
            "after": {
                "solidity": _distribution([
                    item["solidity"] for item in silhouette_post]),
                "perimeter_excess": _distribution([
                    item["perimeter_excess"] for item in silhouette_post]),
                "outline_vertices": _distribution([
                    item["outline_vertices"] for item in silhouette_post]),
            },
            "hard_rule": (
                "convexification is allowed only inside the road/water/hero "
                "safe clip and within bounded area change; boundary-limited "
                "concavities are retained"),
        },
        "component_min_width_floor_m": (
            round(float(min(widths)), 3) if widths else None),
        "component_below_nozzle_fraction": (
            round(float(np.mean(np.asarray(widths) < nozzle_real_m)), 5)
            if widths else None),
        "component_aspect_above_4_fraction": (
            round(float(np.mean(np.asarray(aspects) > 4.0)), 5)
            if aspects else None),
        "hard_boundaries": {
            "road_water_crossing": "prevented_by_existing_block_membership",
            "observed_minimum_block_boundary_clearance_m": (
                round(minimum_clearance, 5)
                if minimum_clearance is not None else None),
            "observed_minimum_two_sided_seam_nozzles": (
                round(minimum_two_sided_nozzles, 5)
                if minimum_two_sided_nozzles is not None else None),
            "required_minimum_two_sided_seam_nozzles": round(
                2.0 * physical_half_seam_nozzles, 5),
            "boundary_clearance_passed": (
                None if downstream_final_clearance else bool(
                    minimum_two_sided_nozzles is not None
                    and minimum_two_sided_nozzles + 1e-6
                    >= 2.0 * physical_half_seam_nozzles)),
            "boundary_clearance_status": (
                "delegated_to_final_surface_plan"
                if downstream_final_clearance else "measured_in_S6"),
            "post_shape_printable_core_required": True,
            "formal_mesh_affected": False,
        },
    }
    return BuildingMassCandidate(
        quiet_texture=quiet_texture,
        urban_mass=urban_mass,
        sparse_printable=sparse_printable,
        evidence=evidence,
    )
