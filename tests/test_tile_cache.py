# -*- coding: utf-8 -*-
"""Phase 2 瓦片缓存纯逻辑单测：图层去重 + 高程瓦片拼接。"""

import numpy as np
import pandas as pd
import geopandas as gpd
import pytest
import shutil
import os
import sys
from shapely.geometry import LineString, Point

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osmium_cli_fetcher import (
    OsmiumCLIFetcher,
    _bbox_option,
    _export_geometry_types,
    _export_timeout_seconds,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers import osmium_cli_fetcher
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import OSMPipeline
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


class TestPbfCacheIdentity:
    def test_same_bbox_from_different_regions_has_different_namespace(
            self, tmp_path):
        beijing = tmp_path / "beijing-latest.osm.pbf"
        zhejiang = tmp_path / "zhejiang-latest.osm.pbf"
        beijing.write_bytes(b"beijing")
        zhejiang.write_bytes(b"zhejiang")
        fetcher = OsmiumCLIFetcher()

        beijing_key = fetcher._pbf_cache_namespace(str(beijing))
        zhejiang_key = fetcher._pbf_cache_namespace(str(zhejiang))

        assert beijing_key != zhejiang_key
        assert "beijing-latest.osm.pbf" in beijing_key
        assert "zhejiang-latest.osm.pbf" in zhejiang_key


class TestPipelineCleanup:
    def test_null_osm_ids_do_not_collapse_anonymous_road_network(self):
        gdf = gpd.GeoDataFrame({
            "osm_type": [None, None, "way", "way"],
            "osm_id": [None, None, 7, 7],
            "highway": ["primary", "secondary", "residential", "residential"],
            "geometry": [
                LineString([(-0.20, 51.50), (-0.10, 51.51)]),
                LineString([(-0.15, 51.52), (-0.05, 51.53)]),
                LineString([(-0.12, 51.54), (-0.02, 51.55)]),
                LineString([(-0.12, 51.54), (-0.02, 51.55)]),
            ],
        }, crs="EPSG:4326")
        pipeline = OSMPipeline.__new__(OSMPipeline)
        pipeline.feature_type = "road"
        pipeline.bbox = (51.49, -0.21, 51.56, 0.01)

        out = pipeline.step4_cleanup(gdf)

        assert len(out) == 3
        assert out["osm_id"].isna().sum() == 2
        assert (out["osm_id"] == 7).sum() == 1


class TestFullFrameRefreshDecision:
    def test_refreshes_when_half_or_more_tiles_are_missing(self):
        assert OsmiumCLIFetcher._should_refresh_full_frame(16, 20)

    def test_refreshes_when_missing_rectangle_covers_frame(self):
        assert OsmiumCLIFetcher._should_refresh_full_frame(
            2, 9,
            missing_bbox=(48.74, 2.19, 48.96, 2.46),
            frame_bbox=(48.75, 2.20, 48.95, 2.45),
        )

    def test_keeps_sparse_reuse_for_one_or_small_edge_miss(self):
        assert not OsmiumCLIFetcher._should_refresh_full_frame(
            1, 20,
            missing_bbox=(48.74, 2.19, 48.81, 2.26),
            frame_bbox=(48.75, 2.20, 48.95, 2.45),
        )
        assert not OsmiumCLIFetcher._should_refresh_full_frame(
            2, 20,
            missing_bbox=(48.74, 2.19, 48.81, 2.31),
            frame_bbox=(48.75, 2.20, 48.95, 2.45),
        )


class TestCacheColumnPruning:
    def test_keeps_model_fields_and_drops_export_only_tags(self):
        gdf = gpd.GeoDataFrame({
            "osm_type": ["way"],
            "osm_id": [42],
            "building": ["cathedral"],
            "building:levels": [5],
            "name": ["Notre-Dame"],
            "wikidata": ["Q2981"],
            "height_source": ["temporary"],
            "contact:facebook": [None],
            "payment:bitcoin": [None],
            "geometry": [Point(2.35, 48.85)],
        }, crs="EPSG:4326")

        out = OsmiumCLIFetcher._prune_cache_columns(gdf, "building")

        assert list(out.columns) == [
            "osm_type", "osm_id", "building", "building:levels",
            "name", "wikidata", "geometry",
        ]
        assert out.crs == gdf.crs
        assert "contact:facebook" not in out
        assert "payment:bitcoin" not in out

    def test_cache_reader_projects_columns_before_returning(self, tmp_path):
        path = tmp_path / "tile.geojson"
        gdf = gpd.GeoDataFrame({
            "osm_type": ["way"],
            "osm_id": [7],
            "highway": ["primary"],
            "contact:facebook": ["unused"],
            "geometry": [Point(2.35, 48.85)],
        }, crs="EPSG:4326")
        gdf.to_file(path, driver="GeoJSON")

        out = OsmiumCLIFetcher._try_read_geojson_cache(path, "road")

        assert list(out.columns) == [
            "osm_type", "osm_id", "highway", "geometry",
        ]


class TestOsmiumBinaryOverride:
    def test_western_hemisphere_bbox_is_one_cli_token(self):
        option = _bbox_option(-87.75, 41.8, -87.5, 42.0)

        assert option == "--bbox=-87.75,41.8,-87.5,42.0"

    def test_explicit_binary_wins_over_portable_path(self, monkeypatch):
        executable = shutil.which("true")
        assert executable is not None
        monkeypatch.setenv("OSMIUM_BIN", executable)

        fetcher = OsmiumCLIFetcher()

        assert fetcher.osmium_available
        assert fetcher._get_tool_path("osmium") == executable
        assert fetcher._get_osmium_command() == [executable]

    def test_portable_backend_uses_active_python(self, monkeypatch):
        monkeypatch.delenv("OSMIUM_BIN", raising=False)
        monkeypatch.setenv("PATH", "")
        monkeypatch.setattr(
            osmium_cli_fetcher,
            "_STANDARD_EXECUTABLE_DIRS",
            (),
        )

        fetcher = OsmiumCLIFetcher()
        command = fetcher._get_osmium_command()

        assert fetcher.osmium_available
        assert command[0] == sys.executable
        assert command[1].endswith("tools/osmium_pyosmium.py")

    def test_homebrew_standard_path_wins_when_worker_path_is_minimal(
            self, monkeypatch, tmp_path):
        executable = tmp_path / "osmium"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
        monkeypatch.delenv("OSMIUM_BIN", raising=False)
        monkeypatch.setenv("PATH", "")
        monkeypatch.setattr(
            osmium_cli_fetcher,
            "_STANDARD_EXECUTABLE_DIRS",
            (str(tmp_path),),
        )

        fetcher = OsmiumCLIFetcher()

        assert fetcher.osmium_available
        assert fetcher._get_osmium_command() == [str(executable)]

    def test_portable_export_budget_handles_dense_road_extract(self):
        portable = [sys.executable, "/repo/tools/osmium_pyosmium.py"]

        assert _export_timeout_seconds(4_886.5, portable) >= 720
        assert _export_timeout_seconds(4_886.5, ["/usr/bin/osmium"]) < 300

    def test_export_geometry_types_match_downstream_consumers(self):
        assert _export_geometry_types("building") == "polygon"
        assert _export_geometry_types("vegetation") == "polygon"
        assert _export_geometry_types("landuse") == "polygon"
        assert _export_geometry_types("road") == "linestring"
        assert _export_geometry_types("water") == "linestring,polygon"
        assert _export_geometry_types("unknown") == "point,linestring,polygon"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX executable bit")
    def test_portable_osmium_entry_is_executable(self):
        portable = os.path.abspath(os.path.join(
            os.path.dirname(__file__), "..", "tools", "osmium"))

        assert os.path.isfile(portable)
        assert os.access(portable, os.X_OK)


class TestExtractionFailureSafety:
    def test_failed_extract_is_not_saved_as_empty_tile_cache(
            self, monkeypatch, tmp_path):
        pbf = tmp_path / "fixture.osm.pbf"
        pbf.write_bytes(b"fixture")
        tile_dir = tmp_path / "tiles"
        tile_dir.mkdir()
        fetcher = OsmiumCLIFetcher()
        monkeypatch.setattr(fetcher, "osmium_available", True)
        monkeypatch.setattr(
            fetcher, "_tile_cache_path",
            lambda tag, ix, iy, pbf_file: str(
                tile_dir / f"{tag}_{ix}_{iy}.geojson"),
        )
        monkeypatch.setattr(fetcher, "_run_osmium_pipeline", lambda *a: False)

        with pytest.raises(RuntimeError, match="false empty result"):
            fetcher.fetch_tiled_features(
                "road", 1.111, 2.222, 1.121, 2.232,
                pbf_file=str(pbf), step=0.05,
            )

        assert list(tile_dir.iterdir()) == []


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
