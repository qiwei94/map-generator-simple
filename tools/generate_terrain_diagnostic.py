#!/usr/bin/env python3
"""Generate a terrain-only West Lake diagnostic artifact.

This intentionally skips OSM extraction and every city layer.  It exercises
the exact formal terrain builder, exports a one-part 3MF/GLB, and renders the
actual final mesh vertices so terrain topology and Z mapping can be reviewed
before an expensive full-city rerun.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf
from _TEXTURE_STYLE_OF_DEEPSEEK.design_spec import (
    build_design_spec,
    write_design_spec,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.print_profile import DEFAULT_PRINTER_PROFILE, PrintScale
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import (
    build_deepseek_terrain,
    build_terrain_evidence,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.elevation import (
    fetch_elevation_grid,
    fetch_elevation_grid_tiled,
)
from _TEXTURE_STYLE_OF_DEEPSEEK._tile_grid import snap_bbox
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm
from _TEXTURE_STYLE_OF_DEEPSEEK import config as _cfg
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d import config as _t3d_cfg


PRESETS = {
    "westlake": (30.13, 120.01, 30.36, 120.29),
}


def _render_final_mesh(mesh, evidence: dict, output_path: Path,
                       label: str) -> None:
    os.environ.setdefault("MPLCONFIGDIR", "/tmp/map-generator-mpl")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LightSource

    rows, cols = evidence["grid"]["output_shape"]
    bottom_z = float(mesh.vertices[:, 2].min())
    top = np.asarray(mesh.vertices)[mesh.vertices[:, 2] > bottom_z + 1e-6]
    if len(top) != rows * cols:
        raise ValueError(
            f"cannot reconstruct regular surface: {len(top)} != {rows}x{cols}")
    order = np.lexsort((top[:, 0], top[:, 1]))
    top = top[order]
    xx = top[:, 0].reshape(rows, cols)
    yy = top[:, 1].reshape(rows, cols)
    zz = top[:, 2].reshape(rows, cols)

    dx = float(np.median(np.diff(xx[0])))
    dy = float(np.median(np.diff(yy[:, 0])))
    shade = LightSource(azdeg=315, altdeg=38).hillshade(
        zz, vert_exag=1.0, dx=dx, dy=dy)

    fig = plt.figure(figsize=(15, 7.5), constrained_layout=True)
    ax0 = fig.add_subplot(1, 2, 1)
    ax0.imshow(
        shade,
        origin="lower",
        extent=[xx.min(), xx.max(), yy.min(), yy.max()],
        cmap="gray",
    )
    ax0.set_aspect("equal")
    ax0.set_title("Final 3MF terrain surface · hillshade")
    ax0.set_xlabel("model X (mm)")
    ax0.set_ylabel("model Y (mm)")

    ax1 = fig.add_subplot(1, 2, 2, projection="3d")
    stride = max(1, int(max(rows, cols) / 110))
    ax1.plot_surface(
        xx[::stride, ::stride],
        yy[::stride, ::stride],
        zz[::stride, ::stride],
        cmap="Greys",
        linewidth=0,
        antialiased=True,
        shade=True,
    )
    ax1.view_init(elev=28, azim=-62)
    ax1.set_box_aspect((np.ptp(xx), np.ptp(yy), 28))
    ax1.set_title("Oblique review · display Z expanded for inspection")
    ax1.set_xlabel("X (mm)")
    ax1.set_ylabel("Y (mm)")
    ax1.set_zlabel("Z (mm)")

    fig.suptitle(
        f"{label} terrain-only diagnostic\n"
        f"actual max XY edge {evidence['surface_mesh']['max_xy_edge_mm']:.3f} mm · "
        f"artifact Z span {evidence['artifact_z_span_mm']:.3f} mm · QEM off"
    )
    fig.savefig(output_path, dpi=180)
    plt.close(fig)


def _parse_bbox(value: str) -> tuple[float, float, float, float]:
    try:
        bbox = tuple(float(part.strip()) for part in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "bbox must contain four comma-separated numbers") from exc
    if len(bbox) != 4 or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        raise argparse.ArgumentTypeError(
            "bbox must be south,west,north,east")
    return bbox


def _grid_shape_for_bbox(
    south: float, west: float, north: float, east: float, resolution: int,
) -> tuple[int, int]:
    lat_range = north - south
    lon_range = east - west
    if lat_range >= lon_range:
        return resolution, max(2, int(resolution * lon_range / lat_range))
    return max(2, int(resolution * lat_range / lon_range)), resolution


def _crop_grid_to_bbox(
    grid: np.ndarray,
    source_bbox: tuple[float, float, float, float],
    target_bbox: tuple[float, float, float, float],
    target_shape: tuple[int, int],
) -> np.ndarray:
    """Crop/interpolate a snapped DEM grid exactly like the formal path."""
    from scipy.ndimage import map_coordinates

    source_s, source_w, source_n, source_e = source_bbox
    target_s, target_w, target_n, target_e = target_bbox
    rows, cols = grid.shape
    out_rows, out_cols = target_shape
    rr = np.linspace(
        (target_s - source_s) / (source_n - source_s) * (rows - 1),
        (target_n - source_s) / (source_n - source_s) * (rows - 1),
        out_rows,
    )
    cc = np.linspace(
        (target_w - source_w) / (source_e - source_w) * (cols - 1),
        (target_e - source_w) / (source_e - source_w) * (cols - 1),
        out_cols,
    )
    row_grid, col_grid = np.meshgrid(rr, cc, indexing="ij")
    return map_coordinates(
        grid, [row_grid, col_grid], order=1, mode="nearest",
    ).astype(grid.dtype, copy=False)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preset", choices=sorted(PRESETS), default="westlake")
    parser.add_argument(
        "--bbox", type=_parse_bbox,
        help="south,west,north,east; overrides --preset")
    parser.add_argument("--name", help="artifact label; defaults to preset")
    parser.add_argument("--output-dir")
    parser.add_argument("--resolution", type=int, default=1024)
    parser.add_argument("--elevation-file")
    parser.add_argument(
        "--fetch-mode", choices=("tiled", "direct"), default="tiled",
        help="tiled reproduces the formal S1 cache path; direct is an A/B control",
    )
    parser.add_argument(
        "--smoothing-sigma", type=float,
        help="diagnostic override for the declared S1 smoothing policy",
    )
    parser.add_argument(
        "--max-surface-edge-mm", type=float,
        help="terrain triangle XY edge limit; defaults to 2x extrusion width",
    )
    args = parser.parse_args()

    if args.elevation_file and args.fetch_mode == "tiled":
        parser.error("--elevation-file requires --fetch-mode direct")

    requested_bbox = args.bbox or PRESETS[args.preset]
    label = args.name or args.preset
    safe_label = "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in label.lower())
    output_dir = ROOT / (args.output_dir or
                         f"output/{safe_label}_terrain_diagnostic")
    output_dir.mkdir(parents=True, exist_ok=True)
    south, west, north, east = requested_bbox
    bbox = bbox_to_utm(south, west, north, east)
    width_m = float(bbox["width_m"])
    height_m = float(bbox["height_m"])
    area_km2 = float(bbox["area_km2"])
    scale = PrintScale(width_m, height_m).scale_mm_per_m
    profile = DEFAULT_PRINTER_PROFILE

    # Keep the diagnostic and formal product on the same declared smoothing
    # policy.  The low-level terrain3d package has a generic 1.0 default.
    _t3d_cfg.ELEVATION_SMOOTHING_SIGMA = (
        float(args.smoothing_sigma)
        if args.smoothing_sigma is not None
        else _cfg.ELEVATION_SMOOTHING_SIGMA
    )
    max_surface_edge_mm = (
        float(args.max_surface_edge_mm)
        if args.max_surface_edge_mm is not None
        else profile.terrain_max_surface_edge_mm
    )

    started = time.perf_counter()
    if args.fetch_mode == "tiled":
        snapped_bbox = snap_bbox(south, west, north, east)
        exact_span = max(north - south, east - west)
        snapped_span = max(
            snapped_bbox[2] - snapped_bbox[0],
            snapped_bbox[3] - snapped_bbox[1],
        )
        fetch_resolution = int(
            (args.resolution - 1) * snapped_span / exact_span) + 1
        snapped_dem, dem_sampling = fetch_elevation_grid_tiled(
            *snapped_bbox,
            resolution=fetch_resolution,
            use_cache=True,
            return_evidence=True,
        )
        dem = _crop_grid_to_bbox(
            snapped_dem,
            snapped_bbox,
            requested_bbox,
            _grid_shape_for_bbox(
                south, west, north, east, args.resolution),
        )
        dem_sampling["exact_output_shape"] = list(dem.shape)
        dem_sampling["exact_bbox_wgs84"] = list(requested_bbox)
    else:
        dem = fetch_elevation_grid(
            south, west, north, east,
            resolution=args.resolution,
            use_cache=True,
            elevation_file=args.elevation_file,
        )
        dem_sampling = {
            "mode": "direct",
            "requested_resolution": int(args.resolution),
            "exact_output_shape": list(dem.shape),
        }
    dem_sampling["mode"] = args.fetch_mode
    mesh = build_deepseek_terrain(
        dem,
        width_m,
        height_m,
        area_km2,
        scale,
        base_thickness_mm=0.4,
        max_surface_edge_mm=max_surface_edge_mm,
        min_surface_height_mm=profile.min_surface_height_mm,
    )
    evidence = build_terrain_evidence(mesh)
    evidence.update({
        "name": label,
        "bbox_wgs84": list(requested_bbox),
        "input_dem_shape": list(dem.shape),
        "input_dem_range_m": [float(np.nanmin(dem)), float(np.nanmax(dem))],
        "dem_sampling": dem_sampling,
        "declared_max_surface_edge_mm": float(max_surface_edge_mm),
        "elapsed_seconds": float(time.perf_counter() - started),
        "printer_profile": profile.to_dict(),
    })

    stem = f"{safe_label}_terrain_only_{args.fetch_mode}_v2"
    three_mf = output_dir / f"{stem}.3mf"
    glb = output_dir / f"{stem}.glb"
    preview = output_dir / f"{stem}.png"
    evidence_path = output_dir / "terrain_evidence.json"
    export_deepseek_3mf({"terrain": mesh}, str(three_mf))
    mesh.export(glb)
    _render_final_mesh(mesh, evidence, preview, label)
    evidence_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    design_spec = build_design_spec(
        city=label,
        bbox_wgs84=requested_bbox,
        artifact_path=three_mf,
        pipeline="terrain_diagnostic_v2",
        params={
            "fetch_mode": args.fetch_mode,
            "resolution": int(args.resolution),
            "smoothing_sigma": float(_t3d_cfg.ELEVATION_SMOOTHING_SIGMA),
            "max_surface_edge_mm": float(max_surface_edge_mm),
        },
        decisions={"terrain_only": True, "qem_decimation": False},
        source_features={"dem_cells": int(dem.size)},
        printable_features={"terrain_meshes": 1},
        printability={
            "printer_profile": profile.to_dict(),
            "terrain_max_xy_edge_mm": evidence["surface_mesh"][
                "max_xy_edge_mm"],
            "terrain_faces_over_2mm": evidence["surface_mesh"][
                "faces_over_2mm"],
            "terrain_faces_over_5mm": evidence["surface_mesh"][
                "faces_over_5mm"],
        },
        terrain=evidence,
    )
    design_spec_path = write_design_spec(output_dir, design_spec)

    print(json.dumps({
        "3mf": str(three_mf),
        "glb": str(glb),
        "preview": str(preview),
        "evidence": str(evidence_path),
        "design_spec": str(design_spec_path),
        "metrics": evidence,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
