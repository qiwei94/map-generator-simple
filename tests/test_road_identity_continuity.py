from shapely.geometry import LineString
from shapely.ops import unary_union
from aesthetic.road_identity_continuity import recover_identities


def road(x0,x1,name='Main',y=0,**kw):
    return dict(geometry=LineString([(x0,y),(x1,y)]),name=name,highway='residential',**kw)


def test_restores_missing_middle_with_real_source_not_chord():
    rows=[road(0,100),road(100,200),road(200,300)]
    selected=unary_union([rows[0]['geometry'],rows[2]['geometry']])
    restored,e=recover_identities(rows,selected)
    assert abs(restored.length-300)<1e-8
    assert restored.difference(unary_union([r['geometry'] for r in rows])).length==0
    assert e['invented_connectors']==0


def test_does_not_jump_disconnected_same_name_or_select_cross_street():
    rows=[road(0,100),road(100,200),road(500,700),road(0,200,name='Other',y=50)]
    restored,_=recover_identities(rows,rows[0]['geometry'])
    assert restored.length==200


def test_does_not_merge_unnamed_roads_or_promote_tunnel():
    rows=[road(0,100,name=None),road(100,200,name=None),road(0,200,name='Tunnel',tunnel='yes')]
    restored,_=recover_identities(rows,rows[0]['geometry'])
    assert restored.length==100


def test_short_perpendicular_contact_does_not_promote_entire_identity():
    row=road(0,300)
    restored,_=recover_identities([row],LineString([(150,-100),(150,100)]))
    assert restored.is_empty


def test_spatial_support_matches_original_global_buffer():
    import pytest
    from shapely.geometry import GeometryCollection
    rows = [road(0,100,name='A'), road(100,200,name='A'),
            road(0,100,name='B',y=10), road(0,100,name='C',y=20)]
    selected = [LineString([(0,.025),(80,.025)]),
                LineString([(0,10),(40,10)]),
                LineString([(50,19),(50,21)]),
                LineString([(1000,0),(1100,0)])]
    restored, evidence = recover_identities(rows, GeometryCollection(selected))
    support = unary_union(selected).buffer(.05)
    expected = []
    for name in ('A','B','C'):
        route = unary_union([r['geometry'] for r in rows if r['name']==name])
        matched = route.intersection(support).length
        if matched >= 30 and matched/route.length >= .15:
            expected.append(route)
            record = next(r for r in evidence['identities'] if r['identity']==name)
            assert record['selected_support_m'] == pytest.approx(matched)
    assert restored.equals(unary_union(expected))
