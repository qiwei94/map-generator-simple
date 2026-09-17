#!/usr/bin/env python3
"""Generate a DEM-owned Changbai white-topcoat style trial.

The bundled reference satellite image is used only to recover Tianchi's water
outline.  Terrain and the gray/white height field are generated from the
public DEM supplied on the command line.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np
import rasterio
import trimesh
from PIL import Image
from rasterio.features import shapes
from scipy.ndimage import label, shift, zoom
from shapely import affinity
from shapely.geometry import shape

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from _TEXTURE_STYLE_OF_DEEPSEEK.config import compute_scale
from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf
from _TEXTURE_STYLE_OF_DEEPSEEK.natural_layers import build_variable_topcoat
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import resolve_terrain_surface_plan
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm


def _read_dem(path: Path, bbox, shape=(420, 420)) -> np.ndarray:
    south, west, north, east = bbox
    with rasterio.open(path) as src:
        data = src.read(1, masked=True)
        rows, cols = shape
        yy, xx = np.meshgrid(np.linspace(north, south, rows),
                             np.linspace(west, east, cols), indexing="ij")
        sampled = list(src.sample(np.column_stack((xx.ravel(), yy.ravel()))))
        # The input Terrain-RGB GeoTIFF uses EPSG:3857, so transform points.
        from pyproj import Transformer
        transformer = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
        mx, my = transformer.transform(xx.ravel(), yy.ravel())
        sampled = np.asarray(list(src.sample(np.column_stack((mx, my)))), dtype=float)
    result = sampled[:, 0].reshape(shape)
    if not np.isfinite(result).all():
        raise ValueError("DEM sampling produced missing cells")
    return result


def _guide_lake_mask(path: Path, shape: tuple[int, int], bbox) -> np.ndarray:
    """Extract the central dark lake, discarding the white image border/logo."""
    image = np.asarray(Image.open(path).convert("RGB"))
    # Reference image's geographic square is manually bounded by its clean
    # satellite panel; the logo lives below this crop.
    panel = image[72:862, 105:895]
    dark = ((panel[:, :, 0] < 52) & (panel[:, :, 1] < 86)
            & (panel[:, :, 2] < 112)).astype(np.uint8)
    labels, count = label(dark)
    center = np.array([panel.shape[1] / 2, panel.shape[0] / 2])
    choices = []
    for i in range(1, count + 1):
        rows, cols = np.where(labels == i)
        area = len(rows)
        if area < 300:
            continue
        centroid = np.array([cols.mean(), rows.mean()])
        choices.append((np.linalg.norm(centroid - center), area, i))
    if not choices:
        raise ValueError("could not find Tianchi in satellite guide")
    selected_label = min(choices)[2]
    mask = labels == selected_label
    resized = zoom(mask.astype(float), (shape[0] / mask.shape[0],
                                        shape[1] / mask.shape[1]), order=0) > .5
    # The supplied reference image is a presentation crop, not a GeoTIFF.
    # Anchor its extracted lake at Tianchi's known WGS84 centre before using it
    # on our independently selected 20km DEM crop.
    south, west, north, east = bbox
    target = np.array([
        (north - 42.006) / (north - south) * (shape[0] - 1),
        (128.054 - west) / (east - west) * (shape[1] - 1),
    ])
    source = np.array(np.where(resized)).mean(axis=1)
    return shift(resized.astype(float), target - source, order=0,
                 mode="constant", cval=0.0) > .5


def _lake_mesh(mask: np.ndarray, plan, surface_z: np.ndarray) -> trimesh.Trimesh:
    rows, cols = mask.shape
    candidates = [shape(geometry) for geometry, value in shapes(mask.astype(np.uint8))
                  if value == 1]
    polygon = max(candidates, key=lambda item: item.area)
    polygon = affinity.scale(
        polygon, xfact=plan.width_m * plan.scale_mm_per_m / (cols - 1),
        yfact=-plan.height_m * plan.scale_mm_per_m / (rows - 1), origin=(0, 0))
    polygon = affinity.translate(
        polygon, xoff=-plan.width_m * plan.scale_mm_per_m / 2,
        yoff=plan.height_m * plan.scale_mm_per_m / 2).buffer(0)
    if polygon.is_empty or polygon.area <= 0:
        raise ValueError("Tianchi contour is not a valid polygon")
    water_level = float(np.percentile(surface_z[mask], 3)) + .02
    mesh = trimesh.creation.extrude_polygon(polygon, height=.58)
    mesh.apply_translation((0, 0, water_level - .58))
    return mesh


def _preview(path: Path, top_z, thickness, lake_mask) -> None:
    gy, gx = np.gradient(top_z)
    shade = 0.58 + .42 * np.clip((-0.55 * gx + 0.8 * gy + .35), 0, 1)
    intensity = .35 + .61 * (thickness - thickness.min()) / max(
        1e-8, thickness.max() - thickness.min())
    rgb = np.clip(255 * intensity[..., None] * shade[..., None], 0, 255)
    rgb = np.repeat(rgb, 3, axis=2).astype(np.uint8)
    rgb[lake_mask] = np.array([32, 39, 43], dtype=np.uint8)
    image = Image.fromarray(rgb)
    image.resize((1200, int(round(1200 * image.height / image.width))),
                 Image.Resampling.LANCZOS).save(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dem", type=Path, required=True)
    parser.add_argument("--guide", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--bbox", default="41.910,127.910,42.090,128.150")
    parser.add_argument("--terrain-height-mm", type=float, default=14.0,
                        help="Total natural-terrain height budget; 14mm matches the reference-scale relief.")
    args = parser.parse_args()
    bbox = tuple(map(float, args.bbox.split(",")))
    args.output_dir.mkdir(parents=True, exist_ok=True)

    elevation = _read_dem(args.dem, bbox)
    south, west, north, east = bbox
    projected = bbox_to_utm(south, west, north, east)
    scale = compute_scale(projected["width_m"], projected["height_m"])
    if not 4.0 <= args.terrain_height_mm <= 16.0:
        raise ValueError("terrain-height-mm must be in the 4..16mm trial range")
    # ``resolve_terrain_surface_plan`` deliberately uses the project-wide
    # city default.  Override it only for this isolated natural-landscape
    # trial, then restore it immediately.
    from _TEXTURE_STYLE_OF_DEEPSEEK import config as style_config
    prior_height = style_config.TERRAIN_THICKNESS_MM
    style_config.TERRAIN_THICKNESS_MM = args.terrain_height_mm
    try:
        plan = resolve_terrain_surface_plan(
            elevation, projected["width_m"], projected["height_m"], scale,
            base_thickness_mm=1.20, max_surface_edge_mm=1.40,
        )
    finally:
        style_config.TERRAIN_THICKNESS_MM = prior_height
    terrain, snowcap, evidence = build_variable_topcoat(plan)
    # Derive the same deterministic field again for the raster preview.
    from _TEXTURE_STYLE_OF_DEEPSEEK.natural_layers import derive_snow_topcoat_thickness
    thickness = derive_snow_topcoat_thickness(plan.regular_grid_m)
    lake_mask = _guide_lake_mask(args.guide, plan.surface_z_grid_mm.shape, bbox)
    water = _lake_mesh(lake_mask, plan, plan.surface_z_grid_mm)
    preview = args.output_dir / "changbai_topcoat_preview.png"
    _preview(preview, plan.surface_z_grid_mm, thickness, lake_mask)
    artifact = args.output_dir / "changbai_topcoat_trial.3mf"
    export_deepseek_3mf({"terrain": terrain, "snowcap": snowcap,
                          "water": water}, str(artifact))
    report = {"bbox_wgs84": bbox, "dem_range_m": [float(elevation.min()), float(elevation.max())],
              "model_size_mm": [projected["width_m"] * scale, projected["height_m"] * scale],
              "terrain_height_budget_mm": args.terrain_height_mm,
              "topcoat": evidence, "water_outline": "central dark-water mask from supplied reference satellite guide",
              "artifact": str(artifact), "preview": str(preview)}
    (args.output_dir / "trial_evidence.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
