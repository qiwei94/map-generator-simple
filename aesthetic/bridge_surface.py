"""Source corridor bridge surface with terrain-bound dry abutments.

A positive-weight harmonic interpolation transfers measured abutment heights
through the wet deck, following its actual triangular connectivity. It handles
curved and joined source spans without sampling the riverbed as bridge Z.
This is a documented model surface, not surveyed civil-engineering elevation.
"""
import hashlib
import numpy as np
from scipy.sparse import coo_matrix, diags
from scipy.sparse.linalg import spsolve
from shapely import contains_xy, set_precision
from shapely.geometry import box
from shapely.affinity import scale as scale_geometry
from shapely.ops import unary_union
from aesthetic.bridge_sources import bridge_corridor
from aesthetic.surface_grounding import _patch_mesh, _parts


def bank_supported_surface(poly, lines, scale, terrain, height, offset, water, gap):
    from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import sample_terrain_surface_plan_z
    radius = gap/scale/2
    sources = [g for g in lines if g.distance(poly) <= radius+1e-7]
    approved = bridge_corridor(sources, scale, gap, box(*poly.bounds))
    if poly.difference(approved.buffer(1e-7)).area*scale*scale > 1e-8:
        raise ValueError('桥面超出有来源的连续走廊')
    model = set_precision(scale_geometry(poly, xfact=scale, yfact=scale, origin=(0,0)),1e-8)
    wet = set_precision(scale_geometry(water.intersection(poly), xfact=scale, yfact=scale, origin=(0,0)),1e-8).intersection(model)
    dry_parts = list(_parts(model.difference(wet)))
    if not dry_parts:
        raise ValueError('桥面没有可验证的陆地支撑：缺少连接的源道路引道')
    xy, faces, boundary = _patch_mesh(model, terrain, partition=wet)
    dry = unary_union(dry_parts)
    # Boundary vertices are shared with the exact dry terrain partition.
    full_water = scale_geometry(water.intersection(poly.buffer(1/scale)),
        xfact=scale,yfact=scale,origin=(0,0))
    fixed = ~contains_xy(full_water.buffer(-1e-9), xy[:,0], xy[:,1])
    z = sample_terrain_surface_plan_z(terrain, xy[:,0], xy[:,1])
    edges = np.unique(np.sort(np.vstack([faces[:,[0,1]], faces[:,[1,2]], faces[:,[2,0]]]),axis=1),axis=0)
    distances = np.linalg.norm(xy[edges[:,0]]-xy[edges[:,1]],axis=1)
    if np.any(distances<=0): raise ValueError('桥面存在零长拓扑边')
    weights = 1/distances
    graph = coo_matrix((np.r_[weights,weights],(np.r_[edges[:,0],edges[:,1]],np.r_[edges[:,1],edges[:,0]])),shape=(len(xy),len(xy))).tocsr()
    laplacian = diags(np.asarray(graph.sum(axis=1)).ravel())-graph
    unknown = np.flatnonzero(~fixed); known = np.flatnonzero(fixed)
    if not len(known): raise ValueError('桥面没有落岸顶点')
    if len(unknown):
        z[unknown] = spsolve(laplacian[unknown][:,unknown], -laplacian[unknown][:,known]@z[known])
        if not np.isfinite(z).all(): raise ValueError('桥面存在未连接陆地的网格分量')
        if z[unknown].min()<z[known].min()-1e-8 or z[unknown].max()>z[known].max()+1e-8:
            raise ValueError('桥面高程超出实际支撑范围')
    patch = dict(mode='source_network_bank_surface')
    for key, a in dict(xy=xy,faces=faces,boundary=boundary,bottom_z=z+offset,top_z=z+offset+height).items():
        patch[key]=np.frombuffer(a.tobytes(),dtype=a.dtype).reshape(a.shape)
    banks = [p.representative_point() for p in dry_parts]
    proof = dict(status='ready', source_sha256=hashlib.sha256(unary_union(sources).wkb).hexdigest(),
        source_parts=len(sources), bank_xy_m=[[p.x/scale,p.y/scale] for p in banks],
        bank_surface_z_mm=[float(sample_terrain_surface_plan_z(terrain,p.x,p.y)) for p in banks],
        span_mm=float(model.length/2), support_policy='exact_dry_terrain_and_harmonic_wet_deck_v1',
        dry_support_regions=len(dry_parts),dry_support_area_mm2=float(dry.area),
        representation='city_relief_connection', two_separate_banks_required=False,
        cap_deviation_max_mm=0.,cap_deviation_limit_mm=height/4,
        min_bank_embedding_mm=float(-offset),min_bank_vertical_overlap_mm=float(height),
        fixed_land_vertices=int(fixed.sum()), interpolated_wet_vertices=int((~fixed).sum()),
        final_boolean_contact='pending',slicer_bridge_span='pending',
        说明='源道路走廊内的弯曲/多线桥面；陆地分片精确贴合冻结地形，水上按网格连通插值岸高，不使用河床高度；最终接触和切片另验。')
    return patch,proof
