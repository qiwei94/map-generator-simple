#!/usr/bin/env python3
"""Batch city validation tool.

Runs auto-parameter detection for multiple cities and reports results.
Does NOT run the full pipeline (no PBF/mesh generation) — only tests
the parameter decision logic against different city profiles.

Usage:
    venv/bin/python tools/batch_validate.py
    venv/bin/python tools/batch_validate.py --output tmp/validation
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _TEXTURE_STYLE_OF_DEEPSEEK.auto_params.city_profile import CityProfile
from _TEXTURE_STYLE_OF_DEEPSEEK.auto_params.param_resolver import resolve_params, explain_decisions


# Synthetic city profiles for validation (based on known characteristics)
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
    "la_paz": CityProfile(
        area_km2=200, elevation_range_m=1200, relief_ratio="mountainous",
        water_ratio=0.01, building_density=600, avg_building_area_m2=100,
        height_tag_coverage=0.05, road_density_km_per_km2=6.0,
        vegetation_ratio=0.05, is_coastal=False, osm_quality="poor",
    ),
    "reykjavik": CityProfile(
        area_km2=100, elevation_range_m=200, relief_ratio="moderate",
        water_ratio=0.10, building_density=80, avg_building_area_m2=300,
        height_tag_coverage=0.70, road_density_km_per_km2=5.0,
        vegetation_ratio=0.03, is_coastal=True, osm_quality="fair",
    ),
}


def validate_city(name: str, profile: CityProfile) -> dict:
    """Run parameter resolution and check for anomalies."""
    params = resolve_params(profile)
    report = explain_decisions(profile, params)
    report["city"] = name

    # Sanity checks
    warnings = []
    if params.z_gamma < 0.30 or params.z_gamma > 0.65:
        warnings.append(f"z_gamma={params.z_gamma} outside comfort zone [0.30, 0.65]")
    if params.building_density_threshold > 0.02:
        warnings.append(f"density_threshold={params.building_density_threshold} very high")
    if params.building_v2_road_tier < 3 and profile.road_density_km_per_km2 > 10:
        warnings.append(f"road_tier={params.building_v2_road_tier} low for dense roads")

    report["warnings"] = warnings
    return report


def main():
    parser = argparse.ArgumentParser(description="Batch city validation")
    parser.add_argument(
        "--cities", type=str, default=None,
        help="Comma-separated city names (default: all)"
    )
    parser.add_argument(
        "--output", default="tmp/batch_validate",
        help="Output directory"
    )
    args = parser.parse_args()

    cities = CITY_PROFILES
    if args.cities:
        names = [c.strip() for c in args.cities.split(",")]
        cities = {k: v for k, v in CITY_PROFILES.items() if k in names}

    os.makedirs(args.output, exist_ok=True)

    print(f"Validating {len(cities)} cities...")
    print("=" * 70)

    all_results = []
    total_warnings = 0

    for name, profile in cities.items():
        result = validate_city(name, profile)
        all_results.append(result)

        style = result["style_selected"]
        n_warn = len(result["warnings"])
        total_warnings += n_warn
        status = "✓" if n_warn == 0 else f"⚠ {n_warn} warning(s)"

        print(f"  {name:12s} → style={style:14s} {status}")
        for w in result["warnings"]:
            print(f"    ⚠ {w}")

    print("=" * 70)
    print(f"Total: {len(cities)} cities, {total_warnings} warnings")

    # Save full report
    report_path = os.path.join(args.output, "batch_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False, default=str)
    print(f"Report: {report_path}")


if __name__ == "__main__":
    main()
