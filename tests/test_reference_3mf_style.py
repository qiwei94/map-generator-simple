import json
import zipfile

from tools.analyze_reference_3mf_style import analyze_reference_3mf


def _object_xml(object_id, *, span=4.0, height=0.8):
    return f"""
    <object id="{object_id}" type="model"><mesh><vertices>
      <vertex x="0" y="0" z="0"/>
      <vertex x="{span}" y="0" z="0"/>
      <vertex x="0" y="{span}" z="{height}"/>
    </vertices><triangles><triangle v1="0" v2="1" v3="2"/>
    </triangles></mesh></object>"""


def test_reference_audit_reads_bambu_parts_and_material_roles(tmp_path):
    path = tmp_path / "sample.3mf"
    project = {
        "nozzle_diameter": ["0.4"],
        "layer_height": "0.16",
        "line_width": "0.42",
        "outer_wall_line_width": "0.42",
        "wall_loops": "2",
        "detect_thin_wall": "0",
        "filament_colour": ["#FFFFFF", "#8E9089", "#000000"],
    }
    model_settings = """<config><object id="5">
      <part id="1"><metadata key="name" value="quiet"/><metadata key="extruder" value="1"/></part>
      <part id="3"><metadata key="name" value="mass"/><metadata key="extruder" value="1"/></part>
      <part id="4"><metadata key="name" value="substrate"/><metadata key="extruder" value="2"/></part>
      <part id="6"><metadata key="name" value="backing"/><metadata key="extruder" value="3"/></part>
    </object></config>"""
    object_model = """<?xml version="1.0"?>
    <model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
    <resources>{}</resources></model>""".format("".join((
        _object_xml(1, height=0.24),
        _object_xml(3, height=0.8),
        _object_xml(4, span=200.0, height=1.0),
        _object_xml(6, span=200.0, height=1.2),
    )))
    build_model = """<?xml version="1.0"?>
    <model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
      <resources/><build><item objectid="5"
      transform="1 0 0 0 1 0 0 0 1 0 0 0"/></build>
    </model>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Metadata/project_settings.config", json.dumps(project))
        archive.writestr("Metadata/model_settings.config", model_settings)
        archive.writestr("3D/Objects/object_1.model", object_model)
        archive.writestr("3D/3dmodel.model", build_model)

    report = analyze_reference_3mf(path)

    assert [volume["material_role"] for volume in report["volumes"]] == [
        "white_relief", "white_relief", "neutral_substrate",
        "black_negative_backing",
    ]
    assert report["summary"]["white_relief_object_height_p50_tiers_mm"] == [
        0.24, 0.8]
    assert report["summary"]["black_full_frame_backing_detected"] is True
    assert report["summary"]["thin_wall_rescue_disabled"] is True
