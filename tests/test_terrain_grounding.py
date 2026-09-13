"""Z sampling must follow real terrain triangles, not their nearby maxima."""
from types import SimpleNamespace

import numpy as np
import pytest

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import sample_terrain_surface_plan_z
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.terrain import build_terrain_mesh


def plan(grid):
    return SimpleNamespace(surface_z_grid_mm=np.asarray(grid, dtype=float),
                           width_m=2., height_m=2., scale_mm_per_m=1.)


def test_sampler_matches_actual_triangle_barycentres():
    grid = np.array([[1., 2., 4.], [4., 1., 8.], [2., 6., 3.]])
    mesh = build_terrain_mesh(grid, 2., 2.)
    # Independent reference: interpolate within every generated mesh face.
    for weights in ([1/3, 1/3, 1/3], [.05, .8, .15], [.6, .1, .3]):
        pts = np.einsum('ijk,j->ik', mesh.triangles, weights)
        np.testing.assert_allclose(sample_terrain_surface_plan_z(plan(grid),
                                   pts[:, 0], pts[:, 1]), pts[:, 2], atol=1e-12)


def test_saddle_uses_triangle_not_bilinear_or_neighbour_max():
    p = plan([[0., 0.], [0., 4.]])
    assert sample_terrain_surface_plan_z(p, np.array([0.]), np.array([0.]))[0] == 0.
    assert sample_terrain_surface_plan_z(p, np.array([.5]), np.array([.5]))[0] == 2.


def test_flat_slope_vertices_boundary_and_shapes():
    yy, xx = np.mgrid[-1:1:5j, -1:1:5j]
    for grid in (np.full((5, 5), 3.), 3 + xx + 2 * yy):
        np.testing.assert_allclose(sample_terrain_surface_plan_z(plan(grid), xx, yy), grid)
    xs, ys = np.array([-.75, .125, 1.]), np.array([-.8, .4, 1.])
    np.testing.assert_allclose(sample_terrain_surface_plan_z(plan(3 + xx + 2 * yy), xs, ys),
                               3 + xs + 2 * ys)


@pytest.mark.parametrize('xs,ys', [([1.01], [0.]), ([0.], [-1.01]),
                                  ([np.nan], [0.]), ([0., 1.], [0.])])
def test_invalid_or_outside_samples_do_not_invent_heights(xs, ys):
    with pytest.raises(ValueError):
        sample_terrain_surface_plan_z(plan([[0., 0.], [0., 0.]]), xs, ys)


def test_correct_centroid_alone_still_does_not_ground_a_slope():
    from shapely.geometry import box
    from _TEXTURE_STYLE_OF_DEEPSEEK.prepared_surface import materialize_flat_surfaces
    p = plan([[0., 2.], [0., 2.]])
    sampler = lambda x, y: sample_terrain_surface_plan_z(p, x, y)
    mesh, proof = materialize_flat_surfaces([box(-.5, -.5, .5, .5)], [.24], 1., sampler)
    # Keep this explicit regression documenting the remaining *whole footprint*
    # defect. Closed/footprint-verified is not a grounding acceptance certificate.
    assert proof['z_policy'] == 'shared_centroid_surface_sample'
    assert mesh.bounds[0, 2] - sampler(np.array([-.5]), np.array([0.]))[0] == .5
    assert mesh.bounds[1, 2] < sampler(np.array([.5]), np.array([0.]))[0]
