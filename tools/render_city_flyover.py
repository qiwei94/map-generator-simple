#!/usr/bin/env python3
"""Render a short cinematic flyover from an existing city GLB.

Run this file with Blender rather than the project Python interpreter::

    blender --background --python tools/render_city_flyover.py -- \
      --input city.glb --output city_flyover.mp4 \
      --stills-dir city_flyover_stills

The script changes only camera, lighting, and render state.  It deliberately
does not edit the map mesh, Z values, booleans, or printable geometry.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import Iterable, Sequence


Bounds = tuple[tuple[float, float, float], tuple[float, float, float]]
RouteKey = tuple[float, tuple[float, float, float], tuple[float, float, float]]
BACKGROUND_COLOR = (0.018, 0.022, 0.030)
BACKGROUND_STRENGTH = 0.42
WATER_COLOR = (0.035, 0.095, 0.120)
WATER_STRENGTH = 0.65
APPROVED_OVERVIEW_LENS_MM = 35.0
APPROVED_OVERVIEW_PITCH_DEG = 30.0
APPROVED_OVERVIEW_DISTANCE_SCALE = 2.90
APPROVED_OVERVIEW_MOTION_SAMPLES = 33

LAYER_STYLES = {
    "terrain": ((0.78, 0.74, 0.66), 0.88, 0.00),
    "block_base": ((0.46, 0.45, 0.43), 0.92, 0.00),
    "vegetation": ((0.29, 0.38, 0.30), 0.86, 0.00),
    # Keep water matte at overview altitude.  A glossy/metallic material makes
    # the triangulated draped preview surface read as white radial streaks.
    "water": ((0.018, 0.025, 0.040), 0.72, 0.00),
    "roads": ((0.105, 0.105, 0.120), 0.72, 0.00),
    "buildings": ((0.78, 0.76, 0.72), 0.78, 0.00),
    "landmarks": ((0.65, 0.50, 0.34), 0.60, 0.02),
}


def parse_resolution(value: str) -> tuple[int, int]:
    """Parse WIDTHxHEIGHT while keeping CLI errors human-readable."""

    try:
        width_text, height_text = value.lower().split("x", 1)
        width, height = int(width_text), int(height_text)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("resolution must look like 1280x720") from exc
    if width < 320 or height < 180:
        raise argparse.ArgumentTypeError("resolution must be at least 320x180")
    return width, height


def build_focus_route(
    bounds: Bounds,
    *,
    focus_x_frac: float = 0.762,
    focus_y_frac: float = 0.522,
) -> list[RouteKey]:
    """Return a normalized five-key city-identity route.

    Fractions locate the visual focus within the model bounds.  For Chicago
    the default is the Loop / Chicago River area: east of the model centre and
    close to its north-south midpoint.  Camera coordinates are derived from
    the actual GLB bounds, so the same route grammar can be reused elsewhere.
    """

    (xmin, ymin, zmin), (xmax, ymax, zmax) = bounds
    if not (0.0 <= focus_x_frac <= 1.0 and 0.0 <= focus_y_frac <= 1.0):
        raise ValueError("focus fractions must be between 0 and 1")
    sx, sy = xmax - xmin, ymax - ymin
    if sx <= 0.0 or sy <= 0.0:
        raise ValueError("model bounds must have positive X and Y spans")
    span = max(sx, sy)
    fx = xmin + sx * focus_x_frac
    fy = ymin + sy * focus_y_frac
    surface = zmin + (zmax - zmin) * 0.40

    def camera(xf: float, yf: float, zf: float) -> tuple[float, float, float]:
        return xmin + sx * xf, ymin + sy * yf, zmax + span * zf

    def target(x_offset: float, y_offset: float, z_offset: float = 0.0):
        return fx + sx * x_offset, fy + sy * y_offset, surface + z_offset

    # Begin above Lake Michigan, approach the Loop, follow the Chicago River
    # westward, then rise into a closing city-and-shoreline composition.
    return [
        (0.00, camera(0.96, 0.28, 0.160), target(0.00, 0.00, 0.7)),
        (0.24, camera(0.92, 0.37, 0.140), target(-0.01, 0.01, 0.5)),
        (0.50, camera(0.86, 0.45, 0.125), target(-0.02, 0.04, 0.2)),
        (0.74, camera(0.72, 0.56, 0.140), target(-0.12, 0.08, 0.0)),
        (1.00, camera(0.51, 0.69, 0.240), target(-0.03, 0.03, -0.3)),
    ]


def _side_transform(
    point: tuple[float, float], side: str
) -> tuple[float, float]:
    """Rotate/mirror an east-coast normalized point onto another frame side."""

    x, y = point
    transforms = {
        "east": (x, y),
        "west": (1.0 - x, y),
        "north": (y, x),
        "south": (y, 1.0 - x),
    }
    try:
        return transforms[side]
    except KeyError as exc:
        raise ValueError("side must be east, west, north, or south") from exc


def build_coast_route(
    bounds: Bounds,
    *,
    side: str = "east",
    reverse: bool = False,
    view_pitch_deg: float = 30.0,
    distance_scale: float = 1.0,
) -> list[RouteKey]:
    """Traverse a full coastal divider from an oblique structural viewpoint.

    The canonical route travels south-to-north over the water beside an east
    coast while looking inland. ``view_pitch_deg`` is the downward angle from
    horizontal, so a value around 30 degrees reveals building height without
    turning the flight into a close landmark orbit.
    """

    (xmin, ymin, zmin), (xmax, ymax, zmax) = bounds
    sx, sy = xmax - xmin, ymax - ymin
    if sx <= 0.0 or sy <= 0.0:
        raise ValueError("model bounds must have positive X and Y spans")
    if not 25.0 <= view_pitch_deg <= 60.0:
        raise ValueError("view pitch must be between 25 and 60 degrees")
    if not 0.75 <= distance_scale <= 3.5:
        raise ValueError("coast distance scale must be between 0.75 and 3.5")
    surface = zmin + (zmax - zmin) * 0.32
    progress = [0.0, 0.22, 0.50, 0.78, 1.0]
    # Fly beyond the water-side frame edge and look well inland.  The larger
    # horizontal camera-target separation lets the requested pitch remain
    # genuinely oblique while retaining a whole-city composition.
    camera_xy = [
        (0.975, 0.120),
        (0.965, 0.300),
        (0.945, 0.480),
        (0.915, 0.660),
        (0.875, 0.840),
    ]
    target_xy = [
        (0.820, 0.270),
        (0.790, 0.450),
        (0.740, 0.630),
        (0.690, 0.810),
        (0.630, 0.960),
    ]
    edge_rise = [1.08, 1.04, 1.00, 1.04, 1.08]
    camera_xy = [_side_transform(point, side) for point in camera_xy]
    target_xy = [_side_transform(point, side) for point in target_xy]

    route = []
    for p, (cx, cy), (tx, ty), rise in zip(
        progress, camera_xy, target_xy, edge_rise
    ):
        target_x, target_y = xmin + sx * tx, ymin + sy * ty
        base_camera_x, base_camera_y = xmin + sx * cx, ymin + sy * cy
        camera_x = target_x + (base_camera_x - target_x) * distance_scale
        camera_y = target_y + (base_camera_y - target_y) * distance_scale
        horizontal_distance = math.hypot(camera_x - target_x, camera_y - target_y)
        camera_z = surface + horizontal_distance * math.tan(
            math.radians(view_pitch_deg)
        ) * rise
        route.append(
            (
                p,
                (camera_x, camera_y, camera_z),
                (target_x, target_y, surface),
            )
        )
    if not reverse:
        return route
    reversed_samples = list(reversed(route))
    return [
        (progress[index], camera, target)
        for index, (_old_progress, camera, target) in enumerate(reversed_samples)
    ]


def _hermite_point(
    times: Sequence[float],
    points: Sequence[tuple[float, float, float]],
    value: float,
) -> tuple[float, float, float]:
    """Interpolate a 3D point without Blender's independent curve handles."""

    if len(times) != len(points) or len(points) < 2:
        raise ValueError("Hermite interpolation needs matching route points")
    if value <= times[0]:
        return points[0]
    if value >= times[-1]:
        return points[-1]
    segment = next(
        index for index in range(len(times) - 1) if value <= times[index + 1]
    )
    t0, t1 = times[segment], times[segment + 1]
    local = (value - t0) / (t1 - t0)

    def tangent(index: int) -> tuple[float, float, float]:
        if index == 0:
            left, right = 0, 1
        elif index == len(points) - 1:
            left, right = len(points) - 2, len(points) - 1
        else:
            left, right = index - 1, index + 1
        duration = times[right] - times[left]
        return tuple(
            (points[right][axis] - points[left][axis]) / duration
            for axis in range(3)
        )

    start, end = points[segment], points[segment + 1]
    start_tangent, end_tangent = tangent(segment), tangent(segment + 1)
    h00 = 2 * local**3 - 3 * local**2 + 1
    h10 = local**3 - 2 * local**2 + local
    h01 = -2 * local**3 + 3 * local**2
    h11 = local**3 - local**2
    return tuple(
        h00 * start[axis]
        + h10 * (t1 - t0) * start_tangent[axis]
        + h01 * end[axis]
        + h11 * (t1 - t0) * end_tangent[axis]
        for axis in range(3)
    )


