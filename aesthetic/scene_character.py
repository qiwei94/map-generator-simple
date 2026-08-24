"""Local, auditable scene-character analysis for printable city maps.

The analyzer describes evidence; it does not choose mesh geometry, Z values,
materials or booleans.  Its grid-relative labels are intended for diagnostic
review before any adaptive rendering policy consumes them.
"""
from __future__ import annotations

from collections import defaultdict
import math
from pathlib import Path
from typing import Iterable

import numpy as np
from shapely.geometry import box
from shapely.ops import unary_union


SCENE_ANALYSIS_VERSION = "scene-character-v1"

_STRUCTURAL_HIGHWAYS = {
    "motorway", "motorway_link", "trunk", "trunk_link",
    "primary", "primary_link", "secondary", "secondary_link",
    "tertiary", "tertiary_link", "residential", "unclassified",
    "living_street",
}
_MAJOR_HIGHWAYS = {
    "motorway", "motorway_link", "trunk", "trunk_link",
    "primary", "primary_link", "secondary", "secondary_link",
}
_LANDMARK_BUILDINGS = {
    "stadium", "university", "college", "hospital", "train_station",
    "mall", "public", "government", "museum", "cathedral", "church",
    "temple", "mosque", "synagogue", "civic", "library", "pagoda",
    "shrine", "chapel", "monastery", "convent", "abbey",
}
_LANDMARK_AMENITIES = {
    "university", "hospital", "mall", "theatre", "cinema",
    "place_of_worship", "library", "townhall", "courthouse", "college",
}
_LANDMARK_TOURISM = {
    "museum", "gallery", "attraction", "theme_park", "aquarium",
}
_LANDMARK_MAN_MADE = {
    "tower", "lighthouse", "water_tower", "obelisk",
}


def _iter_lines(geometry) -> Iterable[object]:
    if geometry is None or geometry.is_empty:
        return
    if geometry.geom_type == "LineString":
        yield geometry
    elif geometry.geom_type == "MultiLineString":
        yield from (part for part in geometry.geoms if not part.is_empty)


def _cell_indexes(x, y, bbox_local, grid_size: int):
    xmin, ymin, xmax, ymax = bbox_local
    gx = np.floor((np.asarray(x) - xmin) / max(xmax - xmin, 1.0)
                  * grid_size).astype(int)
    gy = np.floor((np.asarray(y) - ymin) / max(ymax - ymin, 1.0)
                  * grid_size).astype(int)
    return (np.clip(gx, 0, grid_size - 1),
            np.clip(gy, 0, grid_size - 1))


