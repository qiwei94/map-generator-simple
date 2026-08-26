"""Deterministic, portable DesignSpec records for generated artifacts.

The DesignSpec is an audit record.  It describes the inputs and resolved
pipeline decisions, but never drives mesh vertices, Z values, or booleans.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


SCHEMA_VERSION = "1.3"


def _json_value(value: Any) -> Any:
    """Convert numpy/pydantic/path values to stable JSON-compatible values."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    if hasattr(value, "item"):
        return _json_value(value.item())
    if hasattr(value, "to_dict"):
        return _json_value(value.to_dict())
    return str(value)


def artifact_identity(path: os.PathLike | str) -> dict:
    """Return a reproducible identity for a generated artifact."""
    artifact = Path(path)
    digest = hashlib.sha256()
    with artifact.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "filename": artifact.name,
        "size_bytes": artifact.stat().st_size,
        "sha256": digest.hexdigest(),
    }


def layer_evidence(layers: Any) -> dict:
    """Capture non-zero acceptance evidence from a LayerPolygons-like object."""
    evidence = {
        "building_landmarks": len(getattr(layers, "BL", ()) or ()),
        "building_blocks": len(getattr(layers, "BO", ()) or ()),
        "vegetation_landmarks": len(getattr(layers, "VL", ()) or ()),
        "vegetation_polygons": len(getattr(layers, "VO", ()) or ()),
        "water_landmarks": len(getattr(layers, "WL", ()) or ()),
        "water_polygons": len(getattr(layers, "WO", ()) or ()),
        "block_base_polygons": len(getattr(layers, "block_base", ()) or ()),
        "roads": len(getattr(layers, "roads_lines", ()) or ()),
    }
    road_roles = getattr(layers, "road_roles", {}) or {}
    for source_key, output_key in (
        ("source_line_features", "road_source_lines"),
        ("topology_candidates", "road_topology_candidates"),
        ("structural_candidates", "road_structural_candidates"),
        ("visible_candidates", "road_visible_candidates"),
        ("visible_segments", "road_visible_segments"),
    ):
        if source_key in road_roles:
            evidence[output_key] = int(road_roles[source_key])
    water_roles = getattr(layers, "water_roles", {}) or {}
    for source_key, output_key in (
        ("source_line_segments", "water_source_line_segments"),
        ("candidate_groups", "water_candidate_groups"),
        ("selected_groups", "water_selected_groups"),
        ("visible_line_segments", "water_visible_line_segments"),
        ("gap_bridges", "water_gap_bridges"),
        ("ordinary_polygon_drops", "water_ordinary_polygon_drops"),
    ):
        if source_key in water_roles:
            evidence[output_key] = int(water_roles[source_key])
    return evidence


def build_design_spec(
    *,
    city: str,
    bbox_wgs84: Sequence[float],
    artifact_path: os.PathLike | str,
    params: Optional[Mapping] = None,
    decisions: Optional[Mapping] = None,
    profile: Optional[Mapping] = None,
    source_features: Optional[Mapping[str, int]] = None,
    printable_features: Optional[Mapping[str, int]] = None,
    height_sources: Optional[Mapping[str, int]] = None,
    height_evidence: Optional[Mapping] = None,
    block_base: Optional[Mapping] = None,
    printability: Optional[Mapping] = None,
    road_roles: Optional[Mapping] = None,
    water_roles: Optional[Mapping] = None,
    pipeline: str = "generate_city_legacy",
) -> dict:
    """Build a validated DesignSpec dictionary without mutating generation."""
    bbox = [float(value) for value in bbox_wgs84]
    if len(bbox) != 4 or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        raise ValueError("bbox_wgs84 must be [south, west, north, east]")
    if not city.strip():
        raise ValueError("city must not be empty")

    source = {str(key): int(value)
              for key, value in (source_features or {}).items()}
    printable = {str(key): int(value)
                 for key, value in (printable_features or {}).items()}
    heights = {str(key): int(value)
               for key, value in (height_sources or {}).items()}
    if any(value < 0 for value in (
            *source.values(), *printable.values(), *heights.values())):
        raise ValueError("feature counts must be non-negative")

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "city": city,
        "bbox_wgs84": bbox,
        "pipeline": pipeline,
        "generation_mode": "full",
        "artifact": artifact_identity(artifact_path),
        "params": _json_value(params or {}),
        "decisions": _json_value(decisions or {}),
        "profile": _json_value(profile or {}),
        "block_base": _json_value(block_base or {}),
        "printability": _json_value(printability or {}),
        "road_roles": _json_value(road_roles or {}),
        "water_roles": _json_value(water_roles or {}),
        "evidence": {
            "source_features": source,
            "printable_features": printable,
            "building_height_sources": heights,
            "building_height": _json_value(height_evidence or {}),
        },
    }


def write_design_spec(output_dir: os.PathLike | str, spec: Mapping) -> str:
    """Atomically write ``design_spec.json`` and return its path."""
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / "design_spec.json"
    payload = json.dumps(_json_value(spec), ensure_ascii=False,
                         indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=directory,
        prefix=".design_spec.", suffix=".tmp", delete=False,
    ) as stream:
        temporary = Path(stream.name)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, destination)
    return str(destination)