def build_helicopter_route(
    route: Sequence[RouteKey],
    *,
    samples: int = APPROVED_OVERVIEW_MOTION_SAMPLES,
    view_pitch_deg: float | None = None,
) -> list[RouteKey]:
    """Return a smooth, near-constant-speed sightseeing route.

    Blender Bezier handles applied independently to camera and target can make
    an otherwise gentle route yaw and pitch around its five control keys. This
    helper constructs a shared cubic route, resamples it by camera arc length,
    and leaves Blender only a dense linear path with no handle overshoot.
    """

    if len(route) < 3 or samples < len(route):
        raise ValueError("helicopter motion needs at least the source key count")
    times = [key[0] for key in route]
    if times[0] != 0.0 or times[-1] != 1.0 or any(
        right <= left for left, right in zip(times, times[1:])
    ):
        raise ValueError("route progress must increase from zero to one")
    cameras = [key[1] for key in route]
    targets = [key[2] for key in route]
    pitch_radians = None if view_pitch_deg is None else math.radians(view_pitch_deg)

    def evaluate(
        value: float,
    ) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
        camera = _hermite_point(times, cameras, value)
        target = _hermite_point(times, targets, value)
        if pitch_radians is not None:
            horizontal = math.hypot(camera[0] - target[0], camera[1] - target[1])
            camera = (
                camera[0],
                camera[1],
                target[2] + horizontal * math.tan(pitch_radians),
            )
        return camera, target

    dense_count = max(257, samples * 12)
    dense_times = [index / (dense_count - 1) for index in range(dense_count)]
    dense = [evaluate(value) for value in dense_times]
    cumulative = [0.0]
    for index in range(1, dense_count):
        cumulative.append(
            cumulative[-1] + math.dist(dense[index - 1][0], dense[index][0])
        )
    if cumulative[-1] <= 0.0:
        raise ValueError("helicopter route camera path must have positive length")

    result = []
    dense_index = 1
    for sample_index in range(samples):
        progress = sample_index / (samples - 1)
        desired = cumulative[-1] * progress
        while dense_index < dense_count - 1 and cumulative[dense_index] < desired:
            dense_index += 1
        low = dense_index - 1
        segment_length = cumulative[dense_index] - cumulative[low]
        fraction = (
            0.0
            if segment_length == 0
            else (desired - cumulative[low]) / segment_length
        )
        value = dense_times[low] + fraction * (
            dense_times[dense_index] - dense_times[low]
        )
        camera, target = evaluate(value)
        result.append((progress, camera, target))
    return result


