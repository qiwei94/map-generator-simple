"""PNG rendering must reuse the geometry-safe pipeline subtraction path."""

from shapely.geometry import box


def test_png_subtraction_delegates_to_shared_pipeline(monkeypatch):
    from _TEXTURE_STYLE_OF_DEEPSEEK import _layer_preprocess
    from tools import tune_buildings_v2

    source = [box(0, 0, 10, 10)]
    cutter = box(5, 0, 15, 10)
    sentinel = [box(0, 0, 1, 1)]
    calls = []

    def fake_subtract(polys, minus_geom):
        calls.append((polys, minus_geom))
        return sentinel

    monkeypatch.setattr(_layer_preprocess, "_subtract", fake_subtract)

    assert tune_buildings_v2._subtract(source, cutter) is sentinel
    assert calls == [(source, cutter)]


def test_png_subtraction_preserves_expected_difference_area():
    from tools.tune_buildings_v2 import _subtract

    result = _subtract([box(0, 0, 10, 10)], box(5, 0, 15, 10))

    assert sum(poly.area for poly in result) == 50.0
