#!/usr/bin/env python3
"""Single-parameter sensitivity analysis tool.

Shows how varying one parameter affects the resolved parameter set
across multiple city profiles. Outputs a table + optional JSON report.

Usage:
    python tools/param_sensitivity.py --param z_gamma --range 0.30,0.40,0.45,0.55,0.65
    python tools/param_sensitivity.py --param building_density_threshold --range 0.001,0.005,0.01,0.05
    python tools/param_sensitivity.py --param building_v2_road_tier --range 2,3,4,5 --cities westlake,chicago
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _TEXTURE_STYLE_OF_DEEPSEEK.auto_params.city_profile import CityProfile
from _TEXTURE_STYLE_OF_DEEPSEEK.auto_params.param_resolver import (
    ResolvedParams,
    resolve_params,
)

# Reuse synthetic profiles from batch_validate
CITY_PROFILES = {
    "westlake": CityProfile(
        area_km2=685, elevation_range_m=456, relief_ratio="moderate",
        water_ratio=0.08, building_density=850, avg_building_area_m2=220,
        height_tag_coverage=0.12, road_density_km_per_km2=9.3,
        vegetation_ratio=0.22, is_coastal=False, osm_quality="fair",
    ),
    "chicago": CityProfile(
        area_km2=625, elevation_range_m=30, relief_ratio="flat",
        water_ratio=0.12, building_density=2500, avg_building_area_m2=180,
        height_tag_coverage=0.45, road_density_km_per_km2=18.0,
        vegetation_ratio=0.05, is_coastal=False, osm_quality="good",
    ),
    "chongqing": CityProfile(
        area_km2=500, elevation_range_m=800, relief_ratio="mountainous",
        water_ratio=0.10, building_density=1200, avg_building_area_m2=150,
        height_tag_coverage=0.08, road_density_km_per_km2=7.0,
        vegetation_ratio=0.30, is_coastal=False, osm_quality="fair",
    ),
    "tokyo": CityProfile(
        area_km2=400, elevation_range_m=50, relief_ratio="flat",
        water_ratio=0.08, building_density=4000, avg_building_area_m2=120,
        height_tag_coverage=0.60, road_density_km_per_km2=22.0,
        vegetation_ratio=0.08, is_coastal=True, osm_quality="good",
    ),
    "dubai": CityProfile(
        area_km2=300, elevation_range_m=20, relief_ratio="flat",
        water_ratio=0.15, building_density=100, avg_building_area_m2=800,
        height_tag_coverage=0.35, road_density_km_per_km2=4.0,
        vegetation_ratio=0.01, is_coastal=True, osm_quality="fair",
    ),
    "venice": CityProfile(
        area_km2=8, elevation_range_m=3, relief_ratio="flat",
        water_ratio=0.55, building_density=3000, avg_building_area_m2=100,
        height_tag_coverage=0.20, road_density_km_per_km2=5.0,
        vegetation_ratio=0.02, is_coastal=True, osm_quality="fair",
    ),
}

# Key output metrics to track
_TRACKED_FIELDS = [
    "style", "z_gamma", "flat_mode", "building_density_threshold",
    "building_print_limit_m2", "building_v2_road_tier",
    "road_width_multiplier", "vegetation_min_area_m2",
    "water_min_area_m2", "brick_perlin_amp",
]


def analyze_sensitivity(
    param_name: str,
    values: list,
    cities: dict,
) -> list:
    """Run sensitivity analysis for one parameter across cities.

    Returns list of dicts: [{city, param_value, resolved_metrics, reason}]
    """
    results = []

    for city_name, profile in cities.items():
        for val in values:
            override = {param_name: val}
            params = resolve_params(profile, user_overrides=override)

            metrics = {}
            for field in _TRACKED_FIELDS:
                metrics[field] = getattr(params, field, None)

            reason = params.reasons.get(param_name, "N/A")
            results.append({
                "city": city_name,
                "param_value": val,
                "metrics": metrics,
                "reason": reason,
            })

    return results


def print_table(results: list, param_name: str, values: list, cities: dict):
    """Print a formatted sensitivity table."""
    city_names = list(cities.keys())

    # Header
    print(f"\n{'='*80}")
    print(f"  Sensitivity: {param_name}")
    print(f"  Values: {values}")
    print(f"  Cities: {city_names}")
    print(f"{'='*80}")

    # For each tracked metric, show how it changes
    for metric in _TRACKED_FIELDS:
        if metric == param_name:
            continue  # skip the input param itself

        print(f"\n  ┌─ {metric}")
        header = f"  │ {'value':>10s}"
        for cn in city_names:
            header += f" │ {cn:>10s}"
        print(header)
        print(f"  │{'─' * (12 + len(city_names) * 13)}")

        for val in values:
            row = f"  │ {str(val):>10s}"
            for cn in city_names:
                # Find matching result
                match = next(
                    (r for r in results
                     if r["city"] == cn and r["param_value"] == val),
                    None
                )
                if match:
                    mv = match["metrics"].get(metric, "?")
                    if isinstance(mv, float):
                        row += f" │ {mv:>10.4f}"
                    elif isinstance(mv, bool):
                        row += f" │ {'T' if mv else 'F':>10s}"
                    else:
                        row += f" │ {str(mv):>10s}"
                else:
                    row += f" │ {'?':>10s}"
            print(row)
        print(f"  └{'─' * (12 + len(city_names) * 13)}")

    # Show reasons for the varied param
    print(f"\n  Reasons for {param_name}:")
    for val in values:
        reasons_set = set()
        for r in results:
            if r["param_value"] == val:
                reasons_set.add(r["reason"])
        for reason in sorted(reasons_set):
            print(f"    {val}: {reason}")


def main():
    parser = argparse.ArgumentParser(
        description="Single-parameter sensitivity analysis"
    )
    parser.add_argument(
        "--param", required=True,
        help="Parameter name to vary (e.g. z_gamma, building_density_threshold)"
    )
    parser.add_argument(
        "--range", required=True,
        help="Comma-separated values to test (e.g. 0.001,0.005,0.01,0.05)"
    )
    parser.add_argument(
        "--cities", type=str, default=None,
        help="Comma-separated city names (default: all)"
    )
    parser.add_argument(
        "--output", default=None,
        help="Output JSON path (optional)"
    )
    args = parser.parse_args()

    # Parse values
    values = []
    for v in args.range.split(","):
        v = v.strip()
        try:
            values.append(int(v) if "." not in v else float(v))
        except ValueError:
            values.append(v)

    # Select cities
    cities = CITY_PROFILES
    if args.cities:
        names = [c.strip() for c in args.cities.split(",")]
        cities = {k: v for k, v in CITY_PROFILES.items() if k in names}
        if not cities:
            print(f"ERROR: No matching cities. Available: {list(CITY_PROFILES.keys())}")
            sys.exit(1)

    # Validate param name
    test_params = ResolvedParams()
    if not hasattr(test_params, args.param):
        print(f"ERROR: '{args.param}' is not a valid ResolvedParams field.")
        print(f"Available: {[f for f in vars(test_params) if not f.startswith('_')]}")
        sys.exit(1)

    # Run analysis
    results = analyze_sensitivity(args.param, values, cities)

    # Print table
    print_table(results, args.param, values, cities)

    # Save JSON if requested
    if args.output:
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump({
                "param": args.param,
                "values": values,
                "cities": list(cities.keys()),
                "results": results,
            }, f, indent=2, ensure_ascii=False, default=str)
        print(f"\n  JSON report: {args.output}")


if __name__ == "__main__":
    main()
