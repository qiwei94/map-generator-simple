"""Pure-geometry brick transform functions (no matplotlib dependency).

Shared between:
  - tools/brick_render.py (PNG rendering)
  - _TEXTURE_STYLE_OF_DEEPSEEK/block_base.py (3MF extrusion)
  - _TEXTURE_STYLE_OF_DEEPSEEK/buildings.py (3MF landmarks)

Spec: doc/handdrawn_brick_style_spec.md
"""

from __future__ import annotations

import math
import multiprocessing as mp
import os
from functools import partial
from typing import List, Tuple

import numpy as np
import shapely
import shapely.affinity as sa
from shapely.geometry import Polygon, MultiPolygon

from _TEXTURE_STYLE_OF_DEEPSEEK._z_displacement import _hash_noise_2d


_NUM_WORKERS = max(1, os.cpu_count() - 1)


# ---------------------------------------------------------------------------
# Spec section 1: round corners
# ---------------------------------------------------------------------------

def _round_corners(polygon: Polygon, r: float) -> Polygon:
    """buffer(r) → buffer(-r) 圆角化（祛除尖角）。"""
    if r <= 0 or polygon.is_empty:
        return polygon
    try:
        rounded = polygon.buffer(r, resolution=8, join_style=1)
        rounded = rounded.buffer(-r, resolution=8, join_style=1)
    except Exception:
        return polygon
    if rounded.is_empty:
        return polygon
    if isinstance(rounded, MultiPolygon):
        rounded = max(rounded.geoms, key=lambda g: g.area)
    if not isinstance(rounded, Polygon):
        return polygon
    return rounded


# ---------------------------------------------------------------------------
# Spec section 2: individual perturb (micro-rotation + shift)
# ---------------------------------------------------------------------------

def _individual_perturb(polygon: Polygon, noise_seed: int,
                        rot_deg: float, shift_m: float,
                        seed_offset: float = 0.0) -> Polygon:
    """每块砖整体微旋微移，hash noise 驱动保证相邻连续渐变。"""
    if not isinstance(polygon, Polygon) or polygon.is_empty:
        return polygon
    if rot_deg <= 0 and shift_m <= 0:
        return polygon
    c = polygon.centroid
    freq = 0.005
    xs = np.array([c.x * freq + seed_offset, c.x * freq + 100.0 + seed_offset, c.x * freq])
    ys = np.array([c.y * freq, c.y * freq, c.y * freq + 100.0 + seed_offset])
    vals = _hash_noise_2d(xs, ys, seed=noise_seed)
    angle = float(vals[0]) * rot_deg
    dx = float(vals[1]) * shift_m
    dy = float(vals[2]) * shift_m
    out = sa.rotate(polygon, angle, origin='centroid')
    out = sa.translate(out, xoff=dx, yoff=dy)
    return out


# ---------------------------------------------------------------------------
# Spec section 3: edge resample + Perlin edge offset
# ---------------------------------------------------------------------------

def _resample_ring(coords: List[Tuple[float, float]],
                   segment_m: float) -> List[Tuple[float, float]]:
    """把环上的边按 segment_m 重采样为等距点列。"""
    out = [coords[0]]
    for i in range(len(coords) - 1):
        p0 = np.array(coords[i])
        p1 = np.array(coords[i + 1])
        L = float(np.linalg.norm(p1 - p0))
        n_seg = max(1, int(math.ceil(L / segment_m)))
        for k in range(1, n_seg + 1):
            t = k / n_seg
            out.append(tuple(p0 + (p1 - p0) * t))
    return out


def _perlin_edge_offset(ring_coords, noise_seed: int, amp: float, freq: float,
                        seed_offset: float = 0.0) -> List[Tuple[float, float]]:
    """边点 hash noise 偏移（垂直法向），全向量化。"""
    coords = np.asarray(ring_coords, dtype=np.float64)
    n = len(coords)
    if n < 4:
        return [tuple(c) for c in coords]

    prev = np.roll(coords, 1, axis=0)
    nxt = np.roll(coords, -1, axis=0)
    tangent = nxt - prev
    lengths = np.linalg.norm(tangent, axis=1)
    mask = lengths > 1e-9

    nx = np.zeros(n)
    ny = np.zeros(n)
    nx[mask] = -tangent[mask, 1] / lengths[mask]
    ny[mask] = tangent[mask, 0] / lengths[mask]

    indices = np.arange(n, dtype=np.float64)
    noise_vals = _hash_noise_2d(indices * freq + seed_offset,
                                np.full(n, seed_offset), seed=noise_seed)
    displacements = noise_vals * amp

    result = coords.copy()
    result[:, 0] += nx * displacements
    result[:, 1] += ny * displacements

    out = [tuple(result[i]) for i in range(n)]
    if out[0] != out[-1]:
        out.append(out[0])
    return out


# ---------------------------------------------------------------------------
# Spec section 4: edge shrink (灰缝)
# ---------------------------------------------------------------------------

