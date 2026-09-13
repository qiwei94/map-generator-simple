"""Conforming midpoint refinement by shared edge identity, without welding."""
import numpy as np


def refine_surface(vertices,faces,max_edge,max_faces):
    for _ in range(24):
        edges=np.stack((faces[:,[0,1]],faces[:,[1,2]],faces[:,[2,0]]),axis=1)
        unique,inverse=np.unique(np.sort(edges.reshape(-1,2),axis=1),axis=0,return_inverse=True)
        split=np.linalg.norm(vertices[unique[:,0]]-vertices[unique[:,1]],axis=1)>max_edge*(1+1e-10)
        if not split.any():return vertices,faces
        inverse=inverse.reshape(-1,3);flags=split[inverse]
        if len(faces)+int(flags.sum())>max_faces:
            raise ValueError('ground texture conforming subdivision budget exceeded; explicit coarser policy required')
        mids=np.full(len(unique),-1,dtype=np.int64)
        mids[split]=np.arange(split.sum())+len(vertices)
        vertices=np.vstack((vertices,vertices[unique[split]].mean(axis=1)))
        mids=mids[inverse];mask=flags@np.array([1,2,4]);parts=[faces[mask==0]]
        for value,rotation in ((1,0),(2,1),(4,2),(3,0),(6,1),(5,2),(7,0)):
            selected=mask==value
            f=np.roll(faces[selected],-rotation,axis=1);m=np.roll(mids[selected],-rotation,axis=1)
            a,b,c=f.T;ab,bc,ca=m.T
            if value in (1,2,4):
                triples=((a,ab,c),(ab,b,c))
            elif value in (3,6,5):
                triples=((b,bc,ab),(a,ab,c),(ab,bc,c))
            else:
                triples=((a,ab,ca),(ab,b,bc),(ca,bc,c),(ab,bc,ca))
            parts.extend(np.column_stack(t) for t in triples)
        faces=np.vstack(parts)
    raise ValueError('surface subdivision failed to reach requested edge length')
