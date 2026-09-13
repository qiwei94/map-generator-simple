from shapely.geometry import LineString,box
from shapely.ops import unary_union
from aesthetic.bridge_approaches import recover_bridge_approaches

def test_approach_follows_connected_source_and_reaches_dry_bank():
    bridge=LineString([(-.4,0),(.4,0)])
    approaches=[LineString([(-2,0),(-.4,0)]),LineString([(.4,0),(1,.2),(2,.2)])]
    rows=[dict(geometry=g,highway='primary',bridge='yes' if g==bridge else 'no') for g in [bridge,*approaches]]
    paths,e=recover_bridge_approaches([bridge],rows,box(-.5,-2,.5,2),1.,.21)
    assert len(paths)==2
    assert e['invented_connectors']==0
    assert unary_union(paths).difference(unary_union(approaches).buffer(1e-10)).length<1e-9
    assert all(not box(-.5,-2,.5,2).intersects(__import__('shapely').geometry.Point(g.coords[-1])) for g in paths)

def test_nearby_disconnected_or_tunnel_road_cannot_become_approach():
    bridge=LineString([(-.4,0),(.4,0)])
    rows=[dict(geometry=LineString([(.4001,0),(2,0)]),highway='primary'),
          dict(geometry=LineString([(-.4,0),(-2,0)]),highway='primary',tunnel='yes')]
    paths,e=recover_bridge_approaches([bridge],rows,box(-.5,-2,.5,2),1.,.21)
    assert not paths
    assert all(x['status']=='no_connected_dry_source_path' for x in e['source_paths'])
