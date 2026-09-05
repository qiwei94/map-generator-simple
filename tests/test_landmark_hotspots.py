from shapely.geometry import box

from _TEXTURE_STYLE_OF_DEEPSEEK._landmark import compute_hotspot_block_ids


def test_hotspot_density_uses_batched_building_assignment():
    blocks = [box(0, 0, 10, 10), box(10, 0, 20, 10)]
    buildings = [
        box(1, 1, 3, 3),
        box(6, 6, 8, 8),
        box(12, 2, 18, 8),
        box(30, 30, 31, 31),
        # The centroid lies on the shared edge and is excluded by the same
        # strict-within semantics used before vectorisation.
        box(9, 4, 11, 6),
    ]

    assert compute_hotspot_block_ids(
        blocks, buildings, top_percent=50.0) == {1}


def test_hotspot_density_handles_empty_inputs():
    assert compute_hotspot_block_ids([], [], top_percent=10.0) == set()
    assert compute_hotspot_block_ids(
        [box(0, 0, 1, 1)], [], top_percent=10.0) == set()
