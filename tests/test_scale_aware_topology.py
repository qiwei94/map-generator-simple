import pytest
from shapely.geometry import LineString, box

from aesthetic.scale_aware_topology import (
    _building_counts,
    coarsen_city_blocks_for_print,
)


MODEL_SPAN_MM = 196.0


def _fixture():
    blocks = [box(x, 0, x + 200, 600) for x in (0, 200, 400, 600)]
    buildings = [box(x + 50, 100, x + 150, 250)
                 for x in (0, 200, 400, 600)]
    cuts = [
        LineString([(200, 0), (200, 600)]),
        LineString([(400, 0), (400, 600)]),
        LineString([(600, 0), (600, 600)]),
    ]
    return blocks, buildings, cuts


def _run(crop_km, *, protected=()):
    blocks, buildings, cuts = _fixture()
    return coarsen_city_blocks_for_print(
        blocks,
        buildings=buildings,
        cut_lines=cuts,
        protected_cut_lines=protected,
        scale_mm_per_m=MODEL_SPAN_MM / (crop_km * 1000.0),
        target_min_model_mm=1.30,
        hard_floor_model_mm=0.63,
        boundary_inset_model_mm=0.42,
    )


def test_building_counts_query_blocks_against_centroid_index():
    blocks = [box(0, 0, 10, 10), box(10, 0, 20, 10)]
    buildings = [
        box(1, 1, 2, 2),
        box(7, 7, 9, 9),
        box(14, 4, 16, 6),
        # Centroid lies exactly on the shared boundary and ``within`` must
        # continue to exclude it after reversing the spatial query direction.
        box(9, 4, 11, 6),
    ]

    assert _building_counts(blocks, buildings) == [2, 1]


def test_same_real_blocks_stay_separate_at_small_crop():
    final_blocks, retained, evidence = _run(15.0)

    assert len(final_blocks) == 4
    assert len(retained) == 3
    assert evidence["merged_blocks"] == 0
    assert evidence["status"] == "met"
    assert evidence["before"][
        "occupied_core_short_axis_p50_model_mm"] == pytest.approx(
            1.77333, abs=1e-5)


def test_wider_crop_dissolves_low_order_cuts_until_median_is_printable():
    _blocks, _buildings, cuts = _fixture()
    final_blocks, retained, evidence = _run(25.0, protected=[cuts[1]])

    assert len(final_blocks) == 2
    assert evidence["merged_blocks"] == 2
    assert evidence["status"] == "met"
    assert evidence["before"][
        "occupied_core_short_axis_p50_model_mm"] < 1.30
    assert evidence["after"][
        "occupied_core_short_axis_p50_model_mm"] >= 1.30
    # The major x=400 separator survives; x=200 and x=600 become internal.
    assert sum(line.length for line in retained) == pytest.approx(600, abs=5)
    assert evidence["retained_cut_fraction"] == pytest.approx(
        1.0 / 3.0, abs=0.02)


def test_protected_roads_are_not_dissolved_even_below_target():
    _blocks, _buildings, cuts = _fixture()
    final_blocks, retained, evidence = _run(25.0, protected=cuts)

    assert len(final_blocks) == 4
    assert len(retained) == 3
    assert evidence["merged_blocks"] == 0
    assert evidence["status"] == "below_target"
    assert evidence["stop_reason"] == "protected_or_disconnected_topology"
    assert evidence["protected_edge_rejections"] > 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("scale_mm_per_m", 0.0),
        ("target_min_model_mm", 0.0),
        ("hard_floor_model_mm", 0.0),
        ("boundary_inset_model_mm", -0.1),
    ],
)
def test_topology_parameters_are_validated(field, value):
    blocks, buildings, cuts = _fixture()
    kwargs = {
        "buildings": buildings,
        "cut_lines": cuts,
        "scale_mm_per_m": MODEL_SPAN_MM / 25_000.0,
        "target_min_model_mm": 1.3,
        "hard_floor_model_mm": 0.63,
        "boundary_inset_model_mm": 0.42,
    }
    kwargs[field] = value
    with pytest.raises(ValueError):
        coarsen_city_blocks_for_print(blocks, **kwargs)
