"""Bounded block-level relief abstraction for a local visual experiment."""
import numpy as np
from shapely.affinity import scale as scale_geometry, rotate, translate
from shapely import set_precision
from shapely.ops import unary_union
from _TEXTURE_STYLE_OF_DEEPSEEK.block_base import _polygon_parts


def carve_blocks(geometry,scale):
    results=[];heights=[];changed=0
    for part in _polygon_parts(geometry):
        p=scale_geometry(part,xfact=scale,yfact=scale,origin=(0,0))
        c=p.centroid;x,y=c.x,c.y
        broad=.5+.5*np.sin(.35*x+.18*y+.7)*np.cos(.22*y-.8)
        inset=.02+.04*broad
        q=p.buffer(-inset,join_style=2).buffer(inset*.35,join_style=3)
        if q.is_empty or q.area<p.area*.75:
            q=p
        else:
            radius=max(np.hypot(np.array(p.exterior.coords)[:,0]-x,np.array(p.exterior.coords)[:,1]-y))
            angle=np.degrees(.035/max(radius,.1))*np.sin(.7*x+.5*y)
            q=rotate(q,angle,origin=(x,y))
            q=translate(q,xoff=.015*np.sin(.6*y),yoff=.015*np.cos(.5*x)).intersection(p)
            q=q.simplify(.018,preserve_topology=True).intersection(p)
            changed+=1
        q=scale_geometry(q,xfact=1/scale,yfact=1/scale,origin=(0,0))
        parts=list(_polygon_parts(set_precision(q,.002)))
        # Coherent broad spatial regions, not independent random building heights.
        h=.36 if broad<.35 else .60 if broad>.7 else .48
        results.extend(parts);heights.extend([h]*len(parts))
    return results,heights,{'edited_source_blocks':changed,'inset_range_mm':[.02,.06],
        'max_rotation_edge_displacement_mm':.035,'translation_amplitude_mm':.015,
        'relief_tiers_mm':[.36,.48,.60],'policy':'block-coherent asymmetric retreat and sparse clipped corners; shared broad height field; no road-centerline noise'}


def round_blocks(geometry,scale):
    """Round convex block corners without rotation, shearing or bevel cuts."""
    results=[];heights=[];radii=[]
    for part in _polygon_parts(geometry):
        p=scale_geometry(part,xfact=scale,yfact=scale,origin=(0,0))
        c=p.centroid;x,y=c.x,c.y
        broad=.5+.5*np.sin(.35*x+.18*y+.7)*np.cos(.22*y-.8)
        inset=.015+.02*broad
        radius=.10
        while True:
            q=p.buffer(-radius,resolution=8,join_style=1).buffer(max(0,radius-inset),resolution=8,join_style=1)
            if (not q.is_empty and q.area>=.70*p.area) or radius<=.00625:break
            radius*=.5;inset=min(inset,radius*.3)
        if q.is_empty:q=p.buffer(-.001,join_style=1)
        if q.is_empty:q=p
        radii.append(radius)
        q=scale_geometry(q,xfact=1/scale,yfact=1/scale,origin=(0,0))
        parts=list(_polygon_parts(set_precision(q,.002)))
        results.extend(parts);heights.extend([.36 if broad<.35 else .60 if broad>.7 else .48]*len(parts))
    return results,heights,{'policy':'round morphological opening; no rotation, translation, bevel cut or simplification',
        'preferred_corner_radius_mm':.10,'adaptive_min_radius_mm':min(radii),'inset_mm':[.015,.035],
        'relief_tiers_mm':[.36,.48,.60]}