def _shrink_edges_in_ring(ring_coords: List[Tuple[float, float]],
                          ratio: float) -> List[List[Tuple[float, float]]]:
    """把环拆成独立边段，每段两端各收缩 ratio，留灰缝。"""
    if ratio <= 0:
        return [list(ring_coords)]
    segs: list = []
    for i in range(len(ring_coords) - 1):
        p0 = np.array(ring_coords[i])
        p1 = np.array(ring_coords[i + 1])
        v = p1 - p0
        new_p0 = p0 + v * ratio
        new_p1 = p1 - v * ratio
        if np.linalg.norm(new_p1 - new_p0) < 1e-6:
            continue
        segs.append([tuple(new_p0), tuple(new_p1)])
    return segs


# ---------------------------------------------------------------------------
# Single polygon transform (public API for 3MF)
# ---------------------------------------------------------------------------

def brick_transform_polygon(
    cell: Polygon, *,
    corner_r_m: float = 8.0,
    rot_deg: float = 10.0,
    shift_m: float = 8.0,
    noise: "object | None" = None,
    perlin_amp: float = 4.0,
    perlin_freq: float = 0.15,
    resample_m: float = 12.0,
    brick_idx: int = 0,
    noise_seed: int = 2026,
) -> Polygon:
    """纯几何 brick 变换：圆角 + 微旋微移 + hash noise 边偏移。返回 Shapely Polygon。"""
    if not isinstance(cell, Polygon) or cell.is_empty:
        return cell

    rounded = _round_corners(cell, corner_r_m)
    if not isinstance(rounded, Polygon) or rounded.is_empty:
        return cell

    perturbed = _individual_perturb(
        rounded, noise_seed, rot_deg=rot_deg, shift_m=shift_m,
        seed_offset=brick_idx * 0.13)
    if not isinstance(perturbed, Polygon) or perturbed.is_empty:
        return cell

    ring = list(perturbed.exterior.coords)
    if len(ring) < 4:
        return perturbed

    ring_resampled = _resample_ring(ring, resample_m)
    ring_jittered = _perlin_edge_offset(
        ring_resampled, noise_seed, perlin_amp, perlin_freq,
        seed_offset=brick_idx * 0.37)

    result = Polygon(ring_jittered)
    if result.is_empty or not result.is_valid:
        result = shapely.make_valid(result)
    if result.is_empty:
        return cell
    if isinstance(result, MultiPolygon):
        result = max(result.geoms, key=lambda g: g.area)
    return result


# ---------------------------------------------------------------------------
# Batch transform (multiprocessing, public API)
# ---------------------------------------------------------------------------

def _brick_transform_batch_worker(batch, corner_r_m, rot_deg, shift_m,
                                   perlin_amp, perlin_freq, resample_m,
                                   noise_seed):
    """Worker: 批量 brick 几何变换，返回 (idx, wkb_bytes) list。"""
    from shapely import wkb
    results = []
    for brick_idx, cell_wkb in batch:
        cell = wkb.loads(cell_wkb)
        transformed = brick_transform_polygon(
            cell, corner_r_m=corner_r_m, rot_deg=rot_deg, shift_m=shift_m,
            perlin_amp=perlin_amp, perlin_freq=perlin_freq,
            resample_m=resample_m, brick_idx=brick_idx, noise_seed=noise_seed)
        results.append((brick_idx, wkb.dumps(transformed)))
    return results


def brick_transform_batch(
    polys: List[Polygon], *,
    corner_r_m: float = 8.0,
    rot_deg: float = 10.0,
    shift_m: float = 8.0,
    perlin_amp: float = 4.0,
    perlin_freq: float = 0.15,
    resample_m: float = 12.0,
    noise_seed: int = 2026,
) -> List[Polygon]:
    """批量 brick 几何变换（多核并行）。返回变换后的 Polygon 列表（顺序对应输入）。"""
    from shapely import wkb

    indexed = [(i, wkb.dumps(p)) for i, p in enumerate(polys)
               if isinstance(p, Polygon) and not p.is_empty]
    if not indexed:
        return []

    total = len(indexed)
    n_workers = min(_NUM_WORKERS, max(1, total // 50))
    batch_size = max(50, math.ceil(total / (n_workers * 4)))
    batches = [indexed[i:i+batch_size] for i in range(0, total, batch_size)]

    worker_fn = partial(
        _brick_transform_batch_worker,
        corner_r_m=corner_r_m, rot_deg=rot_deg, shift_m=shift_m,
        perlin_amp=perlin_amp, perlin_freq=perlin_freq,
        resample_m=resample_m, noise_seed=noise_seed)

    result_map: dict = {}

    if n_workers > 1 and total > 100:
        ctx = mp.get_context("fork" if hasattr(os, "fork") else "spawn")
        with ctx.Pool(n_workers) as pool:
            for batch_res in pool.imap_unordered(worker_fn, batches):
                for idx, poly_wkb in batch_res:
                    result_map[idx] = wkb.loads(poly_wkb)
    else:
        for batch in batches:
            batch_res = worker_fn(batch)
            for idx, poly_wkb in batch_res:
                result_map[idx] = wkb.loads(poly_wkb)

    return [result_map[i] for i in range(total)]
