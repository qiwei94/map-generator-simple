"""Tests for exporter.py — split_terrain_mesh, export_deepseek_3mf, XML builders."""

import sys
import os
import zipfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pytest
import trimesh

from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import (
    split_terrain_mesh,
    export_deepseek_3mf,
    _format_sub_mesh,
    _build_model_settings,
    _uuid4,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.config import EXTRUDER_MAP


def _make_watertight_box(size=10.0, z_offset=0.0):
    mesh = trimesh.creation.box(extents=[size, size, size])
    mesh.vertices[:, 2] += z_offset
    return mesh


class TestSplitTerrainMesh:

    def test_none_input(self):
        result = split_terrain_mesh(None)
        assert result["terrain_surface"] is None
        assert result["terrain_walls"] is None

    def test_empty_mesh(self):
        mesh = trimesh.Trimesh(vertices=[], faces=[])
        result = split_terrain_mesh(mesh)
        assert result["terrain_surface"] is None
        assert result["terrain_walls"] is None

    def test_box_splits_into_surface_and_walls(self):
        mesh = _make_watertight_box()
        result = split_terrain_mesh(mesh)
        surface = result["terrain_surface"]
        walls = result["terrain_walls"]
        assert surface is not None
        assert walls is not None
        assert len(surface.faces) > 0
        assert len(walls.faces) > 0
        assert len(surface.faces) + len(walls.faces) == len(mesh.faces)

    def test_surface_normals_point_up(self):
        mesh = _make_watertight_box()
        result = split_terrain_mesh(mesh)
        surface = result["terrain_surface"]
        if surface is not None and len(surface.faces) > 0:
            normals = surface.face_normals
            assert all(normals[:, 2] > 0.1)


class TestFormatSubMesh:

    def test_none_mesh_returns_empty(self):
        assert _format_sub_mesh(1, None, "test-uuid") == ""

    def test_empty_mesh_returns_empty(self):
        mesh = trimesh.Trimesh(vertices=[], faces=[])
        assert _format_sub_mesh(1, mesh, "test-uuid") == ""

    def test_valid_mesh_contains_vertices_and_triangles(self):
        mesh = _make_watertight_box()
        xml = _format_sub_mesh(1, mesh, "test-uuid")
        assert '<vertex x=' in xml
        assert '<triangle v1=' in xml
        assert 'id="1"' in xml
        assert 'p:UUID="test-uuid"' in xml


class TestBuildModelSettings:

    def test_extruder_assignments(self):
        mesh = _make_watertight_box()
        active = [(1, "terrain", "#9A9A9A", EXTRUDER_MAP["terrain"], mesh)]
        xml = _build_model_settings(active)
        assert '<metadata key="extruder" value="2"/>' in xml
        assert '<metadata key="name" value="terrain"/>' in xml

    def test_multiple_parts(self):
        mesh = _make_watertight_box()
        active = [
            (1, "terrain", "#9A9A9A", EXTRUDER_MAP["terrain"], mesh),
            (2, "water", "#000000", EXTRUDER_MAP["water"], mesh),
        ]
        xml = _build_model_settings(active)
        assert xml.count('<part ') == 2


class TestExportDeepseek3mf:

    def test_writes_valid_zip(self, tmp_path):
        mesh = _make_watertight_box()
        out = str(tmp_path / "test.3mf")
        export_deepseek_3mf({"terrain": mesh}, out)
        assert os.path.exists(out)
        with zipfile.ZipFile(out, "r") as zf:
            names = zf.namelist()
            assert "[Content_Types].xml" in names
            assert "_rels/.rels" in names
            assert "3D/3dmodel.model" in names
            assert "3D/Objects/object_1.model" in names
            assert "Metadata/model_settings.config" in names

    def test_raises_on_empty_meshes(self, tmp_path):
        out = str(tmp_path / "empty.3mf")
        with pytest.raises(ValueError, match="No non-empty"):
            export_deepseek_3mf({}, out)

    def test_multiple_meshes(self, tmp_path):
        terrain = _make_watertight_box()
        water = _make_watertight_box(z_offset=-5)
        out = str(tmp_path / "multi.3mf")
        export_deepseek_3mf({"terrain": terrain, "water": water}, out)
        with zipfile.ZipFile(out, "r") as zf:
            obj_xml = zf.read("3D/Objects/object_1.model").decode()
            assert obj_xml.count('<object ') == 2

    def test_creates_parent_directory(self, tmp_path):
        out = str(tmp_path / "sub" / "dir" / "test.3mf")
        mesh = _make_watertight_box()
        export_deepseek_3mf({"terrain": mesh}, out)
        assert os.path.exists(out)
