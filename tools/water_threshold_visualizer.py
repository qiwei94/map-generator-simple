"""Visualise water feature filtering across multiple area thresholds.

Usage:
    cd /path/to/map_generator_final
    python tools/water_threshold_visualizer.py

Output:
    output/water_only/threshold_comparison_<timestamp>.png
"""

import os
import sys
import time
import argparse
from datetime import datetime

# Ensure project root is on path
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon as MplPolygon
from matplotlib.collections import PatchCollection
import geopandas as gpd
from shapely.geometry import (
    Polygon, MultiPolygon, LineString, MultiLineString,
)

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import (
    bbox_to_utm, project_geodataframe,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import fetch_water
from _TEXTURE_STYLE_OF_DEEPSEEK.config import WATERWAY_WIDTHS

# =====================================================================
# Defaults (Hangzhou West Lake)
# =====================================================================
DEFAULT_LAT1, DEFAULT_LON1 = 30.1375, 120.020
DEFAULT_LAT2, DEFAULT_LON2 = 30.3625, 120.280

# Thresholds to compare (m²)
DEFAULT_THRESHOLDS = [0, 1000, 5000, 10000, 20000, 50000]

# Colours
COLOR_KEPT = "#3B82F6"       # blue — kept water
COLOR_SKIPPED = "#D1D5DB"    # light gray — filtered out
COLOR_LINE_KEPT = "#1D4ED8"  # dark blue — kept water line
COLOR_LINE_SKIPPED = "#9CA3AF"  # darker gray — skipped water line
COLOR_BG = "#F8FAFC"         # background


def _estimate_line_buffered_area(
    geom: LineString, waterway_tag: str = None,
) -> float:
    """Fast approximate area of a buffered water line.

    Actual pipeline buffers the line with WATERWAY_WIDTHS[waterway],
    then checks area.  We approximate as length × width to avoid
    expensive buffer() calls on thousands of features.
    """
    width = WATERWAY_WIDTHS.get(waterway_tag or "river", 30.0)
    return geom.length * width


def _classify_features(
    gdf: gpd.GeoDataFrame, threshold_m2: float,
) -> tuple:
    """Classify each feature as kept / skipped at given threshold.

    Returns (kept_polys, skipped_polys, kept_lines, skipped_lines, stats_dict).
    Polys are (x, y) arrays for matplotlib; lines are (x, y) arrays.
    """
    kept_polys = []
    skipped_polys = []
    kept_lines = []
    skipped_lines = []

    n_poly_kept = 0
    n_poly_skipped = 0
    n_line_kept = 0
    n_line_skipped = 0
    poly_kept_area = 0.0
    poly_skipped_area = 0.0
    line_kept_len = 0.0
    line_skipped_len = 0.0

    for _, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue

        if isinstance(geom, (Polygon, MultiPolygon)):
            # ---- Polygon feature ----
            if isinstance(geom, MultiPolygon):
                polys = list(geom.geoms)
            else:
                polys = [geom]

            for poly in polys:
                area = poly.area
                if area >= threshold_m2:
                    _add_polygon_xy(poly, kept_polys)
                    n_poly_kept += 1
                    poly_kept_area += area
                else:
                    _add_polygon_xy(poly, skipped_polys)
                    n_poly_skipped += 1
                    poly_skipped_area += area

        elif isinstance(geom, (LineString, MultiLineString)):
            # ---- Line feature (waterway) ----
            if isinstance(geom, MultiLineString):
                lines = list(geom.geoms)
            else:
                lines = [geom]

            waterway = row.get("waterway", "river")
            for line in lines:
                approx_area = _estimate_line_buffered_area(line, waterway)
                xy = np.array(line.coords)
                if approx_area >= threshold_m2:
                    kept_lines.append(xy)
                    n_line_kept += 1
                    line_kept_len += line.length
                else:
                    skipped_lines.append(xy)
                    n_line_skipped += 1
                    line_skipped_len += line.length

    stats = {
        "n_poly_kept": n_poly_kept,
        "n_poly_skipped": n_poly_skipped,
        "n_line_kept": n_line_kept,
        "n_line_skipped": n_line_skipped,
        "poly_kept_area_km2": poly_kept_area / 1e6,
        "poly_skipped_area_km2": poly_skipped_area / 1e6,
        "line_kept_len_km": line_kept_len / 1000,
        "line_skipped_len_km": line_skipped_len / 1000,
    }
    return kept_polys, skipped_polys, kept_lines, skipped_lines, stats


def _add_polygon_xy(poly: Polygon, target_list: list):
    """Add polygon exterior + holes as separate loops for rendering."""
    ext = np.array(poly.exterior.coords)
    target_list.append(ext)
    for interior in poly.interiors:
        hole = np.array(interior.coords)
        target_list.append(hole)


def _render_subplot(
    ax, kept_polys, skipped_polys, kept_lines, skipped_lines,
    threshold: float, stats: dict, map_extent: tuple,
):
    """Render one threshold subplot."""
    ax.set_facecolor(COLOR_BG)
    ax.set_aspect("equal")

    # --- Skipped polygons (light gray, low z-order) ---
    if skipped_polys:
        patches = [
            MplPolygon(poly, closed=True)
            for poly in skipped_polys
        ]
        pc = PatchCollection(patches, facecolor=COLOR_SKIPPED,
                             edgecolor="none", alpha=0.6, zorder=1)
        ax.add_collection(pc)

    # --- Skipped lines (dashed gray) ---
    for line_xy in skipped_lines:
        ax.plot(line_xy[:, 0], line_xy[:, 1],
                color=COLOR_LINE_SKIPPED, linewidth=0.4,
                alpha=0.5, zorder=2)

    # --- Kept polygons (blue) ---
    if kept_polys:
        patches = [
            MplPolygon(poly, closed=True)
            for poly in kept_polys
        ]
        pc = PatchCollection(patches, facecolor=COLOR_KEPT,
                             edgecolor="#1E40AF", linewidth=0.15,
                             alpha=0.85, zorder=3)
        ax.add_collection(pc)

    # --- Kept lines (dark blue, thicker) ---
    for line_xy in kept_lines:
        ax.plot(line_xy[:, 0], line_xy[:, 1],
                color=COLOR_LINE_KEPT, linewidth=0.6,
                alpha=0.8, zorder=4)

    # Extent & labels
    xmin, xmax, ymin, ymax = map_extent
    ax.set_xlim(xmin, xmax)
    ax.set_ylim(ymin, ymax)
    ax.ticklabel_format(style="sci", scilimits=(0, 0), axis="both")
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v/1000:.0f}k"))
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v/1000:.0f}k"))
    ax.set_xlabel("Easting (km)")
    ax.set_ylabel("Northing (km)")

    if threshold == 0:
        title_str = "All water features (no filter)"
    else:
        title_str = f"Threshold: ≥{threshold:,.0f} m²"

    n_kept = stats["n_poly_kept"] + stats["n_line_kept"]
    n_skipped = stats["n_poly_skipped"] + stats["n_line_skipped"]
    n_total = n_kept + n_skipped

    kept_area = stats["poly_kept_area_km2"]
    skipped_area = stats["poly_skipped_area_km2"]

    info = (
        f"Kept: {n_kept}  |  Skipped: {n_skipped}  |  Total: {n_total}\n"
        f"Kept area: {kept_area:.1f} km²  |  Skipped: {skipped_area:.1f} km²"
    )
    ax.set_title(title_str, fontsize=11, fontweight="bold", pad=6)
    ax.text(0.5, -0.09, info, transform=ax.transAxes,
            fontsize=8, ha="center", va="top",
            fontfamily="monospace",
            bbox=dict(boxstyle="round,pad=0.3",
                      facecolor="#F1F5F9", edgecolor="#CBD5E1"))


