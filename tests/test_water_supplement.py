# -*- coding: utf-8 -*-
"""水体补面（supplement_wl_coverage）自适应 buffer 回归测试。

历史 bug：城市河道（waterway=river 中心线）的自适应 buffer 以最近 WL
多边形为宽度参考，最近的是西湖（估宽 ~850m）→ 半宽顶到 450m 上限，
预览图出现几百米宽蓝带。
"""
import sys
import json
from pathlib import Path

from shapely.geometry import LineString, Point, Polygon, mapping

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from _TEXTURE_STYLE_OF_DEEPSEEK._water_supplement import (  # noqa: E402
    _adaptive_buffer_segments, _fetch_amap_water)
from _TEXTURE_STYLE_OF_DEEPSEEK import _water_supplement  # noqa: E402


def _mean_width(p):
    return 2 * p.area / p.length


class TestAdaptiveBuffer:
    def test_lake_does_not_inflate_canal(self):
        """远处大湖不得作为城市河道宽度参考。"""
        lake = Point(0, 0).buffer(1500)          # ~3km 宽湖
        canal = LineString([(3000, 0), (3000, 4000)])
        polys = _adaptive_buffer_segments(
            [(canal, "river")], lake, [lake])
        assert polys, "河道应被 buffer 成面"
        for p in polys:
            w = _mean_width(p)
            assert w < 200, f"河道宽 {w:.0f}m，被大湖参考污染"

    def test_parallel_canal_near_lake_shore(self):
        """贴近湖岸但非同一水体的河道也不得继承湖宽。"""
        lake = Point(0, 0).buffer(1500)
        canal = LineString([(1700, -2000), (1700, 2000)])  # 距岸 ~200m
        polys = _adaptive_buffer_segments(
            [(canal, "river")], lake, [lake])
        for p in polys:
            assert _mean_width(p) < 200, "湖宽泄漏到平行河道"

    def test_riverband_continuation_inherits_width(self):
        """中心线作为河带多边形延伸时继承其宽度。"""
        band = LineString([(0, 0), (0, 5000)]).buffer(100)  # 200m 宽河带
        cont = LineString([(0, 5000), (0, 6000)])          # 紧贴延伸
        polys = _adaptive_buffer_segments(
            [(cont, "river")], band, [band])
        assert polys
        w = max(_mean_width(p) for p in polys)
        assert 100 < w < 300, f"延伸段应继承 ~200m 河宽，实际 {w:.0f}m"


class TestAmapOfflineMode:
    BBOX = (30.13, 120.01, 30.36, 120.29)

    def test_cache_miss_does_not_touch_network_when_disabled(
            self, monkeypatch, tmp_path):
        monkeypatch.setenv("AMAP_WATER_AUTO_FETCH", "0")
        monkeypatch.setattr(
            _water_supplement, "_cache_path",
            lambda _bbox, _zoom: tmp_path / "missing.geojson")

        def fail_if_called(*_args, **_kwargs):
            raise AssertionError("offline mode attempted a live tile request")

        monkeypatch.setattr(
            _water_supplement, "_fetch_nolabel_tiles", fail_if_called)

        assert _fetch_amap_water(self.BBOX) == []

    def test_existing_cache_is_used_before_offline_gate(
            self, monkeypatch, tmp_path):
        cache = tmp_path / "cached.geojson"
        polygon = Polygon([(120.1, 30.2), (120.2, 30.2),
                           (120.2, 30.3), (120.1, 30.3)])
        cache.write_text(json.dumps({
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "geometry": mapping(polygon),
                "properties": {},
            }],
        }), encoding="utf-8")
        monkeypatch.setenv("AMAP_WATER_AUTO_FETCH", "0")
        monkeypatch.setattr(
            _water_supplement, "_cache_path",
            lambda _bbox, _zoom: cache)

        result = _fetch_amap_water(self.BBOX)

        assert len(result) == 1
        assert result[0].equals(polygon)
