"""Bounded 2D block-support experiment; no change to production defaults."""
from dataclasses import dataclass, asdict
import hashlib
import math
import numpy as np
from shapely.geometry import shape, Polygon, GeometryCollection
from shapely.ops import unary_union
from rasterio.features import rasterize, shapes
from rasterio.transform import from_origin
from scipy.ndimage import distance_transform_edt


@dataclass(frozen=True)
class OccupancyPolicy:
    cell_mm: float = .08
    support_mm: float = .315
    edge_cut_mm: float = .08
    edge_probability: float = .30
    max_area_loss: float = .015
    max_cells: int = 4_000_000
    seed: int = 20260906


def occupancy_support(buildings, bounds, scale, policy=OccupancyPolicy()):
    """Rasterize footprints once, not their precise union. Return support only.

    All-touched cells conservatively retain small buildings, with a recorded
    boundary error. Protected spaces and final streets remain exact vectors.
    """
    if not np.isfinite(scale) or scale<=0 or policy.cell_mm<=0 or policy.support_mm<0:
        raise ValueError('invalid occupancy scale or resolution')
    x0,y0,x1,y1=bounds; cell=policy.cell_mm/scale
    if x1<=x0 or y1<=y0: raise ValueError('empty occupancy frame')
    nx,ny=math.ceil((x1-x0)/cell),math.ceil((y1-y0)/cell)
    if nx*ny>policy.max_cells: raise ValueError('occupancy frame exceeds bounded memory budget; tile it')
    transform=from_origin(x0,y1,cell,cell)
    geoms=[g for g in buildings if g is not None and not g.is_empty]
    if not geoms:return GeometryCollection(),dict(source_count=0,occupied_cells=0)
    occupied=rasterize(((g,1) for g in geoms),out_shape=(ny,nx),transform=transform,
                       all_touched=True,dtype='uint8')
    supported=(distance_transform_edt(occupied==0)*policy.cell_mm<=policy.support_mm) if occupied.any() else occupied.astype(bool)
    polys=[shape(g) for g,v in shapes(supported.astype('uint8'),mask=supported,transform=transform) if v==1]
    return unary_union(polys),dict(source_count=len(geoms),occupied_cells=int(occupied.sum()),
        grid_shape=[ny,nx],policy=asdict(policy),conservative_boundary_error_mm=math.sqrt(2)*policy.cell_mm,
        support_semantics='建筑占用支撑，不是逐栋建筑或真实新增建筑')


def inward_brick_edges(poly, scale, policy=OccupancyPolicy()):
    """Deterministic convex-corner chamfers, never outward street intrusion.

    Skip short edges/concave corners. Reject splitting, hole changes, invalid
    output or excess area loss. This tests a style hypothesis, not demo truth.
    """
    if scale<=0 or not poly.is_valid or poly.geom_type!='Polygon':raise ValueError('valid polygon and positive scale required')
    if not 0<=policy.edge_probability<=1 or not 0<=policy.max_area_loss<1 or policy.edge_cut_mm<0:
        raise ValueError('invalid edge policy')
    poly=poly.normalize()
    guide=poly.simplify(1e-7/scale,preserve_topology=True)
    pts=np.asarray(guide.exterior.coords)[:-1]
    digest=hashlib.sha256(poly.wkb+str(policy.seed).encode()).digest()
    rng=np.random.default_rng(int.from_bytes(digest[:8],'little'))
    result=poly; accepted=0
    for i,b in enumerate(pts):
        if rng.random()>policy.edge_probability:continue
        a,c=pts[i-1],pts[(i+1)%len(pts)]
        u,v=a-b,c-b; lu,lv=np.linalg.norm(u),np.linalg.norm(v)
        depth=policy.edge_cut_mm/scale*rng.uniform(.45,1.)
        if min(lu,lv)<depth*4 or depth==0:continue
        cosine=np.dot(u,v)/(lu*lv)
        if cosine<-.8 or cosine>.8:continue
        cut=Polygon([b,b+u/lu*depth,b+v/lv*depth])
        if cut.difference(poly).area>max(1e-12,cut.area*1e-8):continue
        candidate=result.difference(cut)
        if (candidate.geom_type!='Polygon' or not candidate.is_valid
                or len(candidate.interiors)!=len(poly.interiors)
                or candidate.area<poly.area*(1-policy.max_area_loss)):continue
        result=candidate;accepted+=1
    return result,dict(accepted_corners=accepted,area_loss_fraction=1-result.area/poly.area,
                       max_cut_mm=policy.edge_cut_mm,seed=policy.seed)