def parse_route_points(value: str) -> list[tuple[float, float]]:
    """Parse normalized ``x,y;x,y;...`` points for a river/divider route."""

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
            "route points must look like 0.1,0.2;0.4,0.5;0.8,0.9"
        ) from exc
    if len(points) < 3:
        raise argparse.ArgumentTypeError("a structure route needs at least 3 points")
    return points


def build_path_route(
    bounds: Bounds,
    points: Sequence[tuple[float, float]],
    *,
    reverse: bool = False,
) -> list[RouteKey]:
    """Fly above a reviewed river, coast, ring, or visual divider path."""

    (xmin, ymin, zmin), (xmax, ymax, zmax) = bounds
    sx, sy = xmax - xmin, ymax - ymin
    if sx <= 0.0 or sy <= 0.0:
        raise ValueError("model bounds must have positive X and Y spans")
    if len(points) < 3:
        raise ValueError("a structure route needs at least 3 points")
    if any(not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0) for x, y in points):
        raise ValueError("route point fractions must be between 0 and 1")
    samples = list(reversed(points)) if reverse else list(points)
    span = max(sx, sy)
    surface = zmin + (zmax - zmin) * 0.32
    route = []
    last = len(samples) - 1
    for index, (x, y) in enumerate(samples):
        progress = index / last
        edge_rise = abs(progress - 0.5) * 2.0
        height = 0.70 + 0.12 * edge_rise
        look_index = min(index + 1, last)
        look_x, look_y = samples[look_index]
        if look_index == index and index:
            prev_x, prev_y = samples[index - 1]
            look_x = x + (x - prev_x) * 0.25
            look_y = y + (y - prev_y) * 0.25
        look_x = min(max(look_x, 0.0), 1.0)
        look_y = min(max(look_y, 0.0), 1.0)
        route.append(
            (
                progress,
                (xmin + sx * x, ymin + sy * y, zmax + span * height),
                (xmin + sx * look_x, ymin + sy * look_y, surface),
            )
        )
    return route


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="source GLB")
    parser.add_argument("--output", required=True, type=Path, help="output MP4")
    parser.add_argument(
        "--stills-dir",
        type=Path,
        help="optional directory for opening, midpoint, and closing PNGs",
    )
    parser.add_argument("--blend-output", type=Path, help="optional reusable .blend")
    parser.add_argument("--resolution", type=parse_resolution, default=(1280, 720))
    parser.add_argument("--fps", type=int, default=24)
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--samples", type=int, default=32)
    parser.add_argument(
        "--route-style",
        choices=("coast", "path", "focus"),
        default="coast",
        help="coast/path reveal city structure; focus preserves the legacy landmark orbit",
    )
    parser.add_argument(
        "--coast-side",
        choices=("east", "west", "north", "south"),
        default="east",
        help="frame side occupied by the coast in normalized model coordinates",
    )
    parser.add_argument(
        "--route-points",
        type=parse_route_points,
        help="reviewed normalized path as x,y;x,y;... (required for --route-style path)",
    )
    parser.add_argument("--reverse-route", action="store_true")
    parser.add_argument("--focus-x-frac", type=float, default=0.762)
    parser.add_argument("--focus-y-frac", type=float, default=0.522)
    parser.add_argument(
        "--lens-mm", type=float, default=APPROVED_OVERVIEW_LENS_MM
    )
    parser.add_argument(
        "--view-pitch-deg",
        type=float,
        default=APPROVED_OVERVIEW_PITCH_DEG,
        help="coast-route downward view angle from horizontal (25-60 degrees)",
    )
    parser.add_argument(
        "--coast-distance-scale",
        type=float,
        default=APPROVED_OVERVIEW_DISTANCE_SCALE,
        help=(
            "pull coast camera away from its targets while preserving view angle; "
            "the default is the reviewed whole-city overview distance"
        ),
    )
    parser.add_argument(
        "--motion-samples",
        type=int,
        default=APPROVED_OVERVIEW_MOTION_SAMPLES,
        help="dense keys used for smooth, near-constant-speed helicopter motion",
    )
    parser.add_argument(
        "--water-preview-lift-mm",
        type=float,
        default=0.0,
        help="optional diagnostic-only water Z separation; normally keep at zero",
    )
    parser.add_argument(
        "--depth-of-field",
        action="store_true",
        help="enable cinematic focus blur; off by default for whole-city legibility",
    )
    parser.add_argument(
        "--preview-only",
        action="store_true",
        help="configure the scene and render stills, but skip the MP4",
    )
    args = parser.parse_args(argv)
    if args.fps < 1 or args.duration <= 0 or args.samples < 1:
        parser.error("fps, duration, and samples must be positive")
    if args.lens_mm < 18 or args.lens_mm > 120:
        parser.error("lens-mm must be between 18 and 120")
    if args.view_pitch_deg < 25 or args.view_pitch_deg > 60:
        parser.error("view-pitch-deg must be between 25 and 60")
    if args.coast_distance_scale < 0.75 or args.coast_distance_scale > 3.5:
        parser.error("coast-distance-scale must be between 0.75 and 3.5")
    if args.motion_samples < 9 or args.motion_samples > 97:
        parser.error("motion-samples must be between 9 and 97")
    if args.water_preview_lift_mm < 0 or args.water_preview_lift_mm > 1:
        parser.error("water-preview-lift-mm must be between 0 and 1")
    if args.route_style == "path" and not args.route_points:
        parser.error("--route-style path requires --route-points")
    if args.preview_only and args.stills_dir is None:
        parser.error("--preview-only requires --stills-dir")
    return args


