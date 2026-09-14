"""S5 policy / S6 frozen B+C surface detail; S8 only materializes it.

Base DEM identity is never replaced. Ground microrelief is a separate frozen
surface plan, united with the terrain solid by the shared GLB/3MF consumer.
"""
from dataclasses import asdict, dataclass
import hashlib
import numpy as np
import trimesh
from shapely import distance, points
from shapely.affinity import scale as scale_geometry
from shapely.geometry import box
from shapely.ops import unary_union

VERSION='flat-block-local-ground-v1'


@dataclass(frozen=True)
class ZTexturePolicy:
    version: str = VERSION
    max_block_relief_mm: float = .24
    amplitude_mm: float = .07
    wavelength_mm: float = .8
    boundary_fade_mm: float = .15
    road_half_clearance_mm: float = .16
    overlap_mm: float = .12
    max_edge_mm: float = .20
    seed: int = 20260908
    max_faces: int = 2000000

    def __post_init__(self):
        if self.version!=VERSION:raise ValueError('unknown Z texture policy')
        for key in ('max_block_relief_mm','amplitude_mm','wavelength_mm','boundary_fade_mm',
                    'road_half_clearance_mm','overlap_mm','max_edge_mm'):
            if not np.isfinite(getattr(self,key)) or getattr(self,key)<=0:
                raise ValueError('invalid Z texture parameter: '+key)
        if self.max_faces<1:raise ValueError('invalid texture face budget')

    def payload(self):
        return asdict(self)


def continuous_field(x,y,policy):
    """Accepted C field: model-mm anchored, bounded and deterministic."""
    raw=np.zeros_like(np.asarray(x,dtype=float))
    rng=np.random.default_rng(policy.seed)
    for _ in range(24):
        angle,phase,multiple=rng.uniform(0,2*np.pi),rng.uniform(0,2*np.pi),rng.uniform(.5,1.7)
        raw+=np.sin(2*np.pi*(x*np.cos(angle)+y*np.sin(angle))/(policy.wavelength_mm*multiple)+phase)
    return policy.amplitude_mm*(.5+.5*np.tanh(raw/np.sqrt(12.)))


def choose_roof(bottom,top,mode,max_relief_mm):
    if mode!='draped_thickness':return None,'unchanged_flat_core'
    if float(np.ptp(bottom))>max_relief_mm:return None,'steep_keep_drape'
    return float(np.max(bottom)+np.median(top-bottom)),'flat_above_support'


def _polygons(g):
    if g.is_empty:return
    if g.geom_type=='Polygon':yield g
    elif hasattr(g,'geoms'):
        for part in g.geoms:yield from _polygons(part)


def allowed_ground(layers,bbox,scale,policy):
    """Only source-backed green candidates; never city-wide decorative noise."""
    from shapely import set_precision, make_valid
    clip=box(*bbox)
    def clean(g): return set_precision(make_valid(g), 1e-9)
    source_green=[p.intersection(clip) for p in list(layers.VL)+list(layers.VO) if p.intersects(clip)]
    # A city-scale first model does not need thousands of tiny park slivers.
    # Keep the largest source-backed regions; all occupied/water/road masks
    # remain exact inside those regions.
    source_green=sorted(source_green,key=lambda p:p.area,reverse=True)[:1200]
    greens=clean(unary_union(source_green))
    if greens.is_empty:return greens
    def local(polygons,window):
        return [p.intersection(window) for p in polygons if p.intersects(window)]
    occupied=clean(unary_union(local(list(layers.block_base)+list(layers.BO)+[p for p,h in layers.BL],clip)))
    water=clean(unary_union(local(list(layers.WL)+list(layers.WO),clip)))
    from shapely.strtree import STRtree
    radius=policy.road_half_clearance_mm/scale
    roads=list(layers.block_base_cut_lines)+list(layers.block_base_major_cut_lines)
    tree=STRtree(roads)
    fragments=[]
    # Buffer source lines locally, before union. Buffering the globally noded
    # street graph took 75 minutes for Paris; most of it is outside any green.
    for green in _polygons(clean(greens.difference(occupied).difference(water))):
        window=box(*green.bounds).buffer(radius)
        hits=tree.query(window,predicate='intersects')
        guard=unary_union([roads[int(i)].intersection(window).buffer(radius) for i in hits])
        fragments.append(green.difference(guard))
    return unary_union(fragments).intersection(clip)


