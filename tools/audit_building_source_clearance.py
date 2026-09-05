#!/usr/bin/env python3
"""Replay only source assignment/clearance from a trusted local mass A/B run.

No downloading, aggregation, mesh generation or deployment. The operator's
explicit evidence file references trusted project pickle caches, not uploads.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aesthetic.building_mass_strategy import (
    _assign_to_blocks, _clip_sources_for_mass, _flatten_buildings,
)
from tools.evaluate_building_mass_strategy import (
    _load_trusted_pickle, _subset_wgs84_frame,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import (
    bbox_to_utm, project_geodataframe,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    started = time.perf_counter()
    evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
    provenance, candidate = evidence["provenance"], evidence["candidate"]
    bbox = provenance["bbox_wgs84"]
    topology = _load_trusted_pickle(Path(provenance["topology_cache"]["path"]))
    key = topology["key"]
    if list(key["bbox"]) != list(bbox):
        raise SystemExit("topology cache bbox does not match the evidence")
    source_path = Path(provenance["gdf_cache"])
    stat = source_path.stat()
    if (str(source_path.resolve()) != key["gdf_cache"]
            or stat.st_size != key["gdf_cache_size"]
            or stat.st_mtime_ns != key["gdf_cache_mtime_ns"]):
        raise SystemExit("source cache has changed since topology derivation")
    width = candidate["component_width_target"]
    if abs(float(key["model_span_mm"]) - width["current_model_span_mm"]) > 1e-6:
        raise SystemExit("topology and candidate model spans differ")
    projection = bbox_to_utm(*bbox)
    scale = width["current_model_span_mm"] / max(
        projection["width_m"], projection["height_m"])
    nozzle = candidate["printer_profile"]["nozzle_diameter_mm"] / scale
    if abs(nozzle - candidate["nozzle_real_m"]) > 1e-4:
        raise SystemExit("candidate scale does not match the requested bbox")
    inset = nozzle * candidate["policy"]["block_inset_nozzles"]
    topology_inset = topology["evidence"]["boundary_inset_each_side_model_mm"]
    if abs(inset * scale - topology_inset) > 1e-4:
        raise SystemExit("topology and source candidate clearances differ")
    payload = _load_trusted_pickle(source_path)
    source = _subset_wgs84_frame(payload["buildings"], bbox)
    buildings = project_geodataframe(
        source, projection["utm_crs"], projection["origin"],
        clip_bbox=projection["utm_bbox"])
    footprints = _flatten_buildings(buildings)
    blocks = topology["blocks"]
    assignments = _assign_to_blocks(footprints, blocks)
    totals = dict(input_footprints=0, footprints_surviving_clearance=0,
                  source_inside_block_area_m2=0.0,
                  source_after_clearance_area_m2=0.0)
    print(f"[audit] {len(footprints):,} footprints / {len(assignments):,} occupied blocks",
          flush=True)
    for ordinal, (index, indexes) in enumerate(assignments.items(), 1):
        _, _, _, audit = _clip_sources_for_mass(
            [footprints[i] for i in indexes], blocks[index], inset=inset)
        for name in totals:
            totals[name] += audit[name]
        if ordinal % 1000 == 0:
            print(f"[audit] {ordinal:,}/{len(assignments):,} blocks", flush=True)
    retention = (totals["source_after_clearance_area_m2"]
                 / totals["source_inside_block_area_m2"]
                 if totals["source_inside_block_area_m2"] > 0 else None)
    report = {
        "schema": "building-source-clearance-audit-v1",
        "status": "diagnostic_only", "evidence": str(args.evidence.resolve()),
        "bbox_wgs84": bbox, "scale_mm_per_m": scale,
        "inset_each_side_model_mm": inset * scale,
        "source_footprints": len(footprints), "occupied_blocks": len(assignments),
        "unassigned_source_footprints": len(footprints) - totals["input_footprints"],
        **totals, "clearance_source_area_retention": retention,
        "aggregation_run": False, "print_acceptance": "not_evaluated",
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    markdown = (
        "# 建筑源数据退让审计\n\n"
        "仅复用该次运行的源数据、分块与退让参数，不运行聚合或 3MF。\n\n"
        f"- 原始有效轮廓：{len(footprints):,}\n"
        f"- 未分配到街区：{report['unassigned_source_footprints']:,}\n"
        f"- 每侧退让：{inset * scale:.3f} mm（模型单位）\n"
        f"- 分块内原始支撑面积：{totals['source_inside_block_area_m2']:.1f} ㎡\n"
        f"- 退让后支撑面积：{totals['source_after_clearance_area_m2']:.1f} ㎡\n"
        f"- 退让后面积保留率：{retention if retention is not None else '无数据'}\n"
        f"- 耗时：{report['elapsed_seconds']} 秒\n\n"
        "面积是在同一分块内对原始建筑做并集后计算；不是拿旧版填充面积作分母。\n"
        "本报告不评价聚合、美观、地标和打印是否通过；没有修改任何源数据。\n")
    args.output.with_suffix(".md").write_text(markdown, encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
