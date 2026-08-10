# -*- coding: utf-8 -*-
"""Phase 2 瓦片缓存纯逻辑单测：图层去重 + 高程瓦片拼接。"""

import numpy as np
import pandas as pd
import geopandas as gpd
import pytest
from shapely.geometry import Point

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osmium_cli_fetcher import (
    OsmiumCLIFetcher,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.elevation import (
    _stitch_tile_grids,
)


def _gdf(osm_types, osm_ids, xs):
    return gpd.GeoDataFrame(
        {'osm_type': osm_types, 'osm_id': osm_ids,
         'geometry': [Point(x, x) for x in xs]})


class TestDedupeFeatures:

    def test_dedupe_by_osm_id(self):
        g1 = _gdf(['way', 'way'], [1, 2], [0.0, 1.0])
        g2 = _gdf(['way', 'node'], [2, 3], [1.0, 2.0])
        merged = pd.concat([g1, g2], ignore_index=True)
        out = OsmiumCLIFetcher._dedupe_features(merged)
        assert len(out) == 3
        assert set(out['osm_id']) == {1, 2, 3}

    def test_dedupe_keeps_first_occurrence(self):
        g1 = gpd.GeoDataFrame(
            {'osm_type': ['way'], 'osm_id': [7], 'src': ['a'],
             'geometry': [Point(0, 0)]})
        g2 = gpd.GeoDataFrame(
            {'osm_type': ['way'], 'osm_id': [7], 'src': ['b'],
             'geometry': [Point(0.5, 0.5)]})
        merged = pd.concat([g1, g2], ignore_index=True)
        out = OsmiumCLIFetcher._dedupe_features(merged)
        assert len(out) == 1
        assert out.iloc[0]['src'] == 'a'

    def test_dedupe_empty(self):
        g = gpd.GeoDataFrame(
            {'osm_type': [], 'osm_id': [], 'geometry': []})
        out = OsmiumCLIFetcher._dedupe_features(g)
        assert len(out) == 0

    def test_dedupe_fingerprint_fallback(self):
        # 无 osm_type/osm_id 列 → 几何指纹去重（相同几何视为同一要素）
        g1 = gpd.GeoDataFrame({'geometry': [Point(0, 0), Point(1, 1)]})
        g2 = gpd.GeoDataFrame({'geometry': [Point(1, 1), Point(2, 2)]})
        merged = pd.concat([g1, g2], ignore_index=True)
        out = OsmiumCLIFetcher._dedupe_features(merged)
        assert len(out) == 3


class TestStitchTileGrids:

    def test_single_tile(self):
        t = np.arange(9, dtype=float).reshape(3, 3)
        out = _stitch_tile_grids({(0, 0): t}, 0, 0, 0, 0)
        assert np.array_equal(out, t)

    def test_2x2_shape_and_corners(self):
        # row0=south, col0=west：iy 小的瓦片在南，ix 小的在西
        r = 4
        tiles = {}
        for iy in range(2):
            for ix in range(2):
                tiles[(ix, iy)] = np.full((r, r), iy * 10 + ix, dtype=float)
        out = _stitch_tile_grids(tiles, 0, 0, 1, 1)
        # 共享边界行/列去重：2*4-1 = 7
        assert out.shape == (2 * r - 1, 2 * r - 1)
        # 四角对应四块瓦片
        assert out[0, 0] == 0        # 西南 = tile(0,0)
        assert out[0, -1] == 1       # 东南 = tile(1,0)
        assert out[-1, 0] == 10      # 西北 = tile(0,1)
        assert out[-1, -1] == 11     # 东北 = tile(1,1)

    def test_stitch_value_continuity(self):
        # 每瓦片值随纬度（行号）线性递增，拼接后应保持全局单调
        r = 3
        tiles = {}
        for iy in range(2):
            for ix in range(1):
                # 瓦片内 row0=south；全局值 = 纬度序号
                tiles[(ix, iy)] = np.tile(
                    (np.arange(r) + iy * (r - 1)).reshape(-1, 1), (1, r)
                ).astype(float)
        out = _stitch_tile_grids(tiles, 0, 0, 0, 1)
        col = out[:, 0]
        assert np.all(np.diff(col) == 1.0)
        assert out.shape[0] == 2 * r - 1
