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
