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


def test_chicago_route_approaches_core_then_rises():
    bounds = ((-98.425, -97.04, -2.0), (98.0, 97.001, 8.675))
    route = MODULE.build_camera_route(bounds)
    assert [progress for progress, _, _ in route] == [0.0, 0.24, 0.5, 0.74, 1.0]
    camera_z = [camera[2] for _, camera, _ in route]
    assert camera_z[2] == min(camera_z)
    assert camera_z[-1] > camera_z[2]
    # The flyover starts over the east/lake side and travels westward.
    camera_x = [camera[0] for _, camera, _ in route]
    assert camera_x == sorted(camera_x, reverse=True)


def test_route_rejects_invalid_bounds_and_focus():
    with pytest.raises(ValueError):
        MODULE.build_camera_route(((0, 0, 0), (0, 1, 1)))
    with pytest.raises(ValueError):
        MODULE.build_camera_route(((0, 0, 0), (1, 1, 1)), focus_x_frac=1.1)
