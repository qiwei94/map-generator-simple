#!/usr/bin/env python3
"""Compare a printable top-down render with AMap visual salience masks."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from aesthetic.amap_salience import (
    compare_salience_masks,
    extract_amap_salience_masks,
    extract_review_salience_masks,
    fetch_amap_salience_reference,
    render_salience_comparison,
)


def _parse_bbox(value: str) -> tuple[float, float, float, float]:
    try:
        parts = tuple(float(part.strip()) for part in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("bbox values must be numbers") from exc
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("bbox must be south,west,north,east")
    return parts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit printable road/water salience against AMap style 7")
    parser.add_argument("--bbox", required=True, type=_parse_bbox)
    parser.add_argument("--candidate-png", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--reference-png", type=Path,
                        help="already cropped exact-frame AMap style-7 PNG")
    parser.add_argument("--zoom", type=int, default=13)
    parser.add_argument("--size", type=int, default=1024)
    parser.add_argument("--tolerance-px", type=int, default=5)
    parser.add_argument(
        "--allow-network", action="store_true",
        help="allow a cache miss to download keyless AMap style-7 tiles")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.candidate_png.is_file():
        raise SystemExit(f"candidate PNG not found: {args.candidate_png}")
    args.output_dir.mkdir(parents=True, exist_ok=True)

    if args.reference_png:
        if not args.reference_png.is_file():
            raise SystemExit(f"reference PNG not found: {args.reference_png}")
        reference_rgb = Image.open(args.reference_png).convert("RGB")
        source_evidence = {
            "status": "available",
            "source": "explicit_exact_frame_png",
            "path": str(args.reference_png),
            "bbox_wgs84": list(args.bbox),
        }
    else:
        reference_array, source_evidence = fetch_amap_salience_reference(
            args.bbox,
            zoom=args.zoom,
            output_size=args.size,
            allow_network=args.allow_network,
        )
        if reference_array is None:
            raise SystemExit(source_evidence.get(
                "reason", "AMap salience reference unavailable"))
        reference_rgb = Image.fromarray(reference_array)

    reference = extract_amap_salience_masks(reference_rgb)
    candidate_rgb = Image.open(args.candidate_png).convert("RGB")
    candidate_roads, candidate_water = extract_review_salience_masks(
        candidate_rgb)
    report = compare_salience_masks(
        reference, candidate_roads, candidate_water,
        tolerance_px=args.tolerance_px)
    report["bbox_wgs84"] = list(args.bbox)
    report["source"] = source_evidence
    report["reference_evidence"] = reference.evidence
    report["candidate"] = {
        "path": str(args.candidate_png),
        "adapter": "urban_topdown_rgb_v1",
        "warning": (
            "Flattened PNG extraction is diagnostic. Production integration "
            "must consume render_review_bundle masks directly."
        ),
    }

    reference_path = args.output_dir / f"{args.tag}_amap_reference.png"
    json_path = args.output_dir / f"{args.tag}_amap_salience.json"
    comparison_path = (
        args.output_dir / f"{args.tag}_amap_salience_comparison.png")
    reference_rgb.save(reference_path)
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    render_salience_comparison(
        reference, candidate_roads, candidate_water, report, comparison_path)
    print(json.dumps({
        "reference": str(reference_path),
        "comparison": str(comparison_path),
        "json": str(json_path),
        "roads": report["roads"],
        "water": report["water"],
        "road_ink_distribution_similarity": report[
            "road_ink_distribution_similarity"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
