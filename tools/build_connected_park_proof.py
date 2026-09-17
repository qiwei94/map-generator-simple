"""Local park prototypes: connected source-guided routes and physical relief."""
from pathlib import Path
import argparse, sys, pickle, json, time
import numpy as np
from scipy.ndimage import distance_transform_edt
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra
from shapely.geometry import box, Point, LineString
from shapely.ops import unary_union, nearest_points
from shapely import contains_xy, prepare
from PIL import Image, ImageDraw, ImageFont

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from aesthetic.z_texture import _polygons
from aesthetic.surface_grounding import resolve_grounding, materialize_grounding
from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf

def pieces(g):
    if g.is_empty:return []
    if g.geom_type=='LineString':return [g]
    return [p for q in getattr(g,'geoms',[]) for p in pieces(q)]

def park_shape_metrics(park, water, scale):
    """Measure the printable silhouette in model millimetres."""
    obb=park.minimum_rotated_rectangle
    coords=np.asarray(obb.exterior.coords)[:4]
    edges=np.roll(coords,-1,axis=0)-coords
    lengths=np.linalg.norm(edges,axis=1)
    long_i=int(np.argmax(lengths));long_m=float(lengths[long_i]);short_m=float(np.min(lengths))
    long_vec=edges[long_i]/long_m
    return dict(
        area_mm2=float(park.area*scale**2),
        long_mm=long_m*scale,
        short_mm=short_m*scale,
        aspect_ratio=long_m/max(short_m,1e-9),
        rectangularity=float(park.area/max(obb.area,1e-9)),
        solidity=float(park.area/max(park.convex_hull.area,1e-9)),
        water_ratio=float(park.intersection(water).area/max(park.area,1e-9)),
        long_axis=long_vec,
    )

def choose_axis_strategy(metrics):
    if metrics['area_mm2']<8 or metrics['short_mm']<5:
        return 'none'
    if metrics['aspect_ratio']>=1.75:
        return 'two_longitudinal'
    if (metrics['aspect_ratio']<=1.35 and metrics['short_mm']>=18
            and metrics['solidity']>=.72 and metrics['water_ratio']<=.18):
        return 'three_compact'
    return 'two_balanced'