def _world_bounds(objects: Iterable[object], Vector: object) -> Bounds:
    points = []
    for obj in objects:
        if getattr(obj, "type", None) != "MESH":
            continue
        points.extend(obj.matrix_world @ Vector(corner) for corner in obj.bound_box)
    if not points:
        raise RuntimeError("GLB import did not produce any mesh objects")
    return (
        tuple(min(point[i] for point in points) for i in range(3)),
        tuple(max(point[i] for point in points) for i in range(3)),
    )


def _normalize_model_up_axis(bpy: object, objects: Sequence[object], Vector: object):
    """Rotate a flat imported map so Blender Z is the vertical axis.

    The project's GLB is geometrically Z-up when inspected with trimesh, but
    Blender's glTF importer presents this particular export as Y-down.  Detect
    the thin axis instead of hard-coding that exporter detail.  Only root
    object transforms are changed; mesh vertices and printable geometry stay
    untouched.
    """

    from mathutils import Matrix

    before = _world_bounds(objects, Vector)
    spans = [before[1][axis] - before[0][axis] for axis in range(3)]
    vertical_axis = min(range(3), key=spans.__getitem__)
    ordered = sorted(spans)
    if ordered[0] > ordered[1] * 0.35:
        print(f"FLYOVER_AXIS unchanged spans={spans} reason=not_flat")
        return before
    if vertical_axis == 2:
        print(f"FLYOVER_AXIS unchanged spans={spans} up=Z")
        return before
    if vertical_axis == 1:
        # Blender Y equals the negative of the source height for this GLB.
        rotation = Matrix.Rotation(math.radians(-90.0), 4, "X")
    else:
        rotation = Matrix.Rotation(math.radians(-90.0), 4, "Y")

    object_set = set(objects)
    roots = [obj for obj in objects if obj.parent not in object_set]
    for obj in roots:
        obj.matrix_world = rotation @ obj.matrix_world
    bpy.context.view_layer.update()
    after = _world_bounds(objects, Vector)
    print(
        f"FLYOVER_AXIS normalized from={vertical_axis} spans={spans} "
        f"bounds_before={before} bounds_after={after}"
    )
    return after


