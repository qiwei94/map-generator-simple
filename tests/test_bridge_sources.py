import copy

import pytest
from shapely.geometry import LineString, MultiLineString, box
from shapely.ops import unary_union

from aesthetic.bridge_sources import extract_bridge_sources, water_bridge_lines
from aesthetic.city_surface_plan import finalize_city_surfaces, verify_surface_plan
from _TEXTURE_STYLE_OF_DEEPSEEK._layer_preprocess import LayerPolygons
from _TEXTURE_STYLE_OF_DEEPSEEK.print_profile import DEFAULT_PRINTER_PROFILE as PROFILE


def _row(**kwargs):
    return dict(geometry=LineString([(-10, 0), (10, 0)]), highway='primary',
                bridge='yes', name='Pont Neuf', **kwargs)


@pytest.mark.parametrize('name,highway', [('Pont Neuf', 'primary'),
    (None, 'footway'), ('Pont des Arts', 'pedestrian'), ('无名', 'cycleway')])
def test_bridge_truth_does_not_depend_on_name_or_foreground_class(name, highway):
    row = _row(); row.update(name=name, highway=highway)
    lines, evidence = extract_bridge_sources([row])
    assert len(lines) == 1
    assert evidence['invented_connectors'] == 0


@pytest.mark.parametrize('change', [{'bridge': 'no'}, {'bridge': None}, {'bridge': float('nan')},
    {'bridge': 'false'}, {'bridge': 'unknown'}, {'tunnel': 'yes'}, {'layer': -1},
    {'highway': 'construction'}])
def test_nonbridges_and_conflicting_sources_are_not_promoted(change):
    row = _row(); row.update(change)
    assert extract_bridge_sources([row])[0] == []


def test_short_parts_reversed_duplicates_and_covered_bridge():
    row = _row(covered='yes'); row['geometry'] = LineString([(0, 0), (2, 0)])
    other = dict(row, geometry=LineString([(2, 0), (0, 0)]))
    lines, _ = extract_bridge_sources([row, other])
    assert len(lines) == 1 and lines[0].length == 2


def test_tight_hairpin_bridge_buffer_covers_its_own_source():
    from aesthetic.bridge_sources import bridge_corridor
    # Actual Westlake source shape, shifted close to the local origin.
    line = LineString([(4.0118955, 10.0549309), (.0983539, 10.2860329),
                       (.6303930, .7643191), (39.0971456, 2.1314454)])
    corridor = bridge_corridor([line], .0078, .42, box(-100,-100,100,100))
    assert line.difference(corridor.buffer(1e-7)).length < 1e-7


def test_straight_bridge_retains_flat_end_caps_and_width():
    from aesthetic.bridge_sources import bridge_corridor
    line = LineString([(0,0), (100,0)])
    corridor = bridge_corridor([line], 1., 2., box(-200,-200,200,200))
    assert corridor.equals(box(0,-1,100,1))


def test_dataframe_stream_and_frozen_context_preserve_physical_bridges():
    import geopandas as gpd
    from aesthetic.pipeline_domain import freeze_layer_containers, thaw_layer_containers
    lines, evidence = extract_bridge_sources(gpd.GeoDataFrame([_row()], geometry='geometry'))
    layers = LayerPolygons(bridge_lines=lines, bridge_source_evidence=evidence)
    restored = thaw_layer_containers(freeze_layer_containers(layers))
    assert restored.bridge_lines[0].equals(lines[0])
    assert restored.bridge_source_evidence == evidence


def _layers():
    return LayerPolygons(WL=[box(-4, -20, 4, 20)],
        BO=[box(-20, -20, -4, 20), box(4, -20, 20, 20)], BO_heights=[.84, .84])


def _finalize(layers):
    return finalize_city_surfaces(layers, bbox_local=(-20, -20, 20, 20),
                                  scale=1., printer_profile=PROFILE)


def test_source_bridge_survives_empty_foreground_and_cuts_bank_masses(tmp_path):
    from aesthetic.review_render import render_review_bundle
    from PIL import Image
    layers = _layers()
    layers.bridge_lines, layers.bridge_source_evidence = extract_bridge_sources([_row()])
    assert layers.roads_lines == []
    plan = _finalize(layers)
    road = unary_union(layers.surface_road_polygons)
    assert layers.bridge_lines[0].difference(road.buffer(1e-8)).length < 1e-6
    assert road.intersection(unary_union(layers.BO)).area < 1e-8
    assert plan['road_surface_plan']['bridge_source_line_parts'] == 1
    bundle = render_review_bundle(layers, {'bbox_local': (-20,-20,20,20), 'scale': 1.},
                                  1., str(tmp_path), 'bridge')
    im = Image.open(tmp_path / 'bridge_topdown.png').convert('RGB')
    assert sum(im.getpixel((im.width//2, im.height//2))) > 100  # actual bridge face
    assert sum(im.getpixel((im.width//2, im.height//4))) == 0  # river remains black
    assert verify_surface_plan(layers, 1.) == plan


def test_untagged_crossing_or_old_landmark_flag_cannot_cross_water():
    layers = _layers()
    line = _row()['geometry']
    layers.block_base_cut_lines = [line]
    layers.roads_lines = [(line, 'primary', True)]
    _finalize(layers)
    assert unary_union(layers.surface_road_polygons).intersection(layers.WL[0]).area < 1e-8


def test_non_water_viaduct_not_added_and_bridge_frame_transforms():
    from generate_city_legacy import _transform_layers_to_exact
    layers = _layers()
    layers.bridge_lines = [_row()['geometry'], LineString([(-15, 8), (-10, 8)])]
    assert len(water_bridge_lines(layers)) == 1
    original = copy.deepcopy(layers)
    moved = _transform_layers_to_exact(layers, 100, 100, (80,80,120,120))
    assert moved.bridge_lines[0].bounds == (90,100,110,100)
    assert original.bridge_lines[0].bounds == (-10,0,10,0)
    assert len(water_bridge_lines(moved)) == 1
