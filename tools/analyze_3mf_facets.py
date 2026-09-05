#!/usr/bin/env python3
"""Measure top-surface triangle size for every object in a 3MF artifact."""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _TEXTURE_STYLE_OF_DEEPSEEK.validator import (  # noqa: E402
    _get_object_meshes,
    _terrain_surface_edge_metrics,
)


def analyze(path: Path) -> dict:
    with zipfile.ZipFile(path, "r") as archive:
        objects = _get_object_meshes(archive)

    report = {"artifact": str(path.resolve()), "objects": {}}
    for name, obj in sorted(objects.items()):
        vertices = np.asarray(obj["vertices"], dtype=np.float64)
        faces = np.asarray(obj["faces"], dtype=np.int32)
        metrics = _terrain_surface_edge_metrics(vertices, faces)
        metrics.update({
            "vertex_count": int(len(vertices)),
            "face_count": int(len(faces)),
        })
        report["objects"][name] = metrics
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    report = analyze(args.path)
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(payload)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
