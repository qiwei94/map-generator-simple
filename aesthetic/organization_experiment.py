"""Opt-in S6 spatial-organization experiment, never a production default.

B organizes source footprints by proximity. C organizes occupied street-block
interiors. Both share source confidence, topology, protected spaces, physical
scale and height-role rules. No city-name lookup or stochastic tessellation.
"""
from __future__ import annotations
import time
from functools import partial
import numpy as np
import shapely
from shapely.errors import GEOSException
from shapely.geometry import GeometryCollection
from shapely.strtree import STRtree
from aesthetic.building_mass_strategy import (
    BuildingMassPolicy, _parts, _safe_union, _safe_difference,
    _safe_intersection, _flatten_buildings, _assign_to_blocks,
    _local_strategy_cells, _strategy_for_block, _rank,
    _regularize_silhouette, _has_printable_core,
)

VERSION = 'urban-organization-experiment-v1'
LABELS = {'A': '当前方案', 'B': '建筑群主导', 'C': '街区主导'}


def protected_open_spaces(sources):
    """Preserve explicitly tagged natural/open land; do not mask urban landuse.

    Retaining source geometry here does not turn the vegetation mesh back on.
    Courtyards without tags are protected by the bounded inference distance.
    """
    result = []
    values = {'forest', 'wood', 'grass', 'meadow', 'farmland', 'orchard',
              'vineyard', 'cemetery', 'park', 'garden', 'nature_reserve',
              'wetland', 'heath', 'scrub', 'water', 'recreation_ground',
              'golf_course', 'pitch'}
    for name in ('landuse', 'vegetation'):
        frame = getattr(sources, name, None)
        if frame is None or frame.empty:
            continue
        mask = np.zeros(len(frame), dtype=bool)
        for key in ('landuse', 'natural', 'leisure'):
            if key in frame:
                mask |= frame[key].isin(values).to_numpy()
        for geom in frame.loc[mask].geometry:
            result.extend(_parts(geom))
    return result


def organize_block(seeds, block, exclusion, *, variant, scale, profile,
                   policy=None):
    """Return explicit polygons and evidence for one bounded block.

    B: closing connects only sufficiently close source footprints; no outward
       blanket dilation. C: clip the block to a locally source-supported band;
       maximum inference reach is half one printable coloured strip. Large
       unsupported courtyards/open areas cannot be filled by a convex hull.
    """
    if variant not in ('B', 'C'):
        raise ValueError('organization variant must be B or C')
    if not np.isfinite(scale) or scale <= 0:
        raise ValueError('invalid physical scale')
    policy = policy or BuildingMassPolicy()
    allowed = _safe_difference(block, exclusion)
    # Boundary-only line/point contacts have zero footprint area. Remove them
    # explicitly before areal overlays; GEOS fixed-precision overlay rejects
    # mixed-dimensional collections even when every polygon is valid.
    source = _safe_union(_parts(_safe_intersection(_safe_union(seeds), allowed)))
    if source.is_empty:
        return [], {'source_area_m2': 0., 'inferred_area_m2': 0., 'rejected_area_m2': 0.}
    close_r = profile.min_gap_mm / scale / 2
    support_r = profile.min_colored_strip_mm / scale / 2
    if variant == 'B':
        shape = source.buffer(close_r, join_style=2).buffer(-close_r, join_style=2)
        shaping_clip = allowed
    else:
        # Street-block silhouette is the carrier only where source exists
        # nearby. Unlike B, small inter-building voids become coherent mass.
        shape = allowed.intersection(source.buffer(support_r, join_style=2))
        shaping_clip = shape
    # Both alternatives preserve source evidence then remove the exact same
    # protected spaces. No random offset, blanket urban fill or per-city map.
    shape = _safe_intersection(_safe_union([shape, source]), allowed)
    result, rejected = [], 0.
    for poly in _parts(shape):
        shaped, _e = _regularize_silhouette(
            poly, safe_clip=shaping_clip, nozzle_real_m=profile.nozzle_diameter_mm / scale,
            policy=policy)
        for part in _parts(shaped):
            if _has_printable_core(part, profile.nozzle_diameter_mm / scale):
                result.append(part)
            else:
                rejected += part.area
    union = _safe_union(result)
    metric_precision_retry = False
    try:
        retained = union.intersection(source).area
    except GEOSException:
        # Audit overlay only, not candidate geometry. Micrometre ground-grid
        # noding avoids a GEOS coincident-edge exception; failure still raises.
        retained = shapely.intersection(union, source, grid_size=1e-6).area
        metric_precision_retry = True
    return result, {
        'source_area_m2': source.area,
        'retained_source_area_m2': retained,
        'inferred_area_m2': max(0., union.area-retained),
        'metric_precision_retries': int(metric_precision_retry),
        'rejected_area_m2': rejected,
        'close_radius_model_mm': close_r * scale,
        'max_support_distance_model_mm': support_r * scale if variant == 'C' else None,
    }


