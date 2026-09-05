from types import SimpleNamespace

from PIL import Image
from shapely.geometry import LineString, box

from aesthetic.review_render import render_review_bundle


def _water_only_layers():
    return SimpleNamespace(
        WL=[box(20.0, 20.0, 80.0, 80.0)],
        WO=[],
        BO=[],
        BL=[],
        block_base=[],
        VL=[],
        VO=[],
        roads_lines=[],
    )


def test_landscape_water_is_rendered_pure_black(tmp_path):
    bundle = render_review_bundle(
        _water_only_layers(),
        {"bbox_local": (0.0, 0.0, 100.0, 100.0)},
        road_width_multiplier=2.0,
        out_dir=str(tmp_path),
        tag="landscape",
        scene_type="water_landscape",
    )

    with Image.open(bundle["topdown"]) as image:
        assert image.convert("RGB").getpixel((image.width // 2,
                                               image.height // 2)) == (0, 0, 0)


def test_review_vegetation_is_opt_in(tmp_path):
    layers = _water_only_layers()
    layers.WL = []
    layers.VL = [box(20.0, 20.0, 80.0, 80.0)]

    disabled = render_review_bundle(
        layers, {"bbox_local": (0.0, 0.0, 100.0, 100.0)}, 1.0,
        str(tmp_path), "vegetation-off")
    enabled = render_review_bundle(
        layers, {"bbox_local": (0.0, 0.0, 100.0, 100.0)}, 1.0,
        str(tmp_path), "vegetation-on", vegetation_enabled=True)

    assert disabled["veg_mask"].max() == 0.0
    assert enabled["veg_mask"].max() == 1.0


def test_review_uses_complete_topology_as_visible_street_texture(tmp_path):
    layers = _water_only_layers()
    layers.block_base = [box(0.0, 0.0, 100.0, 100.0)]
    layers.block_base_cut_lines = [LineString([(10.0, 50.0),
                                               (90.0, 50.0)])]
    layers.block_base_major_cut_lines = []
    layers.road_roles = {"width_policy": {
        "surface_road_gap_mm": 0.55,
        "major_road_gap_mm": 0.84,
    }}

    bundle = render_review_bundle(
        layers,
        {"bbox_local": (0.0, 0.0, 100.0, 100.0), "scale": 1.0},
        road_width_multiplier=1.0,
        out_dir=str(tmp_path),
        tag="topology-road",
    )

    with Image.open(bundle["topdown"]) as image:
        center = image.convert("RGB").getpixel(
            (image.width // 2, image.height // 2))
    assert center[0] < 200
    assert bundle["road_mask"].max() == 1.0
