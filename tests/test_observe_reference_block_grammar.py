import math

import numpy as np

from tools.observe_reference_block_grammar import (
    _provisional_profile,
    _rotated_geometry_metrics,
    orientation_metrics,
    spatial_metrics,
)
from shapely.affinity import rotate
from shapely.geometry import Polygon, box


def _sample(angle, *, aspect=3.0, area=6.0):
    return {
        "orientation_deg": angle,
        "aspect_ratio": aspect,
        "area_mm2": area,
    }


def test_orientation_metrics_recognize_orthogonal_grid_as_one_system():
    aligned = orientation_metrics([
        _sample(0), _sample(90), _sample(1), _sample(89),
    ])
    mixed = orientation_metrics([
        _sample(0), _sample(17), _sample(39), _sample(71),
    ])

    assert aligned["orthogonal_coherence"] > 0.98
    assert aligned["orientation_entropy_mod_90"] < mixed[
        "orientation_entropy_mod_90"]
    assert mixed["orthogonal_coherence"] < 0.35


def test_rotated_geometry_metrics_are_rotation_invariant():
    original = box(0, 0, 4, 2)
    rotated = rotate(original, 33, origin="centroid")

    before = _rotated_geometry_metrics(original)
    after = _rotated_geometry_metrics(rotated)

    assert math.isclose(before["short_axis_mm"], 2.0, abs_tol=1e-7)
    assert math.isclose(after["short_axis_mm"], 2.0, abs_tol=1e-7)
    assert math.isclose(after["long_axis_mm"], 4.0, abs_tol=1e-7)
    assert math.isclose(after["rectangularity"], 1.0, abs_tol=1e-7)


def test_rotated_geometry_metrics_expose_gnawed_outline():
    gnawed = Polygon([
        (0, 0), (4, 0), (4, 3), (2.4, 3), (2.4, 1.4),
        (1.6, 1.4), (1.6, 3), (0, 3),
    ])

    metrics = _rotated_geometry_metrics(gnawed)

    assert metrics["rectangularity"] < 0.9


def test_spatial_metrics_distinguish_distributed_from_clustered_population():
    distributed = []
    for row in range(8):
        for column in range(8):
            distributed.append({
                "center_x_mm": column * 10 + 5,
                "center_y_mm": row * 10 + 5,
                "top_area_mm2": 4.0,
            })
    clustered = [
        {"center_x_mm": 5 + index % 4, "center_y_mm": 5 + index // 4,
         "top_area_mm2": 4.0}
        for index in range(16)
    ]

    distributed_metrics, _ = spatial_metrics(
        distributed, (0, 0, 80, 80), grid_size=8)
    clustered_metrics, _ = spatial_metrics(
        clustered, (0, 0, 80, 80), grid_size=8)

    assert distributed_metrics["occupied_cell_fraction"] == 1.0
    assert clustered_metrics["occupied_cell_fraction"] < 0.1
    assert distributed_metrics["cell_load_entropy"] > clustered_metrics[
        "cell_load_entropy"]


def test_provisional_profile_is_explainable_and_non_authoritative():
    shape = {
        "orientation": {"orthogonal_coherence": 0.8},
        "metrics": {
            "solidity": {"p50": 0.99},
            "rectangularity": {"p50": 0.9},
        },
    }
    spatial = {
        "occupied_cell_fraction": 0.8,
        "largest_occupied_cluster_fraction": 0.95,
    }

    profile = _provisional_profile(shape, spatial)

    assert profile["label"] == "continuous_orthogonal_urban_carpet"
    assert profile["compact_silhouette_supported"] is True
    assert profile["not_a_generation_instruction"] is True
