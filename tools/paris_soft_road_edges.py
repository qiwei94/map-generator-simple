"""Bounded diagnostic corridor breathing; source centerlines are unchanged."""
import numpy as np
from shapely.geometry import LineString,Point,Polygon
from shapely.ops import unary_union


def soft_corridor(lines,scale,nominal_mm,extra_width_mm=.04,wavelength_mm=3.,step_mm=.24):
    """Continuous deterministic XY field controls total gap widening, 0..extra.

    Tapered segments and vertex disks form the corridor. Each cross-section
    includes the nominal clearance; this is not a recreation of demo code.
    """
    pieces=[];count=0;sample_widths=[]
    for line in lines:
        parts=list(line.geoms) if hasattr(line,'geoms') else [line]
        for part in parts:
            if part.geom_type not in ('LineString','LinearRing') or part.is_empty:continue
            part_start=len(pieces)
            coords=np.asarray(part.coords)[:,:2]
            dense=[]
            for a,b in zip(coords[:-1],coords[1:]):
                length=np.linalg.norm(b-a);n=max(1,int(np.ceil(length*scale/step_mm)))
                dense.extend(a+(b-a)*t for t in np.arange(n)/n)
            dense.append(coords[-1]);xy=np.asarray(dense)
            mm=xy*scale
            phase=2*np.pi/wavelength_mm
            field=.5+.3*np.sin(phase*(.8*mm[:,0]+.6*mm[:,1]))+.2*np.sin(phase*(-.35*mm[:,0]+.94*mm[:,1])+1.3)
            widths=nominal_mm+extra_width_mm*np.clip(field,0,1)
            radius=widths/(2*scale)
            sample_widths.extend(widths.tolist());count+=len(xy)
            # Tapered ribbons preserve the unchanged segment direction.
            for i,(a,b) in enumerate(zip(xy[:-1],xy[1:])):
                d=b-a;length=np.linalg.norm(d)
                if length<1e-12:continue
                n=np.array([-d[1],d[0]])/length
                pieces.append(Polygon([a+n*radius[i],b+n*radius[i+1],b-n*radius[i+1],a-n*radius[i]]))
            # Round internal joins only; source endpoints retain flat caps.
            pieces.extend(Point(p).buffer(r,resolution=8) for p,r in zip(xy[1:-1],radius[1:-1]))
            # Near-end join disks must not protrude beyond the original flat cap.
            clipped=unary_union(pieces[part_start:]).intersection(part.buffer(float(radius.max()),cap_style=2,resolution=8))
            del pieces[part_start:]
            pieces.append(clipped)
    geom=unary_union(pieces)
    return geom,{'nominal_gap_mm':nominal_mm,'extra_total_width_mm':extra_width_mm,'wavelength_mm':wavelength_mm,
        'sample_step_mm':step_mm,'sampled_vertices':count,'sampled_width_min_max_mm':[min(sample_widths),max(sample_widths)],
        'method':'unchanged centerline; deterministic smooth spatial width field; tapered ribbons and round joins',
        'author_method':'unknown; independent visual experiment'}
