"""Landing-page samples must be real, consistent 15×15 km outputs."""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
WEBAPP = ROOT / "webapp"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(WEBAPP) not in sys.path:
    sys.path.insert(0, str(WEBAPP))

import server  # noqa: E402
from tools import generate_showcase_samples as showcase  # noqa: E402
from tools.generate_showcase_samples import bbox_around  # noqa: E402


def test_bbox_around_is_a_physical_15_km_square():
    south, west, north, east = bbox_around([48.8566, 2.3522], 15)
    north_south = (north - south) * 110.574
    east_west = ((east - west) * 111.320
                 * math.cos(math.radians((south + north) / 2)))

    assert north_south == pytest.approx(15, abs=0.02)
    assert east_west == pytest.approx(15, abs=0.02)


def test_size_override_uses_an_isolated_output_slug():
    city = {"key": "paris", "slug": "custom_327de4", "title": "Paris"}

    runtime = showcase.city_for_size(city, 25)

    assert runtime["slug"] == "showcase_paris_25km"
    assert runtime["sample_size_km"] == 25
    assert city["slug"] == "custom_327de4"


def test_southern_hemisphere_bbox_is_passed_as_one_option(monkeypatch):
    captured = {}

    class Result:
        returncode = 0

    def fake_run(command, **kwargs):
        captured["command"] = command
        return Result()

    monkeypatch.setattr(showcase.subprocess, "run", fake_run)
    city = {
        "key": "sydney",
        "slug": "showcase_sydney_25km",
        "title": "Sydney",
        "center": [-33.8688, 151.2093],
        "pbf": "new-south-wales-latest.osm.pbf",
        "prototype": "landscape",
    }

    assert showcase.generate(city, 25) == 0
    bbox_token = next(token for token in captured["command"]
                      if token.startswith("--bbox="))
    assert bbox_token.startswith("--bbox=-")
    assert "--bbox" not in captured["command"]


def test_suzhou_showcase_uses_jiangsu_extract():
    plan = json.loads((ROOT / "data" / "showcase_cities.json").read_text(
        encoding="utf-8"))
    suzhou = next(city for city in plan["cities"]
                  if city["key"] == "suzhou")

    assert suzhou["pbf"] == "jiangsu-latest.osm.pbf"


def test_input_gate_rejects_partial_pbf(monkeypatch, tmp_path):
    monkeypatch.setattr(showcase, "PBF_DIR", tmp_path)
    path = tmp_path / "city.osm.pbf"
    path.write_bytes(b"partial")

    problems = showcase.input_problems(
        [{"pbf": "city.osm.pbf"}], {"city.osm.pbf": 100})

    assert problems == ["city.osm.pbf: 7/100 bytes"]


def test_showcase_api_omits_outputs_with_the_wrong_physical_area(
        monkeypatch, tmp_path):
    plan = {
        "size_km": 15,
        "cities": [
            {"key": "valid", "slug": "valid", "title": "Valid",
             "caption": "Valid city", "hero_style": "baseline",
             "featured": True},
            {"key": "small", "slug": "small", "title": "Small",
             "caption": "Small city", "hero_style": "baseline",
             "featured": True},
            {"key": "planned", "slug": "valid", "title": "Planned",
             "caption": "Not featured", "hero_style": "baseline"},
        ],
    }
    plan_path = tmp_path / "showcase.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    gallery_dir = tmp_path / "gallery"
    for slug, area in (("valid", 225), ("small", 25)):
        target = gallery_dir / slug
        target.mkdir(parents=True)
        styles = {}
        for index, style in enumerate(showcase.REQUIRED_STYLES):
            filename = f"{style}_topdown.png"
            image = Image.new("L", (96, 96), 240)
            draw = ImageDraw.Draw(image)
            draw.rectangle((0, 0, 30 + index, 95), fill=20 + index)
            image.save(target / filename)
            styles[style] = {"renders": {"topdown": filename}}
        (target / "gallery_metadata.json").write_text(json.dumps({
            "profile": {"area_km2": area, "building_density": 1},
            "styles": styles,
        }), encoding="utf-8")

    monkeypatch.setattr(server, "SHOWCASE_PLAN_PATH", plan_path)
    monkeypatch.setattr(server, "GALLERY_DIR", gallery_dir)
    server._SHOWCASE_VERIFY_CACHE.clear()

    result = server.api_showcase()

    assert [sample["title"] for sample in result["samples"]] == ["Valid city"]
    assert result["samples"][0]["size_km"] == 15


