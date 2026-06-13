"""PNG preview renderer — adapter from LayerPolygons to tune_buildings_v2.render().

Reuses the existing matplotlib-based renderer without duplicating geometry
preprocessing. All geometry comes from preprocess_layers() output.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import List, Optional, Tuple

from shapely.geometry import Polygon

from _TEXTURE_STYLE_OF_DEEPSEEK._layer_preprocess import LayerPolygons


def render_from_layers(
    layers: LayerPolygons,
    ctx: dict,
    output_path: str,
    *,
    city_name: str = "",
    annotate: bool = False,
    block_jitter: bool = True,
    water_gdf=None,
    landuse_gdf=None,
    railway_gdf=None,
    pier_gdf=None,
    stadium_gdf=None,
    dpi: int = 220,
    fig_inches: float = 18.0,
) -> str:
    """Render a PNG preview from preprocessed LayerPolygons.

    Args:
        layers: output of preprocess_layers()
        ctx: dict with keys bbox_utm, origin, width_m, height_m, utm_crs, bbox_wgs84
        output_path: target .png path
        city_name: displayed in the title
        annotate: draw text annotations for landmarks
        block_jitter: use brick-style rendering (Voronoi+Perlin)
        water_gdf: projected GeoDataFrame for block exclusion + classification
        landuse_gdf: projected GeoDataFrame for block semantic classification
        railway_gdf: projected GeoDataFrame for railway overlay
        pier_gdf: projected GeoDataFrame for pier/breakwater overlay
        stadium_gdf: projected GeoDataFrame for stadium overlay
        dpi: output resolution
        fig_inches: figure size

    Returns:
        output_path on success.
    """
    from tools.tune_buildings_v2 import render, classify_blocks

    t0 = time.time()

    # --- Map LayerPolygons → render() parameters ---

    # BL: list of (polygon, height) → all treated as tag_landmarks for PNG
    tag_landmarks = [poly for poly, _h in layers.BL] if layers.BL else []
    size_landmarks: List[Polygon] = []

    # BO → blocks_aggregated
    blocks_aggregated = list(layers.BO) if layers.BO else []

    # block_base → city_blocks_outline (with optional exclusion subtraction)
    city_blocks = list(layers.block_base) if layers.block_base else []
    block_classes = (list(layers.block_base_classes)
                     if layers.block_base_classes else None)

    # Apply water/road exclusion for brick rendering
    if block_jitter and city_blocks and water_gdf is not None:
        try:
            from _TEXTURE_STYLE_OF_DEEPSEEK._block_filter import (
                build_and_subtract_exclusions,
            )
            veg_lm = [poly for poly, _h in layers.BL] if layers.BL else None
            city_blocks_render = build_and_subtract_exclusions(
                city_blocks, water_gdf, veg_lm,
                roads_gdf=None, road_inset=25.0, water_inset=40.0)
        except Exception:
            city_blocks_render = city_blocks
    else:
        city_blocks_render = city_blocks

    # Classify blocks semantically
    building_polys = tag_landmarks + blocks_aggregated
    render_classes = classify_blocks(
        city_blocks_render, landuse_gdf, water_gdf, building_polys)

    # Water
    water_landmark_polys = list(layers.WL) if layers.WL else []
    small_water_polys = list(layers.WO) if layers.WO else []

    # Vegetation
    veg_landmarks = list(layers.VL) if layers.VL else []
    veg_fill_polys = list(layers.VO) if layers.VO else []

    # raw_polys / individuals — used only for stats display, not for rendering
    raw_polys = tag_landmarks + blocks_aggregated
    individuals = tag_landmarks + size_landmarks

    # Params / stats for title
    params = {"city": city_name, "mode": "pipeline"}
    stats = {
        "BL": len(layers.BL),
        "BO": len(layers.BO),
        "WL": len(layers.WL),
        "WO": len(layers.WO),
        "VL": len(layers.VL),
        "VO": len(layers.VO),
        "blocks": len(city_blocks),
    }

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    render(
        out, raw_polys, individuals, blocks_aggregated,
        city_blocks_render, ctx,
        f"{city_name} — pipeline preview",
        params, stats,
        fig_inches=fig_inches, dpi=dpi,
        tag_landmarks=tag_landmarks,
        size_landmarks=size_landmarks,
        veg_landmarks=veg_landmarks,
        water_landmark_polys=water_landmark_polys,
        small_water_polys=small_water_polys,
        veg_fill_polys=veg_fill_polys,
        annotate=annotate,
        block_tessellation=True,
        block_jitter=block_jitter,
        railway_gdf=railway_gdf,
        pier_gdf=pier_gdf,
        stadium_gdf=stadium_gdf,
        block_classes=render_classes,
    )

    elapsed = time.time() - t0
    print(f"  [render_png] {output_path} ({elapsed:.1f}s)")
    return output_path
