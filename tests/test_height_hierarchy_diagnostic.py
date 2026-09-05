import numpy as np

from tools.generate_height_hierarchy_diagnostic import _top_surface_grid


def test_top_surface_grid_ignores_watertight_base_vertices():
    vertices = np.array([
        [0, 0, -2], [1, 0, -2], [0, 1, -2], [1, 1, -2],
        [0, 0, 0], [1, 0, 0.5], [0, 1, 1], [1, 1, 2],
    ], dtype=float)
    xx, yy, zz = _top_surface_grid(vertices, (2, 2))

    assert xx.shape == yy.shape == zz.shape == (2, 2)
    assert zz.tolist() == [[0.0, 0.5], [1.0, 2.0]]
