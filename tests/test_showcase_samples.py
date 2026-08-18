"""Landing-page samples must be real, consistent 15×15 km outputs."""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
WEBAPP = ROOT / "webapp"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(WEBAPP) not in sys.path:
    sys.path.insert(0, str(WEBAPP))

import server  # noqa: E402
from tools.generate_showcase_samples import bbox_around  # noqa: E402


def test_bbox_around_is_a_physical_15_km_square():
    south, west, north, east = bbox_around([48.8566, 2.3522], 15)
    north_south = (north - south) * 110.574
    east_west = ((east - west) * 111.320
                 * math.cos(math.radians((south + north) / 2)))

    assert north_south == pytest.approx(15, abs=0.02)
    assert east_west == pytest.approx(15, abs=0.02)


def test_showcase_api_omits_outputs_with_the_wrong_physical_area(
        monkeypatch, tmp_path):
    plan = {
        "size_km": 15,
        "cities": [
            {"key": "valid", "slug": "valid", "title": "Valid",
             "caption": "Valid city", "hero_style": "baseline"},
            {"key": "small", "slug": "small", "title": "Small",
             "caption": "Small city", "hero_style": "baseline"},
        ],
    }
    plan_path = tmp_path / "showcase.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    gallery_dir = tmp_path / "gallery"
    for slug, area in (("valid", 225), ("small", 25)):
        target = gallery_dir / slug
        target.mkdir(parents=True)
        (target / "baseline_topdown.png").write_bytes(b"image")
        (target / "gallery_metadata.json").write_text(json.dumps({
            "profile": {"area_km2": area},
            "styles": {"baseline": {"renders": {
                "topdown": "baseline_topdown.png"}}},
        }), encoding="utf-8")

    monkeypatch.setattr(server, "SHOWCASE_PLAN_PATH", plan_path)
    monkeypatch.setattr(server, "GALLERY_DIR", gallery_dir)

    result = server.api_showcase()

    assert [sample["title"] for sample in result["samples"]] == ["Valid city"]
    assert result["samples"][0]["size_km"] == 15
