"""Fixed-grid geographic tiling for cache reuse across overlapping queries.

所有缓存 key 基于"量化后的取数框"而非用户精确框：稍有偏移的相邻请求
量化后大概率得到完全相同的取数框，从而整条管线命中缓存。

网格以 (0, 0) 为绝对原点，step 为度数。用户精确框仅用于最终裁剪与
输出度量（area_km2 / width_m / origin），不影响缓存 key。

Usage:
    from _TEXTURE_STYLE_OF_DEEPSEEK._tile_grid import snap_bbox, tile_range

    fs, fw, fn, fe = snap_bbox(30.22, 120.10, 30.29, 120.17)
    ix0, iy0, ix1, iy1 = tile_range((30.22, 120.10, 30.29, 120.17))
"""

from __future__ import annotations

import math
from typing import Tuple

# 默认网格步长：0.05° ≈ 5.5km（纬度 30° 附近）。
# 10km 查询量化后最多多取 ~50% 面积（一次性成本，后续请求全命中）。
DEFAULT_TILE_STEP = 0.05

# 网格线容差（度）：距网格线 <1e-6°（约 0.1m）视为恰在线上，
# 避免 v/step 浮点误差导致 floor/ceil 跳格。
_LINE_TOL = 1e-6


def _floor_idx(v: float, step: float) -> int:
    q = v / step
    nearest = round(q)
    if abs(q - nearest) <= _LINE_TOL / step:
        return int(nearest)
    return math.floor(q)


def _ceil_idx(v: float, step: float) -> int:
    q = v / step
    nearest = round(q)
    if abs(q - nearest) <= _LINE_TOL / step:
        return int(nearest)
    return math.ceil(q)


def snap_bbox(south: float, west: float, north: float, east: float,
              step: float = DEFAULT_TILE_STEP) -> Tuple[float, float, float, float]:
    """把 bbox 向外量化到绝对网格倍数：south/west 向下取整，north/east 向上取整。

    Args:
        south, west, north, east: 用户精确 bbox (WGS84, 度)
        step: 网格步长（度）

    Returns:
        (snap_south, snap_west, snap_north, snap_east)，包含原 bbox。
    """
    if step <= 0:
        raise ValueError(f"step must be positive, got {step}")
    if north < south or east < west:
        raise ValueError(f"invalid bbox: ({south}, {west}, {north}, {east})")

    snap_south = _floor_idx(south, step) * step
    snap_west = _floor_idx(west, step) * step
    snap_north = _ceil_idx(north, step) * step
    snap_east = _ceil_idx(east, step) * step

    # 防御：网格线吸附可能使边界反超原框（亚毫米级），超出容差则收回一格；
    # 容差内的偏差（约 0.1m）忽略，保证量化结果稳定对齐网格。
    if snap_south - south > _LINE_TOL:
        snap_south -= step
    if snap_west - west > _LINE_TOL:
        snap_west -= step
    if snap_north < north - _LINE_TOL:
        snap_north += step
    if snap_east < east - _LINE_TOL:
        snap_east += step

    return snap_south, snap_west, snap_north, snap_east


def tile_range(south: float, west: float, north: float, east: float,
               step: float = DEFAULT_TILE_STEP) -> Tuple[int, int, int, int]:
    """返回 bbox 覆盖的瓦片索引范围（闭区间）。

    瓦片 (ix, iy) 覆盖 [ix*step, (ix+1)*step) x [iy*step, (iy+1)*step)，
    与原点在 (0,0) 的绝对网格对齐，跨查询可复用。

    Returns:
        (ix_min, iy_min, ix_max, iy_max)，ix 为经度方向、iy 为纬度方向。
    """
    if step <= 0:
        raise ValueError(f"step must be positive, got {step}")
    if north < south or east < west:
        raise ValueError(f"invalid bbox: ({south}, {west}, {north}, {east})")

    ix_min = _floor_idx(west, step)
    iy_min = _floor_idx(south, step)
    ix_max = _ceil_idx(east, step) - 1
    iy_max = _ceil_idx(north, step) - 1

    return ix_min, iy_min, ix_max, iy_max


def tile_bbox(ix: int, iy: int,
              step: float = DEFAULT_TILE_STEP) -> Tuple[float, float, float, float]:
    """瓦片索引 → 瓦片 bbox (south, west, north, east)。"""
    return iy * step, ix * step, (iy + 1) * step, (ix + 1) * step


def tile_key(ix: int, iy: int) -> str:
    """瓦片索引 → 缓存文件名主干（如 '2402_604'）。"""
    return f"{ix}_{iy}"


def bbox_str(south: float, west: float, north: float, east: float) -> str:
    """bbox → 缓存文件名用的坐标串（4 位小数，与现有 osmium 缓存命名一致）。"""
    return f"{south:.4f}_{west:.4f}_{north:.4f}_{east:.4f}"
