import numpy as np
import pytest
from _TEXTURE_STYLE_OF_DEEPSEEK.print_profile import DEFAULT_PRINTER_PROFILE as PROFILE
from aesthetic.road_width_contract import resolve_road_width_contract
from tools.verify_road_width_slicing import parse_paths


def test_visual_presets_do_not_redefine_physical_printer():
    before=PROFILE.to_dict()
    widths, contract=resolve_road_width_contract(PROFILE,'negative-space-v1')
    assert widths.surface_road_gap_mm==.28
    assert widths.final_block_base_gap_mm==.42
    assert PROFILE.to_dict()==before
    assert contract['negative_gap']['minimum_is_nozzle_width'] is False
    assert contract['physical_acceptance']=='pending'
    assert contract['multi_material']['conservative_colored_strip_mm']==.63
    assert contract['role_mapping']['local']=='negative_gap'
    assert contract['role_mapping']['bridge']=='positive_strip'


def test_default_widths_stay_compatible():
    widths, contract=resolve_road_width_contract(PROFILE,'printer-default')
    assert widths.surface_road_gap_mm==PROFILE.surface_road_gap_mm
    assert widths.final_block_base_gap_mm==PROFILE.final_block_base_gap_mm
    assert contract['physical_acceptance']=='pending'
    with pytest.raises(ValueError):resolve_road_width_contract(PROFILE,'unknown')


def test_parse_excludes_travel_retraction_and_custom_paths():
    paths=parse_paths('''G90
M83
; Z_HEIGHT: 1.28
; FEATURE: Outer wall
; LINE_WIDTH: 0.42
G1 X1 Y2
G1 X1 Y8 E.4
G1 X1 Y7 E-.2
G1 E.2
; FEATURE: Custom
G1 X8 Y8 E1
''')
    np.testing.assert_allclose(paths,[[1,2,1,8,1.28,.42]])


def test_parse_absolute_extrusion_reset():
    paths=parse_paths('''G90
M82
G92 E0
; Z_HEIGHT: 1.28
; FEATURE: Outer wall
G1 X1 Y1
G1 X2 E.1
G1 X3 E.1
G92 E0
G1 X4 E.1
''')
    assert len(paths)==2
    assert paths[1,0]==3


def test_slice_analysis_refuses_unhandled_model_arcs():
    with pytest.raises(ValueError,match='arc'):
        parse_paths('; Z_HEIGHT: 1.28\n; FEATURE: Outer wall\nG2 X2 Y2 E.1')
