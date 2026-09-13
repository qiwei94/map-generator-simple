import pytest
from shapely.geometry import box, LineString
from shapely.ops import unary_union
from _TEXTURE_STYLE_OF_DEEPSEEK._layer_preprocess import LayerPolygons
from _TEXTURE_STYLE_OF_DEEPSEEK.print_profile import DEFAULT_PRINTER_PROFILE as PROFILE
from aesthetic.city_surface_plan import finalize_city_surfaces, verify_surface_plan


def fixture():
    complete = LineString([(-100,0),(100,0)])
    layers = LayerPolygons(BO=[box(-100,-100,0,100),box(0,-100,100,100)],
        BO_heights=[.24,.84],
        block_base_cut_lines=[LineString([(-80,0),(-35,0)])], seam_graph=[complete])
    rows = [dict(geometry=complete,highway='primary',name='Main',osmid=1)]
    return layers, rows


def finalize(layers, rows):
    return finalize_city_surfaces(layers,bbox_local=(-100,-100,100,100),scale=.01,
        printer_profile=PROFILE,road_style='negative-space-v1',source_roads=rows)


def test_negative_s6_uses_frozen_s3_seam_and_preserves_height_ownership():
    layers, rows = fixture()
    report=finalize(layers,rows)
    assert report['source_road_recovery']['invented_connectors']==0
    assert report['source_road_recovery']['recovered_route_length_m']==0
    assert report['source_road_recovery']['mutation']=='none'
    assert layers.BO_heights==[.24,.24,.84,.84]
    assert [r['source_index'] for r in report['surface_owners']]==[0,0,1,1]
    upper=unary_union([p for p in layers.BO if p.centroid.y>0])
    lower=unary_union([p for p in layers.BO if p.centroid.y<0])
    assert upper.distance(lower)*.01==pytest.approx(.28)
    assert not layers.surface_road_polygons  # negative streets are not solids
    verify_surface_plan(layers,.01)
    with pytest.raises(ValueError,match='restyle'):
        finalize_city_surfaces(layers,bbox_local=(-100,-100,100,100),scale=.01,printer_profile=PROFILE)


def test_negative_cannot_guess_missing_source():
    layers,_=fixture()
    with pytest.raises(ValueError,match='source road evidence'):
        finalize(layers,None)


def test_fine_negative_width_is_explicit_and_preserves_frozen_streets():
    from aesthetic.road_width_contract import resolve_road_width_contract
    profile, contract = resolve_road_width_contract(PROFILE, 'negative-space-fine-v1')
    assert profile.surface_road_gap_mm == .14
    assert profile.final_block_base_gap_mm == .21
    assert contract['physical_acceptance'] == 'pending'
    layers, rows = fixture()
    finalize_city_surfaces(layers,bbox_local=(-100,-100,100,100),scale=.01,
        printer_profile=PROFILE,road_style='negative-space-fine-v1',source_roads=rows)
    upper=unary_union([p for p in layers.BO if p.centroid.y>0])
    lower=unary_union([p for p in layers.BO if p.centroid.y<0])
    assert upper.distance(lower)*.01 == pytest.approx(.14)
    assert not layers.surface_road_polygons


def test_negative_keeps_tiny_approved_piece_instead_of_unreported_filter():
    layers=LayerPolygons(BO=[box(0,0,1,1)],BO_heights=[.36])
    report=finalize(layers,[])
    assert report['output_polygons']==1
    assert layers.BO_heights==[.36]


def test_negative_cuts_landmarks_but_keeps_height_and_covered_crossing():
    layers,rows=fixture()
    layers.BL=[(box(-80,-20,-40,20),2.1)]
    layers.BL_height_roles=['identity_anchor']
    finalize(layers,rows)
    assert len(layers.BL)==2
    assert all(h==2.1 for p,h in layers.BL)
    assert layers.BL_height_roles==['identity_anchor','identity_anchor']
    layers,rows=fixture()
    layers.BL=[(box(-80,-20,-40,20),2.1)]
    rows.append(dict(geometry=LineString([(-100,0),(100,0)]),tunnel='yes'))
    finalize(layers,rows)
    assert len(layers.BL)==1


def test_negative_cli_is_explicit_not_silent_default_change():
    from generate_city_legacy import parse_args
    from generate_model import canonical_arguments
    args=['--bbox','48.8,2.2,48.9,2.4','--pbf','unused.pbf','--city','test']
    assert parse_args(canonical_arguments(args)).surface_road_style=='printer-default'
    assert parse_args(canonical_arguments(args+['--surface-road-style','negative-space-v1'])).surface_road_style=='negative-space-v1'
    assert parse_args(canonical_arguments(args+['--urban-organization','C'])).urban_organization=='C'


def test_negative_png_uses_shared_holes_not_repainted_roads(tmp_path):
    from aesthetic.review_render import render_review_bundle
    from PIL import Image
    import numpy as np
    layers,rows=fixture()
    finalize(layers,rows)
    bundle=render_review_bundle(layers,{'bbox_local':(-100,-100,100,100),'scale':.01},
                               1.,str(tmp_path),'negative')
    assert bundle['road_mask'].max()==0
    img=np.asarray(Image.open(tmp_path/'negative_topdown.png'))
    h,w=img.shape[:2]
    assert tuple(img[h//2,w//2,:3])==(160,160,157)
    assert tuple(img[h//4,w//4,:3])==(247,247,245)
