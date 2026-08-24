import json

from aesthetic.composition_spec import (
    build_composition_spec,
    write_composition_spec,
)
from _TEXTURE_STYLE_OF_DEEPSEEK._layer_preprocess import LayerPolygons


def _layers():
    return LayerPolygons(
        roads_lines=[("not-serialized-geometry", "primary", False, "primary")],
        road_roles={
            "policy_version": "print-road-roles-v8",
            "source_line_features": 12,
            "width_policy": {
                "road_width_multiplier": 2.0,
                "min_colored_strip_mm": 0.63,
            },
            "composition_roles": {
                "schema_version": "1.0",
                "primary": {
                    "features": 1,
                    "identities": ["primary:name:central avenue"],
                },
                "background": {"role": "block_base_only", "features": 9},
            },
        },
        water_roles={
            "policy_version": "print-water-roles-v6",
            "source_line_segments": 4,
            "visible_line_segments": 1,
            "composition_roles": {
                "schema_version": "1.0",
                "surface": {"role": "primary", "visible_surface_ratio": 0.1},
                "primary": {"groups": 0, "identities": []},
            },
        },
        block_base=[1, 2, 3],
        nozzle_real_m=50.0,
        min_area_m2=2500.0,
    )


def test_composition_spec_records_contract_and_identities_not_geometry():
    spec = build_composition_spec(
        city="Test City",
        bbox_wgs84=(10, 20, 11, 21),
        layers=_layers(),
        amap_evidence={
            "status": "ready",
            "palette_version": "amap-mask-v1",
            "template_policy_version": "amap-template-v1",
            "cache_path": "/private/controller/cache/reference.png",
            "secret": "must-not-leak",
        },
    )

    payload = json.dumps(spec, sort_keys=True)
    assert spec["roads"]["primary"]["identities"] == [
        "primary:name:central avenue"]
    assert spec["decision_contract"]["geometry_authority"] == (
        "OSM source geometry")
    assert "not-serialized-geometry" not in payload
    assert "must-not-leak" not in payload
    assert "/private/controller" not in payload
    assert spec["reference"]["evidence"]["cache_file"] == "reference.png"
    assert spec["reference"]["evidence"]["template_policy_version"] == (
        "amap-template-v1")
    assert "spatially matching" in (
        spec["decision_contract"]["salience_reference"])
    assert spec["warnings"] == []


def test_composition_spec_is_deterministic_and_written_atomically(tmp_path):
    kwargs = dict(
        city="Test City", bbox_wgs84=(10, 20, 11, 21), layers=_layers(),
        amap_evidence={"status": "disabled", "reason": "test"})
    first = build_composition_spec(**kwargs)
    second = build_composition_spec(**kwargs)

    assert first == second
    path = write_composition_spec(tmp_path, first)
    assert path.endswith("composition_spec.json")
    assert json.loads((tmp_path / "composition_spec.json").read_text()) == first
    assert first["warnings"]
