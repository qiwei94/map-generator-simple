"""桥接验证：validator 校验 + 裁剪区域 3D 截图（block_fill 墙面目检）。

用法: python tools/inspect_3mf_bridge.py output/chicago/full_chicago_xxx.3mf
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import trimesh
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection

from _TEXTURE_STYLE_OF_DEEPSEEK.validator import validate_3mf

MF_PATH = sys.argv[1]
OUT_DIR = os.path.join(os.path.dirname(MF_PATH), "bridge_inspect")
os.makedirs(OUT_DIR, exist_ok=True)

# ── 1. validator ─────────────────────────────────────────────────────
print("=" * 60)
print("[1/2] validator.validate_3mf")
res = validate_3mf(MF_PATH)
n_pass = sum(1 for r in res["rules"] if r["passed"])
for r in res["rules"]:
    mark = "PASS" if r["passed"] else "FAIL"
    print(f"  [{mark}] {r['id']}: {r['name']} — {r.get('detail', '')}")
print(f"  => {n_pass}/{len(res['rules'])} rules passed, overall={'PASS' if res['passed'] else 'FAIL'}")
for e in res["errors"]:
    print(f"  ERROR: {e}")
for w in res["warnings"]:
    print(f"  WARN: {w}")

# ── 2. 裁剪区域 3D 渲染 ──────────────────────────────────────────────
print("\n[2/2] crop renders")
scene = trimesh.load(MF_PATH)
meshes = {n: g for n, g in scene.geometry.items()
          if hasattr(g, "vertices") and len(g.faces) > 0}
all_v = np.vstack([m.vertices for m in meshes.values()])
xmin, ymin = all_v[:, 0].min(), all_v[:, 1].min()
xmax, ymax = all_v[:, 0].max(), all_v[:, 1].max()
W, H = xmax - xmin, ymax - ymin
print(f"  model span: {W:.1f} x {H:.1f} mm, meshes: {list(meshes.keys())}")

COLOR = {
    "terrain": [0.80, 0.76, 0.70],
    "block_base": [0.85, 0.85, 0.85],
    "buildings": [0.96, 0.96, 0.93],
    "landmarks": [1.00, 1.00, 1.00],
    "roads": [0.35, 0.35, 0.35],
    "water": [0.10, 0.18, 0.40],
    "vegetation": [0.55, 0.65, 0.50],
}


def _color(name):
    for k, c in COLOR.items():
        if k in name.lower():
            return np.array(c)
    return np.array([0.7, 0.7, 0.7])


# (名称, 中心x比例, 中心y比例, 半宽mm)  bbox=41.77..41.99 / -87.77..-87.47
CROPS = [
    ("downtown", (-87.63 + 87.77) / 0.30, (41.878 - 41.77) / 0.22, 12.0),
    ("residential", (-87.70 + 87.77) / 0.30, (41.95 - 41.77) / 0.22, 12.0),
]

for tag, fx, fy, half in CROPS:
    cx, cy = xmin + fx * W, ymin + fy * H
    x0, x1, y0, y1 = cx - half, cx + half, cy - half, cy + half
    t0 = time.time()
    parts = []  # (z_order, tri_verts, colors)
    n_faces = 0
    for name, m in meshes.items():
        v, f = m.vertices, m.faces
        cen = v[f].mean(axis=1)
        keep = ((cen[:, 0] >= x0) & (cen[:, 0] <= x1)
                & (cen[:, 1] >= y0) & (cen[:, 1] <= y1))
        if not keep.any():
            continue
        tri = v[f[keep]]
        tri = tri - np.array([cx, cy, 0.0])
        n_faces += len(tri)
        v1, v2, v3 = tri[:, 0], tri[:, 1], tri[:, 2]
        nrm = np.cross(v2 - v1, v3 - v1)
        ln = np.linalg.norm(nrm, axis=1, keepdims=True)
        ln[ln < 1e-10] = 1e-10
        nrm = nrm / ln
        light = np.array([0.5, -0.5, 0.707])
        dot = np.abs(nrm @ light)
        shade = (0.35 + 0.65 * dot).reshape(-1, 1)
        col = np.clip(_color(name) * shade, 0, 1)
        parts.append((tri[:, :, 2].mean(), tri, col))
    print(f"  [{tag}] {n_faces:,} faces in crop ({time.time()-t0:.1f}s extract)")

    for elev, azim, suffix in [(90, -90, "top"), (35, -60, "oblique")]:
        fig = plt.figure(figsize=(12, 12), dpi=130)
        ax = fig.add_subplot(111, projection="3d")
        for _, tri, col in sorted(parts, key=lambda p: p[0]):
            pc = Poly3DCollection(tri, facecolors=col, edgecolors="none")
            ax.add_collection3d(pc)
        r = half
        ax.set_xlim(-r, r); ax.set_ylim(-r, r); ax.set_zlim(0, r * 0.8)
        ax.set_box_aspect((1, 1, 0.4))
        ax.view_init(elev=elev, azim=azim)
        ax.set_axis_off()
        out = os.path.join(OUT_DIR, f"{tag}_{suffix}.png")
        plt.savefig(out, bbox_inches="tight", pad_inches=0.05,
                    facecolor="white")
        plt.close(fig)
        print(f"    -> {out}")

print("\ndone.")
