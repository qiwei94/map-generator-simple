"""Render Chicago 3MF to top-down PNG."""
import trimesh
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
import os

p = "output/chicago_cli/full_chicago_cli_2layer_0619_0936.3mf"
out_dir = "output/chicago_cli"
os.makedirs(out_dir, exist_ok=True)

print(f"Loading: {p}")
scene = trimesh.load(p)

meshes = {}
total_faces = 0
for name, geom in scene.geometry.items():
    if hasattr(geom, 'vertices') and len(geom.faces) > 0:
        m = geom.copy()
        meshes[name] = m
        total_faces += len(m.faces)
        print(f"  {name}: {len(m.vertices)} verts, {len(m.faces)} faces")
print(f"  Total faces: {total_faces}")

# Compute center and span
all_verts = np.vstack([m.vertices for m in meshes.values()])
center = (all_verts.min(axis=0) + all_verts.max(axis=0)) / 2
span_xy = max(np.ptp(all_verts[:, 0]), np.ptp(all_verts[:, 1]))
span_z = np.ptp(all_verts[:, 2])
print(f"Span XY: {span_xy:.1f}, Z: {span_z:.1f}")

# Color scheme (matches reference style)
color_map = {
    "terrain": np.array([0.76, 0.68, 0.60]),     # warm beige
    "buildings": np.array([0.92, 0.92, 0.88]),    # off-white
    "landmarks": np.array([0.92, 0.92, 0.88]),    # off-white
    "roads": np.array([0.30, 0.30, 0.32]),        # dark gray
    "water": np.array([0.10, 0.18, 0.40]),         # dark blue
    "block_base": np.array([0.85, 0.80, 0.72]),    # warm base
}

def get_color(name):
    for key, col in color_map.items():
        if key in name.lower():
            return col
    return np.array([0.6, 0.6, 0.6])

def render_view(elev, azim, filename, title=""):
    fig = plt.figure(figsize=(16, 14), dpi=150)
    ax = fig.add_subplot(111, projection='3d')
    
    sorted_names = sorted(meshes.keys(), key=lambda n: meshes[n].vertices[:, 2].mean())
    
    for name in sorted_names:
        m = meshes[name]
        verts = m.vertices - center
        faces = m.faces
        base_color = get_color(name)
        
        tri_verts = verts[faces]
        if len(tri_verts) == 0:
            continue
        
        v1, v2, v3 = tri_verts[:, 0], tri_verts[:, 1], tri_verts[:, 2]
        normals = np.cross(v2 - v1, v3 - v1)
        norm_len = np.linalg.norm(normals, axis=1, keepdims=True)
        norm_len[norm_len < 1e-10] = 1e-10
        normals = normals / norm_len
        
        light_dir = np.array([0.57735, -0.57735, 0.57735])
        dot = np.abs(np.dot(normals, light_dir))
        shading = (0.3 + 0.7 * dot).reshape(-1, 1)
        face_colors = np.clip(np.tile(base_color, (len(tri_verts), 1)) * shading, 0, 1)
        
        pc = Poly3DCollection(tri_verts, facecolors=face_colors,
                              edgecolor='none', linewidth=0, alpha=1.0)
        ax.add_collection3d(pc)
    
    half_xy = span_xy * 0.65
    ax.set_xlim(-half_xy, half_xy)
    ax.set_ylim(-half_xy, half_xy)
    ax.set_zlim(-span_z * 0.1, span_z * 1.2)
    
    ax.set_xlabel('X (mm)', fontsize=8)
    ax.set_ylabel('Y (mm)', fontsize=8)
    ax.set_zlabel('Z (mm)', fontsize=8)
    ax.set_box_aspect([1, 1, 0.15])
    ax.view_init(elev=elev, azim=azim)
    ax.grid(True, alpha=0.3, linewidth=0.5)
    if title:
        ax.set_title(title, fontsize=11)
    
    fig.tight_layout(pad=0)
    out_path = os.path.join(out_dir, filename)
    fig.savefig(out_path, dpi=150, bbox_inches='tight',
                facecolor='white', edgecolor='none')
    plt.close(fig)
    print(f"  Saved: {out_path}")

print("\nRendering views...")
render_view(90, 0, "chicago_topdown.png", "Chicago — Top Down")
render_view(30, 45, "chicago_iso_se.png", "Chicago — Isometric SE")
print("Done!")