def plan_ground_texture(layers,bbox,scale,terrain,payload):
    from aesthetic.surface_grounding import _patch_mesh,grounding_digest
    from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import sample_terrain_surface_plan_z
    policy=ZTexturePolicy(**payload)
    allowed=allowed_ground(layers,bbox,scale,policy)
    from shapely import set_precision
    model=set_precision(set_precision(scale_geometry(allowed,xfact=scale,yfact=scale,origin=(0,0)),1e-8),0)
    plan=dict(version=VERSION,terrain_fingerprint=terrain.fingerprint,
              input_geometry_fingerprint=hashlib.sha256(model.wkb).hexdigest(),
              support_evidence=policy.payload(),patches=[])
    max_delta=0.;face_count=0
    polygons=list(_polygons(model))
    print(f'[texture] {len(polygons)} regions, {model.area:.1f} mm2, edge {policy.max_edge_mm} mm',flush=True)
    for index,poly in enumerate(polygons):
        if index%100==0:print(f'[texture] {index}/{len(polygons)}, {face_count} faces',flush=True)
        estimate=4*poly.area/policy.max_edge_mm**2+4*poly.length/policy.max_edge_mm
        if estimate+face_count>policy.max_faces:
            raise ValueError('ground texture estimated geometry budget exceeded; explicit coarser policy required')
        xy,f,_=_patch_mesh(poly,terrain)
        xyz=np.column_stack([xy,np.zeros(len(xy))])
        # Split long shared edges by index; avoid both whole-polygon over-
        # refinement and the coordinate welding that breaks touching holes.
        from aesthetic.surface_subdivision import refine_surface
        xyz,f=refine_surface(xyz,f,policy.max_edge_mm,policy.max_faces-face_count)
        xy=xyz[:,:2]
        face_count+=len(f)
        if face_count>policy.max_faces:
            raise ValueError('ground texture geometry budget exceeded; use a smaller region or explicit coarser policy')
        edges=np.concatenate([f[:,[0,1]],f[:,[1,2]],f[:,[2,0]]])
        _,inv,counts=np.unique(np.sort(edges,axis=1),axis=0,return_inverse=True,return_counts=True)
        if np.any(counts>2):
            bad=edges[counts[inv]>2][:4]
            raise ValueError('non-manifold texture surface '+str(xy[bad].tolist()))
        boundary=edges[counts[inv]==1]
        z=sample_terrain_surface_plan_z(terrain,xy[:,0],xy[:,1])
        d=distance(points(xy),poly.boundary)
        fade=np.clip(d/policy.boundary_fade_mm,0,1);fade=fade*fade*(3-2*fade)
        bump=continuous_field(xy[:,0],xy[:,1],policy)*fade
        bump[np.unique(boundary)]=0.
        max_delta=max(max_delta,float(bump.max(initial=0)))
        bottom=np.maximum(z-policy.overlap_mm,terrain.terrain_base_z_mm+1e-5)
        if np.any(bottom>=z):raise ValueError('no terrain thickness available for texture overlap')
        patch={'mode':'local_ground_microrelief'}
        for key,a in dict(xy=xy,faces=f,boundary=boundary,bottom_z=bottom,top_z=z+bump).items():
            patch[key]=np.frombuffer(a.tobytes(),dtype=a.dtype).reshape(a.shape)
        plan['patches'].append(patch)
    plan['fingerprint']=grounding_digest(plan)
    plan['evidence']=dict(version=VERSION,owner_stage='S6',fingerprint=plan['fingerprint'],
        terrain_fingerprint=terrain.fingerprint,policy=policy.payload(),
        status='planned' if plan['patches'] else 'no_allowed_ground',
        polygon_count=len(plan['patches']),surface_triangles=face_count,
        allowed_area_mm2=model.area,max_delta_mm=max_delta,
        artificial_texture=True,vegetation_objects_enabled=False,
        reference_algorithm_claim=False,print_acceptance='pending_actual_slicing',
        说明='低坡街块平顶＋来源绿地内微起伏；水体、道路及建筑禁止纹理；不替代真实 DEM。')
    return plan


def materialize_ground_texture(plan):
    from aesthetic.surface_grounding import grounding_digest
    if grounding_digest(plan)!=plan['fingerprint']:raise ValueError('ground texture changed after S6')
    meshes=[]
    for p in plan['patches']:
        xy,f,edges=p['xy'],p['faces'],p['boundary'];n=len(xy)
        v=np.vstack([np.column_stack([xy,p['bottom_z']]),np.column_stack([xy,p['top_z']])])
        walls=np.array([[a,b,b+n] for a,b in edges]+[[a,b+n,a+n] for a,b in edges])
        mesh=trimesh.Trimesh(v,np.vstack([f[:,::-1],f+n,walls]),process=False)
        if not mesh.is_watertight or not mesh.is_winding_consistent or mesh.volume<=0:
            raise ValueError('invalid frozen ground texture shell')
        meshes.append(mesh)
    return trimesh.util.concatenate(meshes) if meshes else None


def materialize_textured_terrain(terrain_mesh,layers,terrain_plan):
    """Shared S7 GLB / S8 consumer. No new style decisions or sampling here."""
    plan=(getattr(layers,'surface_grounding',{}) or {}).get('ground_texture')
    if plan is None:return terrain_mesh
    if plan['terrain_fingerprint']!=terrain_plan.fingerprint:raise ValueError('texture bound to another terrain')
    detail=materialize_ground_texture(plan)
    result=terrain_mesh if detail is None else trimesh.boolean.union([terrain_mesh,detail],engine='manifold')
    if result is None or not result.is_watertight or not result.is_winding_consistent or result.volume<=0:
        raise ValueError('ground texture terrain union failed')
    from _TEXTURE_STYLE_OF_DEEPSEEK.prepared_surface import mesh_digest
    result.metadata.update(terrain_mesh.metadata)
    result.metadata['ground_texture']={**plan['evidence'],'materialized':True,'mesh_sha256':mesh_digest(result)}
    return result