def directional_aims(park, metrics, strategy, scale):
    """Return paired boundary aims aligned to the park's own long axis."""
    if strategy=='none':return []
    center=np.asarray(park.centroid.coords[0]);u=np.asarray(metrics['long_axis']);v=np.array([-u[1],u[0]])
    if strategy=='three_compact':
        # Three evenly spaced undirected axes keep six entrances apart and
        # avoid the near-parallel pair produced by mixing 0/90/35 degrees.
        axes=[np.cos(a)*u+np.sin(a)*v for a in np.deg2rad((0,60,120))]
    else:
        axes=[u,v]
    radius=max(metrics['long_mm'],metrics['short_mm'])/scale*2
    aims=[]
    for axis in axes:
        cut=LineString([center-axis*radius,center+axis*radius]).intersection(park.boundary)
        pts=[]
        if cut.geom_type=='Point':pts=[cut]
        else:pts=[g for g in getattr(cut,'geoms',[]) if g.geom_type=='Point']
        if len(pts)>=2:
            pts=sorted(pts,key=lambda p:np.dot(np.asarray(p.coords[0])-center,axis))
            aims.extend((pts[0],pts[-1]))
        else:
            aims.extend((Point(*(center-axis*radius)),Point(*(center+axis*radius))))
    return aims

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--style',choices=('source','cross','adaptive','adaptive_source'),default='source')
    args=parser.parse_args()
    started=time.monotonic()
    out=ROOT/({'cross':'output/paris_connected_parks_v4_cross','adaptive':'output/paris_connected_parks_v5_adaptive','adaptive_source':'output/paris_connected_parks_v6_adaptive_source'}.get(args.style,'output/paris_connected_parks_v3'))
    out.mkdir(exist_ok=True)
    with (ROOT/'output/paris_BC_full_20260914_v3_inputs/surface_inputs.pkl').open('rb') as f:layers,kw=pickle.load(f)
    scale=kw['scale'];roads=kw['source_roads'];terrain=kw['terrain_surface_plan']
    xmin,ymin,xmax,ymax=kw['bbox_local'];records=[]
    rois={'boulogne':(250,790,560,1130),'vincennes':(1290,1090,1750,1420)}
    for name,(a,b,c,d) in rois.items():
        roi=box(xmin+a/2048*(xmax-xmin),ymax-d/2048*(ymax-ymin),xmin+c/2048*(xmax-xmin),ymax-b/2048*(ymax-ymin))
        greens=[g.intersection(roi) for g in layers.VL+layers.VO if g.intersects(roi)]
        # Close mapping gaps between adjacent green parcels, before subtracting roads.
        merged=unary_union(greens).buffer(100).buffer(-100)
        park=max(_polygons(merged),key=lambda g:g.area).simplify(10)
        window=box(*park.bounds).buffer(260)
        hits=roads.iloc[roads.sindex.query(window,predicate='intersects')]
        live=hits[~hits.highway.isin(['construction','proposed','steps','motorway','motorway_link'])]
        lines=[l for g in live.geometry if g is not None for l in pieces(g.intersection(window))]
        source=unary_union(lines)
        urban=hits[hits.highway.isin(['residential','tertiary','secondary','primary','unclassified','service','pedestrian'])]
        external=unary_union([g.intersection(window).difference(park.buffer(12)) for g in urban.geometry if g is not None])
        water=unary_union([g for g in layers.WL+layers.WO if g.intersects(window)]).buffer(30)
        metrics=park_shape_metrics(park,water,scale)
        strategy=choose_axis_strategy(metrics) if args.style in ('adaptive','adaptive_source') else ('two_balanced' if args.style=='cross' else 'source')
        if external.is_empty:raise ValueError('No external roads')
        x0,y0,x1,y1=window.bounds;step=24.;xs=np.arange(x0,x1+step,step);ys=np.arange(y0,y1+step,step)
        xx,yy=np.meshgrid(xs,ys);shape=xx.shape;n=xx.size
        source_band=source.buffer(20);park_band=park.buffer(45)
        routing_water=water.buffer(40)
        for geometry in (window,routing_water,source_band,park_band):prepare(geometry)
        allowed=contains_xy(window,xx,yy)&~contains_xy(routing_water,xx,yy)
        on_source=contains_xy(source_band,xx,yy)
        cost=1+np.minimum(distance_transform_edt(~on_source)*step/35,9)
        cost[~contains_xy(park_band,xx,yy)]*=3
        ids=np.arange(n).reshape(shape);rr=[];cc=[];vv=[]
        for dy,dx in [(0,1),(1,0),(1,1),(1,-1)]:
            ya=slice(0,shape[0]-dy);yb=slice(dy,shape[0]);xa=slice(max(0,-dx),shape[1]-max(0,dx));xb=slice(max(0,dx),shape[1]-max(0,-dx))
            ok=allowed[ya,xa]&allowed[yb,xb];u=ids[ya,xa][ok];v=ids[yb,xb][ok]
            w=(cost[ya,xa][ok]+cost[yb,xb][ok])*.5*np.hypot(dx,dy)
            rr.extend([u,v]);cc.extend([v,u]);vv.extend([w,w])
        graph=coo_matrix((np.concatenate(vv),(np.concatenate(rr),np.concatenate(cc))),shape=(n,n)).tocsr()
        def node(p):
            distance=(xx-p.x)**2+(yy-p.y)**2
            distance[~allowed]=np.inf
            return int(distance.argmin())
        center=park.representative_point();hub=node(nearest_points(center,source.intersection(park))[1])
        dist,pred=dijkstra(graph,indices=hub,return_predecessors=True)
        print(name,'routing',shape,'reachable',int(np.isfinite(dist).sum()),flush=True)
        px0,py0,px1,py1=park.bounds
        aims=(directional_aims(park,metrics,strategy,scale) if args.style in ('adaptive','adaptive_source') else
              [Point(px0,(py0+py1)/2),Point(px1,(py0+py1)/2),Point((px0+px1)/2,py0),Point((px0+px1)/2,py1)])
        routes=[];portals=[];follow=[]
        possible=[]
        for line in pieces(external):
            for position in np.linspace(0,line.length,max(2,int(line.length/70)+1)):
                p=line.interpolate(position)
                if park.distance(p)>180 or water.distance(p)<15:continue
                boundary_point=nearest_points(p,park)[1]
                if LineString([p,boundary_point]).intersects(water):continue
                idx=node(p)
                if np.isfinite(dist[idx]):possible.append(p)
        for aim in aims:
            min_portal_spacing=250 if len(aims)>4 else 350
            choices=[p for p in possible if all(p.distance(q)>min_portal_spacing for q in portals)]
            if not choices:continue
            p=min(choices,key=lambda p:p.distance(aim))
            if any(p.distance(q)<min_portal_spacing for q in portals):continue
            dd=(xx-p.x)**2+(yy-p.y)**2
            dd.ravel()[~np.isfinite(dist)]=np.inf
            end=int(dd.argmin())
            if dd.flat[end]>180**2:continue
            chain=[end]
            while chain[-1]!=hub:
                chain.append(int(pred[chain[-1]]))
                if chain[-1]<0:raise ValueError('Broken route')
            coords=[(xx.flat[i],yy.flat[i]) for i in reversed(chain)]+[(p.x,p.y)]
            follow.extend(on_source.ravel()[chain].tolist())
            line=LineString(coords).simplify(25)
            smooth=np.asarray(line.coords)
            for _ in range(2):
                pairs=np.stack((.75*smooth[:-1]+.25*smooth[1:],.25*smooth[:-1]+.75*smooth[1:]),axis=1).reshape(-1,2)
                smooth=np.vstack((smooth[0],pairs,smooth[-1]))
            candidate=LineString(smooth)
            if not candidate.intersects(water):line=candidate
            if line.intersects(water):line=LineString(coords)
            if line.intersects(water):continue
            routes.append(line);portals.append(p)
        if args.style in ('cross','adaptive'):
            cross_routes=[];cross_follow=[]
            for left,right in [(i,i+1) for i in range(0,len(portals)-1,2)]:
                if len(portals)<=right:continue
                start,end=portals[left],portals[right]
                direct=LineString([(start.x,start.y),(end.x,end.y)])
                if not direct.intersects(routing_water):
                    cross_routes.append(direct)
                    cross_follow.extend(contains_xy(source_band,*np.asarray(direct.coords).T).tolist())
                    continue
                start_node,end_node=node(start),node(end)
                pair_dist,pair_pred=dijkstra(graph,indices=start_node,return_predecessors=True)
                if not np.isfinite(pair_dist[end_node]):continue
                chain=[end_node]
                while chain[-1]!=start_node:
                    chain.append(int(pair_pred[chain[-1]]))
                    if chain[-1]<0:raise ValueError('Broken cross-axis detour')
                coords=[(start.x,start.y)]+[(xx.flat[i],yy.flat[i]) for i in reversed(chain)]+[(end.x,end.y)]
                detour=LineString(coords).simplify(28)
                cross_routes.append(detour)
                cross_follow.extend(on_source.ravel()[chain].tolist())
            routes=cross_routes
            follow=cross_follow
        network=unary_union(routes);corridor=network.buffer(.52/scale/2,quad_segs=4)
        (out/f'{name}_routes.wkt').write_text(network.wkt)
        assert len(portals)>=2 and all(corridor.intersects(p) for p in portals)
        assert all(p.distance(external)<1e-6 and not park.contains(p) for p in portals)
        assert len(list(_polygons(corridor)))==1
        # Rounded, continuous white terrain plates; the gray paths are physical gaps.
        white=park.difference(corridor.union(water)).buffer(-18).buffer(18)
        polys=[p for p in _polygons(white) if p.area*scale**2>.8]
        def mesh(pp,height,offset):
            hh=[height]*len(pp);plan=resolve_grounding(pp,hh,scale,terrain,['draped_thickness']*len(pp),base_offset_mm=offset)
            return materialize_grounding(plan,pp,hh,scale)[0]
        base=mesh([window],1.2,-1.2);relief=mesh(polys,.36,-.12)
        assert base.is_watertight and relief.is_watertight
        export_deepseek_3mf({'terrain':base,'block_base':relief},str(out/f'{name}_local.3mf'))
        # Render the same polygons used by the relief mesh, including exterior context.
        size=1100;h=round(size*(y1-y0)/(x1-x0));im=Image.new('RGB',(size,h),(150,151,147));draw=ImageDraw.Draw(im)
        def points(g):return [((x-x0)/(x1-x0)*size,(y1-y)/(y1-y0)*h) for x,y in g.coords]
        def paint(poly,color):
            draw.polygon(points(poly.exterior),fill=color)
            for ring in poly.interiors:draw.polygon(points(ring),fill=(150,151,147))
        for p in polys:paint(p,(233,233,226))
        for p in _polygons(water.intersection(window)):paint(p,(30,33,35))
        for l in pieces(external):draw.line(points(l),fill=(100,103,100),width=max(2,round(.18/scale/(x1-x0)*size)))
        im.save(out/f'{name}_preview.png')
        annotated=im.copy();dr=ImageDraw.Draw(annotated)
        for i,p in enumerate(portals):
            x,y=points(p)[0];dr.ellipse((x-9,y-9,x+9,y+9),outline='#ed623b',width=3);dr.text((x+12,y),str(i+1),fill='#be4020')
        annotated.save(out/f'{name}_portals.png')
        public_metrics={k:round(float(v),4) for k,v in metrics.items() if k!='long_axis'}
        rec=dict(name=name,style=args.style,strategy=strategy,shape_metrics=public_metrics,portals=len(portals),connected_components=1,all_portals_on_external_roads=True,route_length_mm=network.length*scale,source_follow_grid_fraction=float(np.mean(follow)),white_parts=len(polys),mesh_watertight=True,faces=len(base.faces)+len(relief.faces),note='Local prototype; exterior roads shown as context; not a full Paris assembly or sliced print.')
        records.append(rec);print(rec,flush=True)
    (out/'report.json').write_text(json.dumps(dict(parks=records,seconds=time.monotonic()-started),indent=2))

if __name__=='__main__':main()
