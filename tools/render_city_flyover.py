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

LAYER_STYLES = {
    "terrain": ((0.78, 0.74, 0.66), 0.88, 0.00),
    "block_base": ((0.46, 0.45, 0.43), 0.92, 0.00),
    "vegetation": ((0.29, 0.38, 0.30), 0.86, 0.00),
    "water": ((0.018, 0.025, 0.040), 0.25, 0.20),
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


def build_camera_route(
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
    parser.add_argument("--focus-x-frac", type=float, default=0.762)
    parser.add_argument("--focus-y-frac", type=float, default=0.522)
    parser.add_argument(
        "--preview-only",
        action="store_true",
        help="configure the scene and render stills, but skip the MP4",
    )
    args = parser.parse_args(argv)
    if args.fps < 1 or args.duration <= 0 or args.samples < 1:
        parser.error("fps, duration, and samples must be positive")
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


def _set_keyframes(obj: object, keys: Sequence[tuple[int, tuple[float, ...]]]):
    for frame, value in keys:
        obj.location = value
        obj.keyframe_insert(data_path="location", frame=frame)
    if obj.animation_data and obj.animation_data.action:
        for curve in obj.animation_data.action.fcurves:
            for point in curve.keyframe_points:
                point.interpolation = "BEZIER"
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
    (xmin, ymin, zmin), (xmax, ymax, zmax) = bounds
    sx, sy = xmax - xmin, ymax - ymin
    span = max(sx, sy)

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
    background.inputs["Color"].default_value = (0.018, 0.022, 0.030, 1.0)
    background.inputs["Strength"].default_value = 0.42

    try:
        scene.view_settings.look = "AgX - Medium High Contrast"
    except TypeError:
        pass
    scene.view_settings.exposure = 0.3

    camera_data = bpy.data.cameras.new("FlyoverCamera")
    camera_data.lens = 44.0
    camera_data.sensor_width = 36.0
    camera_data.dof.use_dof = True
    camera_data.dof.aperture_fstop = 5.6
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

    route = build_camera_route(
        bounds,
        focus_x_frac=args.focus_x_frac,
        focus_y_frac=args.focus_y_frac,
    )
    camera_keys, target_keys = [], []
    for progress, camera_location, target_location in route:
        frame = 1 + round(progress * (scene.frame_end - 1))
        camera_keys.append((frame, camera_location))
        target_keys.append((frame, target_location))
    _set_keyframes(camera, camera_keys)
    _set_keyframes(target, target_keys)

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
        f"resolution={scene.render.resolution_x}x{scene.render.resolution_y}"
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
    frames = [scene.frame_start, (scene.frame_start + scene.frame_end) // 2, scene.frame_end]
    labels = ["opening", "midpoint", "closing"]
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
