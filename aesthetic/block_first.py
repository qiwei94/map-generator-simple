"""Street-block carrier with bounded, explicitly selected building detail.

S5 counts source points and chooses work units. S6 consumes that plan, builds
ordinary surfaces without footprint unions and refines only bounded cores.
"""
from dataclasses import asdict, dataclass
from time import monotonic
from hashlib import sha256

import numpy as np
import shapely
from shapely.strtree import STRtree

from aesthetic.building_mass_strategy import _parts, _safe_union
from aesthetic.organization_experiment import protected_open_spaces, organize_block

VERSION = 'block-first-v2'


def block_digest(blocks):
    digest = sha256()
    for block in blocks:
        digest.update(block.wkb)
    return digest.hexdigest()


@dataclass(frozen=True)
class BlockFirstPolicy:
    max_core_blocks: int = 32
    max_buildings_per_core: int = 256
    max_block_area_m2: float = 500000.
    support_cell_nozzles: float = 1.0


def occupancy_mask(points, blocks, cell_m):
    """Compact S5 support evidence for incomplete/oversized road faces."""
    from scipy.ndimage import binary_closing
    bounds = shapely.total_bounds(blocks)
    xmin, ymin, xmax, ymax = bounds
    nx, ny = max(1, int(np.ceil((xmax-xmin)/cell_m))), max(1, int(np.ceil((ymax-ymin)/cell_m)))
    mask = np.zeros((ny, nx), np.uint8)
    xy = shapely.get_coordinates(points)
    ids = np.floor((xy - [xmin, ymin])/cell_m).astype(int)
    inside = (ids[:,0]>=0)&(ids[:,0]<nx)&(ids[:,1]>=0)&(ids[:,1]<ny)
    ids = ids[inside]
    mask[ids[:,1],ids[:,0]] = 1
    # Close only one-cell inter-building holes; retain the measured cells.
    mask |= binary_closing(mask, iterations=1).astype(np.uint8)
    return {'shape': [ny,nx], 'origin': [float(xmin),float(ymin)],
            'cell_m': float(cell_m), 'bits_hex': np.packbits(mask).tobytes().hex(),
            'meaning': 'building-centroid occupancy with one-cell closing; not building footprints'}


def support_polygons(evidence):
    from rasterio.features import shapes
    from affine import Affine
    from shapely.geometry import shape
    ny,nx = evidence['shape']
    bits = np.frombuffer(bytes.fromhex(evidence['bits_hex']), dtype=np.uint8)
    mask = np.unpackbits(bits)[:ny*nx].reshape(ny,nx).astype(np.uint8)
    transform = Affine(evidence['cell_m'], 0, evidence['origin'][0],
                       0, evidence['cell_m'], evidence['origin'][1])
    return [shape(g) for g,v in shapes(mask, mask=mask.astype(bool), transform=transform)]


def landmark_candidates(buildings):
    """Cheap attribute shortlist; ordinary geometry remains available for counts."""
    if buildings is None or buildings.empty:
        return buildings
    mask = np.zeros(len(buildings), dtype=bool)
    for column in ('wikidata', 'wikipedia'):
        if column in buildings:
            mask |= buildings[column].fillna('').astype(str).str.strip().ne('').to_numpy()
    for column, values in {
        'historic': {'monument', 'castle', 'memorial'},
        'tourism': {'attraction', 'museum'},
        'building': {'cathedral', 'church', 'stadium', 'train_station'},
    }.items():
        if column in buildings:
            mask |= buildings[column].isin(values).to_numpy()
    return buildings.iloc[np.flatnonzero(mask)].copy()