def _line_metrics(roads, bbox_local, grid_size: int) -> dict:
    count = grid_size * grid_size
    length_m = np.zeros(count, dtype=float)
    structural_length_m = np.zeros(count, dtype=float)
    major_length_m = np.zeros(count, dtype=float)
    orientation_hist = np.zeros((count, 18), dtype=float)
    node_degree = defaultdict(int)

    if roads is None or len(roads) == 0:
        return {
            "length_m": length_m,
            "structural_length_m": structural_length_m,
            "major_length_m": major_length_m,
            "orientation_concentration": np.zeros(count),
            "junction_count": np.zeros(count),
        }

    highway_values = (roads["highway"].fillna("").astype(str).str.lower()
                      if "highway" in roads.columns
                      else [""] * len(roads))
    for geometry, highway in zip(roads.geometry, highway_values):
        structural = highway in _STRUCTURAL_HIGHWAYS
        major = highway in _MAJOR_HIGHWAYS
        for line in _iter_lines(geometry) or ():
            coords = np.asarray(line.coords, dtype=float)
            if len(coords) < 2:
                continue
            delta = coords[1:] - coords[:-1]
            lengths = np.hypot(delta[:, 0], delta[:, 1])
            valid = np.isfinite(lengths) & (lengths >= 1.0)
            if not valid.any():
                continue
            mids = (coords[1:] + coords[:-1]) * 0.5
            gx, gy = _cell_indexes(
                mids[valid, 0], mids[valid, 1], bbox_local, grid_size)
            flat = gy * grid_size + gx
            np.add.at(length_m, flat, lengths[valid])
            if structural:
                np.add.at(structural_length_m, flat, lengths[valid])
                folded = np.arctan2(
                    delta[valid, 1], delta[valid, 0]) % (math.pi / 2)
                bins = np.floor(folded / (math.pi / 2) * 18).astype(int) % 18
                np.add.at(orientation_hist, (flat, bins), lengths[valid])
                # Segment incidence, not feature count: a bend has degree 2,
                # a T node degree 3 and a planar crossing degree 4.
                quantized = np.rint(coords / 2.0).astype(np.int64)
                for index, key in enumerate(map(tuple, quantized)):
                    node_degree[key] += 1 if index in (0, len(coords) - 1) else 2
            if major:
                np.add.at(major_length_m, flat, lengths[valid])

    orientation = np.zeros(count, dtype=float)
    totals = orientation_hist.sum(axis=1)
    for index in np.flatnonzero(totals > 0):
        hist = orientation_hist[index]
        smooth = hist + np.roll(hist, 1) + np.roll(hist, -1)
        peak_share = smooth.max() / max(totals[index] * 3.0, 1e-9)
        orientation[index] = np.clip((peak_share - 0.08) / 0.30, 0.0, 1.0)

    junction_count = np.zeros(count, dtype=float)
    if node_degree:
        junctions = np.asarray(
            [(key[0] * 2.0, key[1] * 2.0)
             for key, degree in node_degree.items() if degree >= 3],
            dtype=float,
        )
        if junctions.size:
            gx, gy = _cell_indexes(
                junctions[:, 0], junctions[:, 1], bbox_local, grid_size)
            np.add.at(junction_count, gy * grid_size + gx, 1)

    return {
        "length_m": length_m,
        "structural_length_m": structural_length_m,
        "major_length_m": major_length_m,
        "orientation_concentration": orientation,
        "junction_count": junction_count,
    }


def _truthy_text(values) -> np.ndarray:
    text = values.fillna("").astype(str).str.strip().str.lower()
    return ~text.isin(("", "nan", "none", "false", "0"))


def _building_metrics(buildings, bbox_local, grid_size: int) -> dict:
    count = grid_size * grid_size
    area_m2 = np.zeros(count, dtype=float)
    building_count = np.zeros(count, dtype=float)
    landmark_count = np.zeros(count, dtype=float)
    landmark_area_m2 = np.zeros(count, dtype=float)
    if buildings is None or len(buildings) == 0:
        return {
            "area_m2": area_m2,
            "count": building_count,
            "landmark_count": landmark_count,
            "landmark_area_m2": landmark_area_m2,
        }

    polygon_mask = buildings.geometry.geom_type.isin(("Polygon", "MultiPolygon"))
    work = buildings.loc[polygon_mask]
    if len(work) == 0:
        return {
            "area_m2": area_m2,
            "count": building_count,
            "landmark_count": landmark_count,
            "landmark_area_m2": landmark_area_m2,
        }
    areas = work.geometry.area.to_numpy(dtype=float)
    centroids = work.geometry.centroid
    gx, gy = _cell_indexes(
        centroids.x.to_numpy(), centroids.y.to_numpy(), bbox_local, grid_size)
    flat = gy * grid_size + gx
    np.add.at(area_m2, flat, areas)
    np.add.at(building_count, flat, 1)

    landmark = np.zeros(len(work), dtype=bool)
    for column in ("wikidata", "wikipedia", "historic", "heritage"):
        if column in work.columns:
            landmark |= _truthy_text(work[column]).to_numpy()
    for column, allowed in (
        ("building", _LANDMARK_BUILDINGS),
        ("amenity", _LANDMARK_AMENITIES),
        ("tourism", _LANDMARK_TOURISM),
        ("man_made", _LANDMARK_MAN_MADE),
    ):
        if column in work.columns:
            landmark |= work[column].fillna("").astype(str).str.lower().isin(
                allowed).to_numpy()
    finite_areas = areas[np.isfinite(areas) & (areas > 0)]
    large_cutoff = (max(5000.0, float(np.percentile(finite_areas, 99.9)))
                    if finite_areas.size else math.inf)
    # Strict comparison avoids labelling every building when a synthetic or
    # highly regular source has one repeated footprint size.
    landmark |= areas > large_cutoff
    if landmark.any():
        np.add.at(landmark_count, flat[landmark], 1)
        np.add.at(landmark_area_m2, flat[landmark], areas[landmark])
    return {
        "area_m2": area_m2,
        "count": building_count,
        "landmark_count": landmark_count,
        "landmark_area_m2": landmark_area_m2,
    }


