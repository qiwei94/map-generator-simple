"""Audit whether apparent breaks in the Paris E road style come from source data or selection."""
from __future__ import annotations

import json
import pickle
import sys
from collections import Counter
from pathlib import Path

from PIL import Image, ImageDraw
from shapely.geometry import Point
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SNAP = ROOT / "output/paris_BC_full_20260914_v9_inputs/surface_inputs.pkl"
FINAL = ROOT / "output/paris_25km_rounded_wave_waterfix_20260915"
OUT = ROOT / "output/paris_road_continuity_audit_20260916"


def line_parts(geom):
    if geom.geom_type == "LineString":
        yield geom
    elif geom.geom_type == "MultiLineString":
        yield from geom.geoms


def draw_lines(draw, lines, bbox, size, fill, width):
    minx, miny, maxx, maxy = bbox
    sx = (size - 1) / (maxx - minx)
    sy = (size - 1) / (maxy - miny)
    for geom in lines:
        for part in line_parts(geom):
            pts = [((x - minx) * sx, size - 1 - (y - miny) * sy) for x, y in part.coords]
            if len(pts) > 1:
                draw.line(pts, fill=fill, width=width, joint="curve")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with SNAP.open("rb") as handle:
        layers, kw = pickle.load(handle)
    with (FINAL / "roads.pkl").open("rb") as handle:
        selected, major, selection_evidence = pickle.load(handle)

    source = list(layers.block_base_cut_lines)
    selected_wkb = {g.wkb for g in selected}
    omitted = [g for g in source if g.wkb not in selected_wkb]
    rows = kw["source_roads"]
    tags = {
        row.geometry.wkb: (
            str(row.highway), str(row.name or ""), str(row.tunnel or "").casefold()
        )
        for row in rows.itertuples(index=False)
    }

    selected_classes = Counter(tags.get(g.wkb, ("unmatched", "", ""))[0] for g in selected)
    omitted_classes = Counter(tags.get(g.wkb, ("unmatched", "", ""))[0] for g in omitted)
    omitted_length = Counter()
    omitted_reasons = Counter()
    for g in omitted:
        highway, name, tunnel = tags.get(g.wkb, ("unmatched", "", ""))
        omitted_length[highway] += g.length
        if tunnel not in ("", "none", "nan", "no", "false", "0"):
            omitted_reasons["tunnel"] += g.length
        elif not name or name in ("None", "nan"):
            omitted_reasons["unnamed"] += g.length
        else:
            omitted_reasons["named_but_not_selected"] += g.length

    # Classify selected interior endpoints. If an omitted source line reaches the
    # endpoint while no other selected line does, selection made the visible break.
    all_tree = STRtree(source)
    major_tree = STRtree(major)
    selected_ids = {i for i, g in enumerate(source) if g.wkb in selected_wkb}
    minx, miny, maxx, maxy = kw["bbox_local"]
    endpoint_counts = Counter()
    examples = []
    for line in selected:
        for part in line_parts(line):
            for xy in (part.coords[0], part.coords[-1]):
                p = Point(xy)
                if min(xy[0] - minx, maxx - xy[0], xy[1] - miny, maxy - xy[1]) < 20:
                    endpoint_counts["frame_edge"] += 1
                    continue
                hits = [int(i) for i in all_tree.query(p.buffer(1.5), predicate="intersects")]
                other_selected = any(i in selected_ids and source[i].wkb != line.wkb for i in hits)
                if not other_selected:
                    other_selected = len(major_tree.query(p.buffer(1.5), predicate="intersects")) > 0
                touches_omitted = any(i not in selected_ids for i in hits)
                if touches_omitted and not other_selected:
                    kind = "selection_boundary"
                    if len(examples) < 100:
                        examples.append([round(xy[0], 1), round(xy[1], 1)])
                elif other_selected:
                    kind = "selected_continuation_or_junction"
                elif touches_omitted:
                    kind = "junction_with_omitted_branch"
                else:
                    kind = "source_dead_end_or_segmentation_gap"
                endpoint_counts[kind] += 1

    size = 2600
    image = Image.new("RGB", (size, size), "#f4f1e9")
    draw = ImageDraw.Draw(image)
    draw_lines(draw, omitted, kw["bbox_local"], size, "#e66b55", 1)
    draw_lines(draw, selected, kw["bbox_local"], size, "#183a56", 2)
    draw_lines(draw, major, kw["bbox_local"], size, "#151515", 2)
    image.save(OUT / "source_vs_e_selection.png")

    report = {
        "source_local_features": len(source),
        "e_selected_local_features": len(selected),
        "omitted_local_features": len(omitted),
        "source_local_length_m": sum(g.length for g in source),
        "e_selected_local_length_m": sum(g.length for g in selected),
        "omitted_local_length_m": sum(g.length for g in omitted),
        "selected_classes": dict(selected_classes),
        "omitted_classes": dict(omitted_classes),
        "omitted_length_by_class_m": dict(omitted_length),
        "omitted_length_by_reason_m": dict(omitted_reasons),
        "selected_endpoint_classification": dict(endpoint_counts),
        "selection_boundary_examples_local_m": examples,
        "selection_evidence": selection_evidence,
        "interpretation": {
            "selection_boundary": "Selected road stops where an omitted source road is present within 1.5 m.",
            "source_dead_end_or_segmentation_gap": "No other source road touches within 1.5 m; includes legitimate dead ends.",
            "limitation": "Endpoint proximity is diagnostic, not a legal or map-truth completeness test.",
        },
    }
    (OUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