def _set_keyframes(
    obj: object,
    keys: Sequence[tuple[int, tuple[float, ...]]],
    *,
    interpolation: str = "BEZIER",
):
    for frame, value in keys:
        obj.location = value
        obj.keyframe_insert(data_path="location", frame=frame)
    if obj.animation_data and obj.animation_data.action:
        for curve in obj.animation_data.action.fcurves:
            for point in curve.keyframe_points:
                point.interpolation = interpolation
                if interpolation == "BEZIER":
                    point.handle_left_type = "AUTO_CLAMPED"
                    point.handle_right_type = "AUTO_CLAMPED"


def _add_area_light(bpy: object, name: str, location, energy: float, size: float):
    data = bpy.data.lights.new(name=name, type="AREA")
    data.energy = energy
    data.shape = "DISK"
    data.size = size
    obj = bpy.data.objects.new(name, data)
    bpy.context.collection.objects.link(obj)
    obj.location = location
    obj.rotation_euler = (math.radians(18), 0.0, math.radians(135))
    return obj


def _apply_layer_materials(bpy: object, objects: Sequence[object]):
    """Restore the map layer palette when the GLB imports without materials."""

    for obj in objects:
        layer = obj.name.lower().split(".", 1)[0]
        if layer not in LAYER_STYLES:
            continue
        color, roughness, metallic = LAYER_STYLES[layer]
        material = bpy.data.materials.get(f"Flyover_{layer}")
        if material is None:
            material = bpy.data.materials.new(name=f"Flyover_{layer}")
            material.use_nodes = True
            if layer == "water":
                # Large coastal meshes may include triangulated apron faces
                # that are intentionally hidden by the printable terrain.  A
                # tiny preview-only Z lift prevents depth fighting.  Direct
                # emission keeps the triangulated surface matte, while a
                # restrained blue-grey separates real water from the world.
                nodes = material.node_tree.nodes
                nodes.clear()
                output = nodes.new("ShaderNodeOutputMaterial")
                emission = nodes.new("ShaderNodeEmission")
                emission.inputs["Color"].default_value = (*WATER_COLOR, 1.0)
                emission.inputs["Strength"].default_value = WATER_STRENGTH
                material.node_tree.links.new(
                    emission.outputs["Emission"], output.inputs["Surface"]
                )
                material.diffuse_color = (*WATER_COLOR, 1.0)
                obj.data.materials.clear()
                obj.data.materials.append(material)
                print(
                    f"FLYOVER_MATERIAL object={obj.name} layer={layer} "
                    f"color={WATER_COLOR} shader=emission"
                )
                continue
            principled = next(
                (
                    node
                    for node in material.node_tree.nodes
                    if node.type == "BSDF_PRINCIPLED"
                ),
                None,
            )
            if principled is None:
                principled = material.node_tree.nodes.new("ShaderNodeBsdfPrincipled")
                output = next(
                    node
                    for node in material.node_tree.nodes
                    if node.type == "OUTPUT_MATERIAL"
                )
                material.node_tree.links.new(principled.outputs["BSDF"], output.inputs["Surface"])
            principled.inputs["Base Color"].default_value = (*color, 1.0)
            principled.inputs["Roughness"].default_value = roughness
            principled.inputs["Metallic"].default_value = metallic
            if layer == "landmarks" and "Emission Color" in principled.inputs:
                principled.inputs["Emission Color"].default_value = (*color, 1.0)
                principled.inputs["Emission Strength"].default_value = 0.12
            material.diffuse_color = (*color, 1.0)
        obj.data.materials.clear()
        obj.data.materials.append(material)
        print(f"FLYOVER_MATERIAL object={obj.name} layer={layer} color={color}")


