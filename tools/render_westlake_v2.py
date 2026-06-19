"""Ultra-fast 2D render from 3MF XML - scatter approach."""
import zipfile, os, re, time
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

t0 = time.time()
p = "output/westlake_cli/full_westlake_cli_0619_0812.3mf"
out_dir = "output/westlake_cli"

with zipfile.ZipFile(p) as z:
    obj_xml = z.read("3D/Objects/object_1.model").decode()

objects = re.split(r'<object\s+id="(\d+)"', obj_xml)[1:]

layer_params = {
    "4": {"color": "#1a2e66", "name": "water", "order": 0, "alpha": 1.0},
    "1": {"color": "#c4b49a", "name": "terrain", "order": 1, "alpha": 1.0},
    "7": {"color": "#d1c4b0", "name": "block_base", "order": 2, "alpha": 1.0},
    "6": {"color": "#f0e6d4", "name": "landmarks", "order": 3, "alpha": 0.9},
    "2": {"color": "#ebebe0", "name": "buildings", "order": 3, "alpha": 0.9},
    "3": {"color": "#404040", "name": "roads", "order": 4, "alpha": 1.0},
}

fig, ax = plt.subplots(figsize=(14, 14), dpi=120)
ax.set_aspect('equal')
ax.axis('off')

for i in range(0, len(objects), 2):
    oid = objects[i]
    body = objects[i + 1].split("</object>")[0]
    
    xs, ys = [], []
    for m in re.finditer(r'<vertex\s+x="([-\d.]+)"\s+y="([-\d.]+)"\s+z="([-\d.]+)"', body):
        xs.append(float(m.group(1)))
        ys.append(float(m.group(2)))
    
    if not xs:
        continue
    
    xs = np.array(xs)
    ys = np.array(ys)
    
    params = layer_params.get(oid, {"color": "#999999", "name": f"mesh_{oid}", "order": 5, "alpha": 0.5})
    
    if len(xs) > 50000:
        ax.hexbin(xs, ys, gridsize=300, cmap=plt.cm.colors.ListedColormap([params["color"]]),
                  alpha=params["alpha"], mincnt=1)
    elif len(xs) > 1000:
        ax.hexbin(xs, ys, gridsize=200, cmap=plt.cm.colors.ListedColormap([params["color"]]),
                  alpha=params["alpha"], mincnt=1)
    else:
        ax.scatter(xs, ys, s=2, c=params["color"], alpha=params["alpha"], linewidths=0)
    
    print(f"  {params['name']} (id={oid}): {len(xs):,} vertices")

out_path = os.path.join(out_dir, "westlake_preview.png")
fig.savefig(out_path, dpi=120, bbox_inches='tight', facecolor='white', edgecolor='none', pad_inches=0)
plt.close(fig)
print(f"\nSaved: {out_path} ({time.time()-t0:.1f}s)")
