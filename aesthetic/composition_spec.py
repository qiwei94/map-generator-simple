"""Auditable composition decisions for city-scale printable maps.

The spec records *which source identities and spatially matched segments*
receive visual emphasis and why.
It deliberately contains no geometry, mesh vertices, global Z values, boolean
instructions, or generative-model output.  OSM remains the geometry source;
AMap is an optional read-only salience reference.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "1.0"
POLICY_VERSION = "city-composition-v4"


def _json_value(value: Any) -> Any:
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


def _safe_reference_evidence(evidence: Mapping | None) -> dict:
    """Keep cache/reference provenance without serializing images or secrets."""

    source = evidence or {}
    allowed = (
        "status", "reason", "palette_version", "template_policy_version",
        "bbox_wgs84", "image_size", "coverage", "source", "mask_evidence",
    )
    result = {key: _json_value(source[key])
              for key in allowed if key in source}
    if source.get("cache_path"):
        # Absolute controller/worker paths are not portable evidence.
        result["cache_file"] = Path(str(source["cache_path"])).name
    return result


def _composition_roles(role_evidence: Mapping | None) -> dict:
    roles = (role_evidence or {}).get("composition_roles", {}) or {}
    return _json_value(roles)


def build_composition_spec(
    *,
    city: str,
    bbox_wgs84: Sequence[float],
    layers: Any,
    amap_evidence: Mapping | None = None,
    pipeline: str = "generate_city_legacy",
) -> dict:
    """Build a deterministic, JSON-safe CompositionSpec.

    The result is deterministic for the same selected layer evidence.  Runtime
    timestamps are intentionally omitted so two identical decisions compare
    byte-for-byte after JSON serialization.
    """

    bbox = [float(value) for value in bbox_wgs84]
    if len(bbox) != 4 or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        raise ValueError("bbox_wgs84 must be [south, west, north, east]")
    if not city.strip():
        raise ValueError("city must not be empty")

    road_evidence = getattr(layers, "road_roles", {}) or {}
    water_evidence = getattr(layers, "water_roles", {}) or {}
    roads = _composition_roles(road_evidence)
    water = _composition_roles(water_evidence)
    reference = _safe_reference_evidence(amap_evidence)

    warnings = []
    if reference.get("status") != "ready":
        warnings.append(
            "AMap salience reference unavailable; OSM semantics and physical "
            "budgets determined hierarchy")
    if not roads:
        warnings.append("road composition roles are unavailable")
    if not water:
        warnings.append("water composition roles are unavailable")

    return {
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "city": city.strip(),
        "bbox_wgs84": bbox,
        "pipeline": pipeline,
        "decision_contract": {
            "salience_reference": (
                "AMap class masks may assign hierarchy to spatially matching "
                "OSM linework only"),
            "geometry_authority": "OSM source geometry",
            "print_authority": "printer profile and resolved physical floors",
            "render_authority": "deterministic role-aware renderer",
            "density_authority": (
                "structural OSM network may cut block base without becoming ink"),
        },
        "forbidden_controls": [
            "mesh vertices",
            "global Z values",
            "boolean operations",
            "invented replacement geometry",
        ],
        "reference": {
            "provider": "AMap",
            "use": "read-only spatial composition template",
            "evidence": reference,
        },
        "roads": roads,
        "water": water,
        "background": {
            "block_base_polygons": len(
                getattr(layers, "block_base", ()) or ()),
            "policy": (
                "preserve density from structural seams; hide low-value "
                "corridors from high-contrast ink"),
        },
        "printability": {
            "nozzle_real_m": float(getattr(layers, "nozzle_real_m", 0.0)),
            "min_area_m2": float(getattr(layers, "min_area_m2", 0.0)),
            "road_width_policy": _json_value(
                road_evidence.get("width_policy", {})),
        },
        "evidence": {
            "road_policy_version": road_evidence.get("policy_version"),
            "water_policy_version": water_evidence.get("policy_version"),
            "road_source_lines": int(
                road_evidence.get("source_line_features", 0)),
            "road_visible_segments": len(
                getattr(layers, "roads_lines", ()) or ()),
            "water_source_segments": int(
                water_evidence.get("source_line_segments", 0)),
            "water_visible_segments": int(
                water_evidence.get("visible_line_segments", 0)),
            "road_continuity_restoration": _json_value(
                road_evidence.get("ink_budget", {}).get(
                    "continuity_restoration", {})),
            "road_dangling_chain_pruning": _json_value(
                road_evidence.get("ink_budget", {}).get(
                    "dangling_chain_pruning", {})),
        },
        "warnings": warnings,
    }


def write_composition_spec(output_dir: os.PathLike | str,
                           spec: Mapping) -> str:
    """Atomically write ``composition_spec.json`` and return its path."""

    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / "composition_spec.json"
    payload = json.dumps(_json_value(spec), ensure_ascii=False,
                         indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=directory,
        prefix=".composition_spec.", suffix=".tmp", delete=False,
    ) as stream:
        temporary = Path(stream.name)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, destination)
    return str(destination)
