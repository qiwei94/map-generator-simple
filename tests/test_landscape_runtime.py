from types import SimpleNamespace

from aesthetic.landscape_runtime import is_active_landscape, suppress_urban_fill


def test_landscape_activation_requires_active_policy():
    for activation in ('active', 'audit_only'):
        assert is_active_landscape({'activation': activation,
            'landscape_strategy': {'enabled': True}}) == (activation == 'active')
    assert not is_active_landscape({'activation': 'active'})


def test_landscape_removes_city_fill_and_preserves_natural_and_source_features():
    layers = SimpleNamespace(block_base=['block'], block_base_classes=['urban'],
        BO=['mass'], BO_heights=[1], BL=['landmark'], WL=['lake'],
        VO=['grass'], roads_lines=['road'])
    evidence = suppress_urban_fill(layers)
    assert evidence['removed_urban_components'] == 2
    assert layers.block_base == layers.block_base_classes == layers.BO == layers.BO_heights == []
    assert (layers.BL, layers.WL, layers.VO, layers.roads_lines) == (
        ['landmark'], ['lake'], ['grass'], ['road'])
    assert suppress_urban_fill(layers)['removed_urban_components'] == 0


def test_empty_landscape_can_freeze_surface_plan():
    import numpy as np
    from _TEXTURE_STYLE_OF_DEEPSEEK._layer_preprocess import LayerPolygons
    from _TEXTURE_STYLE_OF_DEEPSEEK.print_profile import DEFAULT_PRINTER_PROFILE
    from aesthetic.city_surface_plan import finalize_city_surfaces
    layers = LayerPolygons()
    terrain = SimpleNamespace(fingerprint='test', surface_z_grid_mm=np.zeros((3, 3)),
        width_m=1000., height_m=800., scale_mm_per_m=.2)
    result = finalize_city_surfaces(layers, bbox_local=(0, 0, 1000, 800),
        scale=.2, printer_profile=DEFAULT_PRINTER_PROFILE, terrain_surface_plan=terrain)
    assert result['status'] == 'finalized'
    assert layers.surface_grounding['city']['patches'] == []


def test_grounding_digest_preserves_frozen_evidence_and_detects_changes():
    from types import MappingProxyType
    from aesthetic.surface_grounding import grounding_digest
    plan = dict(version='test', terrain_fingerprint='terrain',
        input_geometry_fingerprint='geometry', patches=[],
        support_evidence=[{'bank': {'height': 2.0}}])
    expected = grounding_digest(plan)
    plan['support_evidence'] = (MappingProxyType({
        'bank': MappingProxyType({'height': 2.0})}),)
    assert grounding_digest(plan) == expected
    plan['support_evidence'] = [{'bank': {'height': 3.0}}]
    assert grounding_digest(plan) != expected


def test_building_omission_requires_active_landscape_and_keeps_water_gate():
    import pytest
    from aesthetic.pipeline_gates import evaluate_feature_survival
    policy = {'activation': 'active', 'landscape_strategy': {'enabled': True}}
    result = evaluate_feature_survival({'buildings': 431, 'water': 1}, {'WL': 1},
        intentional_omissions=['buildings'], scene_policy=policy)
    assert result['passed']
    assert result['roles']['buildings']['status'] == 'intentionally_omitted'
    assert not evaluate_feature_survival({'water': 1}, {}, scene_policy=policy)['passed']
    with pytest.raises(ValueError):
        evaluate_feature_survival({'buildings': 431}, {},
            intentional_omissions=['buildings'], scene_policy={**policy, 'activation': 'audit_only'})
