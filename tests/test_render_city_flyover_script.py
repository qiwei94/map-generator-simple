"""Pure-Python checks for the Blender flyover route helper."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "tools" / "render_city_flyover.py"
SPEC = importlib.util.spec_from_file_location("render_city_flyover", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_parse_resolution():
    assert MODULE.parse_resolution("1920x1080") == (1920, 1080)
    with pytest.raises(Exception):
        MODULE.parse_resolution("tiny")
    with pytest.raises(Exception):
        MODULE.parse_resolution("100x100")


def test_default_camera_is_reviewed_whole_city_overview():
    args = MODULE._parse_args(["--input", "city.glb", "--output", "flyover.mp4"])
    assert args.route_style == "coast"
    assert args.lens_mm == pytest.approx(35.0)
    assert args.view_pitch_deg == pytest.approx(30.0)
    assert args.coast_distance_scale == pytest.approx(2.90)
    assert args.motion_samples == 33
    assert args.depth_of_field is False


def test_legacy_focus_route_approaches_core_then_rises():
    bounds = ((-98.425, -97.04, -2.0), (98.0, 97.001, 8.675))
    route = MODULE.build_focus_route(bounds)
    assert [progress for progress, _, _ in route] == [0.0, 0.24, 0.5, 0.74, 1.0]
    camera_z = [camera[2] for _, camera, _ in route]
    assert camera_z[2] == min(camera_z)
    assert camera_z[-1] > camera_z[2]
    # The flyover starts over the east/lake side and travels westward.
    camera_x = [camera[0] for _, camera, _ in route]
    assert camera_x == sorted(camera_x, reverse=True)


def test_route_rejects_invalid_bounds_and_focus():
    with pytest.raises(ValueError):
        MODULE.build_focus_route(((0, 0, 0), (0, 1, 1)))
    with pytest.raises(ValueError):
        MODULE.build_focus_route(((0, 0, 0), (1, 1, 1)), focus_x_frac=1.1)


def test_coast_route_is_oblique_and_traverses_full_frame():
    bounds = ((-100.0, -100.0, -2.0), (100.0, 100.0, 8.0))
    route = MODULE.build_coast_route(bounds, side="east")
    assert [progress for progress, _, _ in route] == [0.0, 0.22, 0.5, 0.78, 1.0]
    camera_y = [camera[1] for _, camera, _ in route]
    assert camera_y == sorted(camera_y)
    assert camera_y[-1] - camera_y[0] > 120
    pitches = []
    for _, camera, target in route:
        horizontal = ((camera[0] - target[0]) ** 2 + (camera[1] - target[1]) ** 2) ** 0.5
        pitches.append(
            MODULE.math.degrees(MODULE.math.atan2(camera[2] - target[2], horizontal))
        )
    assert pitches[2] == pytest.approx(30.0)
    assert all(29.9 <= pitch <= 32.0 for pitch in pitches)
    assert all(camera[0] > target[0] for _, camera, target in route)
    # The camera travels along the shore with a constant forward lead.  This
    # keeps the coastline diagonal stable instead of swinging into an orbit.
    forward_lead = [target[1] - camera[1] for _, camera, target in route]
    assert forward_lead == pytest.approx([30.0, 30.0, 30.0, 30.0, 24.0])
    # Every gaze vector has a positive projection on the next movement vector:
    # the camera looks forward-left toward the city instead of flying backward.
    for index in range(len(route) - 1):
        camera = route[index][1]
        next_camera = route[index + 1][1]
        target = route[index][2]
        movement = (next_camera[0] - camera[0], next_camera[1] - camera[1])
        gaze = (target[0] - camera[0], target[1] - camera[1])
        assert movement[0] * gaze[0] + movement[1] * gaze[1] > 0


def test_coast_route_rejects_extreme_pitch():
    bounds = ((-100.0, -100.0, -2.0), (100.0, 100.0, 8.0))
    with pytest.raises(ValueError):
        MODULE.build_coast_route(bounds, view_pitch_deg=20.0)


def test_coast_distance_scale_pulls_back_without_changing_target_or_pitch():
    bounds = ((-100.0, -100.0, -2.0), (100.0, 100.0, 8.0))
    near = MODULE.build_coast_route(bounds)
    for scale in (1.45, 2.90):
        far = MODULE.build_coast_route(bounds, distance_scale=scale)
        for near_key, far_key in zip(near, far):
            _, near_camera, near_target = near_key
            _, far_camera, far_target = far_key
            assert far_target == near_target
            near_distance = MODULE.math.dist(near_camera[:2], near_target[:2])
            far_distance = MODULE.math.dist(far_camera[:2], far_target[:2])
            assert far_distance == pytest.approx(near_distance * scale)
            near_pitch = MODULE.math.atan2(
                near_camera[2] - near_target[2], near_distance
            )
            far_pitch = MODULE.math.atan2(
                far_camera[2] - far_target[2], far_distance
            )
            assert far_pitch == pytest.approx(near_pitch)


def test_helicopter_route_is_constant_speed_and_constant_pitch():
    bounds = ((-100.0, -100.0, -2.0), (100.0, 100.0, 8.0))
    route = MODULE.build_helicopter_route(
        MODULE.build_coast_route(bounds, distance_scale=2.90),
        samples=33,
        view_pitch_deg=30.0,
    )
    assert len(route) == 33
    assert route[0][0] == 0.0
    assert route[-1][0] == 1.0
    distances = [
        MODULE.math.dist(route[index - 1][1], route[index][1])
        for index in range(1, len(route))
    ]
    assert max(distances) / min(distances) < 1.01
    for _progress, camera, target in route:
        horizontal = MODULE.math.hypot(
            camera[0] - target[0], camera[1] - target[1]
        )
        pitch = MODULE.math.degrees(
            MODULE.math.atan2(camera[2] - target[2], horizontal)
        )
        assert pitch == pytest.approx(30.0)


def test_water_extension_covers_oblique_helicopter_frustum():
    bounds = ((-100.0, -100.0, -2.0), (100.0, 100.0, 8.0))
    route = MODULE.build_helicopter_route(
        MODULE.build_coast_route(bounds, distance_scale=2.90),
        samples=33,
        view_pitch_deg=30.0,
    )
    padding = MODULE.coast_water_extension_padding(
        bounds, route, lens_mm=35.0, aspect_ratio=16 / 9
    )
    assert padding >= 1000.0


def test_coast_water_apron_is_one_shared_vertex_ring_with_central_hole():
    bounds = ((0.0, 0.0, -2.0), (100.0, 80.0, 8.0))
    vertices, faces = MODULE.coast_water_apron_geometry(
        bounds, 40.0, 1.25, overlap=2.0
    )

    assert vertices == [
        (-40.0, -40.0, 1.25),
        (140.0, -40.0, 1.25),
        (140.0, 120.0, 1.25),
        (-40.0, 120.0, 1.25),
        (2.0, 2.0, 1.25),
        (98.0, 2.0, 1.25),
        (98.0, 78.0, 1.25),
        (2.0, 78.0, 1.25),
    ]
    assert faces == [
        (0, 1, 5, 4),
        (1, 2, 6, 5),
        (2, 3, 7, 6),
        (3, 0, 4, 7),
    ]
    with pytest.raises(ValueError):
        MODULE.coast_water_apron_geometry(bounds, 40.0, 1.25, overlap=41.0)


def test_coast_route_can_rotate_and_reverse():
    bounds = ((0.0, 0.0, 0.0), (200.0, 100.0, 10.0))
    north = MODULE.build_coast_route(bounds, side="north")
    assert all(camera[1] > target[1] for _, camera, target in north)
    reverse = MODULE.build_coast_route(bounds, side="east", reverse=True)
    assert reverse[0][1] == MODULE.build_coast_route(bounds)[-1][1]


def test_parse_and_build_reviewed_structure_path():
    points = MODULE.parse_route_points("0.1,0.2;0.4,0.5;0.8,0.9")
    route = MODULE.build_path_route(((0, 0, 0), (200, 100, 10)), points)
    assert len(route) == 3
    assert route[0][0] == 0
    assert route[-1][0] == 1
    assert route[0][1][:2] == pytest.approx((20, 20))
    with pytest.raises(Exception):
        MODULE.parse_route_points("0.1,0.2;1.1,0.5")
