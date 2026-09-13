import os
import geopandas as gpd
from shapely.geometry import box

from aesthetic.source_identity import file_content_identity, projected_sources_identity
from _TEXTURE_STYLE_OF_DEEPSEEK._pipeline_cache import PipelineCache
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osmium_cli_fetcher import OsmiumCLIFetcher


def test_same_path_size_mtime_but_changed_bytes_invalidates_raw_cache(tmp_path):
    path = tmp_path / 'city.pbf'
    path.write_bytes(b'abcd')
    stat = path.stat()
    first = file_content_identity(path)
    raw_key = OsmiumCLIFetcher._pbf_cache_namespace(str(path))
    path.write_bytes(b'efgh')
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert file_content_identity(path) != first
    assert OsmiumCLIFetcher._pbf_cache_namespace(str(path)) != raw_key


def test_equal_count_geometry_or_attribute_changes_invalidate_derived_cache(tmp_path):
    frame = gpd.GeoDataFrame({'height': [10.]}, geometry=[box(0, 0, 5, 5)], crs='EPSG:32631')
    cache = PipelineCache('test', cache_dir=str(tmp_path))
    identity = projected_sources_identity({'buildings': frame})
    key = cache._make_key('s3', {'sources': identity})
    frame.loc[0, 'height'] = 20.
    changed_height = projected_sources_identity({'buildings': frame})
    assert cache._make_key('s3', {'sources': changed_height}) != key
    frame.loc[0, 'height'] = 10.
    frame.loc[0, 'geometry'] = box(0, 0, 6, 5)
    assert cache._make_key('s3', {'sources': projected_sources_identity({'buildings': frame})}) != key
