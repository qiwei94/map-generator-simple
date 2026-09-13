import pytest
from shapely.geometry import box, Polygon
from shapely.ops import unary_union
from aesthetic.block_edge_experiment import soften_block_edges


def test_edges_do_not_close_seams_or_remove_blocks_and_are_repeatable():
    urban = unary_union([box(0,0,100,100),box(120,0,220,100),box(0,150,.1,150.1)])
    result, audit = soften_block_edges(urban, scale=.008)
    again, _ = soften_block_edges(urban, scale=.008)
    assert result.equals(again)
    assert result.difference(urban).area < 1e-8
    assert audit['output_blocks'] == audit['input_blocks'] == 3
    assert audit['fallback_blocks'] >= 1
    assert result.area >= urban.area * .98
    assert result.intersection(box(100,0,120,100)).area == 0


def test_hole_count_and_disabled_identity():
    urban = Polygon([(0,0),(100,0),(100,100),(0,100)],
                    [[(30,30),(70,30),(70,70),(30,70)]])
    result, _ = soften_block_edges(urban, scale=.008)
    assert len(result.interiors) == 1
    disabled, _ = soften_block_edges(urban, scale=.008, radius_mm=0)
    assert disabled.equals(urban)


@pytest.mark.parametrize('scale,radius', [(0,.04),(.008,-1),(.008,float('nan'))])
def test_bad_parameters(scale,radius):
    with pytest.raises(ValueError):
        soften_block_edges(box(0,0,1,1),scale=scale,radius_mm=radius)
