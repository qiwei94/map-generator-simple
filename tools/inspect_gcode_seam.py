#!/usr/bin/env python3
"""Measure one straight calibration seam from sliced G-code outer walls."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re


NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"


def _word(line: str, letter: str):
    match = re.search(rf"(?:^|\s){letter}({NUMBER})(?=\s|$)", line)
    return float(match.group(1)) if match else None


def measure_straight_seam(
    lines,
    *,
    seam_x: float,
    y_min: float,
    y_max: float,
    min_layer_z: float,
) -> dict:
    layer_z = None
    feature = ""
    line_width = None
    x = None
    y = None
    candidates = []

    for raw in lines:
        line = raw.strip()
        if line.startswith("; Z_HEIGHT:"):
            layer_z = float(line.split(":", 1)[1].strip())
            continue
        if line.startswith("; FEATURE:"):
            feature = line.split(":", 1)[1].strip()
            continue
        if line.startswith("; LINE_WIDTH:"):
            line_width = float(line.split(":", 1)[1].strip())
            continue
        if not line.startswith("G1"):
            continue

        new_x = _word(line, "X")
        new_y = _word(line, "Y")
        extrusion = _word(line, "E")
        next_x = x if new_x is None else new_x
        next_y = y if new_y is None else new_y
        if (
            layer_z is not None
            and layer_z + 1e-9 >= min_layer_z
            and feature == "Outer wall"
            and line_width is not None
            and extrusion is not None
            and extrusion > 0
            and x is not None and y is not None
            and next_x is not None and next_y is not None
            and abs(next_x - x) <= 0.02
            and max(min(y, next_y), y_min) <= min(max(y, next_y), y_max)
            and abs(next_x - seam_x) <= 5.0
        ):
            candidates.append({
                "layer_z_mm": layer_z,
                "wall_center_x_mm": next_x,
                "line_width_mm": line_width,
            })
        x, y = next_x, next_y

    layers = {}
    for item in candidates:
        layers.setdefault(item["layer_z_mm"], []).append(item)

    measurements = []
    for z_value, walls in sorted(layers.items()):
        left = [wall for wall in walls if wall["wall_center_x_mm"] < seam_x]
        right = [wall for wall in walls if wall["wall_center_x_mm"] > seam_x]
        if not left or not right:
            continue
        left_wall = max(left, key=lambda wall: wall["wall_center_x_mm"])
        right_wall = min(right, key=lambda wall: wall["wall_center_x_mm"])
        clear_gap = (
            right_wall["wall_center_x_mm"]
            - right_wall["line_width_mm"] / 2.0
            - left_wall["wall_center_x_mm"]
            - left_wall["line_width_mm"] / 2.0
        )
        measurements.append({
            "layer_z_mm": z_value,
            "left_outer_wall": left_wall,
            "right_outer_wall": right_wall,
            "clear_gap_mm": round(clear_gap, 6),
        })

    verified_gap = min(
        (item["clear_gap_mm"] for item in measurements), default=0.0)
    return {
        "policy_version": "straight-gcode-seam-v1",
        "seam_x_mm": seam_x,
        "y_interval_mm": [y_min, y_max],
        "min_layer_z_mm": min_layer_z,
        "measured_layers": measurements,
        "verified_min_clear_gap_mm": verified_gap,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gcode", type=Path)
    parser.add_argument("--seam-x", type=float, required=True)
    parser.add_argument("--y-min", type=float, required=True)
    parser.add_argument("--y-max", type=float, required=True)
    parser.add_argument("--min-layer-z", type=float, required=True)
    parser.add_argument("--required-gap", type=float, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    with args.gcode.open(encoding="utf-8", errors="replace") as handle:
        result = measure_straight_seam(
            handle,
            seam_x=args.seam_x,
            y_min=args.y_min,
            y_max=args.y_max,
            min_layer_z=args.min_layer_z,
        )
    result.update({
        "gcode": args.gcode.name,
        "required_gap_mm": args.required_gap,
        "passed": bool(
            math.isfinite(result["verified_min_clear_gap_mm"])
            and result["verified_min_clear_gap_mm"] + 1e-6
            >= args.required_gap
        ),
    })
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