def plan_blocks(layers, buildings, *, policy=None):
    start = monotonic()
    policy = policy or BlockFirstPolicy()
    blocks = list(layers.city_blocks)
    # Vectorized centroids are measurement only. No flatten/union/buffer or
    # assignment of every footprint across multiple blocks is needed here.
    points = shapely.centroid(buildings.geometry.array) if buildings is not None else []
    point_tree = STRtree(points)
    pairs = point_tree.query(blocks, predicate='covers') if blocks else np.empty((2, 0), int)
    counts = np.bincount(pairs[0], minlength=len(blocks))
    categories = list(getattr(layers, 'BL_categories', ()) or ())
    anchors = [(p, h) for i, (p, h) in enumerate(layers.BL)
               if not categories or str(categories[i]) != 'GEOMETRIC']
    anchor_tree = STRtree([p for p, _ in anchors])
    scores = []
    for i, block in enumerate(blocks):
        if not (0 < counts[i] <= policy.max_buildings_per_core):
            continue
        if block.area > policy.max_block_area_m2:
            continue
        hits = anchor_tree.query(block, predicate='intersects')
        if len(hits):
            scores.append((max(float(anchors[j][1]) for j in hits), i))
    scores.sort(key=lambda row: (-row[0], row[1]))
    core_ids = [i for _, i in scores[:policy.max_core_blocks]]
    # Store only selected source indices; full-city point assignments die here.
    cores = {str(i): pairs[1][pairs[0] == i].astype(int).tolist() for i in core_ids}
    support = (occupancy_mask(points, blocks, layers.nozzle_real_m * policy.support_cell_nozzles)
               if blocks else None)
    return {'version': VERSION, 'owner_stage': 'S5', 'policy': asdict(policy),
            'oversized_block_support': support,
            'block_fingerprint': block_digest(blocks),
            'block_count': len(blocks), 'source_buildings': len(points),
            'occupied_block_ids': np.flatnonzero(counts).astype(int).tolist(),
            'core_sources': cores, 'core_block_count': len(cores),
            'detailed_buildings': sum(map(len, cores.values())),
            'selection': 'source occupancy; semantic landmark blocks ranked by resolved height',
            'elapsed_seconds': monotonic()-start}


def apply_block_first(layers, buildings, roads, water, bbox_local, *,
                      printer_profile, scene_policy, sources, **unused):
    start = monotonic()
    plan = scene_policy['block_first']
    if (plan['version'] != VERSION or plan['block_count'] != len(layers.city_blocks)
            or plan['block_fingerprint'] != block_digest(layers.city_blocks)):
        raise ValueError('block-first S5/S6 plan mismatch')
    scale = printer_profile.nozzle_diameter_mm / layers.nozzle_real_m
    protected = protected_open_spaces(sources) + list(layers.WL) + list(layers.WO)
    protected += [p for p, _ in layers.BL]
    tree = STRtree(protected)
    support = support_polygons(plan['oversized_block_support']) if plan['oversized_block_support'] else []
    support_tree = STRtree(support)
    base, detail, heights = [], [], []
    refined, rejected, oversized = 0, 0, 0
    for index in plan['occupied_block_ids']:
        block = layers.city_blocks[index]
        exclusion = _safe_union([protected[int(i)] for i in tree.query(block)])
        allowed = shapely.make_valid(block).difference(exclusion)
        if block.area > plan['policy']['max_block_area_m2']:
            oversized += 1
            occupied = _safe_union([support[int(i)].intersection(block)
                                   for i in support_tree.query(block)])
            allowed = allowed.intersection(occupied)
        ids = plan['core_sources'].get(str(index))
        if ids:
            seeds = []
            for geom in buildings.iloc[ids].geometry:
                seeds.extend(_parts(geom))
            made, _ = organize_block(seeds, block, exclusion, variant='C',
                scale=scale, profile=printer_profile)
            if made:
                detail.extend(made)
                heights.extend([printer_profile.layer_height_mm * 2] * len(made))
                refined += 1
                continue
            rejected += 1
        base.extend(_parts(allowed))
    layers.block_base = base
    layers.block_base_classes = ['residential'] * len(base)
    layers.BO, layers.BO_heights = detail, heights
    return {'status': 'active', 'policy_version': VERSION,
            'output_components': len(base)+len(detail),
            'block_first': plan, 'ordinary_blocks_without_building_unions': len(base),
            'refined_core_blocks': refined, 'core_fallbacks': rejected,
            'oversized_blocks_clipped_to_occupancy': oversized,
            'elapsed_seconds': monotonic()-start,
            '说明': '普通街区直接成面；核心街区才聚合建筑；公园、水体与地标保持独立。'}
