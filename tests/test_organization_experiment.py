import geopandas as gpd
import pytest
from types import SimpleNamespace
from shapely.geometry import box, GeometryCollection
from shapely.ops import unary_union
from _TEXTURE_STYLE_OF_DEEPSEEK.print_profile import PrinterProfile
from aesthetic.organization_experiment import organize_block, protected_open_spaces, apply_organization

PROFILE = PrinterProfile()


def test_candidates_are_distinct_and_deterministic():
    seeds = [box(1,1,3,4), box(3.3,1,5.3,4)]
    b, _ = organize_block(seeds, box(0,0,10,10), GeometryCollection(), variant='B', scale=1., profile=PROFILE)
    c, _ = organize_block(seeds, box(0,0,10,10), GeometryCollection(), variant='C', scale=1., profile=PROFILE)
    again, _ = organize_block(seeds, box(0,0,10,10), GeometryCollection(), variant='C', scale=1., profile=PROFILE)
    assert unary_union(c).area > unary_union(b).area
    assert [p.wkb for p in c] == [p.wkb for p in again]


@pytest.mark.parametrize('variant', ['B', 'C'])
def test_protected_space_and_block_boundaries_never_filled(variant):
    block = box(0,0,12,12)
    courtyard = box(4,4,8,8)
    seeds = [box(1,1,11,3), box(1,8.3,11,11), box(1,1,3,11)]
    result, evidence = organize_block(seeds, block, courtyard, variant=variant, scale=1., profile=PROFILE)
    union = unary_union(result)
    assert union.intersection(courtyard).area < 1e-9
    assert union.difference(block).area < 1e-9
    assert evidence['source_area_m2'] > 0


def test_c_does_not_fill_unsupported_district_or_courtyard():
    seeds = [box(1,1,2,2)]
    result, _ = organize_block(seeds, box(0,0,100,100), GeometryCollection(), variant='C', scale=1., profile=PROFILE)
    union = unary_union(result)
    support = unary_union(seeds).buffer(PROFILE.min_colored_strip_mm/2, join_style=2)
    assert union.difference(support).area < 1e-9
    assert union.area < 3


def test_empty_source_does_not_infer_buildings():
    for variant in ('B', 'C'):
        assert organize_block([], box(0,0,10,10), GeometryCollection(), variant=variant, scale=1., profile=PROFILE)[0] == []


def test_zero_area_boundary_contacts_do_not_break_area_audit():
    seeds = [box(1,1,4,4), box(10,2,12,6)]
    result, audit = organize_block(seeds, box(0,0,10,10), GeometryCollection(),
        variant='B', scale=1., profile=PROFILE)
    assert result and audit['source_area_m2'] == 9
    assert 0 <= audit['retained_source_area_m2'] <= 9.00001


def test_protected_mask_is_semantic_not_all_landuse():
    sources = SimpleNamespace(vegetation=None, landuse=gpd.GeoDataFrame(
        {'landuse':['residential','forest'], 'geometry':[box(0,0,1,1),box(2,2,3,3)]}))
    assert protected_open_spaces(sources) == [box(2,2,3,3)]


def test_adapter_preserves_roads_water_heroes():
    road = [(box(0,0,1,1).boundary, 1., False)]
    lake = [box(8,8,10,10)]
    hero = [(box(3,3,4,4), 2.)]
    layer = SimpleNamespace(nozzle_real_m=.4, city_blocks=[box(0,0,10,10)], WL=lake, WO=[],
        BL=hero, BO=[], BO_heights=[], block_base=[box(0,0,10,10)], block_base_classes=['residential'], roads_lines=road)
    buildings = gpd.GeoDataFrame(geometry=[box(1,1,3,3), box(4,4,6,6)])
    evidence = apply_organization(layer, buildings, None, None, (0,0,10,10), variant='C',
        printer_profile=PROFILE, scene_policy={})
    assert layer.roads_lines is road and layer.WL is lake and layer.BL is hero
    assert evidence['production_default'] is False
    assert len(layer.BO) == len(layer.BO_heights)
