"""Render real diagnostic meshes, oblique and longitudinal sections, in Chinese."""
import argparse
import json
from pathlib import Path
import numpy as np
import trimesh
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties
from mpl_toolkits.mplot3d.art3d import Poly3DCollection


def main():
    p=argparse.ArgumentParser();p.add_argument('--before',type=Path,required=True)
    p.add_argument('--after',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args(); font=FontProperties(fname='/System/Library/Fonts/STHeiti Medium.ttc')
    fig=plt.figure(figsize=(15,12))
    colors={'terrain':'#b3aaa0','water':'#548ba9','roads':'#c45235'}
    for row,index in enumerate((1,22,23)):
        for col,root in enumerate((a.before,a.after)):
            d=root/f'bridge_{index}';r=json.loads((d/'report.json').read_text())
            scene=trimesh.load(d/'semantic_diagnostic.glb')
            ax=fig.add_subplot(3,3,row*3+col+1,projection='3d')
            triangles=[];facecolors=[]
            for key,m in scene.geometry.items():
                triangles.extend(m.triangles);facecolors.extend([colors[key]]*len(m.faces))
            ax.add_collection3d(Poly3DCollection(triangles,facecolor=facecolors,edgecolor='none'))
            bounds=scene.bounds; center=scene.centroid
            ax.set(xlim=bounds[:,0],ylim=bounds[:,1],zlim=bounds[:,2],xlabel='X / mm',ylabel='Y / mm',zlabel='Z / mm')
            ax.set_box_aspect([*np.diff(bounds,axis=0)[0,:2],np.diff(bounds,axis=0)[0,2]*3])
            ax.view_init(elev=25,azim=-60)
            ax.set_title(f'桥 {index}｜'+('旧水体处理' if col==0 else '精确岸线候选'),fontproperties=font)
        ax=fig.add_subplot(3,3,row*3+3)
        for variant,root in [('旧',a.before),('新',a.after)]:
            d=root/f'bridge_{index}';r=json.loads((d/'report.json').read_text())
            scene=trimesh.load(d/'semantic_diagnostic.glb')
            banks=np.array(r['support'][0]['bank_xy_m'])*r['scale_mm_per_m']
            direction=banks[1]-banks[0]; length=np.linalg.norm(direction);direction/=length
            for key in ('terrain','roads'):
                m=scene.geometry[key]; section=m.section(plane_origin=[*banks[0],0],plane_normal=[-direction[1],direction[0],0])
                if section:
                    for curve in section.discrete:
                        distance=(curve[:,:2]-banks[0])@direction
                        ax.plot(distance,curve[:,2],color=colors[key],linestyle='--' if variant=='旧' else '-',linewidth=1.5)
            ax.set(xlim=(-.3,length+.3),ylim=(min(r['support'][0]['bank_surface_z_mm'])-.45,max(r['support'][0]['bank_surface_z_mm'])+.25))
        ax.axvline(0,color='#666',alpha=.3);ax.axvline(length,color='#666',alpha=.3)
        ax.set_title('纵剖面：虚线旧／实线新\n棕色地形 · 红色桥面',fontproperties=font)
        ax.set_xlabel('沿桥距离 / mm',fontproperties=font);ax.set_ylabel('Z / mm');ax.grid(alpha=.2)
    fig.suptitle('巴黎真实桥梁局部｜原 25 km 成品比例，不放大桥宽\n灰＝地形，蓝＝水，红＝桥；斜视 Z 展示放大 3 倍，几何未拉伸；不是完整城市验收',fontproperties=font,fontsize=15)
    fig.tight_layout(rect=(0,0,1,.93));fig.savefig(a.output,dpi=130);plt.close(fig)


if __name__=='__main__':main()
