import geopandas as gpd
from shapely.geometry import LineString

from generate_city_legacy import (
    _amap_salience_cache_fingerprint,
    _clip_gdf_to_bbox,
    _load_amap_salience_guide,
    _load_snap_amap_salience_guide,
    parse_args,
)


def test_amap_salience_cli_is_opt_in():
    base = [
        "--bbox", "39.8,116.2,40.0,116.5",
        "--pbf", "beijing.osm.pbf",
        "--city", "beijing",
    ]

    assert parse_args(base).amap_salience == "off"
    assert parse_args([*base, "--amap-salience", "cache"]).amap_salience == (
        "cache")
    review = parse_args([
        *base, "--draft", "--review-png", "--review-only"])
    assert review.review_only is True


def test_disabled_salience_never_loads_or_fetches_reference():
    guide, evidence = _load_amap_salience_guide(
        "off",
        (39.8, 116.2, 40.0, 116.5),
        (0, 0, 10000, 10000),
    )

    assert guide is None
    assert evidence["status"] == "disabled"
    assert _amap_salience_cache_fingerprint("off", evidence)["mode"] == "off"


def test_snap_salience_reuses_exact_cache_with_paired_bounds(monkeypatch):
    calls = []
    exact_guide = object()

    def fake_loader(mode, bbox_wgs84, bbox_local):
        calls.append((mode, bbox_wgs84, bbox_local))
        if len(calls) == 1:
            return None, {"status": "unavailable", "reason": "snap miss"}
        return exact_guide, {
            "status": "ready",
            "bbox_wgs84": list(bbox_wgs84),
        }

    monkeypatch.setattr(
        "generate_city_legacy._load_amap_salience_guide", fake_loader)
    guide, evidence = _load_snap_amap_salience_guide(
        "cache",
        (39.75, 116.25, 40.05, 116.60),
        (0, 0, 30000, 30000),
        (39.79, 116.26, 40.02, 116.55),
        (1000, 1200, 26000, 26200),
    )

    assert guide is exact_guide
    assert calls[1] == (
        "cache",
        (39.79, 116.26, 40.02, 116.55),
        (1000, 1200, 26000, 26200),
    )
    assert evidence["preprocess_frame"] == "exact_within_snap"
    assert evidence["snap_cache_fallback"] is True


def test_exact_frame_clips_reusable_snap_sources_before_ranking():
    source = gpd.GeoDataFrame(
        {"name": ["crossing", "outside"]},
        geometry=[
            LineString([(-5, 5), (15, 5)]),
            LineString([(20, 20), (30, 30)]),
        ],
        crs="EPSG:32651",
    )

    clipped = _clip_gdf_to_bbox(source, (0, 0, 10, 10))

    assert clipped["name"].tolist() == ["crossing"]
    assert tuple(round(v, 6) for v in clipped.geometry.iloc[0].bounds) == (
        0.0, 5.0, 10.0, 5.0)
