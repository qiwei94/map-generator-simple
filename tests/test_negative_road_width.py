from shapely.geometry import box, LineString
from tools.experiment_negative_road_width import cut_carrier


def test_narrower_gap_changes_geometry_not_only_stroke():
    carrier = box(-5,-5,5,5)
    line = LineString([(-6,0),(6,0)])
    wide, _ = cut_carrier(carrier,[line],[],1,.55,.84)
    fine, _ = cut_carrier(carrier,[line],[],1,.14,.21)
    assert abs(wide.area - 94.5) < 1e-6
    assert abs(fine.area - 98.6) < 1e-6
    assert wide.difference(fine).area == 0


def test_cut_never_grows_outside_carrier_or_fills_protected_hole():
    carrier = box(-10,-10,10,10).difference(box(-2,-2,2,2))
    line = LineString([(-11,5),(11,5)])
    result, _ = cut_carrier(carrier,[],[line],1,.14,.21)
    assert result.difference(carrier).area == 0
    assert result.intersection(box(-2,-2,2,2)).area == 0


def test_two_dimensional_width_comparison_does_not_filter_small_cut_pieces():
    result, _ = cut_carrier(box(0,0,2,2),[LineString([(0,1),(2,1)])],[],1,.14,.21)
    assert abs(result.area - 3.72) < 1e-8


def test_fixed_water_stencil_preserves_island_in_width_comparison(tmp_path):
    import numpy as np
    from PIL import Image
    from tools.experiment_negative_road_width import draw_negative
    from shapely.geometry import GeometryCollection
    stencil = np.ones((200,200),dtype=bool)
    stencil[60:140,60:140] = False
    target = tmp_path/'island.png'
    draw_negative(box(0,0,10,10),GeometryCollection(),box(0,0,10,10),
                  GeometryCollection(),(0,0,10,10),target,pixels=100,
                  visible_water_mask=stencil)
    rgb = np.array(Image.open(target))
    assert tuple(rgb[50,50]) == (247,247,245)
    assert tuple(rgb[10,10]) == (0,0,0)
def test_spatial_cells_preserve_full_frame_cut_without_edge_caps():
    from shapely.geometry import box, LineString
    from shapely.ops import unary_union
    from tools.experiment_negative_road_width import cut_carrier
    carrier = box(-100,-100,100,100)
    lines = [LineString([(-110,-30),(0,15),(110,20)]),
             LineString([(-40,-110),(35,110)])]
    whole,_ = cut_carrier(carrier,lines,[],1.,.28,.42)
    pieces = []
    for x0,x1 in [(-100,0),(0,100)]:
        for y0,y1 in [(-100,0),(0,100)]:
            piece,_ = cut_carrier(carrier.intersection(box(x0,y0,x1,y1)),
                                  lines,[],1.,.28,.42)
            pieces.append(piece)
    assert whole.symmetric_difference(unary_union(pieces)).area < 1e-7
