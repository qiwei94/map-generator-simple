from dataclasses import replace
from types import SimpleNamespace
import numpy as np
import pytest
import trimesh
from shapely.geometry import box,LineString,Polygon
from shapely.ops import unary_union
from _TEXTURE_STYLE_OF_DEEPSEEK._layer_preprocess import LayerPolygons
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import resolve_terrain_surface_plan, materialize_terrain_surface_plan
from _TEXTURE_STYLE_OF_DEEPSEEK.print_profile import DEFAULT_PRINTER_PROFILE as PROFILE
from aesthetic.z_texture import (ZTexturePolicy,allowed_ground,plan_ground_texture,
    materialize_ground_texture,materialize_textured_terrain)
from aesthetic.city_surface_plan import finalize_city_surfaces,verify_surface_plan
from aesthetic.surface_grounding import resolve_grounding,materialize_grounding


def fixture():
    terrain=resolve_terrain_surface_plan(np.full((5,5),20.),8.,8.,1.,max_surface_edge_mm=1.)
    layers=LayerPolygons(VO=[box(-3,-3,3,3)],WL=[box(1,-3,3,3)],
        block_base=[box(-3,1,-1,3)],block_base_classes=['residential'],
        block_base_cut_lines=[LineString([(-4,0),(4,0)])])
    return layers,terrain


def test_allowed_ground_excludes_water_city_and_complete_seams():
    layers,t=fixture();p=ZTexturePolicy()
    g=allowed_ground(layers,(-4,-4,4,4),1.,p)
    assert g.intersection(layers.WL[0]).area==0
    assert g.intersection(layers.block_base[0]).area==0
    assert g.intersection(layers.block_base_cut_lines[0].buffer(.16)).area<1e-10
    assert g.area>0


def test_b_flattens_low_relief_without_changing_xy_or_minimum_thickness():
    t=SimpleNamespace(surface_z_grid_mm=np.array([[1,1.1],[1,1.1]]),width_m=4.,height_m=4.,scale_mm_per_m=1.,fingerprint='fixture')
    polygon=box(-1,-1,1,1)
    plan=resolve_grounding([polygon],[.5],1.,t,['draped_thickness'],flat_block_relief_limit_mm=.24)
    patch=plan['patches'][0]
    assert np.ptp(patch['top_z'])==0
    assert (patch['top_z']-patch['bottom_z']).min()==pytest.approx(.5)
    mesh,_=materialize_grounding(plan,[polygon],[.5],1.)
    assert mesh.is_watertight
    assert plan['evidence']['low_relief_blocks_flattened']==1
    assert plan['evidence']['draped_polygons']==0
    assert np.max(patch['top_z']-patch['bottom_z'])<.75


def test_c_real_shell_union_and_immutability():
    layers,t=fixture()
    p=plan_ground_texture(layers,(-4,-4,4,4),1.,t,ZTexturePolicy().payload())
    layers.surface_grounding={'ground_texture':p}
    detail=materialize_ground_texture(p)
    assert detail.is_watertight and detail.is_winding_consistent
    for patch in p['patches']:
        triangles=patch['xy'][patch['faces']]
        actual=unary_union([Polygon(tri) for tri in triangles])
        assert actual.intersection(layers.WL[0]).area<1e-10
    base=materialize_terrain_surface_plan(t)
    result=materialize_textured_terrain(base,layers,t)
    assert result.is_watertight and result.volume>base.volume
    assert result.volume-base.volume<=p['evidence']['allowed_area_mm2']*.07+1e-5
    with pytest.raises(ValueError,match='another terrain'):
        materialize_textured_terrain(base,layers,replace(t,fingerprint='other'))
    patch=p['patches'][0]
    with pytest.raises(ValueError):patch['top_z'][0]+=1
    patch['top_z']=patch['top_z'].copy()+1
    with pytest.raises(ValueError,match='changed after S6'):materialize_ground_texture(p)


def test_no_green_is_explicit_empty_not_world_noise():
    layers,t=fixture();layers.VO=[]
    p=plan_ground_texture(layers,(-4,-4,4,4),1.,t,ZTexturePolicy().payload())
    assert p['evidence']['status']=='no_allowed_ground'
    assert materialize_ground_texture(p) is None


