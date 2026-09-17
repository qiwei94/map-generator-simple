"""Printable white micro-relief for mapped parks and open green space.

This is deliberately not a building infill.  It uses only the same protected
green areas as the city-surface policy and leaves water and road clearance
untouched.  Sparse rounded bars give large parks a readable material texture
at 25 km scale without turning them into urban blocks.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
import math
import numpy as np
import trimesh
from shapely import affinity, prepared
from shapely.geometry import Point, box

from aesthetic.z_texture import ZTexturePolicy, allowed_ground
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import sample_terrain_surface_plan_z


@dataclass(frozen=True)
class ParkTexturePolicy:
    spacing_mm: float = 1.25
    bar_length_mm: float = .68
    bar_width_mm: float = .34
    height_mm: float = .20
    edge_clearance_mm: float = .32
    seed: int = 20260917

    def payload(self):
        return asdict(self)


def _prism(poly, terrain_plan, height_mm: float) -> trimesh.Trimesh:
    xy = np.asarray(poly.exterior.coords[:-1], dtype=float)
    bottom = sample_terrain_surface_plan_z(terrain_plan, xy[:, 0], xy[:, 1]) + .015
    top = bottom + height_mm
    n = len(xy)
    vertices = np.vstack((np.column_stack((xy, bottom)), np.column_stack((xy, top))))
    faces = [[0, 2, 1], [0, 3, 2], [n, n + 1, n + 2], [n, n + 2, n + 3]]
    for i in range(n):
        j = (i + 1) % n
        faces.extend(((i, j, j + n), (i, j + n, i + n)))
    return trimesh.Trimesh(vertices=vertices, faces=np.asarray(faces), process=False)


def build_park_texture_mesh(layers, bbox_local, scale, terrain_plan, *,
                            policy: ParkTexturePolicy | None = None):
    """Return separate watertight white texture bars inside protected greens."""
    policy = policy or ParkTexturePolicy()
    green_m = allowed_ground(layers, bbox_local, scale, ZTexturePolicy())
    if green_m.is_empty:
        return None, {"status": "no_source_green"}
    green = affinity.scale(green_m, xfact=scale, yfact=scale, origin=(0, 0))
    safe = green.buffer(-policy.edge_clearance_mm)
    if safe.is_empty:
        return None, {"status": "no_printable_green"}
    inside = prepared.prep(safe)
    xmin, ymin, xmax, ymax = safe.bounds
    rng = np.random.default_rng(policy.seed)
    meshes = []
    # A staggered, softly rotated field reads as planted texture rather than
    # city street blocks.  Candidate centres are checked against actual source
    # green geometry; this does not infer any new park extent.
    for row, y in enumerate(np.arange(ymin, ymax, policy.spacing_mm * .86)):
        x_offset = (row % 2) * policy.spacing_mm * .5
        for x in np.arange(xmin + x_offset, xmax, policy.spacing_mm):
            if not inside.contains(Point(float(x), float(y))):
                continue
            angle = float(rng.uniform(-28, 28))
            poly = affinity.rotate(box(x - policy.bar_length_mm / 2, y - policy.bar_width_mm / 2,
                                       x + policy.bar_length_mm / 2, y + policy.bar_width_mm / 2),
                                   angle, origin=(x, y))
            if not safe.contains(poly):
                continue
            meshes.append(_prism(poly, terrain_plan, policy.height_mm))
    if not meshes:
        return None, {"status": "no_texture_cells"}
    mesh = trimesh.util.concatenate(meshes)
    return mesh, {"status": "ready", "policy": policy.payload(),
                  "cells": len(meshes), "faces": len(mesh.faces),
                  "source": "protected_green_polygons_minus_roads_water_buildings"}