def build_comparison(
    lat1: float, lon1: float, lat2: float, lon2: float,
    thresholds: list,
    output_path: str = None,
):
    """Fetch water data and render threshold comparison chart.

    Args:
        lat1, lon1: South-west corner (WGS84).
        lat2, lon2: North-east corner (WGS84).
        thresholds: List of area thresholds in m².
        output_path: Output image path (None = auto-generate).
    """
    t_start = time.time()

    print("=" * 60)
    print("  Water Threshold Visualizer")
    print("=" * 60)
    print(f"  Bbox: ({lat1}, {lon1}) → ({lat2}, {lon2})")
    print(f"  Thresholds: {thresholds}")
    print()

    # ---- Stage 1: Fetch OSM water ----
    print("[1/3] Fetching OSM water data...")
    t1 = time.time()
    bbox = bbox_to_utm(lat1, lon1, lat2, lon2)
    water_gdf = fetch_water(lat1, lon1, lat2, lon2)
    if water_gdf is None or len(water_gdf) == 0:
        print("  ERROR: No water data fetched!")
        return
    print(f"  Features: {len(water_gdf)}  ({time.time()-t1:.1f}s)")

    # ---- Stage 2: Project to local UTM ----
    print("[2/3] Projecting to local coordinates...")
    t2 = time.time()
    utm_crs = bbox["utm_crs"]
    origin = bbox["origin"]
    utm_bbox = bbox["utm_bbox"]
    water_gdf = project_geodataframe(water_gdf, utm_crs, origin,
                                     clip_bbox=utm_bbox)
    print(f"  Projected: {len(water_gdf)} features  ({time.time()-t2:.1f}s)")

    # Compute overall map extent (for consistent axes across subplots)
    xs_all, ys_all = [], []
    for _, row in water_gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        gt = geom.geom_type
        if gt == "Polygon":
            xs, ys = geom.exterior.xy
            xs_all.extend(xs)
            ys_all.extend(ys)
        elif gt == "MultiPolygon":
            for g in geom.geoms:
                xs, ys = g.exterior.xy
                xs_all.extend(xs)
                ys_all.extend(ys)
        elif gt == "LineString":
            xs, ys = geom.xy
            xs_all.extend(xs)
            ys_all.extend(ys)
        elif gt == "MultiLineString":
            for g in geom.geoms:
                xs, ys = g.xy
                xs_all.extend(xs)
                ys_all.extend(ys)

    if xs_all:
        margin = max(
            (max(xs_all) - min(xs_all)) * 0.02,
            (max(ys_all) - min(ys_all)) * 0.02,
            500,
        )
        map_extent = (
            min(xs_all) - margin,
            max(xs_all) + margin,
            min(ys_all) - margin,
            max(ys_all) + margin,
        )
    else:
        map_extent = (0, 25000, 0, 25000)

    # ---- Stage 3: Classify & render per threshold ----
    print("[3/3] Rendering threshold comparison...")
    t3 = time.time()

    n_thresholds = len(thresholds)
    n_cols = min(3, n_thresholds)
    n_rows = (n_thresholds + n_cols - 1) // n_cols

    figsize = (6.0 * n_cols, 5.5 * n_rows)
    fig, axes = plt.subplots(n_rows, n_cols, figsize=figsize,
                             squeeze=False)
    fig.patch.set_facecolor("white")

    for idx, thresh in enumerate(thresholds):
        row = idx // n_cols
        col = idx % n_cols
        ax = axes[row][col]

        kept_p, skipped_p, kept_l, skipped_l, stats = _classify_features(
            water_gdf, thresh,
        )
        _render_subplot(ax, kept_p, skipped_p, kept_l, skipped_l,
                        thresh, stats, map_extent)

    # Hide unused subplots
    for idx in range(n_thresholds, n_rows * n_cols):
        row = idx // n_cols
        col = idx % n_cols
        axes[row][col].set_visible(False)

    # Legend
    legend_elements = [
        plt.Rectangle((0, 0), 1, 1, facecolor=COLOR_KEPT,
                      edgecolor="#1E40AF", label="Kept (polygon)"),
        plt.Rectangle((0, 0), 1, 1, facecolor=COLOR_SKIPPED,
                      edgecolor="none", alpha=0.6, label="Skipped (polygon)"),
        plt.Line2D([0], [0], color=COLOR_LINE_KEPT, linewidth=1.5,
                   label="Kept (waterway)"),
        plt.Line2D([0], [0], color=COLOR_LINE_SKIPPED, linewidth=1.5,
                   alpha=0.5, label="Skipped (waterway)"),
    ]
    fig.legend(handles=legend_elements, loc="lower center",
               ncol=4, fontsize=9,
               bbox_to_anchor=(0.5, -0.02))

    plt.suptitle(
        f"Water feature filtering comparison — "
        f"{abs(lat2-lat1)*111:.0f}×{abs(lon2-lon1)*96:.0f} km area",
        fontsize=14, fontweight="bold", y=1.01,
    )
    plt.tight_layout()
    plt.subplots_adjust(bottom=0.10)

    # Save
    if output_path is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = os.path.join(
            "output", "water_only", f"threshold_comparison_{ts}.png",
        )
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    plt.savefig(output_path, dpi=200, bbox_inches="tight",
                facecolor="white")
    plt.close(fig)

    file_size_mb = os.path.getsize(output_path) / (1024 * 1024)
    print(f"  Image saved: {output_path}  ({file_size_mb:.2f} MB)")
    print(f"  Total time: {time.time() - t_start:.1f}s")
    print(f"\n{'=' * 60}")
    print(f"  Done!")
    print(f"{'=' * 60}")
    return output_path


# =====================================================================
# CLI
# =====================================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Visualise water feature filtering across area thresholds",
    )
    parser.add_argument("--lat1", type=float, default=DEFAULT_LAT1,
                        help="South latitude (default: Hangzhou)")
    parser.add_argument("--lon1", type=float, default=DEFAULT_LON1,
                        help="West longitude (default: Hangzhou)")
    parser.add_argument("--lat2", type=float, default=DEFAULT_LAT2,
                        help="North latitude (default: Hangzhou)")
    parser.add_argument("--lon2", type=float, default=DEFAULT_LON2,
                        help="East longitude (default: Hangzhou)")
    parser.add_argument("--thresholds", type=float, nargs="+",
                        default=DEFAULT_THRESHOLDS,
                        help="Area thresholds in m² (default: %(default)s)")
    parser.add_argument("-o", "--output", type=str, default=None,
                        help="Output image path")
    args = parser.parse_args()

    build_comparison(
        args.lat1, args.lon1, args.lat2, args.lon2,
        args.thresholds, args.output,
    )
