import json
from types import SimpleNamespace

import pytest

from _TEXTURE_STYLE_OF_DEEPSEEK.design_spec import (
    build_design_spec, layer_evidence, write_design_spec,
)


def test_full_design_spec_records_artifact_and_feature_evidence(tmp_path):
    artifact = tmp_path / "model.3mf"
    artifact.write_bytes(b"real-3mf-fixture")
    layers = SimpleNamespace(
        BL=[1, 2], BO=[1], VL=[], VO=[1, 2, 3], WL=[1], WO=[1],
        block_base=[1, 2, 3, 4], roads_lines=[1, 2, 3, 4, 5],
        water_roles={"candidate_groups": 7, "selected_groups": 2,
                     "gap_bridges": 1},
    )

    spec = build_design_spec(
        city="paris",
        bbox_wgs84=[48.83, 2.31, 48.88, 2.38],
        artifact_path=artifact,
        params={"road_tier": 4},
        decisions={"road_tier": "large area printability cap"},
        source_features={"roads": 253628, "water": 1224},
        printable_features=layer_evidence(layers),
        height_sources={"osm_height": 12, "default": 88},
        block_base={"requested_mode": "textured", "resolved_mode": "textured"},
        road_roles={"policy_version": "print-road-roles-v1",
                    "visible_segments": 5},
        water_roles={"policy_version": "print-water-roles-v1",
                     "selected_groups": 2},
    )
    path = write_design_spec(tmp_path, spec)
    saved = json.loads(open(path, encoding="utf-8").read())

    assert saved["schema_version"] == "1.2"
    assert saved["artifact"]["filename"] == "model.3mf"
    assert saved["artifact"]["size_bytes"] == len(b"real-3mf-fixture")
    assert len(saved["artifact"]["sha256"]) == 64
    assert saved["evidence"]["source_features"]["roads"] == 253628
    assert saved["evidence"]["printable_features"]["roads"] == 5
    assert saved["evidence"]["printable_features"]["water_landmarks"] == 1
    assert saved["road_roles"]["policy_version"] == "print-road-roles-v1"
    assert saved["water_roles"]["policy_version"] == "print-water-roles-v1"
    assert saved["evidence"]["printable_features"]["water_selected_groups"] == 2
    assert saved["evidence"]["building_height_sources"] == {
        "default": 88, "osm_height": 12}


def test_design_spec_serializes_printability_report(tmp_path):
    artifact = tmp_path / "model.3mf"
    artifact.write_bytes(b"print-aware")
    spec = build_design_spec(
        city="chicago",
        bbox_wgs84=[41.8, -87.7, 41.9, -87.6],
        artifact_path=artifact,
        printability={
            "printer_profile": {"nozzle_diameter_mm": 0.4},
            "derived_xy_real_m": {"nozzle_diameter": 50.0},
        },
    )
    saved = json.loads(open(write_design_spec(tmp_path, spec),
                            encoding="utf-8").read())
    assert saved["printability"]["printer_profile"]["nozzle_diameter_mm"] == 0.4
    assert saved["printability"]["derived_xy_real_m"]["nozzle_diameter"] == 50.0


@pytest.mark.parametrize("bbox", [
    [48.8, 2.3, 48.7, 2.4],
    [48.8, 2.4, 48.9, 2.3],
    [48.8, 2.3, 48.9],
])
def test_design_spec_rejects_invalid_bbox(tmp_path, bbox):
    artifact = tmp_path / "model.3mf"
    artifact.write_bytes(b"x")
    with pytest.raises(ValueError):
        build_design_spec(city="paris", bbox_wgs84=bbox,
                          artifact_path=artifact)


def test_design_spec_rejects_negative_feature_counts(tmp_path):
    artifact = tmp_path / "model.3mf"
    artifact.write_bytes(b"x")
    with pytest.raises(ValueError, match="non-negative"):
        build_design_spec(
            city="paris", bbox_wgs84=[48.8, 2.3, 48.9, 2.4],
            artifact_path=artifact, source_features={"roads": -1},
        )
