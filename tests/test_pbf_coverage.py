# -*- coding: utf-8 -*-
"""PBF 覆盖表（80 区域）与三态数据可用性判定测试。"""
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "webapp"))

COVERAGE = _ROOT / "data" / "pbf_coverage.json"


@pytest.fixture(scope="module")
def cov():
    return json.loads(COVERAGE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def api():
    import server
    return server


# ─── 覆盖表数据质量 ─────────────────────────────────────────────────

class TestCoverageTable:
    def test_size_and_shape(self, cov):
        assert cov["version"] == 1
        assert cov["remote_host"] and cov["remote_dir"]
        regions = cov["regions"]
        assert len(regions) >= 80
        for name, info in regions.items():
            assert info["file"].endswith(".osm.pbf"), name
            s, w, n, e = info["bbox"]
            assert n > s and e > w, f"{name} bbox 颠倒"
            assert -90 <= s < n <= 90, f"{name} 纬度越界"
            assert -180 <= w < e <= 180, f"{name} 经度越界"

    def test_known_regions_present(self, cov):
        for r in ("zhejiang", "beijing", "great-britain", "california",
                  "kanto", "new-south-wales", "egypt"):
            assert r in cov["regions"], f"缺少区域 {r}"

    def test_bbox_matches_reality(self, cov):
        """抽查：区域 bbox 应包含其代表城市坐标。"""
        cases = [("zhejiang", 30.2455, 120.15),        # 西湖
                 ("beijing", 39.9163, 116.3972),       # 故宫
                 ("great-britain", 51.5007, -0.1246),  # 大本钟
                 ("kanto", 35.7148, 139.7967),         # 浅草寺
                 ("egypt", 29.9773, 31.1325)]          # 金字塔
        for region, lat, lon in cases:
            s, w, n, e = cov["regions"][region]["bbox"]
            assert s <= lat <= n and w <= lon <= e, f"{region} 不含 ({lat},{lon})"


# ─── 三态判定 ───────────────────────────────────────────────────────

class TestPbfStatus:
    def test_local_region(self, api):
        """浙江 PBF 在本地 → local，可直接生成。"""
        st = api._pbf_status([30.21, 120.105, 30.282, 120.1885])
        assert st["state"] == "local"
        assert st["pbf"] == "pbf_cache/zhejiang-latest.osm.pbf"
        assert st["fetch"] is None
        assert api._find_pbf([30.21, 120.105, 30.282, 120.1885]) is not None

    def test_fetchable_region(self, api, cov):
        """远端有、本地无的区域 → fetchable（动态选区域，不依赖已拉取状态）。"""
        remote = api.api_regions()["remote"]
        assert remote, "全部区域都已拉取，无法测 fetchable"
        name = remote[0]["region"]
        s, w, n, e = cov["regions"][name]["bbox"]
        # 取区域中心附近一小块（确保被该区域完整包含）
        clat, clon = (s + n) / 2, (w + e) / 2
        bbox = [clat - 0.02, clon - 0.02, clat + 0.02, clon + 0.02]
        st = api._pbf_status(bbox)
        assert st["state"] == "fetchable", f"{name} 应为待拉取"
        assert st["pbf"] is None
        assert st["fetch"]
        # 向后兼容函数在未就绪时仍返回 None（不会误用不存在的文件）
        assert api._find_pbf(bbox) is None

    def test_none_region(self, api):
        """南极：任何区域都不覆盖 → none。"""
        st = api._pbf_status([-80.0, 10.0, -79.0, 11.0])
        assert st["state"] == "none"
        assert st["pbf"] is None and st["fetch"] is None

    def test_prefers_smallest_matching_region(self, api):
        """北京故宫同时被 beijing 命中；应选面积最小者（处理最快）。"""
        matches = api._match_regions([39.894, 116.368, 39.939, 116.427])
        assert matches, "应有匹配区域"
        assert matches[0]["region"] == "beijing"

    def test_state_fields_flat(self, api):
        f = api._state_fields([30.21, 120.105, 30.282, 120.1885])
        assert set(f) == {"available", "data_state", "region", "fetch"}
        assert f["available"] is True and f["data_state"] == "local"


# ─── API ────────────────────────────────────────────────────────────

class TestRegionsApi:
    def test_regions_overview(self, api):
        r = api.api_regions()
        assert r["total"] >= 80
        assert len(r["local"]) + len(r["remote"]) == r["total"]
        assert r["remote_host"]
        names = {x["region"] for x in r["local"]}
        assert "zhejiang" in names, "浙江应在本地列表"
        for x in r["local"]:
            assert x["size_mb"] > 0

    def test_landmarks_carry_state(self, api):
        r = api.api_landmarks(q="")
        assert r["landmarks"]
        for lm in r["landmarks"]:
            assert lm["data_state"] in ("local", "fetchable", "none")
        # 排序：本地就绪的排在最前
        assert r["landmarks"][0]["data_state"] == "local"

    def test_catalog_coverage_after_table(self, api):
        """接入 80 区域后，目录里应几乎不再有 none。"""
        cat = json.loads(
            (_ROOT / "data" / "landmarks" / "catalog.json")
            .read_text(encoding="utf-8"))
        states = [api._pbf_status(lm["bbox"])["state"]
                  for lm in cat["landmarks"]]
        none_cnt = states.count("none")
        assert none_cnt <= 2, f"仍有 {none_cnt} 个景点无数据覆盖"
        assert states.count("local") + states.count("fetchable") >= 50

    def test_fetch_unknown_region_404(self, api):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as ei:
            api.api_fetch_pbf(api.FetchRequest(region="atlantis"))
        assert ei.value.status_code == 404

    def test_fetch_existing_region_is_noop(self, api):
        r = api.api_fetch_pbf(api.FetchRequest(region="zhejiang"))
        assert r["state"] == "local"
        assert r["job_id"] is None