def _water_metrics(water, bbox_local, grid_size: int) -> dict:
    count = grid_size * grid_size
    water_area_m2 = np.zeros(count, dtype=float)
    shoreline_m = np.zeros(count, dtype=float)
    waterway_m = np.zeros(count, dtype=float)
    if water is None or len(water) == 0:
        return {
            "area_m2": water_area_m2,
            "shoreline_m": shoreline_m,
            "waterway_m": waterway_m,
        }

    polygons = water.loc[water.geometry.geom_type.isin(
        ("Polygon", "MultiPolygon"))]
    water_union = None
    if len(polygons):
        try:
            water_union = unary_union(
                [geometry for geometry in polygons.geometry
                 if geometry is not None and not geometry.is_empty])
        except Exception:
            water_union = None

    xmin, ymin, xmax, ymax = bbox_local
    cell_width = (xmax - xmin) / grid_size
    cell_height = (ymax - ymin) / grid_size
    frame_boundary = box(xmin, ymin, xmax, ymax).boundary.buffer(1.0)
    shoreline = None
    if water_union is not None and not water_union.is_empty:
        try:
            shoreline = water_union.boundary.difference(frame_boundary)
        except Exception:
            shoreline = water_union.boundary
    for row in range(grid_size):
        for column in range(grid_size):
            index = row * grid_size + column
            cell = box(
                xmin + column * cell_width,
                ymin + row * cell_height,
                xmin + (column + 1) * cell_width,
                ymin + (row + 1) * cell_height,
            )
            if water_union is not None and not water_union.is_empty:
                try:
                    water_area_m2[index] = water_union.intersection(cell).area
                except Exception:
                    pass
            if shoreline is not None and not shoreline.is_empty:
                try:
                    shoreline_m[index] = shoreline.intersection(cell).length
                except Exception:
                    pass

    linework = water.loc[water.geometry.geom_type.isin(
        ("LineString", "MultiLineString"))]
    for geometry in linework.geometry:
        for line in _iter_lines(geometry) or ():
            coords = np.asarray(line.coords, dtype=float)
            if len(coords) < 2:
                continue
            delta = coords[1:] - coords[:-1]
            lengths = np.hypot(delta[:, 0], delta[:, 1])
            valid = np.isfinite(lengths) & (lengths >= 1.0)
            if not valid.any():
                continue
            mids = (coords[1:] + coords[:-1]) * 0.5
            gx, gy = _cell_indexes(
                mids[valid, 0], mids[valid, 1], bbox_local, grid_size)
            np.add.at(waterway_m, gy * grid_size + gx, lengths[valid])
    return {
        "area_m2": water_area_m2,
        "shoreline_m": shoreline_m,
        "waterway_m": waterway_m,
    }


def _percentile(values, percentile: float, default=0.0) -> float:
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    return float(np.percentile(values, percentile)) if values.size else default


def _neighbor_mean(values: np.ndarray, row: int, column: int,
                   grid_size: int) -> float:
    neighbors = []
    for other_row in range(max(0, row - 1), min(grid_size, row + 2)):
        for other_column in range(max(0, column - 1), min(grid_size, column + 2)):
            if other_row == row and other_column == column:
                continue
            neighbors.append(values[other_row * grid_size + other_column])
    return float(np.mean(neighbors)) if neighbors else 0.0


