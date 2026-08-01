# -*- coding: utf-8 -*-
"""地名表构建（tools/build_gazetteer.py）与簇命名（journey.name_clusters）测试。"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "webapp"))
sys.path.insert(0, str(_ROOT / "tools"))

import journey  # noqa: E402
from build_gazetteer import poi_prio, reduce_features  # noqa: E402


def feat(name, lon, lat, props=None, geom=None):
    p = {"name": name}
    p.update(props or {})
    return {
        "properties": p,
        "geometry": geom or {"type": "Point", "coordinates": [lon, lat]},
    }


# ─── reduce_features ────────────────────────────────────────────────

class TestReduceFeatures:
    def test_point_and_polygon(self):
        square = {"type": "Polygon", "coordinates": [[
            [120.0, 30.0], [120.02, 30.0], [120.02, 30.02],
            [120.0, 30.02], [120.0, 30.0]]]}
        out = reduce_features([
            feat("断桥", 120.151, 30.259, {"tourism": "attraction"}),
            feat("某公园", None, None, {"leisure": "park"}, geom=square),
        ])
        assert len(out) == 2
        bridge = next(p for p in out if p["name"] == "断桥")
        assert bridge["prio"] == 1
        park = next(p for p in out if p["name"] == "某公园")
        assert park["lat"] == 30.01 and park["lon"] == 120.01  # 多边形质心
        assert park["prio"] == 6

    def test_dedup_keeps_higher_priority(self):
        out = reduce_features([
            feat("灵隐寺", 120.10, 30.24, {"leisure": "park"}),
            feat("灵隐寺", 120.101, 30.241, {"tourism": "attraction"}),
        ])
        assert len(out) == 1
        assert out[0]["prio"] == 1
        assert out[0]["lon"] == 120.101   # 高优先级条目的坐标

    def test_skip_unnamed_and_overlong(self):
        out = reduce_features([
            feat("", 120.1, 30.2, {"tourism": "attraction"}),
            feat("x" * 31, 120.1, 30.2, {"tourism": "attraction"}),
            {"properties": None, "geometry": None},
        ])
        assert out == []


def test_poi_prio_order():
    assert poi_prio({"tourism": "museum"}) == 1
    assert poi_prio({"historic": "memorial"}) == 2
    assert poi_prio({"leisure": "park"}) == 6
    assert poi_prio({"tourism": "museum", "leisure": "park"}) == 1  # 取最高
    assert poi_prio({}) == 9


# ─── name_clusters ──────────────────────────────────────────────────

def cluster(lat, lon):
    return {"lat": lat, "lon": lon, "count": 1,
            "t_start": None, "t_end": None, "dwell_minutes": 0.0}


class TestNameClusters:
    POIS = [
        {"name": "断桥", "lat": 30.2591, "lon": 120.1502, "prio": 1},
        {"name": "白堤", "lat": 30.2589, "lon": 120.1497, "prio": 6},
        {"name": "雷峰塔", "lat": 30.2312, "lon": 120.1489, "prio": 1},
    ]

    def test_names_within_radius(self):
        cs = [cluster(30.2590, 120.1500), cluster(30.2311, 120.1488)]
        journey.name_clusters(cs, self.POIS)
        assert cs[0]["name"] == "断桥"       # 白堤更近但 prio 低，断桥胜出
        assert cs[1]["name"] == "雷峰塔"

    def test_none_beyond_radius(self):
        cs = [cluster(30.30, 120.20)]        # 距最近 POI 数公里
        journey.name_clusters(cs, self.POIS)
        assert cs[0]["name"] is None          # 宁缺毋滥

    def test_empty_gazetteer(self):
        cs = [cluster(30.2590, 120.1500)]
        journey.name_clusters(cs, [])
        assert cs[0]["name"] is None

    def test_priority_beats_distance(self):
        # 同一位置：近处 prio 6 vs 稍远 prio 1 → 选 prio 1
        pois = [
            {"name": "近的公园", "lat": 30.2500, "lon": 120.1400, "prio": 6},
            {"name": "远些的景点", "lat": 30.2515, "lon": 120.1400, "prio": 1},
        ]
        cs = [cluster(30.2501, 120.1400)]
        journey.name_clusters(cs, pois)
        assert cs[0]["name"] == "远些的景点"
