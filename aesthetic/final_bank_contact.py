"""Conservative sampled bank contact on the actual post-water solids.

This is a diagnostic, not full contact coverage or print acceptance. No geometry
is changed and no cached 'passed' flag is accepted instead of current solids.
"""
import numpy as np
import trimesh
from _TEXTURE_STYLE_OF_DEEPSEEK.prepared_surface import mesh_digest
from _TEXTURE_STYLE_OF_DEEPSEEK._geom_utils import mesh_to_manifold64, manifold64_to_mesh


def inspect_bank_contact(layers, meshes):
    ground=(getattr(layers,'surface_grounding',{}) or {}).get('roads',{})
    supports=ground.get('support_evidence',[])
    result=dict(status='pending',scope='实际最终地形与桥面：两岸中心邻域实体相交，非整条岸线验收',banks=[])
    if not supports:
        return dict(result,status='not_applicable')
    if len(supports)>16:
        return dict(result,reason_zh='超过局部诊断的32个岸点预算，需独立批量接触验收；不抽样冒充全量通过')
    roads=meshes.get('roads'); terrain=meshes.get('terrain')
    if roads is None or terrain is None:
        return dict(result,reason_zh='缺少实际道路或最终地形网格')
    proof=roads.metadata.get('surface_materialization',{})
    from aesthetic.surface_grounding import grounding_digest
    if (proof.get('mesh_sha256')!=mesh_digest(roads)
            or proof.get('grounding_fingerprint')!=grounding_digest(ground)):
        return dict(result,status='failed',reason_zh='实际道路实体与接地计划身份不一致')
    result['mesh_sha256']={k:mesh_digest(m) for k,m in [('roads',roads),('terrain',terrain)]}
    if any(not m.is_watertight or not m.is_winding_consistent for m in (roads,terrain)):
        return dict(result,status='failed',reason_zh='输入实体不闭合或绕向错误')
    scale=float(ground['scale_mm_per_m']) if 'scale_mm_per_m' in ground else float(layers.surface_plan_evidence['scale_mm_per_m'])
    # Window size follows the approved bridge width, not city ground metres.
    width=float(layers.surface_plan_evidence['road_width_contract']['resolved_visual_widths']['major'])
    window=width/4
    result['window_side_mm']=window
    result['positive_volume_tolerance_mm3']=1e-9
    road_solid=mesh_to_manifold64(roads)
    terrain_solid=mesh_to_manifold64(terrain)
    for support in supports:
        if support.get('status')!='ready':
            return dict(result,status='failed',reason_zh='桥面源支撑计划未通过')
        for index,bank in enumerate(support['bank_xy_m']):
            center=np.array(bank)*scale
            zlo=min(roads.bounds[0,2],terrain.bounds[0,2])-1
            zhi=max(roads.bounds[1,2],terrain.bounds[1,2])+1
            cutter=trimesh.creation.box(extents=[window,window,zhi-zlo],
                transform=trimesh.transformations.translation_matrix([*center,(zhi+zlo)/2]))
            window_solid=mesh_to_manifold64(cutter)
            overlap=manifold64_to_mesh((terrain_solid^window_solid)^(road_solid^window_solid))
            volume=0. if overlap.is_empty else float(overlap.volume)
            result['banks'].append(dict(polygon_index=support['polygon_index'],bank=index,
                overlap_mm3=volume,status='passed' if np.isfinite(volume) and volume>1e-9 else 'failed'))
    result['status']='passed' if result['banks'] and all(b['status']=='passed' for b in result['banks']) else 'failed'
    return result
