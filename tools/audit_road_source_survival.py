"""Read-only road survival audit for cached 25 km showcase inputs.

This answers whether a sparse-looking image lacks OSM roads or discarded
available ones.  It does not infer road truth from a reference raster.
"""
from __future__ import annotations

import argparse
import json
import pickle
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from _TEXTURE_STYLE_OF_DEEPSEEK.buildings import ROAD_TIERS
CASES = {
    "westlake": (
        "西湖",
        "cache/pipeline/hero_westlake_25km_aesthetic/gdfs_v1_e748a9264a63.pkl",
        "cache/pipeline/hero_westlake_25km_aesthetic/preprocess_v8_95f687ddfc16.pkl",
    ),
    "chicago": (
        "芝加哥",
        "cache/pipeline/showcase_chicago_25km_aesthetic/gdfs_v1_4bfeedffe027.pkl",
        "cache/pipeline/showcase_chicago_25km_aesthetic/preprocess_v5_dc0076aeac05.pkl",
    ),
    "new_york": (
        "纽约",
        "cache/pipeline/showcase_new_york_25km_aesthetic/gdfs_v1_a47e796ad878.pkl",
        "cache/pipeline/showcase_new_york_25km_aesthetic/preprocess_v5_dc0076aeac05.pkl",
    ),
}


def audit(key: str) -> dict:
    name, raw_path, layer_path = CASES[key]
    raw_path, layer_path = ROOT / raw_path, ROOT / layer_path
    with raw_path.open("rb") as handle:
        source = pickle.load(handle)
    with layer_path.open("rb") as handle:
        layers = pickle.load(handle)
    roads = source["roads"]
    lines = roads.loc[roads.geometry.geom_type.isin(("LineString", "MultiLineString"))]
    tiers = {
        str(tier): int(lines["highway"].isin(allowed).sum())
        for tier, allowed in ROAD_TIERS.items()
    }
    final = list(getattr(layers, "roads_lines", ()) or ())
    final_classes = Counter(str(item[1]) for item in final)
    final_roles = Counter(str(item[3]) if len(item) > 3 else "legacy_unspecified" for item in final)
    topology_candidates = tiers["2"]
    return {
        "city": name,
        "source_cache": str(raw_path),
        "preprocess_cache": str(layer_path),
        "raw": {
            "road_features": int(len(roads)),
            "line_features": int(len(lines)),
            "highway_counts": {str(k): int(v) for k, v in lines["highway"].value_counts().items()},
            "tier_candidates": tiers,
        },
        "preprocess": {
            "final_road_segments": len(final),
            "final_segment_fraction_of_tier_2_source": len(final) / max(1, topology_candidates),
            "final_length_m": float(sum(item[0].length for item in final)),
            "block_base_cut_lines": len(getattr(layers, "block_base_cut_lines", ()) or ()),
            "block_base_polygons": len(getattr(layers, "block_base", ()) or ()),
            "final_classes": dict(final_classes),
            "final_roles": dict(final_roles),
            "recorded_road_roles": getattr(layers, "road_roles", {}) or {},
        },
        "finding": (
            "source_has_urban_road_network; investigate selection/topology/rendering"
            if topology_candidates else "source_road_network_insufficient"
        ),
        "limitations": [
            "This compares matching local caches, not legal/ground-truth completeness.",
            "Legacy caches without recorded road_roles cannot attribute every exclusion to one rule.",
            "Feature count is not rendered road length or visual quality.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    payload = {key: audit(key) for key in CASES}
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    for key, record in payload.items():
        raw, final = record["raw"], record["preprocess"]
        print(f"{key}: raw tier-2={raw['tier_candidates']['2']:,}; "
              f"final={final['final_road_segments']:,}; "
              f"fraction={final['final_segment_fraction_of_tier_2_source']:.1%}")


if __name__ == "__main__":
    main()
