"""Build an apples-to-apples West Lake block-base comparison.

The script reuses the final non-block meshes from a known-good 3MF and the
cached terrain/preprocess result.  This isolates exactly one variable:
``block_base``.  It writes off/flat/textured 3MF files, a metrics JSON file,
and local oblique PNGs based on the final meshes rather than the 2D layer
preview.
"""

from __future__ import annotations

import argparse
from array import array
import json
import os
from pathlib import Path
import pickle
import re
import sys
import time
from typing import Dict, Iterable, Optional
import zipfile

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
import numpy as np
import trimesh

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _TEXTURE_STYLE_OF_DEEPSEEK.block_base import build_deepseek_block_base_v3
from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm


OBJECT_NAMES = {
    1: "terrain",
    2: "buildings",
    3: "roads",
    4: "water",
    5: "vegetation",
    6: "landmarks",
    7: "block_base",
}
COLORS = {
    "terrain": np.array([0.60, 0.60, 0.60]),
    "buildings": np.array([0.96, 0.96, 0.93]),
    "roads": np.array([0.52, 0.52, 0.52]),
    "water": np.array([0.08, 0.08, 0.08]),
    "vegetation": np.array([0.58, 0.58, 0.58]),
    "landmarks": np.array([1.00, 1.00, 1.00]),
    "block_base": np.array([0.90, 0.90, 0.87]),
}
_OBJECT_RE = re.compile(r'<object id="(\d+)"')
_VERTEX_RE = re.compile(
    r'<vertex x="([^"]+)" y="([^"]+)" z="([^"]+)"'
)
_FACE_RE = re.compile(r'<triangle v1="(\d+)" v2="(\d+)" v3="(\d+)"')


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline",
        type=Path,
        default=ROOT / "output/westlake_cli/full_westlake_cli_0815_1915.3mf",
    )
    parser.add_argument(
        "--terrain-cache",
        type=Path,
        default=ROOT / "cache/pipeline/westlake_cli/terrain_v1_e6610253cb13.pkl",
    )
    parser.add_argument(
        "--layers-cache",
        type=Path,
        default=ROOT / "cache/pipeline/westlake_cli/preprocess_v1_68a3ab1d54b1.pkl",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "output/westlake_cli/block_base_comparison",
    )
    return parser


def read_exported_meshes(path: Path) -> Dict[str, trimesh.Trimesh]:
    """Stream meshes from this project's line-oriented 3MF exporter.

    ``array`` avoids millions of Python tuples while reading the 475 MB XML
    used by the textured baseline.
    """
    meshes: Dict[str, trimesh.Trimesh] = {}
    current_id: Optional[int] = None
    vertices = array("d")
    faces = array("I")

    with zipfile.ZipFile(path) as zf:
        with zf.open("3D/Objects/object_1.model") as stream:
            for raw in stream:
                line = raw.decode("utf-8")
                object_match = _OBJECT_RE.search(line)
                if object_match:
                    current_id = int(object_match.group(1))
                    vertices = array("d")
                    faces = array("I")
                    continue
                if current_id is None:
                    continue

                vertex_match = _VERTEX_RE.search(line)
                if vertex_match:
                    vertices.extend(float(value) for value in vertex_match.groups())
                    continue
                face_match = _FACE_RE.search(line)
                if face_match:
                    faces.extend(int(value) for value in face_match.groups())
                    continue
                if "</object>" not in line:
                    continue

                name = OBJECT_NAMES.get(current_id)
                if name and vertices and faces:
                    vertex_array = np.frombuffer(vertices, dtype=np.float64).reshape((-1, 3))
                    face_array = np.frombuffer(faces, dtype=np.uint32).reshape((-1, 3))
                    meshes[name] = trimesh.Trimesh(
                        vertices=vertex_array.copy(),
                        faces=face_array.astype(np.int64),
                        process=False,
                    )
                    print(
                        f"  parsed {name}: {len(vertex_array):,} vertices, "
                        f"{len(face_array):,} faces"
                    )
                current_id = None
    return meshes


def _crop_triangles(
    mesh: trimesh.Trimesh,
    center_xy: tuple[float, float],
    half_width: float,
    max_faces: int,
) -> np.ndarray:
    """Select a bounded, deterministic face sample without a huge temporary."""
    selected = []
    cx, cy = center_xy
    chunk_size = 100_000
    for start in range(0, len(mesh.faces), chunk_size):
        face_chunk = mesh.faces[start:start + chunk_size]
        centroids = mesh.vertices[face_chunk][:, :, :2].mean(axis=1)
        keep = (
            (np.abs(centroids[:, 0] - cx) <= half_width)
            & (np.abs(centroids[:, 1] - cy) <= half_width)
        )
        local = np.flatnonzero(keep) + start
        if len(local):
            selected.append(local)
    if not selected:
        return np.empty((0, 3, 3), dtype=np.float64)
    indices = np.concatenate(selected)
    if len(indices) > max_faces:
        indices = indices[np.linspace(0, len(indices) - 1, max_faces, dtype=int)]
    return mesh.vertices[mesh.faces[indices]]