def analyze_scene_character(roads, buildings, water, bbox_local,
                            *, grid_size: int = 8) -> dict:
    """Return transparent local evidence and relative scene roles.

    Inputs must use a projected metric CRS.  Roles are relative to this frame;
    they are evidence for review, not universal land-use truth.
    """

    if grid_size < 3 or grid_size > 16:
        raise ValueError("grid_size must be between 3 and 16")
    xmin, ymin, xmax, ymax = (float(value) for value in bbox_local)
    if xmax <= xmin or ymax <= ymin:
        raise ValueError("bbox_local must have positive width and height")
    bbox_local = (xmin, ymin, xmax, ymax)
    cell_area_m2 = ((xmax - xmin) * (ymax - ymin)
                    / (grid_size * grid_size))

    road = _line_metrics(roads, bbox_local, grid_size)
    building = _building_metrics(buildings, bbox_local, grid_size)
    water_data = _water_metrics(water, bbox_local, grid_size)

    water_fraction = np.clip(water_data["area_m2"] / cell_area_m2, 0.0, 1.0)
    # Use a floor so a bridge in a nearly all-water cell does not report an
    # absurd road density; the cell is still classified by its water evidence.
    effective_land_km2 = np.maximum(
        cell_area_m2 * np.maximum(1.0 - water_fraction, 0.25) / 1e6,
        1e-6,
    )
    road_density = road["structural_length_m"] / 1000.0 / effective_land_km2
    major_density = road["major_length_m"] / 1000.0 / effective_land_km2
    junction_density = road["junction_count"] / effective_land_km2
    building_coverage = np.clip(
        building["area_m2"] / np.maximum(
            cell_area_m2 * (1.0 - water_fraction), cell_area_m2 * 0.25),
        0.0, 1.0,
    )
    landmark_score = (
        np.log1p(building["landmark_count"])
        * np.sqrt(np.clip(
            building["landmark_area_m2"] / cell_area_m2, 0.0, 1.0))
    )
    # OSM often maps a campus, palace or station as hundreds of tagged
    # building parts.  "Has one landmark tag" therefore cannot make a cell a
    # focus.  Rank aggregate evidence and keep at most the strongest 5% of
    # frame cells as diagnostic focus candidates.
    landmark_focus = np.zeros(grid_size * grid_size, dtype=bool)
    eligible_landmarks = np.flatnonzero(
        (landmark_score > 0)
        & (building["landmark_area_m2"]
           >= max(1500.0, cell_area_m2 * 0.001))
    )
    landmark_focus_limit = max(1, int(math.ceil(grid_size * grid_size * 0.05)))
    if eligible_landmarks.size:
        ranked = sorted(
            eligible_landmarks.tolist(),
            key=lambda index: (-landmark_score[index], index),
        )[:landmark_focus_limit]
        landmark_focus[ranked] = True

    land = water_fraction < 0.80
    land_values = lambda values: values[land]
    quantiles = {
        "road_density_p25": _percentile(land_values(road_density), 25),
        "road_density_p50": _percentile(land_values(road_density), 50),
        "road_density_p75": _percentile(land_values(road_density), 75),
        "major_density_p75": _percentile(land_values(major_density), 75),
        "building_coverage_p25": _percentile(
            land_values(building_coverage), 25),
        "building_coverage_p50": _percentile(
            land_values(building_coverage), 50),
        "building_coverage_p75": _percentile(
            land_values(building_coverage), 75),
        "junction_density_p60": _percentile(
            land_values(junction_density), 60),
        "junction_density_p75": _percentile(
            land_values(junction_density), 75),
    }

    def normalized(values, reference, floor):
        return np.clip(values / max(reference, floor), 0.0, 1.0)

    urban_signal = (
        0.45 * normalized(
            building_coverage, quantiles["building_coverage_p75"], 0.02)
        + 0.35 * normalized(
            junction_density, quantiles["junction_density_p75"], 1.0)
        + 0.20 * normalized(
            road_density, quantiles["road_density_p75"], 0.1)
    )

    cells = []
    role_counts = defaultdict(int)
    cell_width = (xmax - xmin) / grid_size
    cell_height = (ymax - ymin) / grid_size
    for row in range(grid_size):
        for column in range(grid_size):
            index = row * grid_size + column
            neighbor_urban = _neighbor_mean(
                urban_signal, row, column, grid_size)
            neighbor_water = _neighbor_mean(
                water_fraction, row, column, grid_size)
            roles = []
            if water_fraction[index] >= 0.65:
                roles.append("water")
            if (0.05 <= water_fraction[index] < 0.65
                    or (neighbor_water >= 0.20
                        and water_fraction[index] < 0.65)):
                roles.append("waterfront")
            if landmark_focus[index]:
                roles.append("landmark_focus")
            if (land[index]
                    and building_coverage[index]
                    >= max(0.02, quantiles["building_coverage_p75"])
                    and (junction_density[index]
                         >= max(1.0, quantiles["junction_density_p60"])
                         or road_density[index]
                         >= quantiles["road_density_p75"])):
                roles.append("dense_core")
            if (land[index]
                    and road["orientation_concentration"][index] >= 0.42
                    and road_density[index]
                    >= max(0.1, quantiles["road_density_p50"])):
                roles.append("grid")
            if (land[index]
                    and major_density[index]
                    >= max(0.05, quantiles["major_density_p75"])
                    and major_density[index]
                    >= road_density[index] * 0.20):
                roles.append("arterial")
            possible_gap = (
                land[index]
                and water_fraction[index] < 0.10
                and urban_signal[index] <= 0.18
                and neighbor_urban >= 0.52
            )
            if possible_gap:
                roles.append("possible_data_gap")
            if (land[index] and urban_signal[index] <= 0.25
                    and not possible_gap):
                roles.append("sparse")
            if not roles:
                roles.append("background")

            priority = (
                "possible_data_gap", "water", "landmark_focus", "waterfront",
                "dense_core", "grid", "arterial", "sparse", "background",
            )
            dominant = next(role for role in priority if role in roles)
            role_counts[dominant] += 1
            cells.append({
                "row": row,
                "column": column,
                "bounds": [
                    round(xmin + column * cell_width, 3),
                    round(ymin + row * cell_height, 3),
                    round(xmin + (column + 1) * cell_width, 3),
                    round(ymin + (row + 1) * cell_height, 3),
                ],
                "dominant_role": dominant,
                "roles": roles,
                "road_density_km_km2": round(road_density[index], 3),
                "major_road_density_km_km2": round(major_density[index], 3),
                "junction_density_km2": round(junction_density[index], 3),
                "orientation_concentration": round(
                    road["orientation_concentration"][index], 4),
                "building_coverage": round(building_coverage[index], 5),
                "building_count": int(building["count"][index]),
                "landmark_evidence_features": int(
                    building["landmark_count"][index]),
                "landmark_focus_score": round(landmark_score[index], 5),
                "water_fraction": round(water_fraction[index], 5),
                "shoreline_km": round(
                    water_data["shoreline_m"][index] / 1000.0, 3),
                "waterway_density_km_km2": round(
                    water_data["waterway_m"][index] / 1000.0
                    / effective_land_km2[index], 3),
                "urban_signal": round(urban_signal[index], 4),
                "neighbor_urban_signal": round(neighbor_urban, 4),
            })

    cell_total = grid_size * grid_size
    role_fractions = {
        role: round(value / cell_total, 4)
        for role, value in sorted(role_counts.items())
    }
    traits = []
    if float(np.mean(water_fraction)) >= 0.12:
        traits.append("water_led")
    if role_fractions.get("waterfront", 0.0) >= 0.08:
        traits.append("waterfront_composition")
    grid_cell_fraction = sum("grid" in cell["roles"] for cell in cells) / cell_total
    if grid_cell_fraction >= 0.20:
        traits.append("grid_structure")
    dense_cell_fraction = sum(
        "dense_core" in cell["roles"] for cell in cells) / cell_total
    if dense_cell_fraction >= 0.10:
        traits.append("dense_urban_core")
    if sum(building["landmark_count"]) > 0:
        traits.append("landmark_evidence")

    gap_count = sum(
        cell["dominant_role"] == "possible_data_gap" for cell in cells)
    if buildings is None or len(buildings) == 0:
        confidence = "low"
        confidence_reason = "building evidence is missing"
    elif roads is None or len(roads) == 0:
        confidence = "low"
        confidence_reason = "road evidence is missing"
    elif gap_count / max(1, int(land.sum())) > 0.08:
        confidence = "medium"
        confidence_reason = "several urban holes need external validation"
    else:
        confidence = "high"
        confidence_reason = "roads and buildings are present without many local holes"

    return {
        "version": SCENE_ANALYSIS_VERSION,
        "grid_size": grid_size,
        "bbox_projected_m": [xmin, ymin, xmax, ymax],
        "cell_area_km2": round(cell_area_m2 / 1e6, 5),
        "feature_counts": {
            "roads": 0 if roads is None else len(roads),
            "buildings": 0 if buildings is None else len(buildings),
            "water": 0 if water is None else len(water),
        },
        "summary": {
            "traits": traits,
            "role_fractions": role_fractions,
            "water_fraction": round(float(np.mean(water_fraction)), 5),
            "grid_cell_fraction": round(grid_cell_fraction, 4),
            "dense_core_cell_fraction": round(dense_cell_fraction, 4),
            "possible_data_gap_cells": gap_count,
            "landmark_evidence_features": int(
                sum(building["landmark_count"])),
            "landmark_focus_cells": int(landmark_focus.sum()),
            "landmark_focus_cell_limit": landmark_focus_limit,
            "quantiles": {key: round(value, 5)
                          for key, value in quantiles.items()},
            "osm_internal_consistency": confidence,
            "consistency_reason": confidence_reason,
            "warning": (
                "Relative OSM evidence only; sparse and incomplete data cannot "
                "be distinguished with certainty without an external reference."
            ),
        },
        "cells": cells,
    }


