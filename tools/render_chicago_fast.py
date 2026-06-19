"""Fast 2D top-down render from 3MF XML geometry data (no trimesh 3D)."""
import zipfile, os, re
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from shapely.geometry import Polygon as SPolygon
from shapely.ops import unary_union
from matplotlib.patches import Polygon as MPolygon
from matplotlib.collections import PatchCollection
import time

t0 = time.time()
p = "output/chicago_cli/full_chicago_cli_2layer_0619_0936.3mf"
out_dir = "output/chicago_cli"

# Define layer display colors for 2D top-down
LAYER_COLORS = {
    1: (0.78, 0.70, 0.62, 1.0),   # terrain - warm beige
    2: (0.92, 0.92, 0.88, 1.0),   # buildings - off-white
    3: (0.25, 0.25, 0.28, 1.0),   # roads - dark gray
    4: (0.08, 0.15, 0.35, 1.0),   # water - dark blue
    5: (0.45, 0.55, 0.30, 0.8),   # vegetation - muted green
    6: (0.95, 0.90, 0.82, 1.0),   # landmarks - warm white
    7: (0.82, 0.76, 0.68, 1.0),   # block_base - warm base
}

# Parse 3MF XML
print(f"Loading 3MF...")
with zipfile.ZipFile(p) as z:
    obj_xml = z.read("3D/Objects/object_1.model").decode()

# Split by object (each object is a sub-mesh)
objects = re.split(r'<object\s+id="(\d+)"', obj_xml)[1:]  # pairs of (id, rest)

render_polys = {}

for i in range(0, len(objects), 2):
    oid = objects[i]
    oid_int = int(oid)
    body = objects[i + 1].split("</object>")[0]
    
    # Parse vertices
    verts = []
    for m in re.finditer(r'<vertex\s+x="([^"]+)"\s+y="([^"]+)"\s+z="([^"]+)"', body):
        verts.append((float(m.group(1)), float(m.group(2)), float(m.group(3))))
    
    if not verts:
        continue
    
    verts = np.array(verts)
    
    # Parse triangles
    faces = []
    for m in re.finditer(r'<triangle\s+v1="(\d+)"\s+v2="(\d+)"\s+v3="(\d+)"', body):
        faces.append((int(m.group(1)), int(m.group(2)), int(m.group(3))))
    
    if not faces:
        continue
    
    faces = np.array(faces)
    
    # Build XY polygons (top-down projection) - union all triangles
    # For large meshes, sample a subset of faces for speed
    max_faces = 50000
    if len(faces) > max_faces:
        # Use stratified sampling
        idx = np.linspace(0, len(faces)-1, max_faces, dtype=int)
        faces_sample = faces[idx]
    else:
        faces_sample = faces
    
    # Convert to shapely polygons and union
    tri_polys = []
    for f in faces_sample:
        xy = verts[f, :2]
        tri_polys.append(SPolygon(xy))
    
    print(f"  Mesh {oid}: {len(verts)} verts, {len(faces)} faces, sampled {len(faces_sample)} for 2D")
    
    if tri_polys:
        render_polys[oid_int] = tri_polys

# Render 2D top-down
print(f"\nRendering 2D top-down...")
fig, ax = plt.subplots(figsize=(16, 16), dpi=150)

# Draw in depth order (lowest Z first)
sorted_ids = sorted(render_polys.keys(), key=lambda oid: -oid if oid == 4 else oid)
# Actually draw by layer order: water(4) bottom, then terrain(1), block_base(7), landmarks(6), buildings(2), roads(3)
layer_order = [4, 1, 7, 6, 2, 3, 5]
for oid in layer_order:
    if oid not in render_polys:
        continue
    color = LAYER_COLORS.get(oid, (0.6, 0.6, 0.6, 1.0))
    polys = render_polys[oid]
    
    patches = [MPolygon(np.array(p.exterior.coords), closed=True) for p in polys if not p.is_empty and p.area > 0.01]
    if patches:
        pc = PatchCollection(patches, facecolor=color[:3], edgecolor='none', alpha=color[3] if len(color) > 3 else 1.0, linewidth=0)
        ax.add_collection(pc)

ax.set_aspect('equal')
ax.axis('off')
# Add minimal padding
all_coords = np.vstack([np.array(p.exterior.coords) for polys in render_polys.values() for p in polys if not p.is_empty])
if len(all_coords) > 0:
    ax.set_xlim(all_coords[:, 0].min() - 5, all_coords[:, 0].max() + 5)
    ax.set_ylim(all_coords[:, 1].min() - 5, all_coords[:, 1].max() + 5)

out_path = os.path.join(out_dir, "chicago_topdown_2d.png")
fig.savefig(out_path, dpi=150, bbox_inches='tight', facecolor='white', edgecolor='none', pad_inches=0)
plt.close(fig)

print(f"Saved: {out_path} ({time.time()-t0:.1f}s)")
