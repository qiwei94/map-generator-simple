"""Z-displacement functions for block_base surface texture.

Each function takes (x, y, amp) where x/y are numpy arrays in mm coordinates,
and returns z-displacement in mm.
"""

from __future__ import annotations

import numpy as np
from scipy.spatial import cKDTree

# ---------------------------------------------------------------------------
# Vectorized noise (replaces per-point opensimplex calls)
# ---------------------------------------------------------------------------

_SEEDS = {"default": 2026, "s2": 7777, "s3": 1234}


def _hash_noise_2d(xs, ys, seed=2026):
    """Fast vectorized 2D value noise via integer hashing."""
    xi = np.floor(xs).astype(np.int64)
    yi = np.floor(ys).astype(np.int64)
    xf = xs - xi
    yf = ys - yi
    u = xf * xf * (3 - 2 * xf)
    v = yf * yf * (3 - 2 * yf)

    def _h(x, y):
        n = x * np.int64(374761393) + y * np.int64(668265263) + np.int64(seed)
        n = (n ^ (n >> 13)) * np.int64(1274126177)
        n = n ^ (n >> 16)
        return (n & np.int64(0x7fffffff)).astype(np.float64) / 0x7fffffff * 2 - 1

    n00 = _h(xi, yi)
    n10 = _h(xi + 1, yi)
    n01 = _h(xi, yi + 1)
    n11 = _h(xi + 1, yi + 1)
    return n00 * (1 - u) * (1 - v) + n10 * u * (1 - v) + n01 * (1 - u) * v + n11 * u * v


def _simplex_field(xs, ys, freq, gen=None):
    seed = _SEEDS.get(gen, 2026) if isinstance(gen, str) else 2026
    return _hash_noise_2d(xs * freq, ys * freq, seed=seed)


def _fbm(xs, ys, octaves=6, persistence=0.5, lacunarity=2.0, base_freq=0.1, gen=None):
    result = np.zeros(len(xs))
    amp = 1.0
    freq = base_freq
    seed = _SEEDS.get(gen, 2026) if isinstance(gen, str) else 2026
    for _ in range(octaves):
        result += amp * _hash_noise_2d(xs * freq, ys * freq, seed=seed)
        freq *= lacunarity
        amp *= persistence
    return result


def _voronoi_f1(xs, ys, cell_size=2.0, seed=42):
    rng = np.random.default_rng(seed)
    x_min, x_max = xs.min() - cell_size, xs.max() + cell_size
    y_min, y_max = ys.min() - cell_size, ys.max() + cell_size
    cols = int((x_max - x_min) / cell_size) + 2
    rows = int((y_max - y_min) / cell_size) + 2
    gx = np.linspace(x_min, x_max, cols)
    gy = np.linspace(y_min, y_max, rows)
    grid_x, grid_y = np.meshgrid(gx, gy)
    offset_x = (rng.random(grid_x.shape) - 0.5) * cell_size * 0.8
    offset_y = (rng.random(grid_y.shape) - 0.5) * cell_size * 0.8
    points = np.column_stack([(grid_x + offset_x).ravel(),
                              (grid_y + offset_y).ravel()])
    tree = cKDTree(points)
    query = np.column_stack([xs, ys])
    dist, _ = tree.query(query, k=1)
    return np.clip(dist / (cell_size * 0.7), 0, 1)


# ---------------------------------------------------------------------------
# Displacement functions
# ---------------------------------------------------------------------------

def disp_residential(x, y, amp=0.15):
    return _fbm(x, y, octaves=4, base_freq=0.8, persistence=0.6) * amp


def disp_commercial(x, y, amp=0.12):
    gx = np.sin(x / 1.2 * 2 * np.pi)
    gy = np.sin(y / 1.2 * 2 * np.pi)
    grid = np.minimum(gx, gy) * 0.5 + 0.5
    noise = _simplex_field(x, y, 0.3) * 0.2
    return (grid + noise) * amp


