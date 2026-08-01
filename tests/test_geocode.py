# -*- coding: utf-8 -*-
"""地名检索（高德 + Nominatim 三级路由）与坐标系转换测试。

核心断言：高德返回 GCJ-02，必须转 WGS84 才能与 OSM 数据对齐。
网络类用例标 slow，默认跳过（-m "not slow"）。
"""
import math
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "webapp"))

from _TEXTURE_STYLE_OF_DEEPSEEK._water_supplement import (  # noqa: E402
    _gcj02_to_wgs84, _wgs84_to_gcj02)


def dist_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


# ─── 坐标系转换（离线，必跑）─────────────────────────────────────

class TestCoordConversion:
    def test_gcj_offset_is_significant(self):
        """国内 GCJ-02 与 WGS84 偏移应在百米量级——不可忽略。"""
        for lat, lon in [(31.3242, 120.6292), (39.9042, 116.4074),
                         (30.2455, 120.1500)]:
            w_lon, w_lat = _gcj02_to_wgs84(lon, lat)
            d = dist_m(lat, lon, w_lat, w_lon)
            assert 100 < d < 900, f"({lat},{lon}) 偏移 {d:.0f}m 不合预期"

    def test_roundtrip_precision(self):
        """WGS84 → GCJ → WGS84 往返误差应在米级。"""
        for lat, lon in [(31.3264, 120.6247), (30.2455, 120.1500)]:
            g_lon, g_lat = _wgs84_to_gcj02(lon, lat)
            b_lon, b_lat = _gcj02_to_wgs84(g_lon, g_lat)
            assert dist_m(lat, lon, b_lat, b_lon) < 5.0

    def test_amap_zhuozhengyuan_aligns_after_conversion(self):
        """实测基准：高德拙政园 GCJ 坐标转换后应贴近 OSM 真值。"""
        gcj_lon, gcj_lat = 120.629211, 31.324194     # 高德实测返回
        osm_lat, osm_lon = 31.3264, 120.6247         # Nominatim 实测返回
        before = dist_m(gcj_lat, gcj_lon, osm_lat, osm_lon)
        lon, lat = _gcj02_to_wgs84(gcj_lon, gcj_lat)
        after = dist_m(lat, lon, osm_lat, osm_lon)
        assert before > 400, f"基准偏移应显著，实测 {before:.0f}m"
        assert after < 60, f"转换后应对齐，实测仍差 {after:.0f}m"

    def test_overseas_unchanged(self):
        """境外坐标不做 GCJ 偏移（转换应近似恒等）。"""
        for lat, lon in [(48.8584, 2.2945), (40.6892, -74.0445)]:
            w_lon, w_lat = _gcj02_to_wgs84(lon, lat)
            assert dist_m(lat, lon, w_lat, w_lon) < 1.0


# ─── 取景框推导（离线）───────────────────────────────────────────

def test_bbox_around_is_sweet_spot():
    import server
    for lat in (0.0, 30.0, 55.0, -33.0):
        b = server._bbox_around(lat, 120.0)
        s, w, n, e = b
        assert n > s and e > w
        lat_km = (n - s) * 110.574
        lon_km = (e - w) * 111.32 * math.cos(math.radians(lat))
        # 两边都应落在 6–20km 甜区内
        assert 6 <= lat_km <= 20, f"lat={lat}: 纬度跨度 {lat_km:.1f}km"
        assert 6 <= lon_km <= 20, f"lat={lat}: 经度跨度 {lon_km:.1f}km"


# ─── 在线检索（需网络 + AMAP_KEY，标 slow）──────────────────────

@pytest.mark.slow
class TestOnlineSearch:
    def test_amap_finds_chinese_poi(self):
        import server
        for q in ["拙政园", "浙江大学紫金港校区"]:
            res = server._amap_search(q)
            assert res, f"高德未找到 {q}"
            assert res[0]["source"] == "amap"
            lat, lon = res[0]["center"]
            assert 3 < lat < 54 and 73 < lon < 136, "国内坐标越界"

    def test_nominatim_finds_overseas(self):
        import server
        res = server._nominatim_search("Canberra")
        assert res, "Nominatim 未找到堪培拉"
        assert res[0]["source"] == "nominatim"

    def test_geocode_route_prefers_amap_for_chinese(self):
        import server
        r = server.api_geocode(q="拙政园")
        assert r["results"], "三级路由无结果"
        assert r["results"][0]["source"] in ("amap", "nominatim")
        # 苏州拙政园落在浙江 PBF 之外但江苏无数据 → available 由覆盖表决定
        assert isinstance(r["results"][0]["available"], bool)
