import numpy as np
from PIL import Image

from aesthetic.amap_salience import (
    AmapSalienceGuide,
    SalienceMasks,
    build_amap_salience_guide,
    compare_salience_masks,
    crop_mosaic_to_wgs84_bbox,
    extract_amap_salience_masks,
    extract_review_salience_masks,
)
from shapely.geometry import LineString


def test_style7_palette_separates_road_tiers_and_transit_colours():
    image = np.full((128, 128, 3), (252, 249, 242), dtype=np.uint8)
    image[10:18, 5:123] = (246, 128, 37)
    image[35:41, 5:123] = (225, 173, 4)
    image[60:64, 5:123] = (246, 227, 163)
    image[80:84, 5:123] = (23, 190, 176)  # metro colour, not a road tier
    image[95:125, 15:55] = (163, 204, 255)

    masks = extract_amap_salience_masks(
        image, min_road_component_pixels=4)

    assert masks.road_major[14, 30]
    assert masks.road_arterial[38, 30]
    assert masks.road_context[62, 30]
    assert not masks.road_all[82, 30]
    assert masks.water[105, 30]


def test_review_adapter_keeps_black_water_separate_from_dark_gray_roads():
    image = np.full((64, 64, 3), 247, dtype=np.uint8)
    image[10:20] = (0, 0, 0)
    image[30:35] = (74, 74, 74)
    image[45:48] = (138, 138, 138)

    roads, water = extract_review_salience_masks(Image.fromarray(image))

    assert water[15, 20] and not roads[15, 20]
    assert roads[32, 20] and not water[32, 20]
    assert not roads[46, 20]


def test_comparison_reports_missing_major_corridor_without_single_score():
    major = np.zeros((100, 100), dtype=bool)
    major[48:52, 10:90] = True
    water = np.zeros_like(major)
    water[10:35, 10:35] = True
    reference = SalienceMasks(
        water=water,
        road_major=major,
        road_arterial=np.zeros_like(major),
        road_context=np.zeros_like(major),
        evidence={},
    )
    candidate_roads = np.zeros_like(major)
    candidate_roads[48:52, 10:45] = True
    candidate_water = water.copy()

    report = compare_salience_masks(
        reference, candidate_roads, candidate_water, tolerance_px=1)

    assert 0.4 < report["roads"]["major"]["recall_with_tolerance"] < 0.7
    assert report["water"]["recall_with_tolerance"] == 1.0
    assert "overall_score" not in report


def test_gcj_crop_returns_exact_requested_output_size():
    mosaic = np.zeros((512, 512, 3), dtype=np.uint8)
    mosaic[:, :, 0] = np.arange(512, dtype=np.uint16)[None, :] % 256
    cropped, evidence = crop_mosaic_to_wgs84_bbox(
        mosaic,
        (39.7, 116.1, 40.2, 116.8),
        (39.8, 116.2, 40.0, 116.5),
        output_size=128,
    )

    assert cropped.shape == (128, 128, 3)
    assert evidence["coordinate_method"].startswith("WGS84")


def test_projected_line_guide_scores_only_supported_corridor():
    shape = (100, 100)
    major = np.zeros(shape, dtype=bool)
    major[49:52, 5:95] = True
    reference = SalienceMasks(
        water=np.zeros(shape, dtype=bool),
        road_major=major,
        road_arterial=np.zeros(shape, dtype=bool),
        road_context=np.zeros(shape, dtype=bool),
        evidence={},
    )
    guide = AmapSalienceGuide(
        reference, (0.0, 0.0, 10_000.0, 10_000.0), tolerance_px=1)

    supported = guide.road_support(
        LineString([(500, 5000), (9500, 5000)]))
    unsupported = guide.road_support(
        LineString([(500, 8000), (9500, 8000)]))

    assert supported["weighted_salience"] > 0.95
    assert supported["major_mask_fraction"] > 0.95
    assert supported["any_template_fraction"] > 0.95
    assert unsupported["weighted_salience"] == 0.0
    assert unsupported["any_template_fraction"] == 0.0


def test_guide_is_explicitly_not_applicable_outside_mainland_china():
    guide, evidence = build_amap_salience_guide(
        (41.80, -87.75, 41.95, -87.55),
        (0, 0, 10000, 10000),
        allow_network=False,
    )

    assert guide is None
    assert evidence["status"] == "not_applicable"
    assert "mainland-China-only" in evidence["reason"]