def _add_coast_water_extension(
    bpy: object,
    meshes: Sequence[object],
    bounds: Bounds,
    side: str,
    padding: float,
    Vector: object,
):
    """Add one continuous, render-only water apron around the cropped GLB.

    Extending independent crop edges creates visible seams, while a full plane
    under the model leaves the near-side backing wall visible.  This mesh is a
    single coplanar rectangular ring: adjacent sides share vertices and the
    centre is a true hole matching the model bounds.  It therefore continues
    open coastal water at the real water level without covering in-crop land.
    The apron is never exported back to GLB/3MF.
    """

    water_objects = [
        obj for obj in meshes if obj.name.lower().split(".", 1)[0] == "water"
    ]
    material = bpy.data.materials.get("Flyover_water")
    if not water_objects or material is None:
        print("FLYOVER_WATER_EXTENSION skipped reason=no_water_mesh")
        return None

    (xmin, ymin, _zmin), (xmax, ymax, _zmax) = bounds
    span = max(xmax - xmin, ymax - ymin)
    water_bounds = _world_bounds(water_objects, Vector)
    water_z = water_bounds[1][2] - max(span * 0.00005, 0.005)
    # Treat this as a virtual studio cyclorama rather than a model-scale
    # feature.  The extra margin costs no meaningful geometry (still one quad)
    # and keeps its outer edge out of very oblique wide-screen shots.
    reach = max(padding, span * 50.0)
    vertices, faces = coast_water_apron_geometry(
        bounds,
        reach,
        water_z,
        overlap=span * 0.01,
    )
    mesh = bpy.data.meshes.new("FlyoverWaterExtensionMesh")
    mesh.from_pydata(vertices, [], faces)
    mesh.materials.append(material)
    extension = bpy.data.objects.new("FlyoverWaterExtension", mesh)
    bpy.context.collection.objects.link(extension)
    print(
        "FLYOVER_WATER_EXTENSION "
        f"side={side} z={water_z:.4f} padding={padding:.4f} "
        "mode=continuous_apron "
        f"bounds={_world_bounds([extension], Vector)}"
    )
    return extension


def coast_water_apron_geometry(
    bounds: Bounds,
    reach: float,
    water_z: float,
    *,
    overlap: float = 0.0,
) -> tuple[
    list[tuple[float, float, float]],
    list[tuple[int, int, int, int]],
]:
    """Return one shared-vertex rectangular ring around the model crop."""

    if reach <= 0 or overlap < 0:
        raise ValueError("water apron reach must be positive and overlap non-negative")
    (xmin, ymin, _zmin), (xmax, ymax, _zmax) = bounds
    if overlap * 2 >= min(xmax - xmin, ymax - ymin):
        raise ValueError("water apron overlap is too large for the model crop")
    vertices = [
        (xmin - reach, ymin - reach, water_z),
        (xmax + reach, ymin - reach, water_z),
        (xmax + reach, ymax + reach, water_z),
        (xmin - reach, ymax + reach, water_z),
        (xmin + overlap, ymin + overlap, water_z),
        (xmax - overlap, ymin + overlap, water_z),
        (xmax - overlap, ymax - overlap, water_z),
        (xmin + overlap, ymax - overlap, water_z),
    ]
    faces = [
        (0, 1, 5, 4),
        (1, 2, 6, 5),
        (2, 3, 7, 6),
        (3, 0, 4, 7),
    ]
    return vertices, faces


def coast_water_extension_padding(
    bounds: Bounds,
    route: Sequence[RouteKey],
    *,
    lens_mm: float,
    aspect_ratio: float,
) -> float:
    """Size the render-only water plane from the complete camera frustum."""

    if lens_mm <= 0 or aspect_ratio <= 0 or not route:
        raise ValueError("water extension needs a valid lens, aspect, and route")
    (xmin, ymin, _zmin), (xmax, ymax, _zmax) = bounds
    span = max(xmax - xmin, ymax - ymin)
    if span <= 0:
        raise ValueError("model bounds must have positive horizontal span")
    center = ((xmin + xmax) * 0.5, (ymin + ymax) * 0.5)
    horizontal_half_fov = math.atan(36.0 / (2.0 * lens_mm))
    sensor_height = 36.0 / aspect_ratio
    vertical_half_fov = math.atan(sensor_height / (2.0 * lens_mm))
    padding = span * 5.0
    for _progress, camera, target in route:
        horizontal = math.hypot(camera[0] - target[0], camera[1] - target[1])
        centre_pitch = math.atan2(camera[2] - target[2], horizontal)
        shallow_pitch = max(
            centre_pitch - vertical_half_fov, math.radians(2.0)
        )
        altitude = max(camera[2] - target[2], span * 0.01)
        far_ground = altitude / math.tan(shallow_pitch)
        half_width = far_ground * math.tan(horizontal_half_fov)
        camera_from_center = math.hypot(
            camera[0] - center[0], camera[1] - center[1]
        )
        padding = max(
            padding,
            camera_from_center + far_ground + half_width + span,
        )
    return padding


