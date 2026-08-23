from types import SimpleNamespace

from PIL import Image
from shapely.geometry import box

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
