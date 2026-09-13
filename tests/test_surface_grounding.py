from types import SimpleNamespace
import numpy as np
import pytest
from shapely.geometry import Polygon, box

from aesthetic.surface_grounding import resolve_grounding, materialize_grounding


def test_bad_earcut_result_uses_clipped_cells_not_filled_holes(monkeypatch):
    import trimesh
    from shapely.ops import unary_union
    from aesthetic.surface_grounding import _triangulate
    poly = Polygon([(0,0),(3,0),(3,3),(0,3)],
                   holes=[[(1,1),(1,2),(2,2),(2,1)]])
    original = trimesh.creation.triangulate_polygon
    def faulty(p, **kwargs):
        if p.equals(poly):
            return np.array([[0,0],[3,0],[3,3],[0,3]]), np.array([[0,1,2],[0,2,3]])
        return original(p, **kwargs)
    monkeypatch.setattr(trimesh.creation, 'triangulate_polygon', faulty)
    actual = unary_union([Polygon(t) for t in _triangulate(poly)])
    assert actual.equals(poly)


def test_collinear_terrain_seam_vertices_are_preserved():
    from aesthetic.surface_grounding import _conform_edges
    xy, faces = _conform_edges(np.array([[0,0],[2,0],[0,2],[1,1],[2,2]], dtype=float),
                              np.array([[0,1,2],[1,4,3],[3,4,2]]))
    edges = np.concatenate([faces[:,[0,1]],faces[:,[1,2]],faces[:,[2,0]]])
    _, inv, count = np.unique(np.sort(edges,axis=1),axis=0,return_inverse=True,return_counts=True)
    from shapely.geometry import LineString
    from shapely.ops import unary_union
    boundary = unary_union([LineString(xy[e]) for e in edges[count[inv]==1]])
    assert boundary.equals(box(0,0,2,2).boundary)


def terrain(grid):
    return SimpleNamespace(surface_z_grid_mm=np.asarray(grid, dtype=float),
        width_m=4., height_m=4., scale_mm_per_m=1., fingerprint='fixture')


def test_hole_touching_exterior_keeps_shape_and_extrudes_closed():
    poly = Polygon([(-2,-2),(2,-2),(2,2),(-2,2)],
                   holes=[[(-2,0),(-1,-1),(0,0),(-1,1)]])
    assert poly.is_valid
    t = terrain([[0,1,0],[1,0,1],[0,1,0]])
    plan = resolve_grounding([poly], [.24], 1., t, ['draped_thickness'])
    mesh, proof = materialize_grounding(plan, [poly], [.24], 1.)
    assert mesh.is_watertight and mesh.is_winding_consistent
    assert mesh.volume == pytest.approx(poly.area * .24)


@pytest.mark.parametrize('mode', ['draped_thickness', 'flat_roof_above_highest_support'])
@pytest.mark.parametrize('grid', [[[1, 1, 1], [1, 1, 1], [1, 1, 1]],
    [[0, 1, 2], [0, 1, 2], [0, 1, 2]], [[0, 1, 0], [2, 3, 1], [0, 1, 2]]])
@pytest.mark.parametrize('poly', [box(-1.7, -1.5, 1.5, 1.7),
    Polygon([(-2,-2),(2,-2),(2,2),(-2,2)], holes=[[(-.5,-.5),(-.5,.5),(.5,.5),(.5,-.5)]]),
    Polygon([(-1.8,-1.8),(1.6,-1.6),(.2,.3),(1.6,1.8),(-1.8,1.7)])])
def test_exact_grounding_on_slopes_holes_and_peaks(poly, grid, mode):
    from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import sample_terrain_surface_plan_z
    t = terrain(grid)
    plan = resolve_grounding([poly], [.24], 1., t, [mode])
    mesh, proof = materialize_grounding(plan, [poly], [.24], 1.)
    assert mesh.is_watertight and mesh.is_winding_consistent
    p = plan['patches'][0]
    # Face interiors, not just grid vertices: no triangle may cut a hill.
    xy = p['xy'][p['faces']].mean(axis=1)
    expected = sample_terrain_surface_plan_z(t, xy[:, 0], xy[:, 1])
    np.testing.assert_allclose(p['bottom_z'][p['faces']].mean(axis=1), expected, atol=1e-9)
    if mode == 'draped_thickness':
        np.testing.assert_allclose(p['top_z'] - p['bottom_z'], .24)
    else:
        assert np.ptp(p['top_z']) == 0
        assert np.min(p['top_z'] - p['bottom_z']) == pytest.approx(.24)
    assert proof['whole_footprint_grounding'] == 'verified_against_frozen_patches'
    assert proof['final_water_boolean_contact'] == 'not_verified'


def test_reject_stale_plan_and_outside_terrain():
    poly, t = box(-1,-1,1,1), terrain([[0, 1], [1, 0]])
    plan = resolve_grounding([poly], [.24], 1., t, ['draped_thickness'])
    with pytest.raises(ValueError):
        plan['patches'][0]['top_z'][0] += 1
    plan['patches'][0]['top_z'] = plan['patches'][0]['top_z'].copy() + 1
    with pytest.raises(ValueError, match='changed'):
        materialize_grounding(plan, [poly], [.24], 1.)
    with pytest.raises(ValueError, match='outside'):
        resolve_grounding([box(-3,-1,1,1)], [.24], 1., t, ['draped_thickness'])


def test_integrated_grounding_cannot_fall_back_or_use_another_terrain():
    from _TEXTURE_STYLE_OF_DEEPSEEK._layer_preprocess import LayerPolygons
    from _TEXTURE_STYLE_OF_DEEPSEEK.print_profile import DEFAULT_PRINTER_PROFILE
    from _TEXTURE_STYLE_OF_DEEPSEEK.block_base import build_deepseek_block_base_v3
    from aesthetic.city_surface_plan import finalize_city_surfaces, materialize_city_role, surface_heights
    layers = LayerPolygons(BO=[box(-1,-1,1,1)], BO_heights=[.4])
    evidence = finalize_city_surfaces(layers, bbox_local=(-2,-2,2,2), scale=1.,
        printer_profile=DEFAULT_PRINTER_PROFILE, terrain_surface_plan=terrain([[1,2],[2,1]]))
    with pytest.raises(ValueError, match='fall back'):
        build_deepseek_block_base_v3(layers.BO, None, 1., brick_style=False,
            prepared_surface_evidence=evidence, polygon_thicknesses_mm=surface_heights(layers))
    with pytest.raises(ValueError, match='same frozen terrain'):
        materialize_city_role(layers, 'city', 1., lambda x,y: np.zeros_like(x))
