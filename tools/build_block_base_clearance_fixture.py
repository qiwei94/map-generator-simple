#!/usr/bin/env python3
"""Build a small deterministic 3MF proving final Block base road clearance."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.collections import PolyCollection
import trimesh
from shapely.geometry import LineString, box


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _TEXTURE_STYLE_OF_DEEPSEEK.block_base import (  # noqa: E402
    build_deepseek_block_base_v3,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.design_spec import (  # noqa: E402
    build_design_spec,
    write_design_spec,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf  # noqa: E402
from _TEXTURE_STYLE_OF_DEEPSEEK.config import BLOCK_BASE_THICKNESS_MM  # noqa: E402
from _TEXTURE_STYLE_OF_DEEPSEEK.print_profile import (  # noqa: E402
    DEFAULT_PRINTER_PROFILE,
    PrintScale,
    build_printability_report,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    # A production-sized XY plate with one transformed-city-block surrogate.
    # Source metres intentionally equal model millimetres in this fixture.
    terrain = trimesh.creation.box(extents=[196.0, 176.0, 4.0])
    structural_line = LineString([(0.0, -40.0), (0.0, 40.0)])
    block_mesh, evidence = build_deepseek_block_base_v3(
        [box(-30.0, -20.0, 30.0, 20.0)],
        terrain,
        scale=1.0,
        brick_style=False,
        clearance_lines=[structural_line],
        final_clearance_mm=DEFAULT_PRINTER_PROFILE.final_block_base_gap_mm,
        return_clearance_evidence=True,
    )
    if block_mesh is None or evidence is None or not evidence["passed"]:
        raise RuntimeError("fixture failed to produce verified block-base geometry")

    # Put the complete fixture on a 256 mm-class printer bed.  Generation
    # coordinates are centre-origin, while slicer plates use positive XY/Z.
    plate_shift = (128.0, 128.0, 2.0)
    terrain.apply_translation(plate_shift)
    block_mesh.apply_translation(plate_shift)

    evidence.update({
        "printer_profile_id": DEFAULT_PRINTER_PROFILE.profile_id,
        "configured_min_gap_mm": DEFAULT_PRINTER_PROFILE.min_gap_mm,
        "extrusion_width_mm": DEFAULT_PRINTER_PROFILE.extrusion_width_mm,
        "derivation": "max(min_gap_mm, 2 * extrusion_width_mm)",
    })
    artifact = args.output_dir / "block_base_clearance_fixture.3mf"
    export_deepseek_3mf(
        {"terrain": terrain, "block_base": block_mesh}, str(artifact))

    # Save visual evidence from the actual exported block mesh, not from a
    # separately invented diagram.  Only upward-facing triangles are needed
    # for the orthographic top-down footprint check.
    top_faces = block_mesh.faces[block_mesh.face_normals[:, 2] > 0.5]
    top_triangles = block_mesh.vertices[top_faces][:, :, :2]
    fig, ax = plt.subplots(figsize=(9.8, 8.8), dpi=160)
    ax.set_facecolor("#d8d5ce")
    ax.add_collection(PolyCollection(
        top_triangles, facecolor="#e8e2d4", edgecolor="#3a3833",
        linewidth=0.12))
    ax.set_xlim(terrain.bounds[0, 0], terrain.bounds[1, 0])
    ax.set_ylim(terrain.bounds[0, 1], terrain.bounds[1, 1])
    ax.set_aspect("equal")
    ax.axis("off")
    fig.savefig(
        args.output_dir / "block_base_clearance_fixture_topdown.png",
        bbox_inches="tight", pad_inches=0)
    plt.close(fig)

    spec = build_design_spec(
        city="synthetic-block-base-clearance",
        bbox_wgs84=(0.0, 0.0, 0.001, 0.001),
        artifact_path=artifact,
        params={"fixture": True},
        source_features={"roads": 1},
        printable_features={"block_base": evidence["output_polygons"]},
        block_base={
            "requested_mode": "flat",
            "resolved_mode": "flat",
            "policy_version": "fixture-v1",
            "final_clearance": evidence,
        },
        printability=build_printability_report(
            DEFAULT_PRINTER_PROFILE,
            PrintScale(196.0, 176.0),
            z_thicknesses_mm={
                "block_base_thickness_mm": BLOCK_BASE_THICKNESS_MM,
            },
        ),
        road_roles={"structural_candidates": 1},
        pipeline="tools/build_block_base_clearance_fixture.py",
    )
    write_design_spec(args.output_dir, spec)
    print(artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
