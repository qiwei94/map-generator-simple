#!/usr/bin/env python3
"""A/B parameter comparison tool.

Generates PNGs with different parameter values for visual comparison.

Usage:
    venv/bin/python tools/param_compare.py --city westlake --param Z_GAMMA --values 0.35,0.45,0.55
    venv/bin/python tools/param_compare.py --city westlake --param building_density_threshold --values 0.001,0.005,0.01
"""

import argparse
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _TEXTURE_STYLE_OF_DEEPSEEK.auto_params.city_profile import CityProfile
from _TEXTURE_STYLE_OF_DEEPSEEK.auto_params.param_resolver import (
    ResolvedParams,
    resolve_params,
    explain_decisions,
)


def main():
    parser = argparse.ArgumentParser(description="Compare parameter values visually")
    parser.add_argument("--city", required=True, help="City name (for report naming)")
    parser.add_argument("--param", required=True, help="Parameter name to vary")
    parser.add_argument(
        "--values", required=True,
        help="Comma-separated values to compare (e.g. 0.35,0.45,0.55)"
    )
    parser.add_argument(
        "--output", default="tmp/param_compare",
        help="Output directory (default: tmp/param_compare)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Only print parameter decisions, don't generate PNGs"
    )
    args = parser.parse_args()

    values = [_parse_value(v.strip()) for v in args.values.split(",")]
    os.makedirs(args.output, exist_ok=True)

    print(f"Parameter comparison: {args.param}")
    print(f"Values: {values}")
    print(f"City: {args.city}")
    print(f"Output: {args.output}")
    print("=" * 60)

    # Create a baseline profile (would be detected from real data in full pipeline)
    baseline_profile = CityProfile(
        area_km2=685.0,
        elevation_range_m=456.0,
        relief_ratio="moderate",
        water_ratio=0.08,
        building_density=850,
        avg_building_area_m2=220,
        height_tag_coverage=0.12,
        road_density_km_per_km2=9.3,
        vegetation_ratio=0.22,
        is_coastal=False,
        osm_quality="fair",
    )

    results = []
    for i, val in enumerate(values):
        override = {args.param: val}
        params = resolve_params(baseline_profile, user_overrides=override)
        report = explain_decisions(baseline_profile, params)
        report["variant"] = f"{args.param}={val}"

        results.append(report)
        print(f"\n[Variant {i+1}] {args.param} = {val}")
        print(f"  Style: {params.style}")
        print(f"  Z_GAMMA: {params.z_gamma}")
        print(f"  flat_mode: {params.flat_mode}")
        print(f"  road_tier: {params.building_v2_road_tier}")

        if not args.dry_run:
            print(f"  → To generate PNG, run pipeline with --auto-params "
                  f"and override {args.param}={val}")

    # Save comparison report
    report_path = os.path.join(args.output, f"compare_{args.param}.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False, default=str)
    print(f"\nComparison report: {report_path}")


def _parse_value(s: str):
    """Parse a string value to int or float."""
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        return s


if __name__ == "__main__":
    main()
