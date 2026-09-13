from dataclasses import replace
import pytest
from shapely.geometry import box, Polygon
from aesthetic.block_occupancy import OccupancyPolicy,occupancy_support,inward_brick_edges


def test_empty_and_bounded_memory():
    g,_=occupancy_support([],(-1,-1,1,1),1);assert g.is_empty
    with pytest.raises(ValueError):occupancy_support([box(0,0,1,1)],(0,0,100,100),1,OccupancyPolicy(max_cells=10))


def test_occupancy_preserves_large_unbuilt_hole():
    buildings=[box(0,0,1,4),box(3,0,4,4),box(1,0,3,1),box(1,3,3,4)]
    g,e=occupancy_support(buildings,(-1,-1,5,5),1)
    assert not g.intersects(box(1.8,1.8,2.2,2.2))
    assert g.covers(box(.1,.1,.9,.9));assert e['source_count']==4


def test_chamfer_is_reproducible_inward_and_area_bounded():
    p=box(0,0,2,2);policy=OccupancyPolicy(edge_probability=1)
    a,e=inward_brick_edges(p,1,policy);b,_=inward_brick_edges(p.reverse(),1,policy)
    assert a.equals(b) and p.covers(a) and e['accepted_corners']>0
    assert 0<e['area_loss_fraction']<=policy.max_area_loss


def test_holes_preserved_and_zero_strength_identity():
    p=Polygon(box(0,0,5,5).exterior.coords,[box(1,1,4,4).exterior.coords])
    g,_=inward_brick_edges(p,1,OccupancyPolicy(edge_probability=1))
    assert len(g.interiors)==1 and not g.intersects(box(2,2,3,3))
    g,_=inward_brick_edges(p,1,OccupancyPolicy(edge_cut_mm=0));assert g.equals(p)
