# -*- coding: utf-8 -*-
"""景点目录（data/landmarks/catalog.json）与搜索 API 测试。"""
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "webapp"))

CATALOG = _ROOT / "data" / "landmarks" / "catalog.json"


@pytest.fixture(scope="module")
def landmarks():
    data = json.loads(CATALOG.read_text(encoding="utf-8"))
    return data["landmarks"]


# ─── 目录数据质量 ───────────────────────────────────────────────────

class TestCatalog:
    def test_min_size(self, landmarks):
        assert len(landmarks) >= 50

    def test_schema_and_bbox_sanity(self, landmarks):
        ids = set()
        for lm in landmarks:
            for key in ("id", "name", "name_en", "country", "city",
                        "center", "bbox", "tags", "style", "curated"):
                assert key in lm, f"{lm.get('id', '?')} 缺字段 {key}"
            assert lm["id"] not in ids, f"重复 id: {lm['id']}"
            ids.add(lm["id"])
            s, w, n, e = lm["bbox"]
            lat, lon = lm["center"]
            assert n > s and e > w, f"{lm['id']} bbox 颠倒"
            assert s <= lat <= n and w <= lon <= e, \
                f"{lm['id']} center 不在 bbox 内"
            # 取景框尺寸合理：单边 1.5km ~ 45km
            assert 0.012 <= (n - s) <= 0.42, f"{lm['id']} 纬度跨度异常"

    def test_styles_valid(self, landmarks):
        valid = {"landscape", "skyline", "terrain", "minimal"}
        for lm in landmarks:
            assert lm["style"] in valid, f"{lm['id']} style 非法"

    def test_covered_regions_present(self, landmarks):
        """本地已有 PBF 的五区域各至少收录 3 个景点（演示可用性）。"""
        by_city = {}
        for lm in landmarks:
            by_city.setdefault(lm["city"], []).append(lm["id"])
        assert len(by_city.get("杭州", [])) >= 3
        assert len(by_city.get("上海", [])) >= 3
        assert len(by_city.get("重庆", [])) >= 3
        assert len(by_city.get("芝加哥", [])) >= 3
        assert len(by_city.get("巴黎", [])) >= 3


# ─── 搜索 API（直接调函数，不起服务）────────────────────────────────

@pytest.fixture(scope="module")
def api():
    import server
    return server


class TestLandmarkSearch:
    def test_search_by_chinese_name(self, api):
        r = api.api_landmarks(q="西湖")
        names = [x["name"] for x in r["landmarks"]]
        assert "西湖" in names

    def test_search_by_english(self, api):
        r = api.api_landmarks(q="eiffel")
        assert any(x["id"] == "eiffel" for x in r["landmarks"])

    def test_search_by_city(self, api):
        r = api.api_landmarks(q="巴黎")
        assert len(r["landmarks"]) >= 5

    def test_availability_flag(self, api, monkeypatch, tmp_path):
        # Availability is a property of the active worker's local dataset.
        # Build an explicit miniature PBF directory so the test is stable on
        # both sparse developer machines and the fully populated Windows node.
        monkeypatch.setattr(api, "PBF_DIR", tmp_path)
        westlake = next(
            item for item in api._load_landmarks() if item["id"] == "westlake")
        westlake_region = api._match_regions(westlake["bbox"])[0]
        (tmp_path / westlake_region["file"]).touch()

        r = api.api_landmarks(q="西湖")
        wl = next(x for x in r["landmarks"] if x["id"] == "westlake")
        assert wl["available"] is True
        r2 = api.api_landmarks(q="悉尼")
        so = next(x for x in r2["landmarks"] if x["id"] == "sydney_opera")
        assert so["available"] is False

    def test_empty_query_available_first(self, api):
        r = api.api_landmarks(q="")
        assert len(r["landmarks"]) == 12
        assert r["landmarks"][0]["available"] is True

    def test_no_match(self, api):
        r = api.api_landmarks(q="不存在的地方xyz")
        assert r["landmarks"] == []