def apply_organization(layers, buildings, roads, water, bbox_local, *,
                       variant, open_spaces=(), printer_profile, scene_policy,
                       topology_blocks=None, **_unused):
    """S6 apply_mass adapter. Roads/water/landmarks/terrain are not changed."""
    start = time.monotonic()
    scale = printer_profile.nozzle_diameter_mm / layers.nozzle_real_m
    policy = BuildingMassPolicy()
    blocks = list(topology_blocks if topology_blocks is not None else layers.city_blocks)
    source = _flatten_buildings(buildings)
    print(f'[{variant}] 分配 {len(source):,} 建筑到 {len(blocks):,} 街区', flush=True)
    assignments = _assign_to_blocks(source, blocks)
    exclusions = list(open_spaces) + list(layers.WL) + list(layers.WO) + [p for p, _ in layers.BL]
    exclusion_tree = STRtree(exclusions)
    local_cells = _local_strategy_cells(scene_policy)
    records = []
    for block_id, ids in assignments.items():
        records.append((block_id, ids, sum(source[i].area for i in ids) / max(1, blocks[block_id].area)))
    score = (.6 * _rank(np.array([r[2] for r in records]))
             + .4 * _rank(np.log1p([len(r[1]) for r in records]))) if records else []
    quiet_threshold = np.quantile(score, policy.quiet_score_quantile) if records else 1.
    mass_threshold = np.quantile(score, policy.urban_mass_score_quantile) if records else 1.
    polygons, heights = [], []
    totals = {'source_area_m2': 0., 'retained_source_area_m2': 0.,
              'inferred_area_m2': 0., 'rejected_area_m2': 0., 'metric_precision_retries': 0}
    role_counts = {'urban_mass': 0, 'quiet_texture': 0, 'sparse_printable': 0}
    for index, ((block_id, ids, _density), value) in enumerate(zip(records, score)):
        block = blocks[block_id]
        local = _strategy_for_block(block, local_cells)
        if local == 'neighborhood_mass' and len(ids) >= 2:
            role = 'urban_mass'
        elif local in {'block_base_support', 'hybrid_mass'}:
            role = 'quiet_texture'
        elif local in {'preserve_printable_footprints', 'open_space_preserve', 'water'}:
            role = 'sparse_printable'
        elif value >= mass_threshold and len(ids) >= 2:
            role = 'urban_mass'
        elif value >= quiet_threshold and len(ids) >= 2:
            role = 'quiet_texture'
        else:
            role = 'sparse_printable'
        exclusion = _safe_union([exclusions[int(i)] for i in exclusion_tree.query(block)])
        # Sparse/open cells use the conservative source organization even in C.
        block_variant = 'B' if role == 'sparse_printable' else variant
        made, evidence = organize_block([source[i] for i in ids], block, exclusion,
            variant=block_variant, scale=scale, profile=printer_profile, policy=policy)
        polygons.extend(made)
        height = printer_profile.layer_height_mm * (
            policy.urban_mass_relief_layers if role == 'urban_mass' else policy.quiet_relief_layers)
        heights.extend([height] * len(made))
        role_counts[role] += len(made)
        for name in totals:
            totals[name] += evidence.get(name, 0.)
        if index % 100 == 0 or index == len(records) - 1:
            print(f'[{variant}] 街区 {index+1}/{len(records)}，块群 {len(polygons)}，{time.monotonic()-start:.1f}s', flush=True)
    layers.BO, layers.BO_heights = polygons, heights
    # Both alternatives replace the ordinary-city carrier as a whole. Keeping
    # a separate broad baseline would hide their different organization.
    baseline_count = len(layers.block_base)
    layers.block_base, layers.block_base_classes = [], []
    return {'status': 'active', 'policy_version': VERSION, 'variant': variant,
            'label': LABELS[variant], 'production_default': False,
            'height_rule': 'same local roles and 2/7 layer relief family as A',
            'baseline_carrier_replaced': baseline_count,
            'source_polygons': len(source), 'assigned_unique_sources': len({i for ids in assignments.values() for i in ids}),
            'blocks_with_sources': len(assignments), 'output_components': len(polygons),
            'role_counts': role_counts, 'support_audit': totals,
            'protected_open_space_polygons': len(open_spaces),
            'elapsed_seconds': time.monotonic()-start,
            'limitations': ['experimental organization, not aesthetic/print acceptance',
                           'unassigned source count is reported, not silently treated as deletion success']}


def adapter(variant, sources):
    return partial(apply_organization, variant=variant,
                   open_spaces=protected_open_spaces(sources))
