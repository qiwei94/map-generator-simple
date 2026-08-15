"""Tests for the constrained DesignSpec contract."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import geopandas as gpd
import numpy as np
import pytest
import trimesh
from shapely.geometry import LineString

from _TEXTURE_STYLE_OF_DEEPSEEK.design_spec import (
    DESIGN_PRESETS,
    DESIGN_SPEC_VERSION,
    LayerSpec,
    filter_features,
    resolve_design_spec,
)


@pytest.mark.parametrize("preset", DESIGN_PRESETS)
def test_presets_are_versioned_and_keep_terrain(preset):
    spec = resolve_design_spec(preset=preset)

    assert spec.version == DESIGN_SPEC_VERSION
    assert spec.preset == preset
    assert spec.enabled("terrain") is True
    assert len(spec.fingerprint) == 64


def test_preset_source_selection_is_minimal():
    assert resolve_design_spec(preset="terrain_only").required_sources() == ()
    assert resolve_design_spec(preset="road_network").required_sources() == ("roads",)
    assert resolve_design_spec(preset="water_focus").required_sources() == ("water",)


def test_landmark_only_design_uses_building_source_without_ambient_buildings():
    spec = resolve_design_spec(
        {
            "version": DESIGN_SPEC_VERSION,
            "name": "landmarks-only",
            "layers": {
                "terrain": {"enabled": True},
                "landmarks": {"enabled": True},
            },
        }
    )

    assert spec.landmarks_only is True
    assert spec.required_sources() == ("buildings",)


def test_printable_spec_rejects_disabled_terrain():
    with pytest.raises(ValueError, match="terrain"):
        resolve_design_spec(
            {
                "version": DESIGN_SPEC_VERSION,
                "layers": {"terrain": {"enabled": False}},
            }
        )


def test_spec_rejects_geometry_controls():
    with pytest.raises(ValueError, match="unsupported DesignSpec fields"):
        resolve_design_spec(
            {
                "version": DESIGN_SPEC_VERSION,
                "global_z": 12,
                "layers": {"terrain": {"enabled": True}},
            }
        )
    with pytest.raises(ValueError, match="unsupported layer fields"):
        resolve_design_spec(
            {
                "version": DESIGN_SPEC_VERSION,
                "layers": {
                    "terrain": {"enabled": True},
                    "roads": {"enabled": True, "boolean_operation": "union"},
                },
            }
        )


def test_fingerprint_is_deterministic_and_round_trips(tmp_path):
    first = resolve_design_spec(
        {
            "preset": "road_network",
            "layers": {
                "roads": {
                    "include_tags": {
                        "highway": ["secondary", "primary"],
                        "surface": "paved",
                    }
                }
            },
        }
    )
    second = resolve_design_spec(
        {
            "layers": {
                "roads": {
                    "include_tags": {
                        "surface": ["paved"],
                        "highway": ["primary", "secondary"],
                    }
                }
            },
            "preset": "road_network",
        }
    )

    assert first.fingerprint == second.fingerprint
    path = first.save(tmp_path / "design_spec.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["fingerprint"] == first.fingerprint
    assert resolve_design_spec(path) == first


def test_filter_features_applies_include_then_exclude():
    roads = gpd.GeoDataFrame(
        {
            "highway": ["primary", "secondary", "service"],
            "access": [None, "private", None],
        },
        geometry=[
            LineString([(0, 0), (1, 0)]),
            LineString([(0, 1), (1, 1)]),
            LineString([(0, 2), (1, 2)]),
        ],
        crs="EPSG:4326",
    )
    layer = LayerSpec.from_dict(
        {
            "enabled": True,
            "include_tags": {"highway": ["primary", "secondary"]},
            "exclude_tags": {"access": "private"},
        }
    )

    filtered = filter_features(roads, layer)

    assert filtered["highway"].tolist() == ["primary"]
    assert len(roads) == 3


def test_saved_fingerprint_detects_tampering(tmp_path):
    spec = resolve_design_spec(preset="water_focus")
    path = spec.save(tmp_path / "design_spec.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["layers"]["roads"]["enabled"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="fingerprint"):
        resolve_design_spec(path)


def test_terrain_only_pipeline_skips_osm_and_saves_spec(monkeypatch, tmp_path):
    from _TEXTURE_STYLE_OF_DEEPSEEK import pipeline

    monkeypatch.setattr(
        pipeline,
        "bbox_to_utm",
        lambda *args: {
            "area_km2": 1.0,
            "wgs84_bbox": (30.0, 120.0, 30.01, 120.01),
            "utm_crs": SimpleNamespace(utm_zone="51N"),
            "origin": (0.0, 0.0),
            "utm_bbox": (0.0, 0.0, 1000.0, 1000.0),
            "width_m": 1000.0,
            "height_m": 1000.0,
        },
    )
    monkeypatch.setattr(
        pipeline,
        "fetch_elevation_grid",
        lambda *args: np.zeros((4, 4), dtype=float),
    )

    def unexpected_fetch(*args, **kwargs):
        raise AssertionError("terrain_only must not fetch OSM sources")

    monkeypatch.setattr(pipeline, "fetch_buildings", unexpected_fetch)
    monkeypatch.setattr(pipeline, "fetch_roads", unexpected_fetch)
    monkeypatch.setattr(pipeline, "fetch_water", unexpected_fetch)
    monkeypatch.setattr(pipeline, "fetch_vegetation", unexpected_fetch)
    monkeypatch.setattr(
        pipeline,
        "build_deepseek_terrain",
        lambda *args, **kwargs: trimesh.creation.box(extents=(10, 10, 2)),
    )
    monkeypatch.setattr(
        pipeline,
        "preprocess_layers",
        lambda **kwargs: SimpleNamespace(
            BL=[], BO=[], roads_lines=[], WL=[], WO=[], VL=[], VO=[],
            block_base=[], summary=lambda: "empty",
        ),
    )

    output = Path(
        pipeline.run(
            30.0,
            120.0,
            30.01,
            120.01,
            output_dir=str(tmp_path),
            city_name="terrain-only",
            preset="terrain_only",
        )
    )

    assert output.is_file()
    saved_spec = output.parent / "design_spec.json"
    assert saved_spec.is_file()
    assert resolve_design_spec(saved_spec).preset == "terrain_only"


def test_generate_city_cli_accepts_design_preset(monkeypatch):
    import generate_city

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate_city.py",
            "--bbox", "30.20,120.10,30.22,120.12",
            "--pbf", "pbf_cache/zhejiang-latest.osm.pbf",
            "--city", "small-road-map",
            "--preset", "road_network",
        ],
    )

    args = generate_city.parse_args()

    assert args.design_preset == "road_network"
    assert args.bbox_tuple == (30.20, 120.10, 30.22, 120.12)


def test_landmarks_only_fetch_uses_narrow_source(monkeypatch, tmp_path):
    from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers import osm as osm_fetcher

    observed = {}

    class FakePipeline:
        def __init__(self, pbf_path, feature_type, bbox, config):
            observed["feature_type"] = feature_type

        def run(self, export_gpkg=None):
            return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

    monkeypatch.setattr(osm_fetcher, "_resolve_pbf_path", lambda: str(tmp_path / "x.pbf"))
    monkeypatch.setattr(osm_fetcher, "_check_tile_cache", lambda *args: None)
    monkeypatch.setattr(osm_fetcher, "OSMPipeline", FakePipeline)

    osm_fetcher.fetch_buildings(
        30.0, 120.0, 30.1, 120.1, landmarks_only=True
    )

    assert observed["feature_type"] == "building_landmarks"
