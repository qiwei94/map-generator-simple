import geopandas as gpd
from shapely.geometry import box

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers import (
    wikidata_height_enrichment as wikidata,
)


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Session:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def get(self, *_args, **_kwargs):
        self.calls += 1
        return _Response(self.payload)


def _entity(height_amount=None, unit="Q11573", label="地标"):
    claims = {}
    if height_amount is not None:
        claims["P2048"] = [{
            "rank": "normal",
            "mainsnak": {
                "datavalue": {
                    "value": {
                        "amount": str(height_amount),
                        "unit": f"http://www.wikidata.org/entity/{unit}",
                    }
                }
            },
        }]
    return {"claims": claims, "labels": {"zh": {"value": label}}}


def test_wikidata_height_and_negative_result_are_both_cached(tmp_path):
    buildings = gpd.GeoDataFrame(
        {
            "wikidata": ["Q1", "Q2"],
            "geometry": [box(0, 0, 1, 1), box(2, 2, 3, 3)],
        },
        geometry="geometry", crs="EPSG:4326",
    )
    first = _Session({"entities": {
        "Q1": _entity(324, label="测试塔"),
        "Q2": _entity(),
    }})

    heights, labels = wikidata.load_wikidata_heights(
        buildings, cache_dir=str(tmp_path), auto_fetch=True, session=first)
    assert first.calls == 1
    assert heights.iloc[0] == 324
    assert labels.iloc[0] == "测试塔"
    assert heights.iloc[1] != heights.iloc[1]  # NaN

    second = _Session({"entities": {}})
    cached_heights, _ = wikidata.load_wikidata_heights(
        buildings, cache_dir=str(tmp_path), auto_fetch=True, session=second)
    assert second.calls == 0
    assert cached_heights.iloc[0] == 324


def test_wikidata_height_units_are_normalized_to_meters():
    assert wikidata._height_from_entity(_entity(1000, unit="Q174728")) == 10
    assert wikidata._height_from_entity(_entity(100, unit="Q3710")) == 30.48


def test_prefetch_batches_queries_and_reuses_negative_cache(tmp_path):
    payload = {"entities": {
        f"Q{index}": _entity(index if index % 2 else None)
        for index in range(1, 53)
    }}
    first = _Session(payload)
    summary, records = wikidata.prefetch_wikidata_landmarks(
        [f"Q{index}" for index in range(1, 53)],
        cache_dir=str(tmp_path), session=first)

    assert first.calls == 2
    assert summary["api_batches"] == 2
    assert summary["height_hits"] == 26
    assert summary["negative_cached"] == 26
    assert len(records) == 52

    second = _Session({"entities": {}})
    cached, _ = wikidata.prefetch_wikidata_landmarks(
        ["Q1", "Q2"], cache_dir=str(tmp_path), session=second)
    assert second.calls == 0
    assert cached["cached_before"] == 2


def test_sparql_discovery_hydrates_only_height_hits(tmp_path):
    class RoutingSession:
        def __init__(self):
            self.urls = []

        def get(self, url, **_kwargs):
            self.urls.append(url)
            if "sparql" in url:
                return _Response({"results": {"bindings": [{
                    "item": {"value": "http://www.wikidata.org/entity/Q1"},
                    "height": {"value": "88"},
                }]}})
            return _Response({"entities": {"Q1": _entity(88, label="命中塔")}})

    session = RoutingSession()
    summary, records = wikidata.prefetch_wikidata_landmarks_sparql(
        ["Q1", "Q2"], cache_dir=str(tmp_path), session=session, delay_s=0)

    assert session.urls == [wikidata._SPARQL_URL, wikidata._API_URL]
    assert summary["entity_qids"] == 1
    assert records["Q1"]["height_m"] == 88
    assert records["Q1"]["label"] == "命中塔"
    assert records["Q2"]["status"] == "missing"
