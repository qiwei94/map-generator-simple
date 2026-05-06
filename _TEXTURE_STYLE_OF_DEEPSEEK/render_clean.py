"""Quick re-render: clean 3D isometric views without grid lines."""
import trimesh, matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
from pathlib import Path

root = Path(r"c:\Users\kiwi\OneDrive\文档\GitHub\map_generator")
scene = trimesh.load(str(root / "output/deepseek/West_Lake_deepseek.3mf"))
out_dir = root / "output/deepseek/screenshots"

meshes = {}
for name, geom in scene.geometry.items():
    if hasattr(geom, 'vertices') and len(geom.faces) > 0:
        meshes[name] = geom

all_v = np.vstack([m.vertices for m in meshes.values()])
center = (all_v.min(axis=0) + all_v.max(axis=0)) / 2
span_xy = max(np.ptp(all_v[:, 0]), np.ptp(all_v[:, 1]))
span_z = np.ptp(all_v[:, 2])

color_map = {
    "terrain_surface": [0.78, 0.70, 0.62],
    "terrain_walls": [0.55, 0.47, 0.39],
    "buildings": [0.94, 0.94, 0.90],
    "roads": [0.35, 0.35, 0.35],
    "water": [0.10, 0.18, 0.40],
}

def get_color(name):
    for key, col in color_map.items():
        if key in name.lower(): return np.array(col)
    return np.array([0.7, 0.7, 0.7])

def render_clean(elev, azim, filename):
    fig = plt.figure(figsize=(16, 14), dpi=150)
    ax = fig.add_subplot(111, projection='3d')
    
    sorted_names = sorted(meshes.keys(), key=lambda n: meshes[n].vertices[:, 2].mean())
    
    for name in sorted_names:
        m = meshes[name]
        verts = m.vertices - center
        faces = m.faces
        base_color = get_color(name)
        tri_verts = verts[faces]
        if len(tri_verts) == 0: continue
        
        v1, v2, v3 = tri_verts[:, 0], tri_verts[:, 1], tri_verts[:, 2]
        normals = np.cross(v2 - v1, v3 - v1)
        norm_len = np.linalg.norm(normals, axis=1, keepdims=True)
        norm_len[norm_len < 1e-10] = 1e-10
        normals = normals / norm_len
        light_dir = np.array([0.57735, -0.57735, 0.57735])
        dot = np.abs(np.dot(normals, light_dir))
        shading = (0.3 + 0.7 * dot).reshape(-1, 1)
        colors = np.clip(np.tile(base_color, (len(tri_verts), 1)) * shading, 0, 1)
        
        pc = Poly3DCollection(tri_verts, facecolors=colors,
                              edgecolor='none', linewidth=0, alpha=1.0)
        ax.add_collection3d(pc)
    
    half_xy = span_xy * 0.65
    ax.set_xlim(-half_xy, half_xy)
    ax.set_ylim(-half_xy, half_xy)
    ax.set_zlim(-span_z * 0.1, span_z * 1.2)
    ax.set_box_aspect([1, 1, 0.15])
    ax.view_init(elev=elev, azim=azim)
    
    # NO grid, NO axis labels, NO ticks - clean render
    ax.grid(False)
    ax.set_xticks([]); ax.set_yticks([]); ax.set_zticks([])
    ax.set_xlabel(''); ax.set_ylabel(''); ax.set_zlabel('')
    ax.xaxis.pane.fill = False; ax.yaxis.pane.fill = False; ax.zaxis.pane.fill = False
    ax.xaxis.pane.set_edgecolor('none'); ax.yaxis.pane.set_edgecolor('none'); ax.zaxis.pane.set_edgecolor('none')
    
    fig.tight_layout(pad=0)
    fig.savefig(str(out_dir / filename), dpi=150, bbox_inches='tight',
                facecolor='white', edgecolor='none', pad_inches=0)
    plt.close(fig)
    print(f"  Saved: {filename}")

print("Rendering clean views...")
render_clean(30, 45, "westlake_iso_se_clean.png")
render_clean(30, 225, "westlake_iso_sw_clean.png")
render_clean(30, 315, "westlake_iso_nw_clean.png")
render_clean(12, 180, "westlake_front_clean.png")
render_clean(12, 90, "westlake_side_clean.png")
print("Done!")
