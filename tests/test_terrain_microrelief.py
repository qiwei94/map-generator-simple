"""Sub-layer Z must survive the production terrain materializer."""
from dataclasses import replace

import numpy as np
import pytest

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import (
    resolve_terrain_surface_plan, materialize_terrain_surface_plan,
    verify_terrain_microrelief,
)


def microrelief_plan():
    y, x = np.mgrid[0:25, 0:33]
    source = 20 + 2 * np.sin(x / 4) * np.cos(y / 5)
    plan = resolve_terrain_surface_plan(
        source, width_m=320, height_m=240, scale_mm_per_m=.025,
        max_surface_edge_mm=.5,
    )
    y, x = np.indices(plan.surface_z_grid_mm.shape)
    wave = np.sin(x / 4) * np.cos(y / 5)
    z = 1.22 + (wave - wave.min()) / np.ptp(wave) * .07
    z.setflags(write=False)
    # Controlled fixture: test S8 preservation, not inventing source elevations.
    return replace(plan, surface_z_grid_mm=z)


def test_production_mesh_preserves_seventy_micron_relief():
    plan = microrelief_plan()
    solid = materialize_terrain_surface_plan(plan)
    report = solid.metadata['terrain_evidence']['microrelief_preservation']
    assert solid.is_watertight
    assert report['planned_peak_to_valley_mm'] == pytest.approx(.07)
    assert report['max_vertex_deviation_mm'] < 1e-6
    top = solid.vertices[solid.vertices[:, 2] > plan.terrain_base_z_mm + .01, 2]
    assert np.ptp(top) == pytest.approx(.07)
    assert len(np.unique(top)) > 20


def test_layer_height_rounding_cannot_silently_flatten_terrain():
    plan = microrelief_plan()
    solid = materialize_terrain_surface_plan(plan)
    top = solid.vertices[:, 2] > plan.terrain_base_z_mm + .01
    solid.vertices[top, 2] = np.round(solid.vertices[top, 2] / .12) * .12
    with pytest.raises(ValueError, match='microrelief lost'):
        verify_terrain_microrelief(plan, solid)