def _configure_scene(args: argparse.Namespace):
    import bpy
    from mathutils import Vector

    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for block in bpy.data.collections:
        if block.users == 0:
            bpy.data.collections.remove(block)

    bpy.ops.import_scene.gltf(filepath=str(args.input.resolve()))
    meshes = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
    bounds = _normalize_model_up_axis(bpy, meshes, Vector)
    _apply_layer_materials(bpy, meshes)
    if args.water_preview_lift_mm:
        for obj in meshes:
            if obj.name.lower().split(".", 1)[0] == "water":
                obj.location.z += args.water_preview_lift_mm
                print(
                    "FLYOVER_PREVIEW_OFFSET "
                    f"object={obj.name} z_mm={args.water_preview_lift_mm}"
                )
        bpy.context.view_layer.update()
    (xmin, ymin, zmin), (xmax, ymax, zmax) = bounds
    sx, sy = xmax - xmin, ymax - ymin
    span = max(sx, sy)

    if args.route_style == "coast":
        route = build_helicopter_route(
            build_coast_route(
                bounds,
                side=args.coast_side,
                reverse=args.reverse_route,
                view_pitch_deg=args.view_pitch_deg,
                distance_scale=args.coast_distance_scale,
            ),
            samples=args.motion_samples,
            view_pitch_deg=args.view_pitch_deg,
        )
    elif args.route_style == "path":
        route = build_helicopter_route(
            build_path_route(
                bounds, args.route_points, reverse=args.reverse_route
            ),
            samples=args.motion_samples,
        )
    else:
        route = build_focus_route(
            bounds,
            focus_x_frac=args.focus_x_frac,
            focus_y_frac=args.focus_y_frac,
        )

    water_padding = None
    if args.route_style == "coast":
        water_padding = coast_water_extension_padding(
            bounds,
            route,
            lens_mm=args.lens_mm,
            aspect_ratio=args.resolution[0] / args.resolution[1],
        )
        _add_coast_water_extension(
            bpy, meshes, bounds, args.coast_side, water_padding, Vector
        )

    scene = bpy.context.scene
    try:
        scene.render.engine = "BLENDER_EEVEE_NEXT"
    except TypeError:
        scene.render.engine = "BLENDER_EEVEE"
    if hasattr(scene, "eevee"):
        if hasattr(scene.eevee, "taa_render_samples"):
            scene.eevee.taa_render_samples = args.samples
        if hasattr(scene.eevee, "use_gtao"):
            scene.eevee.use_gtao = True
            scene.eevee.gtao_distance = span * 0.025
            scene.eevee.gtao_factor = 1.15

    scene.render.resolution_x, scene.render.resolution_y = args.resolution
    scene.render.resolution_percentage = 100
    scene.render.fps = args.fps
    scene.frame_start = 1
    scene.frame_end = max(2, round(args.duration * args.fps))
    scene.render.image_settings.file_format = "FFMPEG"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.ffmpeg.format = "MPEG4"
    scene.render.ffmpeg.codec = "H264"
    scene.render.ffmpeg.constant_rate_factor = "MEDIUM"
    scene.render.ffmpeg.gopsize = args.fps
    # With FFmpeg, Blender 4.0 on Windows may treat a supplied ``.mp4`` as a
    # filename prefix, append a frame range, and then add a default ``.mkv``.
    # Keep the exact requested movie path and let the explicit MPEG4 setting
    # define the container instead.
    scene.render.use_file_extension = False
    scene.render.film_transparent = False
    scene.render.filepath = str(args.output.resolve())

    scene.world.use_nodes = True
    background = scene.world.node_tree.nodes.get("Background")
    background.inputs["Color"].default_value = (*BACKGROUND_COLOR, 1.0)
    background.inputs["Strength"].default_value = BACKGROUND_STRENGTH

    try:
        scene.view_settings.look = "AgX - Medium High Contrast"
    except TypeError:
        pass
    scene.view_settings.exposure = 0.3

    camera_data = bpy.data.cameras.new("FlyoverCamera")
    camera_data.lens = args.lens_mm
    camera_data.sensor_width = 36.0
    if water_padding is not None:
        camera_data.clip_end = max(camera_data.clip_end, water_padding * 2.0)
    camera_data.dof.use_dof = args.depth_of_field
    camera_data.dof.aperture_fstop = 11.0
    camera = bpy.data.objects.new("FlyoverCamera", camera_data)
    bpy.context.collection.objects.link(camera)
    scene.camera = camera

    target = bpy.data.objects.new("FlyoverTarget", None)
    target.empty_display_type = "PLAIN_AXES"
    target.empty_display_size = span * 0.025
    bpy.context.collection.objects.link(target)
    camera_data.dof.focus_object = target
    tracking = camera.constraints.new(type="TRACK_TO")
    tracking.target = target
    tracking.track_axis = "TRACK_NEGATIVE_Z"
    tracking.up_axis = "UP_Y"

    camera_keys, target_keys = [], []
    for progress, camera_location, target_location in route:
        frame = 1 + round(progress * (scene.frame_end - 1))
        camera_keys.append((frame, camera_location))
        target_keys.append((frame, target_location))
    interpolation = "LINEAR" if args.route_style in {"coast", "path"} else "BEZIER"
    _set_keyframes(camera, camera_keys, interpolation=interpolation)
    _set_keyframes(target, target_keys, interpolation=interpolation)

    sun_data = bpy.data.lights.new(name="CitySun", type="SUN")
    sun_data.energy = 1.25
    sun_data.angle = math.radians(18)
    sun = bpy.data.objects.new("CitySun", sun_data)
    bpy.context.collection.objects.link(sun)
    sun.rotation_euler = (
        math.radians(32),
        math.radians(-18),
        math.radians(-38),
    )
    _add_area_light(
        bpy,
        "LakeKey",
        (xmax + span * 0.12, ymin + sy * 0.18, zmax + span * 0.55),
        energy=1300.0,
        size=span * 0.65,
    )
    _add_area_light(
        bpy,
        "CityFill",
        (xmin + sx * 0.25, ymax - sy * 0.10, zmax + span * 0.32),
        energy=800.0,
        size=span * 0.55,
    )

    print(
        "FLYOVER_SCENE "
        f"meshes={len(meshes)} bounds={bounds} frames=1-{scene.frame_end} "
        f"resolution={scene.render.resolution_x}x{scene.render.resolution_y} "
        f"route={args.route_style} lens_mm={args.lens_mm} "
        f"view_pitch_deg={args.view_pitch_deg} "
        f"coast_distance_scale={args.coast_distance_scale} "
        f"motion_samples={args.motion_samples} "
        f"depth_of_field={args.depth_of_field}"
    )
    for frame, camera_location in camera_keys:
        target_location = dict(target_keys)[frame]
        print(
            f"FLYOVER_KEY frame={frame} camera={camera_location} "
            f"target={target_location}"
        )
    return bpy, scene


