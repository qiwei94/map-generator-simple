#!/usr/bin/env python3
"""Audit how an offline city GDF cache consumes the persistent height store."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import pickle
import sys

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from _TEXTURE_STYLE_OF_DEEPSEEK.buildings import (  # noqa: E402
    _compress_height,
    _quantize_height_mm,
    building_height_mapping_context,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.print_profile import (  # noqa: E402
    DEFAULT_PRINTER_PROFILE,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.building_height_store import (  # noqa: E402
    BuildingHeightStore,
    height_store_identity,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import (  # noqa: E402
    _estimate_building_heights,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.wikidata_height_enrichment import (  # noqa: E402
    load_wikidata_heights,
)


def audit_height_usage(cache_path: Path, store_path: Path,
                       *, city: str = "unknown") -> dict:
    """Return source adoption and model-Z evidence without regenerating meshes.

    ``cache_path`` is a project-owned pickle produced by PipelineCache.  It is
    never accepted from a remote user upload because pickle is executable.
    """
    cache_path = cache_path.resolve()
    store_path = store_path.resolve()
    with cache_path.open("rb") as stream:
        cached = pickle.load(stream)
    buildings = cached.get("buildings") if isinstance(cached, dict) else None
    if buildings is None:
        raise ValueError("pipeline cache does not contain a buildings layer")

    BuildingHeightStore(str(store_path))
    wikidata_heights, wikidata_labels = load_wikidata_heights(
        buildings, cache_dir=str(store_path.parent), auto_fetch=False)
    buildings["est_height"] = _estimate_building_heights(
        buildings, wikidata_heights=wikidata_heights)
    mapping = building_height_mapping_context(buildings)
    layer_height = DEFAULT_PRINTER_PROFILE.layer_height_mm
    mapping["layer_height_mm"] = layer_height

    source_counts = {
        str(key): int(value)
        for key, value in buildings["height_source"].value_counts(
            dropna=False).sort_index().items()
    }
    qids = (buildings["wikidata"].dropna().astype(str)
            if "wikidata" in buildings.columns else pd.Series(dtype=str))
    matched = buildings.loc[wikidata_heights.notna()].copy()
    matched["cached_wikidata_height_m"] = wikidata_heights.loc[matched.index]
    matched["cached_wikidata_label"] = wikidata_labels.loc[matched.index]
    rows = []
    for idx, row in matched.sort_values(
            "cached_wikidata_height_m", ascending=False).head(50).iterrows():
        source_height = float(row["est_height"])
        base_mm = _quantize_height_mm(
            _compress_height(
                source_height, 5000,
                height_ceiling_m=mapping["height_ceiling_m"]),
            layer_height,
        )
        rows.append({
            "row_index": str(idx),
            "qid": str(row.get("wikidata") or ""),
            "name": str(row.get("name") or
                        row.get("cached_wikidata_label") or ""),
            "selected_source": str(row["height_source"]),
            "source_height_m": round(source_height, 3),
            "cached_wikidata_height_m": round(
                float(row["cached_wikidata_height_m"]), 3),
            "compressed_base_height_mm": round(float(base_mm), 3),
        })

    return {
        "city": city,
        "pipeline_cache": {
            "path": str(cache_path),
            "size_bytes": cache_path.stat().st_size,
        },
        "height_store": height_store_identity(str(store_path)),
        "building_count": int(len(buildings)),
        "height_source_counts": source_counts,
        "wikidata": {
            "tagged_rows": int(len(qids)),
            "distinct_qids": int(qids.str.upper().nunique()),
            "cached_height_rows": int(wikidata_heights.notna().sum()),
            "cached_height_distinct_qids": int(
                matched["wikidata"].dropna().astype(str).str.upper().nunique())
                if "wikidata" in matched.columns else 0,
        },
        "model_z_mapping": mapping,
        "matched_landmarks": rows,
        "note": (
            "compressed_base_height_mm is pre-category audit evidence; final "
            "printable Z is recorded by the generated 3MF DesignSpec after "
            "landmark selection, penalties, offsets and geometry subtraction"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pipeline_cache", type=Path)
    parser.add_argument(
        "--store", type=Path,
        default=PROJECT_ROOT / "data" / "height_cache" /
        "building_heights.sqlite3")
    parser.add_argument("--city", default="unknown")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = audit_height_usage(args.pipeline_cache, args.store, city=args.city)
    payload = json.dumps(report, ensure_ascii=False, indent=2,
                         sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, args.output)
        print(args.output.resolve())
    else:
        print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
