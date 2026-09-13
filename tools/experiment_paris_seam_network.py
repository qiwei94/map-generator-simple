#!/usr/bin/env python3
"""Compare source-complete road seam policies on frozen Paris S6 regions.

This is deliberately a PNG-only diagnostic.  It never writes to the source
cache and never alters production road selection.  The experiment distinguishes
between the current post-coarsening seam set, a complete tier-2 OSM backbone,
and a bounded tier-3 supplement.  All candidates use existing source geometry;
there is no raster tracing or endpoint invention.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path
import sys
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from shapely.geometry import box
from shapely.ops import unary_union
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _TEXTURE_STYLE_OF_DEEPSEEK.buildings import ROAD_TIERS
from aesthetic.review_render import _Rasterizer, _rasterize_mask
from tools.evaluate_urban_organization import load_checked
from tools.experiment_negative_road_width import (
    _polygon_parts, cut_carrier, draw_negative, nearby,
)


LOCAL_GAP_MM = 0.28
MAJOR_GAP_MM = 0.42
# A 3 km diagnostic needs cells materially smaller than a complete arterial
# crossing; six cells made every cell look "served" in central Paris and could
# not distinguish a grid from a single diagonal.
GRID_SIZE = 16
CELL_CAPACITY = 2
MIN_COMPONENT_LENGTH_M = 180.0


def _line_parts(geometry):
    if geometry is None or geometry.is_empty:
        return []
    if geometry.geom_type == "LineString":
        return [geometry]
    if geometry.geom_type == "MultiLineString":
        return list(geometry.geoms)
    if hasattr(geometry, "geoms"):
        return [part for part in geometry.geoms if part.geom_type == "LineString"]
    return []


def _grid_cells(geometry, bounds, grid_size=GRID_SIZE):
    xmin, ymin, xmax, ymax = bounds
    dx, dy = (xmax - xmin) / grid_size, (ymax - ymin) / grid_size
    cells = set()
    for part in _line_parts(geometry):
        length = max(float(part.length), 1.0)
        samples = max(2, min(48, int(length / max(dx, dy) * 6) + 2))
        for distance in np.linspace(0.0, length, samples):
            point = part.interpolate(float(distance))
            x = min(grid_size - 1, max(0, int((point.x - xmin) / dx)))
            y = min(grid_size - 1, max(0, int((point.y - ymin) / dy)))
            cells.add(y * grid_size + x)
    return cells


def _connected_components(frame):
    """Return connected source-only components, grouped by named/ref identity.

    Unnamed ways stay independent.  This conservative rule cannot accidentally
    join every nearby residential street into a synthetic city-wide corridor.
    """
    buckets = defaultdict(list)
    for index, row in frame.iterrows():
        geom = row.geometry
        if not _line_parts(geom):
            continue
        name = row.get("name") or row.get("ref")
        key = (str(name).strip().casefold() if isinstance(name, str) and name.strip()
               else f"__unnamed_{index}")
        buckets[key].append(geom)
    result = []
    for identity, geometries in buckets.items():
        tree = STRtree(geometries)
        parent = list(range(len(geometries)))

        def root(index):
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        for index, geom in enumerate(geometries):
            for neighbour in tree.query(geom, predicate="intersects"):
                left, right = root(index), root(int(neighbour))
                if left != right:
                    parent[right] = left
        groups = defaultdict(list)
        for index, geom in enumerate(geometries):
            groups[root(index)].append(geom)
        for parts in groups.values():
            merged = unary_union(parts)
            if not merged.is_empty:
                result.append((identity, merged))
    return result


def _supplement_tier3(tier2, tier3_only, bounds):
    """Admit complete tier-3 components only where they add spatial structure."""
    load = defaultdict(int)
    for geom in tier2.geometry:
        for cell in _grid_cells(geom, bounds):
            load[cell] += 1
    selected, evidence = [], []
    frame = box(*bounds)
    t2_union = unary_union(list(tier2.geometry)) if len(tier2) else None
    for identity, geometry in _connected_components(tier3_only):
        geometry = geometry.intersection(frame)
        if geometry.is_empty or geometry.length < MIN_COMPONENT_LENGTH_M:
            continue
        cells = _grid_cells(geometry, bounds)
        under_served = sum(load[cell] < CELL_CAPACITY for cell in cells)
        if under_served < 2:
            continue
        # Keep a real connector, loop, or frame axis.  A one-ended local stub
        # is the exact artifact this experiment must avoid reintroducing.
        endpoints = []
        for part in _line_parts(geometry):
            coords = list(part.coords)
            if len(coords) > 1 and not part.is_ring:
                endpoints.extend((coords[0], coords[-1]))
        attached = 0 if t2_union is None or t2_union.is_empty else sum(
            t2_union.distance(box(x - 18, y - 18, x + 18, y + 18)) <= 18
            for x, y, *_ in endpoints
        )
        margin = 45.0
        frame_hits = sum(
            x - bounds[0] <= margin or bounds[2] - x <= margin
            or y - bounds[1] <= margin or bounds[3] - y <= margin
            for x, y, *_ in endpoints
        )
        loop = any(part.is_ring for part in _line_parts(geometry))
        if not (attached >= 2 or loop or (attached >= 1 and frame_hits >= 1)):
            continue
        score = under_served * 10 + min(5.0, geometry.length / 600.0)
        evidence.append((score, identity, geometry, cells, attached, frame_hits, loop))
    evidence.sort(key=lambda item: (-item[0], -item[2].length, item[1]))
    # A bounded selection; this is explicitly not all residential streets.
    for score, identity, geometry, cells, attached, frame_hits, loop in evidence[:24]:
        selected.append(geometry)
        for cell in cells:
            load[cell] += 1
    return selected, {
        "method": "complete_source_tier3_spatial_seam_experiment_v1",
        "grid_size": GRID_SIZE,
        "cell_capacity": CELL_CAPACITY,
        "minimum_component_length_m": MIN_COMPONENT_LENGTH_M,
        "candidate_components": len(evidence),
        "selected_components": len(selected),
        "selected_length_m": round(sum(item.length for item in selected), 3),
        "geometry_policy": "complete_existing_osm_components_only",
        "invented_connectors": 0,
    }


def _water_mask(bounds, layers, *, pixels):
    raster = _Rasterizer(bounds, pixels * 2, pixels * 2)
    parts = [part for geom in list(layers.WL) + list(layers.WO)
             for part in _polygon_parts(geom) if part.intersects(box(*bounds))]
    return _rasterize_mask(raster, parts).astype(bool)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-dir", type=Path, required=True)
    parser.add_argument("--registration", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--surface-variant", choices=("A", "B", "C"), default="A",
                        help="Frozen S6 carrier; A retains the canonical block base.")
    args = parser.parse_args()
    started = time.monotonic()
    root = args.experiment_dir.resolve()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    registration = json.loads(args.registration.read_text())
    manifest = json.loads((root / "capture_manifest.json").read_text())
    scale = float(manifest["scale_mm_per_m"])
    layers = load_checked(root / args.surface_variant / "layers.pkl")
    source = load_checked(root / "s5_input.pkl")["runtime"]["sources"].roads
    tiers = source[source.highway.isin(set(ROAD_TIERS[3]))].copy()
    tier2 = tiers[tiers.highway.isin(set(ROAD_TIERS[2]))].copy()
    tier3_only = tiers[~tiers.highway.isin(set(ROAD_TIERS[2]))].copy()
    retained_all = list(layers.block_base) + list(layers.BO)
    reference = Image.open(Path(registration["sources"][1]["path"])).convert("RGB")
    font = ImageFont.truetype("/System/Library/Fonts/STHeiti Medium.ttc", 28)
    small = ImageFont.truetype("/System/Library/Fonts/STHeiti Medium.ttc", 20)
    row_images, reports = [], []
    for region in registration["regions"]:
        core = box(*region["bbox_local_m"])
        work = core.buffer(180, join_style=2)
        retained = unary_union(nearby(retained_all, work)).intersection(work)
        removed = unary_union(nearby(layers.surface_road_reveals, work)).intersection(work)
        carrier = unary_union([retained, removed])
        heroes = unary_union(nearby([poly for poly, _ in layers.BL], work)).intersection(work)
        water = unary_union(nearby(list(layers.WL) + list(layers.WO), work)).intersection(work)
        bridges = unary_union(nearby(layers.surface_road_polygons, work)).intersection(water)
        base_local = nearby(layers.block_base_cut_lines, work)
        base_major = nearby(layers.block_base_major_cut_lines, work)
        raw_tier2 = [geom.intersection(work) for geom in tier2.iloc[
            tier2.sindex.query(work, predicate="intersects")].geometry]
        supplement, supplement_evidence = _supplement_tier3(
            tier2.iloc[tier2.sindex.query(work, predicate="intersects")],
            tier3_only.iloc[tier3_only.sindex.query(work, predicate="intersects")],
            core.bounds,
        )
        variants = [
            ("A 当前后处理缝隙", base_local, base_major),
            ("B 完整 tier-2 骨架", raw_tier2, base_major),
            ("C tier-2 + 受控 tier-3", raw_tier2 + supplement, base_major),
        ]
        panels, variant_report = [], []
        for index, (label, local, major) in enumerate(variants):
            city, clearance = cut_carrier(
                carrier, local, major, scale, LOCAL_GAP_MM, MAJOR_GAP_MM)
            city = city.intersection(core)
            target = out / f'{region["id"]}_{chr(65 + index)}.png'
            draw_negative(city, heroes, water, bridges, core.bounds, target,
                          pixels=900, visible_water_mask=_water_mask(
                              core.bounds, layers, pixels=900))
            panels.append(Image.open(target).convert("RGB"))
            variant_report.append({
                "label": label, "local_source_features": len(local),
                "local_source_length_m": round(sum(g.length for g in local), 3),
                "frame_city_coverage": round(city.area / core.area, 6),
                "clearance": clearance, "path": str(target),
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            })
        quad = np.asarray(region["reference_pixel_quad"], dtype=float)
        step_x, step_y = (quad[1] - quad[0]) / 900, (quad[3] - quad[0]) / 900
        ref_panel = reference.transform(
            (900, 900), Image.Transform.AFFINE,
            (step_x[0], step_y[0], quad[0, 0], step_x[1], step_y[1], quad[0, 1]),
            Image.Resampling.BICUBIC,
        )
        panels.append(ref_panel)
        sheet = Image.new("RGB", (3680, 1020), "#f7f7f5")
        draw = ImageDraw.Draw(sheet)
        titles = [item[0] for item in variants] + ["参考 demo"]
        for index, (panel, title) in enumerate(zip(panels, titles)):
            x = index * 920 + 10
            draw.text((x, 10), f'{region["id"]} {region["label"]}｜{title}', font=font, fill="#222")
            if index < 3:
                entry = variant_report[index]
                draw.text((x, 48), f'线段 {entry["local_source_features"]:,} · {entry["local_source_length_m"] / 1000:.1f} km', font=small, fill="#555")
            else:
                draw.text((x, 48), "固定仿射配准 · 只作拓扑观感对照", font=small, fill="#555")
            sheet.paste(panel.resize((900, 900), Image.Resampling.LANCZOS), (x, 100))
        sheet.save(out / f'{region["id"]}_seam_comparison.png')
        row_images.append(sheet)
        reports.append({"region": region, "variants": variant_report,
                        "tier3_supplement": supplement_evidence})
    board = Image.new("RGB", (3680, 3 * 1020 + 85), "#f7f7f5")
    title = ImageDraw.Draw(board)
    title.text((20, 16), "巴黎道路负空间实验｜同一 S6 城市面、同一水体；只替换完整 OSM 缝隙网络", font=font, fill="#222")
    for index, image in enumerate(row_images):
        board.paste(image, (0, 75 + index * 1020))
    board.save(out / "three_regions_seam_comparison.png")
    report = {
        "purpose": "diagnostic", "verdict": "human_review",
        "scope": "PNG-only frozen S6 carrier road-seam comparison",
        "surface_variant": args.surface_variant,
        "production_defaults_changed": False, "downloads": 0,
        "local_gap_mm": LOCAL_GAP_MM, "major_gap_mm": MAJOR_GAP_MM,
        "source": {"roads": len(source), "tier2": len(tier2), "tier3_only": len(tier3_only)},
        "regions": reports, "elapsed_seconds": round(time.monotonic() - started, 3),
        "limitations": [
            "Carrier inherits the frozen S6 building mass; this does not regenerate blocks.",
            "Reference is a registered raster guide, not geometry truth.",
            "No source route is drawn or invented; variants only cut existing OSM lines.",
            "PNG result is not a slicer or 3MF acceptance result.",
        ],
    }
    (out / "evidence.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