def _render_stills(bpy: object, scene: object, directory: Path):
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    original_format = scene.render.image_settings.file_format
    original_path = scene.render.filepath
    duration = scene.frame_end - scene.frame_start
    frames = [
        scene.frame_start,
        scene.frame_start + round(duration * 0.25),
        scene.frame_start + round(duration * 0.50),
        scene.frame_start + round(duration * 0.75),
        scene.frame_end,
    ]
    labels = ["opening", "quarter", "midpoint", "threequarter", "closing"]
    scene.render.image_settings.file_format = "PNG"
    for frame, label in zip(frames, labels):
        scene.frame_set(frame)
        scene.render.filepath = str(directory / f"{label}_{frame:04d}.png")
        bpy.ops.render.render(write_still=True)
        print(f"FLYOVER_STILL frame={frame} path={scene.render.filepath}")
    scene.render.image_settings.file_format = original_format
    scene.render.filepath = original_path


def main(argv: Sequence[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    args = _parse_args(argv)
    args.input = args.input.resolve()
    args.output = args.output.resolve()
    if not args.input.is_file():
        raise FileNotFoundError(f"input GLB does not exist: {args.input}")
    args.output.parent.mkdir(parents=True, exist_ok=True)

    bpy, scene = _configure_scene(args)
    if args.blend_output:
        args.blend_output = args.blend_output.resolve()
        args.blend_output.parent.mkdir(parents=True, exist_ok=True)
        bpy.ops.wm.save_as_mainfile(filepath=str(args.blend_output))
        print(f"FLYOVER_BLEND path={args.blend_output}")
    if args.stills_dir:
        _render_stills(bpy, scene, args.stills_dir)
    if not args.preview_only:
        scene.frame_set(scene.frame_start)
        scene.render.filepath = str(args.output)
        scene.render.image_settings.file_format = "FFMPEG"
        scene.render.ffmpeg.format = "MPEG4"
        scene.render.ffmpeg.codec = "H264"
        scene.render.ffmpeg.constant_rate_factor = "MEDIUM"
        scene.render.use_file_extension = False
        print(
            "FLYOVER_ENCODER "
            f"file_format={scene.render.image_settings.file_format} "
            f"container={scene.render.ffmpeg.format} "
            f"codec={scene.render.ffmpeg.codec} "
            f"path={scene.render.filepath}"
        )
        bpy.ops.render.render(animation=True)
        print(f"FLYOVER_VIDEO path={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
