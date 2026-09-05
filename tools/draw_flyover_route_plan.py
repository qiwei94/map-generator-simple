#!/usr/bin/env python3
"""Draw a normalized flyover route and view directions over a top-down PNG."""

from __future__ import annotations

import argparse
import math
from pathlib import Path
from typing import Sequence

from PIL import Image, ImageDraw, ImageFont


Point = tuple[float, float]


def parse_points(value: str) -> list[Point]:
    try:
        points = []
        for item in value.split(";"):
            x_text, y_text = item.split(",", 1)
            point = float(x_text), float(y_text)
            if not all(0.0 <= coordinate <= 1.0 for coordinate in point):
                raise ValueError
            points.append(point)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "points must look like 0.1,0.2;0.4,0.5;0.8,0.9"
        ) from exc
    if len(points) < 2:
        raise argparse.ArgumentTypeError("at least two points are required")
    return points


def _font(size: int):
    candidates = (
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _pixel(point: Point, width: int, height: int) -> tuple[float, float]:
    """Map model-normalized XY to a north-up top-down image."""

    return point[0] * width, (1.0 - point[1]) * height


def _arrow(draw: ImageDraw.ImageDraw, start, end, color, width: int):
    draw.line((start, end), fill=color, width=width)
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = math.hypot(dx, dy)
    if length <= 1.0:
        return
    ux, uy = dx / length, dy / length
    head = max(width * 3.0, 18.0)
    wing = head * 0.48
    base_x, base_y = end[0] - ux * head, end[1] - uy * head
    left = base_x - uy * wing, base_y + ux * wing
    right = base_x + uy * wing, base_y - ux * wing
    draw.polygon((end, left, right), fill=color)


def _view_cone(camera, target, half_angle_deg: float, length: float):
    dx, dy = target[0] - camera[0], target[1] - camera[1]
    norm = math.hypot(dx, dy)
    ux, uy = dx / norm, dy / norm
    angle = math.radians(half_angle_deg)
    directions = []
    for sign in (-1.0, 1.0):
        cosine, sine = math.cos(sign * angle), math.sin(sign * angle)
        directions.append((ux * cosine - uy * sine, ux * sine + uy * cosine))
    return (
        camera,
        (camera[0] + directions[0][0] * length, camera[1] + directions[0][1] * length),
        (camera[0] + directions[1][0] * length, camera[1] + directions[1][1] * length),
    )


def draw_plan(
    source: Path,
    output: Path,
    camera_points: Sequence[Point],
    target_points: Sequence[Point],
    *,
    horizontal_fov_deg: float,
    view_pitch_deg: float,
):
    if len(camera_points) != len(target_points):
        raise ValueError("camera and target point counts must match")
    image = Image.open(source).convert("RGBA")
    width, height = image.size
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    cameras = [_pixel(point, width, height) for point in camera_points]
    targets = [_pixel(point, width, height) for point in target_points]
    cyan = (0, 210, 255, 255)
    orange = (255, 150, 40, 255)

    # Three representative camera fields of view make water/city coverage
    # legible without turning the route plan into an unreadable fan diagram.
    cone_length = min(width, height) * 0.34
    for index in (0, len(cameras) // 2, len(cameras) - 1):
        cone = _view_cone(
            cameras[index], targets[index], horizontal_fov_deg / 2.0, cone_length
        )
        draw.polygon(cone, fill=(255, 190, 60, 42), outline=(255, 180, 55, 120))

    path_width = max(7, width // 210)
    for start, end in zip(cameras, cameras[1:]):
        _arrow(draw, start, end, cyan, path_width)

    gaze_width = max(4, width // 380)
    dash_count = 12
    for camera, target in zip(cameras, targets):
        for dash in range(0, dash_count, 2):
            start_fraction = dash / dash_count
            end_fraction = min((dash + 1) / dash_count, 1.0)
            start = (
                camera[0] + (target[0] - camera[0]) * start_fraction,
                camera[1] + (target[1] - camera[1]) * start_fraction,
            )
            end = (
                camera[0] + (target[0] - camera[0]) * end_fraction,
                camera[1] + (target[1] - camera[1]) * end_fraction,
            )
            draw.line((start, end), fill=orange, width=gaze_width)

    label_font = _font(max(24, width // 64))
    small_font = _font(max(18, width // 88))
    radius = max(13, width // 100)
    for index, (camera, target) in enumerate(zip(cameras, targets), start=1):
        draw.ellipse(
            (
                camera[0] - radius,
                camera[1] - radius,
                camera[0] + radius,
                camera[1] + radius,
            ),
            fill=(0, 35, 46, 245),
            outline=cyan,
            width=max(3, radius // 4),
        )
        draw.text(
            (camera[0] - radius * 0.34, camera[1] - radius * 0.70),
            str(index),
            font=label_font,
            fill=(255, 255, 255, 255),
        )
        cross = radius * 0.65
        draw.line(
            ((target[0] - cross, target[1]), (target[0] + cross, target[1])),
            fill=orange,
            width=max(3, gaze_width),
        )
        draw.line(
            ((target[0], target[1] - cross), (target[0], target[1] + cross)),
            fill=orange,
            width=max(3, gaze_width),
        )

    panel_width = int(width * 0.60)
    panel_height = int(height * 0.135)
    padding = int(width * 0.018)
    draw.rounded_rectangle(
        (padding, padding, padding + panel_width, padding + panel_height),
        radius=18,
        fill=(7, 12, 16, 220),
        outline=(255, 255, 255, 100),
        width=2,
    )
    draw.text(
        (padding * 1.7, padding * 1.45),
        "CHICAGO 25 KM  /  COAST FLYOVER ROUTE V2",
        font=label_font,
        fill=(255, 255, 255, 255),
    )
    lines = (
        "CYAN: camera path 1 -> 5, south to north (forward flight)",
        "ORANGE: gaze direction; translucent cones: approx. horizontal view",
        f"Camera remains over Lake Michigan  |  view pitch {view_pitch_deg:.0f} deg",
    )
    for line_index, line in enumerate(lines):
        draw.text(
            (padding * 1.7, padding * 3.2 + line_index * small_font.size * 1.28),
            line,
            font=small_font,
            fill=(225, 232, 235, 255),
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    Image.alpha_composite(image, overlay).convert("RGB").save(output, quality=96)
    print(f"ROUTE_PLAN path={output} size={width}x{height}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--camera-points", required=True, type=parse_points)
    parser.add_argument("--target-points", required=True, type=parse_points)
    parser.add_argument("--horizontal-fov-deg", type=float, default=55.0)
    parser.add_argument("--view-pitch-deg", type=float, default=30.0)
    args = parser.parse_args()
    draw_plan(
        args.input,
        args.output,
        args.camera_points,
        args.target_points,
        horizontal_fov_deg=args.horizontal_fov_deg,
        view_pitch_deg=args.view_pitch_deg,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
