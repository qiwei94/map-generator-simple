"""Create a GLB and honest views from the final 3MF, without regenerating geometry."""
import argparse,hashlib,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import trimesh
from tools.render_reference_actual_mesh import meshes_from_file,render


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('input',type=Path);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True)
    meshes=meshes_from_file(a.input)
    colors={'urban_relief':[240,239,235,255],'terrain':[160,160,155,255],
            'water':[20,25,28,255],'road':[130,130,125,255]}
    scene=trimesh.Scene();report={'source':str(a.input.resolve()),'sha256':hashlib.sha256(a.input.read_bytes()).hexdigest(),'objects':[],'views':[]}
    for name,v,f,role in meshes:
        m=trimesh.Trimesh(v,f,process=False);m.visual.face_colors=colors[role]
        scene.add_geometry(m,node_name=name,geom_name=name)
        report['objects'].append(dict(name=name,role=role,vertices=len(v),faces=len(f),watertight=bool(m.is_watertight),winding_consistent=bool(m.is_winding_consistent),volume_mm3=float(m.volume)))
    glb=a.output/'paris_BC.glb';scene.export(glb)
    print('GLB exported',glb.stat().st_size,flush=True)
    for angle,name,crop in ((90,'actual_topdown',None),(30,'actual_oblique',None),(90,'actual_center_detail',(-30,-30,30,30))):
        print('Rendering',name,flush=True)
        report['views'].append(render(meshes,a.output/(name+'.png'),angle,pixel_size=2400,crop=crop))
    (a.output/'artifact_review.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print('Done',flush=True)


if __name__=='__main__':main()
