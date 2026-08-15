"""Tests for native/portable osmium command selection and extraction."""

import sys

import geopandas as gpd
import osmium

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osmium_cli_fetcher import (
    PORTABLE_OSMIUM_SCRIPT,
    OsmiumCLIFetcher,
)


def _write_road_fixture(path):
    writer = osmium.SimpleWriter(str(path), overwrite=True)
    writer.add_node(osmium.osm.mutable.Node(id=1, location=(120.10, 30.10)))
    writer.add_node(osmium.osm.mutable.Node(id=2, location=(120.20, 30.20)))
    writer.add_node(osmium.osm.mutable.Node(id=3, location=(120.25, 30.25)))
    writer.add_way(
        osmium.osm.mutable.Way(
            id=10,
            nodes=[1, 2],
            tags={"highway": "primary", "name": "Portable Road"},
        )
    )
    writer.add_way(
        osmium.osm.mutable.Way(
            id=11,
            nodes=[2, 3],
            tags={"waterway": "stream", "name": "Not a Road"},
        )
    )
    writer.close()


def test_native_osmium_is_preferred(monkeypatch, tmp_path):
    monkeypatch.setattr(
        OsmiumCLIFetcher,
        "_check_tool",
        lambda self, name: name == "osmium",
    )

    fetcher = OsmiumCLIFetcher(pbf_dir=str(tmp_path))

    assert fetcher.osmium_available is True
    assert fetcher.native_osmium_available is True
    assert fetcher.osmium_backend == "native"
    assert fetcher._get_tool_command("osmium") == ["osmium"]


def test_portable_fallback_extracts_nonzero_roads(monkeypatch, tmp_path):
    # Remove native command discovery while keeping the active Python runtime.
    monkeypatch.setenv("PATH", str(tmp_path))
    fixture = tmp_path / "roads.osm.pbf"
    output = tmp_path / "roads.geojson"
    _write_road_fixture(fixture)

    fetcher = OsmiumCLIFetcher(pbf_dir=str(tmp_path))

    assert fetcher.native_osmium_available is False
    assert fetcher.osmium_available is True
    assert fetcher.osmium_backend == "pyosmium"
    assert fetcher._get_tool_command("osmium") == [
        sys.executable,
        PORTABLE_OSMIUM_SCRIPT,
    ]
    assert all(isinstance(arg, str) for arg in fetcher.osmium_command)

    success = fetcher._run_osmium_pipeline(
        str(fixture),
        "road",
        30.0,
        120.0,
        30.3,
        120.3,
        str(output),
    )

    assert success is True
    roads = gpd.read_file(output)
    assert len(roads) > 0
    assert roads["highway"].tolist() == ["primary"]
    assert roads["name"].tolist() == ["Portable Road"]
