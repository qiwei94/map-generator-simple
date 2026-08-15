"""Tests for validator.py — XML parsers, face normals, validate_3mf."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pytest
import trimesh

from _TEXTURE_STYLE_OF_DEEPSEEK.validator import (
    _parse_vertices,
    _parse_faces,
    _compute_face_normals,
    validate_3mf,
)


class TestParseVertices:

    def test_valid_xml(self):
        xml = '''
        <vertex x="1.0" y="2.0" z="3.0"/>
        <vertex x="4.0" y="5.0" z="6.0"/>
        '''
        result = _parse_vertices(xml)
        assert result is not None
        assert result.shape == (2, 3)
        np.testing.assert_array_almost_equal(result[0], [1.0, 2.0, 3.0])
        np.testing.assert_array_almost_equal(result[1], [4.0, 5.0, 6.0])

    def test_no_match_returns_none(self):
        assert _parse_vertices("<mesh></mesh>") is None

    def test_empty_string(self):
        assert _parse_vertices("") is None

    def test_negative_coords(self):
        xml = '<vertex x="-1.5" y="0.0" z="-3.14"/>'
        result = _parse_vertices(xml)
        assert result is not None
        np.testing.assert_array_almost_equal(result[0], [-1.5, 0.0, -3.14])


class TestParseFaces:

    def test_valid_xml(self):
        xml = '''
        <triangle v1="0" v2="1" v3="2"/>
        <triangle v1="0" v2="2" v3="3"/>
        '''
        result = _parse_faces(xml)
        assert result is not None
        assert result.shape == (2, 3)
        np.testing.assert_array_equal(result[0], [0, 1, 2])

    def test_no_match_returns_none(self):
        assert _parse_faces("<mesh></mesh>") is None

    def test_empty_string(self):
        assert _parse_faces("") is None


class TestComputeFaceNormals:

    def test_z_up_triangle(self):
        verts = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=np.float64)
        faces = np.array([[0, 1, 2]], dtype=np.int32)
        normals = _compute_face_normals(verts, faces)
        assert normals.shape == (1, 3)
        assert normals[0, 2] > 0.9

    def test_unit_length(self):
        verts = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=np.float64)
        faces = np.array([[0, 1, 2]], dtype=np.int32)
        normals = _compute_face_normals(verts, faces)
        length = np.linalg.norm(normals[0])
        assert length == pytest.approx(1.0)

    def test_box_normals(self):
        mesh = trimesh.creation.box(extents=[1, 1, 1])
        normals = _compute_face_normals(mesh.vertices, mesh.faces)
        assert normals.shape[0] == len(mesh.faces)
        lengths = np.linalg.norm(normals, axis=1)
        np.testing.assert_array_almost_equal(lengths, 1.0)


class TestValidate3mf:

    def test_file_not_found(self, tmp_path):
        result = validate_3mf(str(tmp_path / "nonexistent.3mf"))
        assert result["passed"] is False
        assert "File not found" in result["errors"]

    def test_invalid_zip(self, tmp_path):
        bad_file = tmp_path / "bad.3mf"
        bad_file.write_text("not a zip")
        result = validate_3mf(str(bad_file))
        assert result["passed"] is False

    def test_valid_3mf_round_trip(self, tmp_path):
        from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf
        mesh = trimesh.creation.box(extents=[196, 196, 4])
        out = str(tmp_path / "valid.3mf")
        export_deepseek_3mf({"terrain": mesh}, out)
        result = validate_3mf(out)
        assert result["file"] == out
        assert isinstance(result["rules"], list)
        assert len(result["rules"]) > 0

    def test_result_structure(self, tmp_path):
        result = validate_3mf(str(tmp_path / "nope.3mf"))
        assert "passed" in result
        assert "rules" in result
        assert "errors" in result
        assert "warnings" in result

    def test_exact_point_four_mm_water_plate_is_not_rejected(self, tmp_path):
        from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf

        terrain = trimesh.creation.box(extents=[196, 176, 4])
        water = trimesh.creation.box(extents=[196, 176, 0.4])
        water.apply_translation([0, 0, -1.8])  # serialized bounds: -2.0 .. -1.6
        out = str(tmp_path / "water-point-four.3mf")
        export_deepseek_3mf({"terrain": terrain, "water": water}, out)

        result = validate_3mf(out)
        v8 = next(rule for rule in result["rules"] if rule["id"] == "V8")
        assert bool(v8["passed"])
