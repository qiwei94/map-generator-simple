import numpy as np
import trimesh
from shapely.geometry import LineString, box
from tests.test_road_grounding import scene
from aesthetic.city_surface_plan import materialize_road_surfaces
from aesthetic.final_bank_contact import inspect_bank_contact
from _TEXTURE_STYLE_OF_DEEPSEEK.water import prepare_deepseek_water_relief, build_deepseek_water_v3
from _TEXTURE_STYLE_OF_DEEPSEEK.config import Z_WATER_BASE_MM


def test_actual_final_bank_not_old_grounding_proof():
    layers,_,sampler=scene(LineString([(-1.5,0),(1.5,0)]))
    road,_=materialize_road_surfaces(layers,1.,sampler.z_mm_vec)
    terrain=trimesh.creation.box(extents=[4,4,1.8])
    terrain.apply_translation([0,0,.1])  # top=1
    result=inspect_bank_contact(layers,{'roads':road,'terrain':terrain})
    assert result['status']=='passed'
    terrain.apply_translation([0,0,-2])
    assert inspect_bank_contact(layers,{'roads':road,'terrain':terrain})['status']=='failed'
    road.apply_translation([0,0,.01])
    assert '身份' in inspect_bank_contact(layers,{'roads':road,'terrain':terrain})['reason_zh']


def test_exact_water_recess_leaves_land_outside_polygon_unchanged():
    # A coarse sloping roof whose triangles cross the water boundary.
    from tools.verify_microrelief_slicing import solid
    top=Z_WATER_BASE_MM+.4+1.
    z=top+.03*np.indices((11,11))[1]
    # fixture builder closes at zero; shift it below the test roof first
    terrain=solid(z-(Z_WATER_BASE_MM))
    terrain.apply_translation([0,0,Z_WATER_BASE_MM])
    before=terrain.copy(); water=[box(.45,0,.75,1)]
    prepare_deepseek_water_relief(terrain,water,[],1.,base_thickness_mm=.4,
        surface_thickness_mm=.24,exact_boundary=True)
    from _TEXTURE_STYLE_OF_DEEPSEEK._geom_utils import mesh_to_manifold64,manifold64_to_mesh
    lost=manifold64_to_mesh(mesh_to_manifold64(before)-mesh_to_manifold64(terrain))
    assert not lost.is_empty
    assert lost.bounds[0,0]>=.45-1e-9 and lost.bounds[1,0]<=.75+1e-9
    assert terrain.is_watertight


def test_water_support_preserves_top_and_reaches_shared_base():
    poly=box(-1,-1,1,1);base=.4;top=Z_WATER_BASE_MM+base+.8
    m=build_deepseek_water_v3([poly],[],-2,-2,2,2,1.,flat_only=False,
        base_thickness_mm=base,surface_levels_mm=[top],support_to_base=True)
    assert m.bounds[1,2]==__import__('pytest').approx(top)
    assert m.is_watertight and len(m.split())==1
