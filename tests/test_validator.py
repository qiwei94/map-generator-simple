"""Tests for validator.py — XML parsers, face normals, validate_3mf."""

import json
import hashlib
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

    def test_explicit_attempt_design_spec_ignores_mutable_latest_alias(
            self, tmp_path):
        from _TEXTURE_STYLE_OF_DEEPSEEK.design_spec import build_design_spec
        from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf

        out = str(tmp_path / "attempt-bound.3mf")
        export_deepseek_3mf(
            {"terrain": trimesh.creation.box(extents=[196, 196, 4])}, out)
        spec = build_design_spec(
            city="fixture",
            bbox_wgs84=[1.0, 1.0, 2.0, 2.0],
            artifact_path=out,
        )
        exact = tmp_path / "design_spec.run-1.attempt-1.json"
        exact.write_text(json.dumps(spec), encoding="utf-8")
        (tmp_path / "design_spec.json").write_text(
            json.dumps({"schema_version": "later", "artifact": {
                "filename": "another.3mf", "sha256": "0" * 64,
                "size_bytes": 1,
            }}),
            encoding="utf-8",
        )

        result = validate_3mf(out, design_spec_path=str(exact))

        assert result["passed"] is True
        assert result["design_spec"] == {
            "filename": exact.name,
            "explicit": True,
        }
        assert not any("Explicit DesignSpec" in item
                       for item in result["errors"])

    def test_explicit_design_spec_must_bind_the_exact_3mf(self, tmp_path):
        from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf

        out = str(tmp_path / "bound.3mf")
        export_deepseek_3mf(
            {"terrain": trimesh.creation.box(extents=[196, 196, 4])}, out)
        wrong = tmp_path / "wrong.json"
        wrong.write_text(json.dumps({
            "schema_version": "1.5",
            "artifact": {
                "filename": "other.3mf", "sha256": "0" * 64,
                "size_bytes": 1,
            },
        }), encoding="utf-8")

        result = validate_3mf(out, design_spec_path=str(wrong))

        assert result["passed"] is False
        assert "Explicit DesignSpec artifact identity does not match 3MF" in (
            result["errors"])

    def test_result_structure(self, tmp_path):
        result = validate_3mf(str(tmp_path / "nope.3mf"))
        assert "passed" in result
        assert "rules" in result
        assert "errors" in result
        assert "warnings" in result

    def test_base_only_water_plate_is_rejected(self, tmp_path):
        from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf

        terrain = trimesh.creation.box(extents=[196, 176, 4])
        water = trimesh.creation.box(extents=[196, 176, 0.4])
        water.apply_translation([0, 0, -1.8])  # serialized bounds: -2.0 .. -1.6
        out = str(tmp_path / "water-point-four.3mf")
        export_deepseek_3mf({"terrain": terrain, "water": water}, out)

        result = validate_3mf(out)
        v8 = next(rule for rule in result["rules"] if rule["id"] == "V8")
        v9 = next(rule for rule in result["rules"] if rule["id"] == "V9")
        assert not bool(v8["passed"])
        assert not bool(v9["passed"])

    def test_water_plate_with_printable_cap_passes(self, tmp_path):
        from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf

        terrain = trimesh.creation.box(extents=[196, 176, 4])
        plate = trimesh.creation.box(extents=[196, 176, 0.4])
        plate.apply_translation([0, 0, -1.8])
        cap = trimesh.creation.box(extents=[40, 30, 0.24])
        cap.apply_translation([0, 0, -1.48])
        water = trimesh.util.concatenate([plate, cap])
        out = str(tmp_path / "water-with-cap.3mf")
        export_deepseek_3mf({"terrain": terrain, "water": water}, out)

        result = validate_3mf(out)
        v8 = next(rule for rule in result["rules"] if rule["id"] == "V8")
        v9 = next(rule for rule in result["rules"] if rule["id"] == "V9")
        assert bool(v8["passed"])
        assert bool(v9["passed"])

    def test_sloped_closed_vegetation_passes_printability_rule(self, tmp_path):
        from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf

        terrain = trimesh.creation.box(extents=[196, 176, 4])
        vegetation = trimesh.creation.box(extents=[20, 20, 0.2])
        vegetation.vertices[:, 2] += vegetation.vertices[:, 0] * 0.02 + 2.1
        out = str(tmp_path / "sloped-vegetation.3mf")
        export_deepseek_3mf({"terrain": terrain, "vegetation": vegetation}, out)

        result = validate_3mf(out)
        v12 = next(rule for rule in result["rules"] if rule["id"] == "V12")
        assert bool(v12["passed"])
        assert "nonmanifold_edges=0" in v12["detail"]

    def test_formal_giant_vegetation_facet_is_a_critical_error(self, tmp_path):
        from _TEXTURE_STYLE_OF_DEEPSEEK.design_spec import (
            build_design_spec,
            write_design_spec,
        )
        from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf

        terrain = trimesh.creation.box(extents=[40, 40, 2])
        vegetation = trimesh.creation.box(extents=[20, 20, 0.2])
        vegetation.apply_translation([0, 0, 1.1])
        out = str(tmp_path / "giant-vegetation-facet.3mf")
        export_deepseek_3mf(
            {"terrain": terrain, "vegetation": vegetation}, out)
        spec = build_design_spec(
            city="fixture",
            bbox_wgs84=[1.0, 1.0, 2.0, 2.0],
            artifact_path=out,
            printability={
                "printer_profile": {"extrusion_width_mm": 0.42},
            },
        )
        write_design_spec(tmp_path, spec)

        result = validate_3mf(out)
        v16 = next(rule for rule in result["rules"] if rule["id"] == "V16")
        assert not bool(v16["passed"])
        assert "vegetation_actual_max=28.284mm" in v16["detail"]
        assert any(error.startswith("V16:") for error in result["errors"])

    def test_formal_printer_bounded_vegetation_facets_pass(self, tmp_path):
        from _TEXTURE_STYLE_OF_DEEPSEEK.design_spec import (
            build_design_spec,
            write_design_spec,
        )
        from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf

        terrain = trimesh.creation.box(extents=[2, 2, 0.5])
        vegetation = trimesh.creation.box(extents=[0.5, 0.5, 0.2])
        vegetation.apply_translation([0, 0, 0.35])
        out = str(tmp_path / "bounded-vegetation-facet.3mf")
        export_deepseek_3mf(
            {"terrain": terrain, "vegetation": vegetation}, out)
        spec = build_design_spec(
            city="fixture",
            bbox_wgs84=[1.0, 1.0, 2.0, 2.0],
            artifact_path=out,
            printability={
                "printer_profile": {"extrusion_width_mm": 0.42},
            },
        )
        write_design_spec(tmp_path, spec)

        result = validate_3mf(out)
        v16 = next(rule for rule in result["rules"] if rule["id"] == "V16")
        assert bool(v16["passed"])
        assert "vegetation_actual_max=0.707mm" in v16["detail"]

    def test_terrain_owned_thin_landmark_is_a_critical_error(self, tmp_path):
        from _TEXTURE_STYLE_OF_DEEPSEEK.design_spec import (
            build_design_spec,
            write_design_spec,
        )
        from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf

        terrain = trimesh.creation.box(extents=[196, 176, 3.0])
        landmark = trimesh.creation.box(extents=[0.30, 2.0, 3.0])
        landmark.apply_translation([0, 0, 1.5])
        out = str(tmp_path / "thin-landmark.3mf")
        export_deepseek_3mf(
            {"terrain": terrain, "landmarks": landmark}, out)
        spec = build_design_spec(
            city="fixture",
            bbox_wgs84=[1.0, 1.0, 2.0, 2.0],
            artifact_path=out,
            decisions={
                "building_height_hierarchy": {
                    "policy_version": "terrain-owned-building-z-v1",
                    "status": "active",
                    "terrain_owned": True,
                    "hero_count": 1,
                },
            },
            printability={
                "printer_profile": {"extrusion_width_mm": 0.42},
            },
        )
        write_design_spec(tmp_path, spec)

        result = validate_3mf(out)
        v17 = next(rule for rule in result["rules"] if rule["id"] == "V17")
        assert not bool(v17["passed"])
        assert "below_0.42mm=1" in v17["detail"]
        assert any(error.startswith("V17:") for error in result["errors"])

    def test_declared_vegetation_cannot_be_silently_missing(self, tmp_path):
        from _TEXTURE_STYLE_OF_DEEPSEEK.design_spec import (
            build_design_spec,
            write_design_spec,
        )
        from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf

        terrain = trimesh.creation.box(extents=[2, 2, 0.5])
        out = str(tmp_path / "missing-vegetation.3mf")
        export_deepseek_3mf({"terrain": terrain}, out)
        spec = build_design_spec(
            city="fixture",
            bbox_wgs84=[1.0, 1.0, 2.0, 2.0],
            artifact_path=out,
            printable_features={"vegetation_polygons": 3},
        )
        write_design_spec(tmp_path, spec)

        result = validate_3mf(out)
        v16 = next(rule for rule in result["rules"] if rule["id"] == "V16")
        assert not bool(v16["passed"])
        assert "missing vegetation mesh for 3" in v16["detail"]

    def test_open_vegetation_reports_boundary_edges(self, tmp_path):
        from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf

        terrain = trimesh.creation.box(extents=[196, 176, 4])
        vegetation = trimesh.Trimesh(
            vertices=[[0, 0, 2.1], [10, 0, 2.2], [0, 10, 2.3]],
            faces=[[0, 1, 2]], process=False,
        )
        out = str(tmp_path / "open-vegetation.3mf")
        export_deepseek_3mf({"terrain": terrain, "vegetation": vegetation}, out)

        result = validate_3mf(out)
        v12 = next(rule for rule in result["rules"] if rule["id"] == "V12")
        assert not bool(v12["passed"])
        assert "boundary_edges=3" in v12["detail"]
        assert any(warning.startswith("V12:") for warning in result["warnings"])

    def test_closed_block_base_passes_printability_rule(self, tmp_path):
        from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf

        terrain = trimesh.creation.box(extents=[196, 176, 4])
        block_base = trimesh.creation.box(extents=[20, 20, 0.5])
        out = str(tmp_path / "closed-block-base.3mf")
        export_deepseek_3mf(
            {"terrain": terrain, "block_base": block_base}, out)

        result = validate_3mf(out)
        v13 = next(rule for rule in result["rules"] if rule["id"] == "V13")
        assert bool(v13["passed"])
        assert "boundary_edges=0" in v13["detail"]
        assert "nonmanifold_edges=0" in v13["detail"]

    def test_open_block_base_is_a_critical_error(self, tmp_path):
        from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf

        terrain = trimesh.creation.box(extents=[196, 176, 4])
        block_base = trimesh.Trimesh(
            vertices=[[0, 0, 2.1], [10, 0, 2.2], [0, 10, 2.3]],
            faces=[[0, 1, 2]], process=False,
        )
        out = str(tmp_path / "open-block-base.3mf")
        export_deepseek_3mf(
            {"terrain": terrain, "block_base": block_base}, out)

        result = validate_3mf(out)
        v13 = next(rule for rule in result["rules"] if rule["id"] == "V13")
        assert not bool(v13["passed"])
        assert "boundary_edges=3" in v13["detail"]
        assert any(error.startswith("V13:") for error in result["errors"])
        assert result["passed"] is False

    @pytest.mark.parametrize(
        ("target_gap", "passed"),
        [(0.84, True), (0.55, False)],
    )
    def test_final_block_base_clearance_evidence_is_strict(
        self, tmp_path, target_gap, passed,
    ):
        from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf

        terrain = trimesh.creation.box(extents=[196, 176, 4])
        block_base = trimesh.creation.box(extents=[20, 20, 0.5])
        out = str(tmp_path / "clearance.3mf")
        export_deepseek_3mf(
            {"terrain": terrain, "block_base": block_base}, out)
        spec = {
            "artifact": {
                "filename": os.path.basename(out),
                "sha256": hashlib.sha256(
                    (tmp_path / "clearance.3mf").read_bytes()).hexdigest(),
            },
            "block_base": {
                "resolved_mode": "textured",
                "final_clearance": {
                    "status": "checked",
                    "passed": True,
                    "configured_min_gap_mm": 0.55,
                    "extrusion_width_mm": 0.42,
                    "target_gap_mm": target_gap,
                    "verified_min_gap_mm": target_gap,
                    "cutter_features": 3,
                    "post_clip_intrusion_area_m2": 0.0,
                    "measurement_tolerance_m2": 1e-6,
                },
            },
        }
        (tmp_path / "design_spec.json").write_text(
            json.dumps(spec), encoding="utf-8")

        result = validate_3mf(out)
        v14 = next(rule for rule in result["rules"] if rule["id"] == "V14")
        assert bool(v14["passed"]) is passed
        assert any(error.startswith("V14:") for error in result["errors"]) is (not passed)

    def test_declared_block_base_cannot_be_silently_missing(self, tmp_path):
        from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf

        terrain = trimesh.creation.box(extents=[196, 176, 4])
        out = str(tmp_path / "missing-block-base.3mf")
        export_deepseek_3mf({"terrain": terrain}, out)
        (tmp_path / "design_spec.json").write_text(json.dumps({
            "artifact": {"filename": os.path.basename(out)},
            "block_base": {"resolved_mode": "textured"},
        }), encoding="utf-8")

        result = validate_3mf(out)
        v14 = next(rule for rule in result["rules"] if rule["id"] == "V14")
        assert not bool(v14["passed"])
        assert "no block_base mesh" in v14["detail"]
        assert any(error.startswith("V14:") for error in result["errors"])

    def test_regular_terrain_contract_is_verified_from_final_3mf(self, tmp_path):
        from _TEXTURE_STYLE_OF_DEEPSEEK.design_spec import (
            build_design_spec,
            write_design_spec,
        )
        from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf
        from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import (
            _add_walls_and_bottom,
            build_terrain_evidence,
        )
        from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.terrain import (
            build_terrain_mesh,
        )

        surface = build_terrain_mesh(
            np.arange(25, dtype=float).reshape(5, 5) / 100.0,
            2.0, 2.0, allow_qem_decimation=False)
        surface.vertices[:, 2] += 0.25
        terrain = _add_walls_and_bottom(surface, bottom_z=0.0)
        terrain.fix_normals()
        terrain.metadata["terrain_evidence"] = {
            "formal_qem_decimation": False,
            "grid": {
                "method": "regular_raster_resample",
                "qem_decimation": False,
                "max_surface_edge_mm": 0.84,
            },
        }
        terrain_evidence = build_terrain_evidence(terrain)

        out = str(tmp_path / "regular-terrain.3mf")
        export_deepseek_3mf({"terrain": terrain}, out)
        spec = build_design_spec(
            city="fixture",
            bbox_wgs84=[1.0, 1.0, 2.0, 2.0],
            artifact_path=out,
            terrain=terrain_evidence,
        )
        write_design_spec(tmp_path, spec)

        result = validate_3mf(out)
        v3 = next(rule for rule in result["rules"] if rule["id"] == "V3")
        v15 = next(rule for rule in result["rules"] if rule["id"] == "V15")
        assert bool(v3["passed"])
        assert bool(v15["passed"])
        assert "over_2mm=0" in v15["detail"]

    def test_qem_terrain_claim_is_a_critical_error(self, tmp_path):
        from _TEXTURE_STYLE_OF_DEEPSEEK.design_spec import (
            build_design_spec,
            write_design_spec,
        )
        from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf

        terrain = trimesh.creation.box(extents=[1.0, 1.0, 0.5])
        out = str(tmp_path / "qem-terrain.3mf")
        export_deepseek_3mf({"terrain": terrain}, out)
        spec = build_design_spec(
            city="fixture",
            bbox_wgs84=[1.0, 1.0, 2.0, 2.0],
            artifact_path=out,
            terrain={
                "artifact_z_span_mm": 0.5,
                "formal_qem_decimation": True,
                "grid": {
                    "method": "qem",
                    "qem_decimation": True,
                    "max_surface_edge_mm": 2.0,
                },
                "surface_mesh": {"max_xy_edge_mm": 1.41421356},
            },
        )
        write_design_spec(tmp_path, spec)

        result = validate_3mf(out)
        v15 = next(rule for rule in result["rules"] if rule["id"] == "V15")
        assert not bool(v15["passed"])
        assert "QEM_off=False" in v15["detail"]
        assert any(error.startswith("V15:") for error in result["errors"])