def test_subdivision_preserves_hole_touching_boundary_fans():
    layers,t=fixture();layers.WL=[];layers.block_base=[];layers.block_base_cut_lines=[]
    layers.VO=[Polygon([(-2,-2),(2,-2),(2,2),(-2,2)],
                      holes=[[(-2,0),(-1,-1),(0,0),(-1,1)]])]
    plan=plan_ground_texture(layers,(-4,-4,4,4),1.,t,ZTexturePolicy().payload())
    mesh=materialize_ground_texture(plan)
    assert mesh.is_volume
    for patch in plan['patches']:
        actual=unary_union([Polygon(xy) for xy in patch['xy'][patch['faces']]])
        assert actual.symmetric_difference(layers.VO[0]).area<1e-8


def test_s6_z_policy_frozen_and_verified():
    layers,t=fixture()
    policy=ZTexturePolicy().payload()
    e=finalize_city_surfaces(layers,bbox_local=(-4,-4,4,4),scale=1.,printer_profile=PROFILE,
        terrain_surface_plan=t,z_texture_policy=policy)
    assert e['z_texture_status']=='frozen_in_s6'
    verify_surface_plan(layers,1.)
    with pytest.raises(ValueError,match='restyle finalized Z'):
        finalize_city_surfaces(layers,bbox_local=(-4,-4,4,4),scale=1.,printer_profile=PROFILE,
            terrain_surface_plan=t,z_texture_policy={**policy,'amplitude_mm':.08})
    layers.surface_grounding['ground_texture']['patches'][0]['top_z']=np.zeros(3)
    with pytest.raises(ValueError,match='grounding plan changed'):verify_surface_plan(layers,1.)


def test_shared_glb_consumer_preserves_exact_textured_terrain(tmp_path):
    from _TEXTURE_STYLE_OF_DEEPSEEK.render_glb import render_glb_preview
    layers,t=fixture();layers.block_base=[];layers.block_base_classes=[];layers.block_base_cut_lines=[]
    finalize_city_surfaces(layers,bbox_local=(-4,-4,4,4),scale=1.,printer_profile=PROFILE,
        terrain_surface_plan=t,z_texture_policy=ZTexturePolicy().payload())
    formal=materialize_textured_terrain(materialize_terrain_surface_plan(t),layers,t)
    path=tmp_path/'preview.glb'
    render_glb_preview(layers,{'bbox_local':(-4,-4,4,4),'scale':1.},str(path),
        terrain_surface_plan=t,preview_quality='fast')
    actual=trimesh.load(path,force='scene',process=False).geometry['terrain']
    np.testing.assert_allclose(actual.vertices,formal.vertices,atol=1e-6)
    np.testing.assert_array_equal(actual.faces,formal.faces)


def test_s8_rejects_missing_or_modified_texture_terrain():
    from aesthetic.city_surface_plan import verify_materialized_city
    layers,t=fixture();layers.WL=[];layers.block_base=[];layers.block_base_classes=[];layers.block_base_cut_lines=[]
    finalize_city_surfaces(layers,bbox_local=(-4,-4,4,4),scale=1.,printer_profile=PROFILE,
        terrain_surface_plan=t,z_texture_policy=ZTexturePolicy().payload())
    base=materialize_terrain_surface_plan(t)
    with pytest.raises(ValueError,match='terrain: missing'):
        verify_materialized_city(layers,{'terrain':base},None,1.)
    actual=materialize_textured_terrain(base,layers,t)
    verify_materialized_city(layers,{'terrain':actual},None,1.)
    actual.vertices[0,2]+=.01
    with pytest.raises(ValueError,match='terrain: missing'):
        verify_materialized_city(layers,{'terrain':actual},None,1.)


def test_local_edge_refinement_is_conforming_and_bounded():
    from aesthetic.surface_subdivision import refine_surface
    v=np.array([[0.,0.,0.],[2.,0.,0.],[2.,.1,0.],[0.,1.,0.]])
    v,f=refine_surface(v,np.array([[0,1,2],[0,2,3]]),.2,10000)
    edges=np.concatenate([f[:,[0,1]],f[:,[1,2]],f[:,[2,0]]])
    unique,counts=np.unique(np.sort(edges,axis=1),axis=0,return_counts=True)
    assert counts.max()==2
    assert np.linalg.norm(v[unique[:,0]]-v[unique[:,1]],axis=1).max()<=.2+1e-9
    border=unary_union([LineString(v[e,:2]) for e in unique[counts==1]])
    assert border.hausdorff_distance(Polygon([(0,0),(2,0),(2,.1),(0,1)]).boundary)<1e-9
