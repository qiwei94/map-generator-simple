"""Exact-bbox extraction must not re-expand all sparse tags or reread a cache."""
import geopandas as gpd
import pytest
from shapely.geometry import Point

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osmium_cli_fetcher import OsmiumCLIFetcher


@pytest.mark.parametrize('empty', [False, True])
def test_exact_bbox_cache_read_once_with_all_features_preserved(tmp_path, monkeypatch, empty):
    pbf = tmp_path / 'paris.pbf'
    pbf.write_bytes(b'fixture')
    fetcher = OsmiumCLIFetcher.__new__(OsmiumCLIFetcher)
    fetcher.osmium_available = True
    source = gpd.GeoDataFrame({'osm_id': [1, 2], 'height': ['12', '20'],
        'geometry': [Point(2.3, 48.8), Point(2.4, 48.9)]}, crs='EPSG:4326')
    if empty:
        source = source.iloc[:0]
    calls = []
    def cache(path, tag_type):
        calls.append((path, tag_type))
        return source
    monkeypatch.setattr(fetcher, '_try_read_geojson_cache', cache)
    monkeypatch.setattr(fetcher, '_enrich_building_heights', lambda frame,*a: frame)
    def unexpected(*a, **kw):
        pytest.fail('cache hit must not read again or invoke osmium')
    monkeypatch.setattr(fetcher, '_run_osmium_pipeline', unexpected)
    monkeypatch.setattr(gpd, 'read_file', unexpected)
    actual = fetcher.fetch_features('building', 48.7,2.1,49.,2.6,str(pbf))
    assert len(calls) == 1
    assert actual.equals(source)


def test_exact_bbox_fresh_export_projects_columns_without_filtering_features(tmp_path, monkeypatch):
    pbf = tmp_path / 'paris.pbf'
    pbf.write_bytes(b'fixture')
    fetcher = OsmiumCLIFetcher.__new__(OsmiumCLIFetcher)
    fetcher.osmium_available = True
    monkeypatch.setattr(fetcher, '_try_read_geojson_cache', lambda *a: None)
    monkeypatch.setattr(fetcher, '_run_osmium_pipeline', lambda *a: True)
    monkeypatch.setattr(fetcher, '_enrich_building_heights', lambda frame,*a: frame)
    raw = gpd.GeoDataFrame({'osm_id': [1,2], 'height': ['12','20'],
        'contact:facebook': ['unused','unused'],
        'geometry': [Point(2.3,48.8),Point(2.4,48.9)]},crs='EPSG:4326')
    calls = []
    def read(path, **kwargs):
        calls.append(kwargs)
        return raw
    monkeypatch.setattr(gpd,'read_file',read)
    actual=fetcher.fetch_features('building',48.7,2.1,49.,2.6,str(pbf))
    assert calls == [{'columns': list(fetcher._CACHE_COLUMNS)}]
    assert len(actual)==2
    assert list(actual['osm_id']) == [1,2]
    assert list(actual['height']) == ['12','20']
    assert actual.geometry.equals(raw.geometry)
    assert 'contact:facebook' not in actual
