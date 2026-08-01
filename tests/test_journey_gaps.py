# -*- coding: utf-8 -*-
"""缺口追问检测（journey.detect_gaps）测试。

设计原则的可执行断言：没缺口就不问、必答项不可跳过、
问句只含事实（无形容词/感叹）、问题数量有上限。
"""
import sys
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "webapp"))

import journey  # noqa: E402

T0 = datetime(2026, 5, 1, 9, 0, 0).timestamp()
H = 3600.0


def cl(lat, lon, t_start, t_end, name=None, count=2):
    return {"lat": lat, "lon": lon, "count": count,
            "t_start": t_start, "t_end": t_end,
            "dwell_minutes": round((t_end - t_start) / 60, 1) if t_end else 0.0,
            "name": name}


def ph(name, status):
    return {"name": name, "status": status, "lat": None, "lon": None,
            "time": None}


def analysis(clusters=(), photos=()):
    return {"clusters": list(clusters), "photos": list(photos)}


# ─── 无缺口时不问 ────────────────────────────────────────────────

def test_no_gaps_asks_nothing():
    a = analysis(
        clusters=[cl(30.26, 120.15, T0, T0 + H, "断桥残雪"),
                  cl(30.24, 120.13, T0 + 2 * H, T0 + 3 * H, "苏堤春晓")],
        photos=[{"name": "a.jpg", "status": "ok"}])
    assert journey.detect_gaps(a) == []


# ─── 四类缺口 ────────────────────────────────────────────────────

class TestGapTypes:
    def test_no_gps_all_is_mandatory(self):
        a = analysis(clusters=[], photos=[ph("a.jpg", "no_gps"),
                                         ph("b.jpg", "no_gps")])
        gaps = journey.detect_gaps(a)
        assert len(gaps) == 1
        assert gaps[0]["type"] == "no_gps_all"
        assert gaps[0]["optional"] is False, "无定位则无法生成，必答"
        assert gaps[0]["detail"]["photo_count"] == 2

    def test_no_photos_no_question(self):
        assert journey.detect_gaps(analysis()) == []

    def test_time_gap_detected_with_range_in_question(self):
        a = analysis(clusters=[
            cl(30.26, 120.15, T0, T0 + H, "断桥残雪"),
            cl(30.23, 120.13, T0 + 6 * H, T0 + 7 * H, "雷峰夕照"),
        ])
        gaps = [g for g in journey.detect_gaps(a) if g["type"] == "time_gap"]
        assert len(gaps) == 1
        g = gaps[0]
        assert g["optional"] is True
        assert "10:00" in g["question"] and "15:00" in g["question"]
        assert g["detail"]["after_cluster"] == 0
        assert g["detail"]["near"] == [30.26, 120.15], "应带上下文位置"

    def test_short_gap_ignored(self):
        a = analysis(clusters=[
            cl(30.26, 120.15, T0, T0 + H, "断桥残雪"),
            cl(30.24, 120.13, T0 + 2 * H, T0 + 3 * H, "苏堤春晓"),
        ])
        assert not [g for g in journey.detect_gaps(a)
                    if g["type"] == "time_gap"]

    def test_overnight_gap_ignored(self):
        """跨夜不算行程缺失（晚上回酒店不必解释）。"""
        day2 = T0 + 24 * H
        a = analysis(clusters=[
            cl(30.26, 120.15, T0, T0 + H, "断桥残雪"),
            cl(30.24, 120.13, day2, day2 + H, "苏堤春晓"),
        ])
        assert not [g for g in journey.detect_gaps(a)
                    if g["type"] == "time_gap"]

    def test_no_gps_partial(self):
        a = analysis(
            clusters=[cl(30.26, 120.15, T0, T0 + H, "断桥残雪")],
            photos=[{"name": "a.jpg", "status": "ok"},
                    ph("b.jpg", "no_gps"), ph("c.jpg", "no_gps")])
        gaps = [g for g in journey.detect_gaps(a)
                if g["type"] == "no_gps_partial"]
        assert len(gaps) == 1
        assert "2 张" in gaps[0]["question"]
        assert gaps[0]["optional"] is True
        assert gaps[0]["detail"]["photo_names"] == ["b.jpg", "c.jpg"]

    def test_unnamed_cluster_picks_longest_dwell(self):
        a = analysis(clusters=[
            cl(30.26, 120.15, T0, T0 + 0.2 * H),              # 未命名，12min
            cl(30.24, 120.13, T0 + H, T0 + 3 * H),            # 未命名，120min
            cl(30.23, 120.14, T0 + 4 * H, T0 + 5 * H, "雷峰夕照"),
        ])
        gaps = [g for g in journey.detect_gaps(a)
                if g["type"] == "unnamed_cluster"]
        assert len(gaps) == 1, "只问停留最久的那一个，不逐个盘问"
        assert gaps[0]["detail"]["cluster"] == 1
        assert "第 2 个" in gaps[0]["question"]


# ─── 设计约束 ────────────────────────────────────────────────────

class TestGapDiscipline:
    def test_question_count_capped(self):
        clusters = []
        for i in range(10):
            t = T0 + i * 8 * H
            clusters.append(cl(30.2 + i * 0.01, 120.1 + i * 0.01, t, t + H))
        a = analysis(clusters=clusters, photos=[ph("x.jpg", "no_gps")])
        gaps = journey.detect_gaps(a)
        assert len(gaps) <= journey.GAP_MAX_QUESTIONS

    def test_questions_are_factual_only(self):
        """文案红线：只问事实，不评价不抒情。"""
        banned = ["精彩", "美好", "难忘", "一定", "呢", "！", "故事", "回忆满满"]
        cases = [
            analysis(clusters=[], photos=[ph("a.jpg", "no_gps")]),
            analysis(clusters=[cl(30.26, 120.15, T0, T0 + H),
                               cl(30.23, 120.13, T0 + 6 * H, T0 + 7 * H)],
                     photos=[ph("b.jpg", "no_gps")]),
        ]
        for a in cases:
            for g in journey.detect_gaps(a):
                q = g["question"]
                assert q.endswith("？"), f"应是疑问句: {q}"
                for w in banned:
                    assert w not in q, f"问句含抒情/评价词 '{w}': {q}"

    def test_every_gap_has_required_shape(self):
        a = analysis(
            clusters=[cl(30.26, 120.15, T0, T0 + H),
                      cl(30.23, 120.13, T0 + 6 * H, T0 + 7 * H)],
            photos=[ph("b.jpg", "no_gps")])
        for g in journey.detect_gaps(a):
            assert set(g) == {"type", "question", "detail", "optional"}
            assert isinstance(g["optional"], bool)
            assert isinstance(g["detail"], dict)
