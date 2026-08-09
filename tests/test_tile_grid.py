"""_tile_grid 单元测试：量化边界、跨 0°、负坐标、网格线恰合。"""

import math

import pytest

from _TEXTURE_STYLE_OF_DEEPSEEK._tile_grid import (
    DEFAULT_TILE_STEP,
    bbox_str,
    snap_bbox,
    tile_bbox,
    tile_key,
    tile_range,
)


def test_snap_contains_original():
    """量化框必须包含原框（网格线容差内允许亚毫米级偏差）。"""
    from _TEXTURE_STYLE_OF_DEEPSEEK._tile_grid import _LINE_TOL
    cases = [
        (30.22, 120.10, 30.29, 120.17),
        (30.2000001, 120.0500001, 30.2499999, 120.0999999),
        (-33.87, 151.19, -33.85, 151.22),   # 负纬度
        (48.85, -0.13, 48.87, 2.36),        # 跨 0° 经度（伦敦-巴黎尺度）
        (0.01, 0.01, 0.02, 0.02),           # 贴近原点
    ]
    for s, w, n, e in cases:
        fs, fw, fn, fe = snap_bbox(s, w, n, e)
        assert fs <= s + _LINE_TOL and fw <= w + _LINE_TOL
        assert fn >= n - _LINE_TOL and fe >= e - _LINE_TOL
        # 量化到网格倍数
        step = DEFAULT_TILE_STEP
        for v in (fs, fw, fn, fe):
            assert abs(v / step - round(v / step)) < 1e-6


def test_snap_shifted_queries_share_key():
    """核心场景：稍有偏移的两个框量化结果相同 → 缓存可复用。"""
    a = snap_bbox(30.22, 120.10, 30.29, 120.17)
    b = snap_bbox(30.23, 120.11, 30.30, 120.18)   # 偏移 ~1km
    assert a == b


def test_snap_on_grid_line():
    """边界恰在网格线上时不外扩。"""
    step = DEFAULT_TILE_STEP
    fs, fw, fn, fe = snap_bbox(step, 2 * step, 3 * step, 4 * step)
    assert fs == pytest.approx(step)
    assert fw == pytest.approx(2 * step)
    assert fn == pytest.approx(3 * step)
    assert fe == pytest.approx(4 * step)


def test_snap_negative_coords():
    fs, fw, fn, fe = snap_bbox(-33.87, 151.19, -33.85, 151.22)
    assert fs == pytest.approx(math.floor(-33.87 / 0.05) * 0.05, abs=1e-9)
    assert fn >= -33.85


def test_snap_invalid():
    with pytest.raises(ValueError):
        snap_bbox(30.3, 120.0, 30.2, 120.1)   # north < south
    with pytest.raises(ValueError):
        snap_bbox(30.2, 120.0, 30.3, 120.1, step=0)


def test_tile_range_covers_bbox():
    """tile_range 覆盖的瓦片并集必须包含原框。"""
    eps = 1e-9
    s, w, n, e = 30.22, 120.10, 30.29, 120.17
    ix0, iy0, ix1, iy1 = tile_range(s, w, n, e)
    assert ix0 * 0.05 <= w + eps and iy0 * 0.05 <= s + eps
    assert (ix1 + 1) * 0.05 >= e - eps and (iy1 + 1) * 0.05 >= n - eps


def test_tile_range_on_grid_line():
    """恰在网格线上时不多不少。"""
    step = DEFAULT_TILE_STEP
    ix0, iy0, ix1, iy1 = tile_range(0.0, 0.0, step, step)
    assert (ix0, iy0, ix1, iy1) == (0, 0, 0, 0)
    ix0, iy0, ix1, iy1 = tile_range(0.0, 0.0, step, 2 * step)  # east=2*step
    assert (ix0, iy0, ix1, iy1) == (0, 0, 1, 0)


def test_tile_range_negative():
    ix0, iy0, ix1, iy1 = tile_range(-0.07, -0.03, -0.01, 0.02)
    # 覆盖 -0.07..0.02：ix 从 floor(-0.03/0.05)=-1 到 floor(0.02/0.05)=0
    assert ix0 == -1 and ix1 == 0
    assert iy0 == math.floor(-0.07 / 0.05) and iy1 == math.floor(-0.01 / 0.05)


def test_tile_bbox_roundtrip():
    for ix, iy in [(2403, 604), (-1, -2), (0, 0)]:
        s, w, n, e = tile_bbox(ix, iy)
        ix0, iy0, ix1, iy1 = tile_range(s + 1e-9, w + 1e-9, n - 1e-9, e - 1e-9)
        assert (ix0, iy0, ix1, iy1) == (ix, iy, ix, iy)


def test_tile_key_and_bbox_str():
    assert tile_key(2403, 604) == "2403_604"
    assert bbox_str(30.2, 120.1, 30.3, 120.2) == "30.2000_120.1000_30.3000_120.2000"
