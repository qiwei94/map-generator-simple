#!/usr/bin/env python3
"""Create a first-pass snow-mountain trial from a public DEM.

This keeps the Changbai material construction reusable for mountains without a
lake: gray substrate below, white variable-thickness cap above.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np
from PIL import Image
from scipy.ndimage import binary_closing, binary_opening, gaussian_filter

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from _TEXTURE_STYLE_OF_DEEPSEEK import config as style_config
from _TEXTURE_STYLE_OF_DEEPSEEK.config import compute_scale
from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf
from _TEXTURE_STYLE_OF_DEEPSEEK.natural_layers import (
    build_masked_surface_cap,
    build_recessed_terrain_substrate,
    build_variable_topcoat,
    derive_snow_topcoat_thickness,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import resolve_terrain_surface_plan
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm
from build_changbai_topcoat_trial import _preview, _read_dem


def _satellite_snow_likelihood(image_path: Path, metadata_path: Path, bbox,
                               target_shape: tuple[int, int]) -> np.ndarray:
    """Map bright, low-chroma terrain in a georeferenced mosaic to the DEM."""
    metadata = json.loads(metadata_path.read_text())
    south_m, west_m, north_m, east_m = metadata["mosaic_bbox_wgs84"]
    south, west, north, east = bbox
    image = Image.open(image_path).convert("RGB")
    width, height = image.size
    left = (west - west_m) / (east_m - west_m) * width
    right = (east - west_m) / (east_m - west_m) * width
    top = (north_m - north) / (north_m - south_m) * height
    bottom = (north_m - south) / (north_m - south_m) * height
    crop = image.crop((left, top, right, bottom)).resize(
        (target_shape[1], target_shape[0]), Image.Resampling.BILINEAR)
    rgb = np.asarray(crop, dtype=np.float64) / 255.0
    low = rgb.min(axis=2)
    high = rgb.max(axis=2)
    chroma = (high - low) / np.maximum(high, 1e-6)
    brightness = np.clip((low - .36) / .42, 0.0, 1.0)
    neutral = np.clip((.56 - chroma) / .34, 0.0, 1.0)
    # Clouds remain possible in a single acquisition.  They have no direct
    # authority: mountain evidence later supplies the continuous fallback.
    return np.power(brightness * neutral, .75)


def _satellite_landcover(image_path: Path, metadata_path: Path, bbox,
                         target_shape: tuple[int, int]) -> tuple[np.ndarray, np.ndarray]:
    """Return conservative snow and vegetation classes from source RGB imagery.

    This is intentionally broad material classification, not a photographic
    texture: it only creates printable white snow and green vegetation regions.
    """
    metadata = json.loads(metadata_path.read_text())
    south_m, west_m, north_m, east_m = metadata["mosaic_bbox_wgs84"]
    south, west, north, east = bbox
    image = Image.open(image_path).convert("RGB")
    width, height = image.size
    crop = image.crop(((west - west_m) / (east_m - west_m) * width,
                       (north_m - north) / (north_m - south_m) * height,
                       (east - west_m) / (east_m - west_m) * width,
                       (north_m - south) / (north_m - south_m) * height)).resize(
        (target_shape[1], target_shape[0]), Image.Resampling.BILINEAR)
    rgb = np.asarray(crop, dtype=np.float64) / 255.0
    red, green, blue = (rgb[..., i] for i in range(3))
    low, high = rgb.min(axis=2), rgb.max(axis=2)
    chroma = (high - low) / np.maximum(high, 1e-6)
    snow = np.power(np.clip((low - .36) / .42, 0.0, 1.0) *
                    np.clip((.56 - chroma) / .34, 0.0, 1.0), .75)
    # Green dominance separates living cover from warm exposed rock.  A small
    # blur removes sub-nozzle speckles before it becomes a mesh boundary.
    vegetation = gaussian_filter(np.clip((green - np.maximum(red, blue) + .08) / .28,
                                          0.0, 1.0), sigma=1.4)
    return snow, vegetation


def _material_preview(path: Path, top_z: np.ndarray, snow: np.ndarray,
                      vegetation: np.ndarray) -> None:
    gy, gx = np.gradient(top_z)
    shade = .57 + .43 * np.clip(-.55 * gx + .80 * gy + .35, 0.0, 1.0)
    rgb = np.full((*top_z.shape, 3), (91, 91, 87), dtype=np.float64)
    rgb[vegetation] = (93, 116, 62)
    rgb[snow] = (232, 232, 228)
    image = Image.fromarray(np.clip(rgb * shade[..., None], 0, 255).astype(np.uint8))
    image.resize((1200, int(round(1200 * image.height / image.width))),
                 Image.Resampling.LANCZOS).save(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dem", type=Path, required=True)
    parser.add_argument("--bbox", required=True, help="south,west,north,east")
    parser.add_argument("--name", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--terrain-height-mm", type=float, default=14.0)
    parser.add_argument("--imagery", type=Path)
    parser.add_argument("--imagery-metadata", type=Path)
    parser.add_argument("--landcover-colours", action="store_true",
                        help="Create printable green vegetation and white snow caps from imagery.")
    args = parser.parse_args()
    if not 4.0 <= args.terrain_height_mm <= 16.0:
        raise ValueError("terrain-height-mm must be in the 4..16mm trial range")
    bbox = tuple(map(float, args.bbox.split(",")))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    elevation = _read_dem(args.dem, bbox)
    south, west, north, east = bbox
    projected = bbox_to_utm(south, west, north, east)
    scale = compute_scale(projected["width_m"], projected["height_m"])
    prior_height = style_config.TERRAIN_THICKNESS_MM
    style_config.TERRAIN_THICKNESS_MM = args.terrain_height_mm
    try:
        plan = resolve_terrain_surface_plan(
            elevation, projected["width_m"], projected["height_m"], scale,
            base_thickness_mm=1.20, max_surface_edge_mm=1.40,
        )
    finally:
        style_config.TERRAIN_THICKNESS_MM = prior_height
    if bool(args.imagery) != bool(args.imagery_metadata):
        raise ValueError("--imagery and --imagery-metadata must be supplied together")
    snow_likelihood = (_satellite_snow_likelihood(
        args.imagery, args.imagery_metadata, bbox, plan.surface_z_grid_mm.shape)
        if args.imagery else None)
    active_meshes = {}
    if args.landcover_colours:
        if not args.imagery:
            raise ValueError("--landcover-colours requires --imagery")
        snow_score, vegetation_score = _satellite_landcover(
            args.imagery, args.imagery_metadata, bbox, plan.surface_z_grid_mm.shape)
        # Remove isolated pixel-scale detections before converting the masks
        # into solid mesh islands.  This also keeps their perimeter printable.
        snow_mask = binary_opening(binary_closing(
            gaussian_filter(snow_score, 1.2) > .28, iterations=2), iterations=1)
        vegetation_mask = binary_opening(binary_closing(
            vegetation_score > .31, iterations=2), iterations=1) & ~snow_mask
        terrain = build_recessed_terrain_substrate(plan)
        vegetation = build_masked_surface_cap(plan, vegetation_mask,
                                              thickness_mm=.24, name="vegetation")
        snowcap = build_masked_surface_cap(plan, snow_mask,
                                           thickness_mm=.24, name="snowcap")
        active_meshes = {"terrain": terrain}
        if vegetation is not None:
            active_meshes["vegetation"] = vegetation
        if snowcap is not None:
            active_meshes["snowcap"] = snowcap
        _material_preview(args.output_dir / f"{args.name}_topcoat_preview.png",
                          plan.surface_z_grid_mm, snow_mask, vegetation_mask)
        cap_evidence = {"policy_version": "natural-satellite-landcover-v1",
                        "snow_coverage_percent": float(100 * snow_mask.mean()),
                        "vegetation_coverage_percent": float(100 * vegetation_mask.mean()),
                        "satellite_guidance": True}
    else:
        terrain, snowcap, cap_evidence = build_variable_topcoat(
            plan, snow_likelihood=snow_likelihood)
        active_meshes = {"terrain": terrain, "snowcap": snowcap}
        thickness = derive_snow_topcoat_thickness(
            plan.regular_grid_m, snow_likelihood=snow_likelihood)
        _preview(args.output_dir / f"{args.name}_topcoat_preview.png", plan.surface_z_grid_mm,
                 thickness, np.zeros(plan.surface_z_grid_mm.shape, dtype=bool))
    preview = args.output_dir / f"{args.name}_topcoat_preview.png"
    artifact = args.output_dir / f"{args.name}_topcoat_trial.3mf"
    export_deepseek_3mf(active_meshes, str(artifact))
    report = {
        "name": args.name, "bbox_wgs84": bbox,
        "dem_range_m": [float(elevation.min()), float(elevation.max())],
        "model_size_mm": [projected["width_m"] * scale,
                          projected["height_m"] * scale],
        "terrain_height_budget_mm": args.terrain_height_mm,
        "topcoat": cap_evidence, "artifact": str(artifact),
        "imagery_guidance": str(args.imagery) if args.imagery else None,
        "preview": str(preview),
    }
    (args.output_dir / "trial_evidence.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
