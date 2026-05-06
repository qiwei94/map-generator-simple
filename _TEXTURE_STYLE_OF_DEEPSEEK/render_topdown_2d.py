"""Render high-quality 2D top-down orthographic view of 3MF scene."""
import trimesh
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Polygon
from matplotlib.collections import PatchCollection
from pathlib import Path

root = Path(r"c:\Users\kiwi\OneDrive\文档\GitHub\map_generator")
mf_path = root / "output/deepseek/West_Lake_deepseek.3mf"
out_dir = root / "output/deepseek/screenshots"
out_dir.mkdir(parents=True, exist_ok=True)

print(f"Loading: {mf_path}")
scene = trimesh.load(str(mf_path))

# Collect all meshes
meshes = {}
for name, geom in scene.geometry.items():
    if hasattr(geom, 'vertices') and len(geom.faces) > 0:
        meshes[name] = geom
        print(f"  {name}: {len(geom.faces)} faces")

# Compute extent
all_v = np.vstack([m.vertices for m in meshes.values()])
x_min, y_min, _ = all_v.min(axis=0)
x_max, y_max, _ = all_v.max(axis=0)
cx, cy = (x_min + x_max) / 2, (y_min + y_max) / 2
span = max(x_max - x_min, y_max - y_min)

print(f"Extent: {x_max-x_min:.1f} x {y_max-y_min:.1f}, span={span:.1f}")

# Color scheme
color_map = {
    "terrain_surface": (0.76, 0.68, 0.60),
    "terrain_walls": (0.50, 0.42, 0.35),
    "buildings": (0.92, 0.92, 0.88),
    "roads": (0.30, 0.30, 0.32),
    "water": (0.10, 0.18, 0.40),
}

def get_color(name):
    for key, col in color_map.items():
        if key in name.lower():
            return col
    return (0.6, 0.6, 0.6)

# Render top-down
fig, ax = plt.subplots(figsize=(14, 14), dpi=150)

# Sort layers: water → terrain → buildings → roads
render_order = sorted(meshes.keys(),
                      key=lambda n: meshes[n].vertices[:, 2].mean())

for name in render_order:
    m = meshes[name]
    verts_2d = m.vertices[:, :2]  # XY only
    faces = m.faces
    base_color = get_color(name)
    
    # Build face polygons
    patches = []
    face_colors = []
    
    tri_verts = verts_2d[faces]
    for tri in tri_verts:
        # Check triangle area (skip degenerate)
        v1, v2, v3 = tri
        area = 0.5 * abs(np.cross(v2 - v1, v3 - v1))
        if area < 1e-6:
            continue
        patches.append(Polygon(tri, closed=True))
        face_colors.append(base_color)
    
    if patches:
        pc = PatchCollection(patches, facecolors=face_colors,
                            edgecolors='none', linewidth=0, alpha=1.0)
        ax.add_collection(pc)
    print(f"  Rendered {name}: {len(patches)} faces")

ax.set_aspect('equal')
margin = span * 0.02
ax.set_xlim(cx - span/2 - margin, cx + span/2 + margin)
ax.set_ylim(cy - span/2 - margin, cy + span/2 + margin)
ax.set_xlabel('X (mm)')
ax.set_ylabel('Y (mm)')
ax.set_title('West Lake 3MF — Top-Down View\nTerrain (brown) + Buildings (white) + Roads (dark) + Water (blue)',
             fontsize=11)
ax.grid(True, alpha=0.15, linewidth=0.5)

# Scale bar
bar_len = 20  # mm
bar_y = cy - span/2 + margin * 4
bar_x = cx + span/2 - bar_len - margin
ax.plot([bar_x, bar_x + bar_len], [bar_y, bar_y], 'k-', linewidth=3)
ax.text(bar_x + bar_len/2, bar_y - margin*2, f'{bar_len} mm',
        ha='center', fontsize=8)

out_path = out_dir / "westlake_topdown_2d.png"
fig.tight_layout(pad=0.5)
fig.savefig(str(out_path), dpi=150, bbox_inches='tight',
            facecolor='white', edgecolor='none')
plt.close(fig)
print(f"\nSaved: {out_path.name}")
