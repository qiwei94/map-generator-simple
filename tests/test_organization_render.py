from types import SimpleNamespace

from tools.evaluate_urban_organization import base_thickness_from_plan


def test_minimum_slab_roundtrip_does_not_fall_below_water_lower_bound():
    assert base_thickness_from_plan(SimpleNamespace(terrain_base_z_mm=-1.6)) == 0.4


def test_slab_roundtrip_preserves_other_thicknesses():
    assert base_thickness_from_plan(SimpleNamespace(terrain_base_z_mm=-0.8)) == 1.2
    assert base_thickness_from_plan(SimpleNamespace(terrain_base_z_mm=-1.7)) == 0.3