def test_showcase_api_omits_false_success_with_zero_features(
        monkeypatch, tmp_path):
    plan_path = tmp_path / "showcase.json"
    plan_path.write_text(json.dumps({
        "size_km": 15,
        "cities": [{"key": "blank", "slug": "blank", "title": "Blank",
                    "hero_style": "baseline", "featured": True}],
    }), encoding="utf-8")
    gallery_dir = tmp_path / "gallery"
    target = gallery_dir / "blank"
    target.mkdir(parents=True)
    (target / "baseline_topdown.png").write_bytes(b"image")
    (target / "gallery_metadata.json").write_text(json.dumps({
        "profile": {"area_km2": 225, "building_density": 0,
                    "road_density_km_per_km2": 0, "water_ratio": 0},
        "styles": {"baseline": {"renders": {
            "topdown": "baseline_topdown.png"}}},
    }), encoding="utf-8")
    monkeypatch.setattr(server, "SHOWCASE_PLAN_PATH", plan_path)
    monkeypatch.setattr(server, "GALLERY_DIR", gallery_dir)

    assert server.api_showcase()["samples"] == []


def test_showcase_static_asset_uses_declared_cache_version(
        monkeypatch, tmp_path):
    plan_path = tmp_path / "showcase.json"
    plan_path.write_text(json.dumps({
        "size_km": 15,
        "cities": [{
            "key": "sample", "title": "Sample", "featured": True,
            "static_asset": "sample.jpg", "asset_version": "water-fix-v2",
        }],
    }), encoding="utf-8")
    static_dir = tmp_path / "static"
    assets_dir = static_dir / "assets"
    assets_dir.mkdir(parents=True)
    (assets_dir / "sample.jpg").write_bytes(b"image")
    monkeypatch.setattr(server, "SHOWCASE_PLAN_PATH", plan_path)
    monkeypatch.setattr(server, "STATIC_DIR", static_dir)

    result = server.api_showcase()

    assert result["samples"][0]["url"] == (
        "/assets/sample.jpg?release=water-fix-v2")


def test_batch_validator_rejects_zero_feature_false_success(
        monkeypatch, tmp_path):
    gallery_dir = tmp_path / "gallery"
    target = gallery_dir / "blank"
    target.mkdir(parents=True)
    (target / "gallery_metadata.json").write_text(json.dumps({
        "profile": {"area_km2": 225, "building_density": 0,
                    "road_density_km_per_km2": 0, "water_ratio": 0},
    }), encoding="utf-8")
    monkeypatch.setattr(showcase, "GALLERY_DIR", gallery_dir)

    valid, detail = showcase.validate_gallery({"slug": "blank"}, 15)

    assert not valid
    assert "zero buildings, roads, and water" in detail


def _write_valid_style_images(target: Path) -> dict:
    styles = {}
    for index, style in enumerate(showcase.REQUIRED_STYLES):
        filename = f"{style}_topdown.png"
        image = Image.new("L", (1600, 1600), 235)
        draw = ImageDraw.Draw(image)
        draw.rectangle((10, 10, 500 + index * 30, 1500), fill=30 + index)
        image.save(target / filename)
        styles[style] = {"renders": {"topdown": filename}}
    return styles


def test_batch_validator_rejects_urban_gallery_without_roads(
        monkeypatch, tmp_path):
    gallery_dir = tmp_path / "gallery"
    target = gallery_dir / "cairo"
    target.mkdir(parents=True)
    styles = _write_valid_style_images(target)
    (target / "gallery_metadata.json").write_text(json.dumps({
        "scene_type": "urban",
        "profile": {
            "area_km2": 625,
            "building_density": 83,
            "road_density_km_per_km2": 0,
            "water_ratio": 0.002,
            "osm_quality": "poor",
        },
        "styles": styles,
    }), encoding="utf-8")
    monkeypatch.setattr(showcase, "GALLERY_DIR", gallery_dir)

    valid, detail = showcase.validate_gallery({"slug": "cairo"}, 25)

    assert not valid
    assert "urban road extraction" in detail


def test_batch_validator_rejects_implausible_full_frame_water(
        monkeypatch, tmp_path):
    gallery_dir = tmp_path / "gallery"
    target = gallery_dir / "mexico_city"
    target.mkdir(parents=True)
    styles = _write_valid_style_images(target)
    (target / "gallery_metadata.json").write_text(json.dumps({
        "scene_type": "water_landscape",
        "profile": {
            "area_km2": 625,
            "building_density": 198,
            "road_density_km_per_km2": 8,
            "water_ratio": 0.999,
            "osm_quality": "fair",
        },
        "styles": styles,
    }), encoding="utf-8")
    monkeypatch.setattr(showcase, "GALLERY_DIR", gallery_dir)

    valid, detail = showcase.validate_gallery(
        {"slug": "mexico_city"}, 25)

    assert not valid
    assert "implausible water coverage" in detail
