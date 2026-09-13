from types import SimpleNamespace
import geopandas as gpd
import pytest
from shapely.geometry import box
from shapely.ops import unary_union
from _TEXTURE_STYLE_OF_DEEPSEEK._layer_preprocess import LayerPolygons
from _TEXTURE_STYLE_OF_DEEPSEEK.print_profile import DEFAULT_PRINTER_PROFILE as PROFILE
from aesthetic.block_first import plan_blocks, apply_block_first, BlockFirstPolicy


def fixture():
    buildings = gpd.GeoDataFrame(geometry=[box(10,10,20,20), box(120,10,140,30)])
    layers = LayerPolygons(city_blocks=[box(0,0,100,100), box(110,0,210,100)],
        BL=[(box(120,10,140,30), 2.)], BL_categories=['SPIRITUAL'],
        nozzle_real_m=PROFILE.nozzle_diameter_mm/.01,
        WO=[box(0,50,100,60)])
    vegetation = gpd.GeoDataFrame({'leisure':['park']}, geometry=[box(0,60,100,100)])
    sources = SimpleNamespace(vegetation=vegetation, landuse=None)
    return layers, buildings, sources


def test_core_budget_and_ordinary_blocks_do_not_union_source_footprints(monkeypatch):
    layers, buildings, sources = fixture()
    plan = plan_blocks(layers, buildings, policy=BlockFirstPolicy(max_core_blocks=0))
    assert plan['detailed_buildings'] == 0
    def forbidden(*args, **kwargs):
        raise AssertionError('ordinary blocks must not aggregate individual buildings')
    monkeypatch.setattr('aesthetic.block_first.organize_block', forbidden)
    evidence = apply_block_first(layers, buildings, None, None, (0,0,210,100),
        printer_profile=PROFILE, scene_policy={'block_first':plan}, sources=sources)
    surface = unary_union(layers.block_base)
    assert surface.intersection(box(0,50,100,100)).area == 0
    assert surface.covers(box(30,10,80,40))  # block carrier extends beyond seeds
    assert surface.intersection(layers.BL[0][0]).area == 0
    assert evidence['refined_core_blocks'] == 0


def test_only_semantic_core_source_indices_are_scheduled_and_plan_is_bound():
    layers, buildings, sources = fixture()
    plan = plan_blocks(layers, buildings)
    assert plan['core_sources'] == {'1':[1]}
    assert plan['detailed_buildings'] == 1
    layers.city_blocks[0] = box(0,0,90,100)
    with pytest.raises(ValueError, match='plan mismatch'):
        apply_block_first(layers, buildings, None, None, (0,0,210,100),
            printer_profile=PROFILE, scene_policy={'block_first':plan}, sources=sources)


def test_large_occupied_block_keeps_urban_support_without_filling_empty_land():
    layers, buildings, sources = fixture()
    layers.city_blocks = [box(0,0,1000,1000)]
    layers.BL, layers.BL_categories = [], []
    plan = plan_blocks(layers, buildings)
    evidence = apply_block_first(layers, buildings, None, None, (0,0,1000,1000),
        printer_profile=PROFILE, scene_policy={'block_first':plan}, sources=sources)
    made = unary_union(layers.block_base)
    assert made.covers(buildings.geometry.iloc[0].centroid)
    assert not made.intersects(box(500,500,900,900))
    assert evidence['oversized_blocks_clipped_to_occupancy'] == 1
