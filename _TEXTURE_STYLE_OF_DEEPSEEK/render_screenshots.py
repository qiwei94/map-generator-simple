"""Render 3MF scene to PNG screenshots - fast matplotlib approach."""
import trimesh
import matplotlib.pyplot as plt
import numpy as np
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
from pathlib import Path

root = Path(r"c:\Users\kiwi\OneDrive\文档\GitHub\map_generator")
mf_path = root / "output/deepseek/West_Lake_deepseek.3mf"
out_dir = root / "output/deepseek/screenshots"
out_dir.mkdir(parents=True, exist_ok=True)

print(f"Loading 3MF: {mf_path}")
scene = trimesh.load(str(mf_path))
print(f"Loaded scene with {len(scene.geometry)} geometries")

# Gather meshes for rendering (no subsampling - pipeline already decimates)
meshes = {}
total_faces = 0
for name, geom in scene.geometry.items():
    if hasattr(geom, 'vertices') and len(geom.faces) > 0:
        m = geom.copy()
        meshes[name] = m
        total_faces += len(m.faces)
        print(f"  {name}: {len(m.vertices)} verts, {len(m.faces)} faces")
print(f"  Total faces for rendering: {total_faces}")

# Compute center and span
all_verts = np.vstack([m.vertices for m in meshes.values()])
center = (all_verts.min(axis=0) + all_verts.max(axis=0)) / 2
span_xy = max(np.ptp(all_verts[:, 0]), np.ptp(all_verts[:, 1]))
span_z = np.ptp(all_verts[:, 2])
print(f"Span XY: {span_xy:.1f}, Z: {span_z:.1f}")

# Color scheme
color_map = {
    "terrain_surface": np.array([0.78, 0.70, 0.62]),
    "terrain_walls": np.array([0.55, 0.47, 0.39]),
    "buildings": np.array([0.94, 0.94, 0.90]),
    "roads": np.array([0.35, 0.35, 0.35]),
    "water": np.array([0.10, 0.18, 0.40]),
}

def get_color(name):
    for key, col in color_map.items():
        if key in name.lower():
            return col
    return np.array([0.7, 0.7, 0.7])

def render_view(elev, azim, filename, title=""):
    fig = plt.figure(figsize=(14, 12), dpi=150)
    ax = fig.add_subplot(111, projection='3d')
    
    # Sort by Z for layer order: water bottom, terrain, buildings, roads top
    sorted_names = sorted(meshes.keys(), key=lambda n: meshes[n].vertices[:, 2].mean())
    
    for name in sorted_names:
        m = meshes[name]
        verts = m.vertices - center
        faces = m.faces
        base_color = get_color(name)
        
        tri_verts = verts[faces]
        if len(tri_verts) == 0:
            continue
        
        # Compute face normals for directional lighting
        v1, v2, v3 = tri_verts[:, 0], tri_verts[:, 1], tri_verts[:, 2]
        normals = np.cross(v2 - v1, v3 - v1)
        norm_len = np.linalg.norm(normals, axis=1, keepdims=True)
        norm_len[norm_len < 1e-10] = 1e-10
        normals = normals / norm_len
        
        # Light from upper-front-right
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
    fig.savefig(str(out_dir / filename), dpi=150, bbox_inches='tight',
                facecolor='white', edgecolor='none')
    plt.close(fig)
    print(f"  Saved: {filename}")

print("\nRendering views...")
render_view(90, 0, "westlake_topdown.png", "West Lake — Top Down")
render_view(30, 45, "westlake_iso_se.png", "West Lake — Isometric SE")
render_view(30, 225, "westlake_iso_sw.png", "West Lake — Isometric SW")
render_view(30, 315, "westlake_iso_nw.png", "West Lake — Isometric NW")
render_view(8, 180, "westlake_front_low.png", "West Lake — Front Low Angle")
render_view(8, 90, "westlake_side_low.png", "West Lake — Side Low Angle")

print(f"\nDone! Screenshots in: {out_dir}")
