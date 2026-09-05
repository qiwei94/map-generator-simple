#!/usr/bin/env python3
"""Render an actual-Z terrain/hero oblique diagnostic from a formal 3MF."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import zipfile

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _TEXTURE_STYLE_OF_DEEPSEEK.validator import (  # noqa: E402
    _component_slender_metrics,
    _get_object_meshes,
)


def _top_surface_grid(vertices: np.ndarray, shape: tuple[int, int]):
    """Recover the highest terrain Z for every regular-grid XY sample."""
    vertices = np.asarray(vertices, dtype=float)
    xy = np.round(vertices[:, :2], 8)
    order = np.lexsort((xy[:, 0], xy[:, 1]))
    xy_sorted = xy[order]
    z_sorted = vertices[order, 2]
    starts = np.r_[
        0,
        np.flatnonzero(np.any(np.diff(xy_sorted, axis=0), axis=1)) + 1,
    ]
    top_xy = xy_sorted[starts]
    top_z = np.maximum.reduceat(z_sorted, starts)
    if len(top_z) != shape[0] * shape[1]:
        raise ValueError(
            f"terrain XY samples {len(top_z)} do not match declared grid {shape}"
        )
    return (
        top_xy[:, 0].reshape(shape),
        top_xy[:, 1].reshape(shape),
        top_z.reshape(shape),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--design-spec", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--azimuth", type=float, default=-58.0)
    parser.add_argument("--elevation", type=float, default=23.0)
    args = parser.parse_args()

    design_path = args.design_spec or args.path.parent / "design_spec.json"
    design = json.loads(design_path.read_text(encoding="utf-8"))
    shape = tuple(int(value) for value in design["terrain"]["grid"]["output_shape"])
    printer = design["printability"]["printer_profile"]
    extrusion_width = float(printer["extrusion_width_mm"])

    with zipfile.ZipFile(args.path, "r") as archive:
        objects = _get_object_meshes(archive)
    terrain = objects["terrain"]
    landmarks = objects.get("landmarks")
    xx, yy, zz = _top_surface_grid(terrain["vertices"], shape)

    if landmarks and len(landmarks["faces"]):
        landmark_metrics = _component_slender_metrics(
            landmarks["vertices"], landmarks["faces"], extrusion_width)
        landmark_top = float(np.max(landmarks["vertices"][:, 2]))
    else:
        landmark_metrics = {"component_count": 0}
        landmark_top = None
    terrain_peak = float(np.max(zz))

    os.environ.setdefault("MPLCONFIGDIR", "/tmp/map-generator-mpl")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection

    output = args.output or args.path.parent / f"{args.path.stem}_actual_z_oblique.png"
    output.parent.mkdir(parents=True, exist_ok=True)
    fig = plt.figure(figsize=(14, 9), dpi=170)
    ax = fig.add_subplot(111, projection="3d")
    stride = max(1, int(max(shape) / 110))
    ax.plot_surface(
        xx[::stride, ::stride], yy[::stride, ::stride], zz[::stride, ::stride],
        color="#b6b2aa", linewidth=0, antialiased=True, shade=True, alpha=0.94,
    )
    if landmarks and len(landmarks["faces"]):
        triangles = landmarks["vertices"][landmarks["faces"]]
        ax.add_collection3d(Poly3DCollection(
            triangles, facecolor="#c45138", edgecolor="none", alpha=0.98))
    ax.set_xlim(float(xx.min()), float(xx.max()))
    ax.set_ylim(float(yy.min()), float(yy.max()))
    ax.set_zlim(float(zz.min()), float(zz.max()) + 0.12)
    ax.set_box_aspect((np.ptp(xx), np.ptp(yy), 34))
    ax.view_init(elev=args.elevation, azim=args.azimuth)
    ax.set_xlabel("model X (mm)")
    ax.set_ylabel("model Y (mm)")
    ax.set_zlabel("actual Z (mm)")
    hero_label = "none" if landmark_top is None else f"{landmark_top:.3f} mm"
    ax.set_title(
        f"{design.get('city', args.path.stem)} · actual-Z hierarchy\n"
        f"terrain peak {terrain_peak:.3f} mm · hero top {hero_label}"
    )
    fig.savefig(output, bbox_inches="tight", facecolor="white")
    plt.close(fig)

    evidence = {
        "artifact": str(args.path.resolve()),
        "design_spec": str(design_path.resolve()),
        "output": str(output.resolve()),
        "terrain_peak_z_mm": terrain_peak,
        "terrain_surface_min_z_mm": float(np.min(zz)),
        "landmark_max_top_z_mm": landmark_top,
        "terrain_owns_peak": landmark_top is None or landmark_top <= terrain_peak + 1e-4,
        "landmarks": landmark_metrics,
        "view": {"elevation_degrees": args.elevation, "azimuth_degrees": args.azimuth},
    }
    evidence_path = output.with_suffix(".json")
    evidence_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