def render_oblique(
    meshes: Dict[str, trimesh.Trimesh],
    output_path: Path,
    title: str,
    center_xy: tuple[float, float] = (-10.0, 0.0),
    half_width: float = 18.0,
) -> None:
    """Render the same local crop for all modes from final 3D meshes."""
    fig = plt.figure(figsize=(10, 9), dpi=150)
    ax = fig.add_subplot(111, projection="3d")
    z_values = []
    layer_order = (
        "water", "terrain", "block_base", "buildings", "landmarks", "roads"
    )
    for name in layer_order:
        mesh = meshes.get(name)
        if mesh is None or not len(mesh.faces):
            continue
        triangles = _crop_triangles(mesh, center_xy, half_width, max_faces=45_000)
        if not len(triangles):
            continue
        z_values.append(triangles[:, :, 2].ravel())
        v1, v2, v3 = triangles[:, 0], triangles[:, 1], triangles[:, 2]
        normals = np.cross(v2 - v1, v3 - v1)
        lengths = np.linalg.norm(normals, axis=1, keepdims=True)
        lengths[lengths < 1e-10] = 1.0
        normals /= lengths
        light = np.array([0.5, -0.5, 0.707])
        shade = (0.42 + 0.58 * np.abs(normals @ light)).reshape((-1, 1))
        colors = np.clip(COLORS[name] * shade, 0.0, 1.0)
        ax.add_collection3d(
            Poly3DCollection(triangles, facecolors=colors, edgecolors="none")
        )

    cx, cy = center_xy
    ax.set_xlim(cx - half_width, cx + half_width)
    ax.set_ylim(cy - half_width, cy + half_width)
    if z_values:
        z = np.concatenate(z_values)
        z_min, z_max = float(np.nanmin(z)), float(np.nanmax(z))
    else:
        z_min, z_max = -2.0, 6.0
    ax.set_zlim(z_min, max(z_max, z_min + 1.0))
    ax.set_box_aspect((1, 1, 0.35))
    ax.view_init(elev=34, azim=-58)
    ax.set_axis_off()
    ax.set_title(title)
    fig.savefig(output_path, bbox_inches="tight", pad_inches=0.05, facecolor="white")
    plt.close(fig)


def mesh_stats(mesh: Optional[trimesh.Trimesh]) -> dict:
    if mesh is None:
        return {"vertices": 0, "faces": 0, "watertight": None}
    return {
        "vertices": int(len(mesh.vertices)),
        "faces": int(len(mesh.faces)),
        "watertight": bool(mesh.is_watertight),
    }


def main() -> int:
    args = build_parser().parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Reading known-good baseline: {args.baseline}")
    baseline_meshes = read_exported_meshes(args.baseline)
    textured_mesh = baseline_meshes.get("block_base")
    common_meshes = {
        name: mesh for name, mesh in baseline_meshes.items() if name != "block_base"
    }

    with args.terrain_cache.open("rb") as stream:
        terrain_mesh = pickle.load(stream)
    with args.layers_cache.open("rb") as stream:
        layers = pickle.load(stream)

    bbox = bbox_to_utm(30.13, 120.01, 30.36, 120.29)
    scale = 196.0 / max(bbox["width_m"], bbox["height_m"])

    modes: Dict[str, Optional[trimesh.Trimesh]] = {
        "off": None,
        "textured": textured_mesh,
    }
    print("Building flat block base from the same cached polygons and terrain...")
    t0 = time.time()
    modes["flat"] = build_deepseek_block_base_v3(
        list(layers.block_base),
        terrain_mesh,
        scale,
        brick_style=False,
        block_classes=None,
    )
    flat_seconds = time.time() - t0
    print(f"  flat build: {flat_seconds:.1f}s")

    metrics = {
        "baseline": str(args.baseline),
        "common_faces": int(sum(len(mesh.faces) for mesh in common_meshes.values())),
        "modes": {},
    }
    for mode in ("off", "flat", "textured"):
        block_mesh = modes[mode]
        output_meshes = dict(common_meshes)
        output_meshes["block_base"] = block_mesh
        output_3mf = args.output_dir / f"westlake_block-{mode}.3mf"
        export_deepseek_3mf(output_meshes, str(output_3mf))
        output_png = args.output_dir / f"westlake_block-{mode}_oblique.png"
        render_oblique(output_meshes, output_png, f"West Lake block_base: {mode}")
        stats = mesh_stats(block_mesh)
        stats.update({
            "total_faces": int(
                sum(len(mesh.faces) for mesh in output_meshes.values() if mesh is not None)
            ),
            "file_mb": round(output_3mf.stat().st_size / (1024 * 1024), 2),
            "3mf": str(output_3mf),
            "preview": str(output_png),
        })
        if mode == "flat":
            stats["build_seconds"] = round(flat_seconds, 2)
        metrics["modes"][mode] = stats
        print(f"  {mode}: {stats}")

    metrics_path = args.output_dir / "metrics.json"
    metrics_path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    print(f"Wrote {metrics_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