def render_scene_character(report: dict, output_path) -> str:
    """Render a four-panel PNG from an analysis report."""

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import ListedColormap

    grid_size = int(report["grid_size"])
    shape = (grid_size, grid_size)
    roles = [
        "background", "sparse", "arterial", "grid", "dense_core",
        "waterfront", "landmark_focus", "water", "possible_data_gap",
    ]
    colors = [
        "#ebe7df", "#d8d3ca", "#d47b4d", "#6d8f82", "#9b4d3f",
        "#4d88a8", "#d7a22a", "#1f3f5b", "#cc2f45",
    ]
    role_index = {role: index for index, role in enumerate(roles)}
    dominant = np.zeros(shape, dtype=int)
    road_density = np.zeros(shape, dtype=float)
    building = np.zeros(shape, dtype=float)
    water = np.zeros(shape, dtype=float)
    gaps = []
    for cell in report["cells"]:
        row, column = int(cell["row"]), int(cell["column"])
        dominant[row, column] = role_index[cell["dominant_role"]]
        road_density[row, column] = cell["road_density_km_km2"]
        building[row, column] = cell["building_coverage"] * 100.0
        water[row, column] = cell["water_fraction"] * 100.0
        if cell["dominant_role"] == "possible_data_gap":
            gaps.append((column, row))

    cross_source = report.get("cross_source_water") or {}
    compact_water_gaps = []
    linear_water_gaps = []
    if cross_source.get("status") == "evidence_only":
        candidate_cells = cross_source.get("candidate_cells", [])
        compact_cells = sorted(
            candidate_cells,
            key=lambda cell: -float(cell.get("compact_gap_area_m2", 0.0)),
        )[:8]
        linear_cells = sorted(
            candidate_cells,
            key=lambda cell: -float(cell.get("linear_gap_area_m2", 0.0)),
        )[:6]
        for cell in compact_cells:
            point = (int(cell["column"]), int(cell["row"]))
            if float(cell.get("compact_gap_area_m2", 0.0)) > 0:
                compact_water_gaps.append(point)
        for cell in linear_cells:
            point = (int(cell["column"]), int(cell["row"]))
            if float(cell.get("linear_gap_area_m2", 0.0)) > 0:
                linear_water_gaps.append(point)

    fig, axes = plt.subplots(2, 2, figsize=(15, 13), constrained_layout=True)
    role_image = axes[0, 0].imshow(
        dominant, origin="lower", cmap=ListedColormap(colors),
        vmin=-0.5, vmax=len(roles) - 0.5)
    axes[0, 0].set_title("Relative local role")
    handles = [plt.Line2D([0], [0], marker="s", linestyle="", color=color,
                          label=role, markersize=9)
               for role, color in zip(roles, colors)
               if role in {cell["dominant_role"] for cell in report["cells"]}]
    axes[0, 0].legend(handles=handles, loc="upper left", fontsize=8,
                      framealpha=0.9)

    road_image = axes[0, 1].imshow(
        np.log1p(road_density), origin="lower", cmap="magma")
    axes[0, 1].set_title("Structural road density (log km / km2)")
    fig.colorbar(road_image, ax=axes[0, 1], fraction=0.046)

    building_image = axes[1, 0].imshow(
        building, origin="lower", cmap="YlOrBr", vmin=0,
        vmax=max(5.0, _percentile(building, 95)))
    axes[1, 0].set_title("Building footprint coverage (%)")
    fig.colorbar(building_image, ax=axes[1, 0], fraction=0.046)

    water_image = axes[1, 1].imshow(
        water, origin="lower", cmap="Blues", vmin=0, vmax=100)
    water_title = "OSM water coverage (%) and gap evidence"
    if cross_source.get("status") == "evidence_only":
        water_title += (
            "\nAMap-only candidates: "
            f"{cross_source.get('compact_gap_count', 0)} compact / "
            f"{cross_source.get('linear_or_noise_gap_count', 0)} linear")
    elif cross_source.get("status") in {"unavailable", "error"}:
        water_title += f"\nAMap cross-check: {cross_source['status']}"
    axes[1, 1].set_title(water_title)
    if gaps:
        axes[1, 1].scatter(
            [x for x, _ in gaps], [y for _, y in gaps],
            marker="x", s=90, linewidths=2.0, color="#cc2f45",
            label="OSM internal urban hole")
    if compact_water_gaps:
        axes[1, 1].scatter(
            [x for x, _ in compact_water_gaps],
            [y for _, y in compact_water_gaps],
            marker="o", facecolors="none", edgecolors="#e58b2a",
            s=125, linewidths=2.2, label="largest AMap-only compact water")
    if linear_water_gaps:
        axes[1, 1].scatter(
            [x for x, _ in linear_water_gaps],
            [y for _, y in linear_water_gaps],
            marker="+", s=125, linewidths=2.2, color="#8d3f8f",
            label="largest AMap-only linear / noise")
    if gaps or compact_water_gaps or linear_water_gaps:
        axes[1, 1].legend(loc="upper left", fontsize=8, framealpha=0.9)
    fig.colorbar(water_image, ax=axes[1, 1], fraction=0.046)

    for axis in axes.flat:
        axis.set_xticks(range(grid_size))
        axis.set_yticks(range(grid_size))
        axis.set_xlabel("west to east")
        axis.set_ylabel("south to north")
        axis.grid(which="major", color="white", linewidth=0.35, alpha=0.45)

    summary = report["summary"]
    fig.suptitle(
        "Scene character diagnostic — "
        + ", ".join(summary["traits"] or ["no strong trait"])
        + f"\nOSM internal consistency: {summary['osm_internal_consistency']}; "
          f"water {summary['water_fraction'] * 100:.1f}%; "
          f"possible gaps {summary['possible_data_gap_cells']}",
        fontsize=15,
    )
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=150)
    plt.close(fig)
    return str(output_path)