def disp_industrial(x, y, amp=0.10):
    bands = np.sin(x / 0.8 * 2 * np.pi) * 0.5 + 0.5
    noise = _simplex_field(x, y, 0.15, gen="s2") * 0.15
    return (bands + noise) * amp


def disp_farmland(x, y, amp=0.25):
    distort = _simplex_field(x, y, 0.2, gen="s2") * 0.8
    phase = y / 1.5 + distort
    waves = np.sin(phase * 2 * np.pi) * 0.5 + 0.5
    return waves * amp


def disp_forest(x, y, amp=0.50):
    vor = _voronoi_f1(x, y, cell_size=2.5, seed=42)
    fbm_val = _fbm(x, y, octaves=5, base_freq=0.15, persistence=0.55)
    detail = _simplex_field(x, y, 0.6, gen="s3") * 0.15
    return (vor * 0.6 + fbm_val * 0.3 + detail) * amp


def disp_water(x, y, amp=0.15):
    centers = [(3.0, 3.0), (7.0, 6.0), (5.0, 8.0)]
    result = np.zeros(len(x))
    for cx, cy in centers:
        dist = np.sqrt((x - cx)**2 + (y - cy)**2)
        wave = np.sin(dist / 1.0 * 2 * np.pi)
        decay = np.exp(-0.15 * dist)
        result += wave * decay
    result = result / len(centers)
    base = _simplex_field(x, y, 0.05) * 0.1
    return (result * 0.5 + 0.5 + base) * amp


def disp_unclassified(x, y, amp=0.08):
    return _fbm(x, y, octaves=3, base_freq=0.2, persistence=0.4) * amp


def disp_veg_landmark(x, y, amp=0.50):
    vor = _voronoi_f1(x, y, cell_size=2.0, seed=99)
    base = _fbm(x, y, octaves=4, base_freq=0.08, persistence=0.5, gen="s2")
    detail = _simplex_field(x, y, 0.4, gen="s3") * 0.2
    return (vor * 0.55 + base * 0.35 + detail) * amp


def disp_veg_ordinary(x, y, amp=0.15):
    fine = _fbm(x, y, octaves=5, base_freq=0.6, persistence=0.5, gen="s3")
    base = _simplex_field(x, y, 0.08, gen="s2") * 0.3
    return (fine * 0.7 + base) * amp


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

DISPLACEMENT_FUNCS = {
    "residential": disp_residential,
    "commercial": disp_commercial,
    "industrial": disp_industrial,
    "farmland": disp_farmland,
    "forest": disp_forest,
    "water": disp_water,
    "unclassified": disp_unclassified,
    "veg_landmark": disp_veg_landmark,
    "veg_ordinary": disp_veg_ordinary,
}

DEFAULT_AMPS = {
    "residential": 0.15,
    "commercial": 0.12,
    "industrial": 0.10,
    "farmland": 0.25,
    "forest": 0.50,
    "water": 0.15,
    "unclassified": 0.08,
    "veg_landmark": 0.50,
    "veg_ordinary": 0.15,
}

BLOCK_CLASS_TO_DISP = {
    "residential": "residential",
    "commercial": "commercial",
    "industrial": "industrial",
    "farmland": "farmland",
    "forest": "forest",
    "water_adjacent": "water",
    "unclassified": "unclassified",
}


def get_displacement(region: str, x, y, amp_scale: float = 2.0):
    """Compute z-displacement for a region at given mm coordinates."""
    disp_key = BLOCK_CLASS_TO_DISP.get(region, region)
    func = DISPLACEMENT_FUNCS.get(disp_key)
    if func is None:
        func = DISPLACEMENT_FUNCS["unclassified"]
        disp_key = "unclassified"
    amp = DEFAULT_AMPS[disp_key] * amp_scale
    return func(x, y, amp=amp)
