# -*- coding: utf-8 -*-
"""webapp/journey.py 单元测试：EXIF 提取、异常点、聚类、bbox、分章。"""
import io
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webapp"))

import journey  # noqa: E402


# ─── 合成照片工具 ────────────────────────────────────────────────────

def make_photo(lat=None, lon=None, dt=None):
    """内存合成 JPEG：可选 GPS 与 DateTimeOriginal。返回 BytesIO。"""
    from PIL import Image
    img = Image.new("RGB", (32, 32), (120, 130, 140))
    exif = Image.Exif()
    if lat is not None:
        alat, alon = abs(lat), abs(lon)
        exif[0x8825] = {
            1: "N" if lat >= 0 else "S",
            2: (float(int(alat)), float(int(alat * 60) % 60),
                round(alat * 3600 % 60, 4)),
            3: "E" if lon >= 0 else "W",
            4: (float(int(alon)), float(int(alon * 60) % 60),
                round(alon * 3600 % 60, 4)),
        }
    if dt is not None:
        exif[0x8769] = {0x9003: dt.strftime("%Y:%m:%d %H:%M:%S")}
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    buf.seek(0)
    return buf


def pt(lat, lon, t):
    return {"name": "x.jpg", "lat": lat, "lon": lon, "time": t}


T0 = datetime(2026, 5, 1, 10, 0, 0).timestamp()


# ─── EXIF 提取 ──────────────────────────────────────────────────────

class TestExtractPhotoMeta:
    def test_gps_and_time(self):
        dt = datetime(2026, 5, 1, 14, 30, 0)
        meta = journey.extract_photo_meta(
            make_photo(30.2482, 120.1410, dt))
        assert meta["lat"] == pytest.approx(30.2482, abs=1e-4)
        assert meta["lon"] == pytest.approx(120.1410, abs=1e-4)
        assert meta["time"] == pytest.approx(dt.timestamp(), abs=1.0)

    def test_south_west_hemisphere(self):
        meta = journey.extract_photo_meta(make_photo(-33.86, -151.21))
        assert meta["lat"] == pytest.approx(-33.86, abs=1e-4)
        assert meta["lon"] == pytest.approx(-151.21, abs=1e-4)

    def test_no_exif(self):
        meta = journey.extract_photo_meta(make_photo())
        assert meta == {"lat": None, "lon": None, "time": None}

    def test_time_without_gps(self):
        dt = datetime(2026, 5, 2, 8, 0, 0)
        meta = journey.extract_photo_meta(make_photo(dt=dt))
        assert meta["lat"] is None
        assert meta["time"] == pytest.approx(dt.timestamp(), abs=1.0)


# ─── 距离 / 异常点 ──────────────────────────────────────────────────

def test_haversine_known_distance():
    # 纬度 1 度 ≈ 110.57km
    d = journey.haversine_m(30.0, 120.0, 31.0, 120.0)
    assert d == pytest.approx(110_574, rel=0.01)


class TestMarkSuspects:
    def test_teleport_flagged(self):
        # 10 分钟内跳 800km → 隐含速度远超 150km/h
        pts = [pt(30.25, 120.14, T0),
               pt(30.251, 120.141, T0 + 300),
               pt(39.90, 116.40, T0 + 600),    # 北京，瞬移
               pt(30.252, 120.142, T0 + 900)]
        journey.mark_suspects(pts)
        assert [p["suspect"] for p in pts] == [False, False, True, False]

    def test_slow_walk_ok(self):
        # 1 小时走 2km，正常
        pts = [pt(30.25, 120.14, T0), pt(30.268, 120.14, T0 + 3600)]
        journey.mark_suspects(pts)
        assert not any(p["suspect"] for p in pts)


# ─── 停留聚类 ───────────────────────────────────────────────────────

class TestClusterStops:
    def test_two_stops(self):
        # 断桥附近 3 张（30 分钟）+ 苏堤附近 2 张
        pts = [pt(30.2590, 120.1500, T0),
               pt(30.2592, 120.1502, T0 + 900),
               pt(30.2594, 120.1499, T0 + 1800),
               pt(30.2410, 120.1330, T0 + 3600),
               pt(30.2412, 120.1332, T0 + 4500)]
        cs = journey.cluster_stops(pts)
        assert len(cs) == 2
        assert cs[0]["count"] == 3
        assert cs[0]["dwell_minutes"] == pytest.approx(30.0)
        assert cs[1]["count"] == 2
        assert cs[0]["lat"] == pytest.approx(30.2592, abs=1e-3)

    def test_empty(self):
        assert journey.cluster_stops([]) == []


# ─── bbox / 分章 ────────────────────────────────────────────────────

class TestSuggestBbox:
    def test_covers_all_clusters(self):
        cs = journey.cluster_stops([
            pt(30.24, 120.13, T0), pt(30.26, 120.16, T0 + 3600)])
        b = journey.suggest_bbox(cs)
        assert b[0] < 30.24 and b[2] > 30.26
        assert b[1] < 120.13 and b[3] > 120.16

    def test_min_span_for_single_point(self):
        cs = journey.cluster_stops([pt(30.25, 120.14, T0)])
        b = journey.suggest_bbox(cs)
        assert (b[2] - b[0]) >= journey.BBOX_MIN_SPAN_DEG * 0.99
        assert (b[3] - b[1]) >= journey.BBOX_MIN_SPAN_DEG * 0.99

    def test_none_for_empty(self):
        assert journey.suggest_bbox([]) is None


def test_chapters_by_day():
    day2 = T0 + 86400
    cs = journey.cluster_stops([
        pt(30.25, 120.14, T0), pt(30.26, 120.16, day2)])
    ch = journey.chapters_by_day(cs)
    assert len(ch) == 2
    assert ch[0]["date"] == "2026-05-01"
    assert ch[1]["date"] == "2026-05-02"


# ─── 集成 ───────────────────────────────────────────────────────────

def test_analyze_journey_end_to_end():
    metas = [
        {"name": "a.jpg", "lat": 30.2590, "lon": 120.1500, "time": T0},
        {"name": "b.jpg", "lat": 30.2592, "lon": 120.1501, "time": T0 + 600},
        {"name": "c.jpg", "lat": None, "lon": None, "time": None},      # 截图
        {"name": "d.jpg", "lat": 39.90, "lon": 116.40, "time": T0 + 900},  # 瞬移
        {"name": "e.jpg", "lat": 30.2410, "lon": 120.1330, "time": T0 + 7200},
    ]
    r = journey.analyze_journey(metas)
    statuses = {p["name"]: p["status"] for p in r["photos"]}
    assert statuses == {"a.jpg": "ok", "b.jpg": "ok", "c.jpg": "no_gps",
                        "d.jpg": "suspect", "e.jpg": "ok"}
    assert len(r["clusters"]) == 2            # 瞬移点未成簇
    assert r["suggested_bbox"] is not None
    assert r["suggest_split"] is False
    # bbox 不应被北京的瞬移点撑大
    assert r["suggested_bbox"][2] < 31.0


def test_analyze_journey_all_no_gps():
    r = journey.analyze_journey(
        [{"name": "a.jpg", "lat": None, "lon": None, "time": None}])
    assert r["clusters"] == []
    assert r["suggested_bbox"] is None
