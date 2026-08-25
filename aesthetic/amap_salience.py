"""AMap style-7 visual salience masks for composition diagnostics.

AMap is used here as a cartographic reference, not as replacement geometry.
The stable no-label palette tells us which roads and water bodies the source
map considers visually important.  Masks remain read-only until representative
city tests demonstrate that they are safe selection constraints.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

from _TEXTURE_STYLE_OF_DEEPSEEK._water_supplement import (
    _extract_water_mask,
    _fetch_nolabel_tiles,
    _out_of_china,
    _wgs84_to_gcj02,
)


PALETTE_VERSION = "amap-style7-salience-v1"
COMPARISON_VERSION = "amap-salience-comparison-v1"

# Exact style-7 anchors observed in real Beijing tiles.  A small RGB distance
# absorbs antialiasing without admitting coloured metro lines or green parks.
_ROAD_PALETTES = {
    "major": ((255, 163, 92), (246, 128, 37)),
    "arterial": ((241, 207, 95), (225, 173, 4), (242, 200, 65)),
    "context": ((246, 227, 163), (248, 210, 145), (233, 178, 83)),
}


@dataclass(frozen=True)
class SalienceMasks:
    water: np.ndarray
    road_major: np.ndarray
    road_arterial: np.ndarray
    road_context: np.ndarray
    evidence: dict

    @property
    def road_all(self) -> np.ndarray:
        return self.road_major | self.road_arterial | self.road_context


class AmapSalienceGuide:
    """Sample projected local linework against a cartographic reference mask.

    The guide returns evidence scores only.  Callers remain responsible for
    print budgets, topology, geometry validity, and the prohibition on
    inventing source features.
    """

    def __init__(self, reference: SalienceMasks, bbox_local,
                 *, tolerance_px: int = 3):
        xmin, ymin, xmax, ymax = (float(value) for value in bbox_local)
        if xmax <= xmin or ymax <= ymin:
            raise ValueError("bbox_local must have positive width and height")
        self.reference = reference
        self.bbox_local = (xmin, ymin, xmax, ymax)
        self.tolerance_px = max(0, int(tolerance_px))
        self.version = PALETTE_VERSION
        self._road_weights = np.zeros(reference.water.shape, dtype=np.float32)
        for mask, weight in (
            (reference.road_context, 0.40),
            (reference.road_arterial, 0.72),
            (reference.road_major, 1.00),
        ):
            support = ndimage.binary_dilation(
                mask, iterations=self.tolerance_px)
            self._road_weights[support] = np.maximum(
                self._road_weights[support], weight)
        self._water_support = ndimage.binary_dilation(
            reference.water, iterations=self.tolerance_px)

    @staticmethod
    def _iter_lines(geometry):
        if geometry is None or geometry.is_empty:
            return []
        if geometry.geom_type == "LineString":
            return [geometry]
        if geometry.geom_type == "MultiLineString":
            return [line for line in geometry.geoms if not line.is_empty]
        if hasattr(geometry, "geoms"):
            lines = []
            for child in geometry.geoms:
                lines.extend(AmapSalienceGuide._iter_lines(child))
            return lines
        return []

    def _sample_pixels(self, geometry):
        xmin, ymin, xmax, ymax = self.bbox_local
        height, width = self.reference.water.shape
        pixel_m = min((xmax - xmin) / width, (ymax - ymin) / height)
        sample_step = max(pixel_m * 0.8, 10.0)
        columns = []
        rows = []
        for line in self._iter_lines(geometry):
            samples = min(768, max(2, int(math.ceil(
                float(line.length) / sample_step))))
            for position in np.linspace(0.0, 1.0, samples):
                point = line.interpolate(float(position), normalized=True)
                column = int(round(
                    (point.x - xmin) / (xmax - xmin) * (width - 1)))
                row = int(round(
                    (ymax - point.y) / (ymax - ymin) * (height - 1)))
                if 0 <= row < height and 0 <= column < width:
                    columns.append(column)
                    rows.append(row)
        return np.asarray(rows, dtype=int), np.asarray(columns, dtype=int)

    def road_support(self, geometry) -> dict:
        rows, columns = self._sample_pixels(geometry)
        if not len(rows):
            return {
                "sample_count": 0,
                "covered_fraction": 0.0,
                "weighted_salience": 0.0,
            }
        weights = self._road_weights[rows, columns]
        return {
            "sample_count": int(len(rows)),
            "covered_fraction": round(float(np.mean(weights > 0)), 5),
            "weighted_salience": round(float(np.mean(weights)), 5),
            "major_fraction": round(float(np.mean(weights >= 0.99)), 5),
            "arterial_or_major_fraction": round(
                float(np.mean(weights >= 0.70)), 5),
        }

    def water_support(self, geometry) -> dict:
        rows, columns = self._sample_pixels(geometry)
        if not len(rows):
            return {"sample_count": 0, "covered_fraction": 0.0}
        support = self._water_support[rows, columns]
        return {
            "sample_count": int(len(rows)),
            "covered_fraction": round(float(np.mean(support)), 5),
        }


def _palette_mask(rgb: np.ndarray, anchors, tolerance: float) -> np.ndarray:
    work = rgb.astype(np.int16)
    output = np.zeros(rgb.shape[:2], dtype=bool)
    threshold = float(tolerance) ** 2
    for anchor in anchors:
        delta = work - np.asarray(anchor, dtype=np.int16)
        output |= np.sum(delta.astype(np.int32) ** 2, axis=2) <= threshold
    return output


def _remove_small_components(mask: np.ndarray, min_pixels: int) -> np.ndarray:
    if not mask.any() or min_pixels <= 1:
        return mask.astype(bool)
    labels, count = ndimage.label(mask)
    if count == 0:
        return mask.astype(bool)
    sizes = np.bincount(labels.ravel())
    keep = sizes >= int(min_pixels)
    keep[0] = False
    return keep[labels]


def extract_amap_salience_masks(
    image,
    *,
    palette_tolerance: float = 18.0,
    min_road_component_pixels: int = 16,
) -> SalienceMasks:
    """Extract water and three road-salience tiers from a style-7 image."""

    rgb = np.asarray(image.convert("RGB") if isinstance(image, Image.Image)
                     else image, dtype=np.uint8)
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError("AMap reference must be an RGB image")

    water = _extract_water_mask(rgb).astype(bool)
    road_masks = {}
    for name, palette in _ROAD_PALETTES.items():
        mask = _palette_mask(rgb, palette, palette_tolerance)
        # Coloured transit lines can overwrite one or two pixels at crossings;
        # close those display artefacts before component filtering.
        mask = ndimage.binary_closing(mask, structure=np.ones((3, 3)))
        mask &= ~water
        road_masks[name] = _remove_small_components(
            mask, min_road_component_pixels)

    pixels = float(rgb.shape[0] * rgb.shape[1])
    evidence = {
        "palette_version": PALETTE_VERSION,
        "shape": [int(rgb.shape[0]), int(rgb.shape[1])],
        "palette_tolerance_rgb": float(palette_tolerance),
        "min_road_component_pixels": int(min_road_component_pixels),
        "water_ratio": round(float(water.sum()) / pixels, 6),
        "road_major_ratio": round(
            float(road_masks["major"].sum()) / pixels, 6),
        "road_arterial_ratio": round(
            float(road_masks["arterial"].sum()) / pixels, 6),
        "road_context_ratio": round(
            float(road_masks["context"].sum()) / pixels, 6),
        "warning": (
            "Style-7 colours express cartographic salience, not legal road "
            "classification or replacement geometry."
        ),
    }
    return SalienceMasks(
        water=water,
        road_major=road_masks["major"],
        road_arterial=road_masks["arterial"],
        road_context=road_masks["context"],
        evidence=evidence,
    )


def extract_review_salience_masks(image) -> tuple[np.ndarray, np.ndarray]:
    """Extract road/water masks from the project's urban top-down review PNG.

    This adapter is diagnostic-only.  Production comparison should consume
    the float masks returned by ``render_review_bundle`` directly.
    """

    rgb = np.asarray(image.convert("RGB") if isinstance(image, Image.Image)
                     else image, dtype=np.uint8)
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError("review render must be an RGB image")
    spread = rgb.max(axis=2).astype(np.int16) - rgb.min(axis=2).astype(np.int16)
    luminance = rgb.mean(axis=2)
    neutral = spread <= 10
    water = neutral & (luminance <= 28)
    # Urban review roads are RGB 74 before antialiasing.  Deliberately exclude
    # the 132/138 landscape-road/building-edge range, which cannot be separated
    # reliably from a flattened PNG.
    roads = neutral & (luminance >= 42) & (luminance <= 108)
    return roads, water


def _mercator_y(lat: float) -> float:
    lat = max(-85.05112878, min(85.05112878, float(lat)))
    return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def crop_mosaic_to_wgs84_bbox(
    mosaic: np.ndarray,
    grid_bounds_gcj,
    bbox_wgs84,
    *,
    output_size: int = 1024,
) -> tuple[np.ndarray, dict]:
    """Crop a north-up GCJ tile mosaic to an exact WGS84 request frame."""

    if output_size < 64:
        raise ValueError("output_size must be at least 64")
    south, west, north, east = (float(v) for v in bbox_wgs84)
    if north <= south or east <= west:
        raise ValueError("bbox_wgs84 must be south,west,north,east")
    corners = [
        _wgs84_to_gcj02(west, south),
        _wgs84_to_gcj02(west, north),
        _wgs84_to_gcj02(east, south),
        _wgs84_to_gcj02(east, north),
    ]
    target_west = min(point[0] for point in corners)
    target_east = max(point[0] for point in corners)
    target_south = min(point[1] for point in corners)
    target_north = max(point[1] for point in corners)

    grid_south, grid_west, grid_north, grid_east = (
        float(v) for v in grid_bounds_gcj)
    height, width = mosaic.shape[:2]
    left = (target_west - grid_west) / (grid_east - grid_west) * width
    right = (target_east - grid_west) / (grid_east - grid_west) * width
    mercator_span = _mercator_y(grid_north) - _mercator_y(grid_south)
    top = ((_mercator_y(grid_north) - _mercator_y(target_north))
           / mercator_span * height)
    bottom = ((_mercator_y(grid_north) - _mercator_y(target_south))
              / mercator_span * height)
    pixel_bounds = [
        max(0, int(math.floor(left))),
        max(0, int(math.floor(top))),
        min(width, int(math.ceil(right))),
        min(height, int(math.ceil(bottom))),
    ]
    if pixel_bounds[2] <= pixel_bounds[0] or pixel_bounds[3] <= pixel_bounds[1]:
        raise ValueError("requested bbox does not overlap AMap mosaic")
    cropped = Image.fromarray(mosaic).crop(tuple(pixel_bounds)).resize(
        (output_size, output_size), Image.Resampling.LANCZOS)
    return np.asarray(cropped.convert("RGB")), {
        "bbox_wgs84": [south, west, north, east],
        "grid_bounds_gcj": list(grid_bounds_gcj),
        "crop_pixel_bounds": pixel_bounds,
        "output_size": output_size,
        "coordinate_method": "WGS84 corners to GCJ-02 then WebMercator crop",
    }


def fetch_amap_salience_reference(
    bbox_wgs84,
    *,
    zoom: int = 13,
    output_size: int = 1024,
    cache_dir=None,
    allow_network: bool = False,
) -> tuple[np.ndarray | None, dict]:
    """Return an exact-frame AMap reference image with explicit cache policy."""

    south, west, north, east = (float(v) for v in bbox_wgs84)
    cache_root = Path(cache_dir or (
        Path(__file__).resolve().parents[1] / "cache" / "amap_salience"))
    cache_root.mkdir(parents=True, exist_ok=True)
    stem = (f"{south:.4f}_{west:.4f}_{north:.4f}_{east:.4f}"
            f"_z{zoom}_{output_size}px_{PALETTE_VERSION}")
    image_path = cache_root / f"{stem}.png"
    metadata_path = cache_root / f"{stem}.json"
    if image_path.is_file() and metadata_path.is_file():
        image = np.asarray(Image.open(image_path).convert("RGB"))
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["cache_hit"] = True
        return image, metadata
    if not allow_network:
        return None, {
            "status": "unavailable",
            "reason": "salience cache miss and network fetch not allowed",
            "cache_path": str(image_path),
        }

    mosaic, bounds = _fetch_nolabel_tiles(
        (south, west, north, east), int(zoom))
    if mosaic is None or bounds is None:
        return None, {
            "status": "unavailable",
            "reason": "AMap tile fetch returned no mosaic",
            "cache_path": str(image_path),
        }
    cropped, crop_evidence = crop_mosaic_to_wgs84_bbox(
        mosaic, bounds, (south, west, north, east), output_size=output_size)
    Image.fromarray(cropped).save(image_path)
    metadata = {
        "status": "available",
        "source": "amap_nolabel_tiles_style7",
        "zoom": int(zoom),
        "cache_hit": False,
        "cache_path": str(image_path),
        **crop_evidence,
    }
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    return cropped, metadata


def build_amap_salience_guide(
    bbox_wgs84,
    bbox_local,
    *,
    zoom: int = 13,
    output_size: int = 1024,
    cache_dir=None,
    allow_network: bool = False,
    tolerance_px: int = 3,
) -> tuple[AmapSalienceGuide | None, dict]:
    """Build an optional guide with an explicit mainland/cache boundary.

    Unavailable external evidence is a normal, auditable fallback to the
    established OSM-only selector.  It must never make generation fail.
    """

    south, west, north, east = (float(value) for value in bbox_wgs84)
    center_lon = (west + east) * 0.5
    center_lat = (south + north) * 0.5
    if _out_of_china(center_lon, center_lat):
        return None, {
            "status": "not_applicable",
            "reason": "AMap salience guidance is mainland-China-only",
            "bbox_wgs84": [south, west, north, east],
            "palette_version": PALETTE_VERSION,
        }

    reference_rgb, source_evidence = fetch_amap_salience_reference(
        (south, west, north, east),
        zoom=zoom,
        output_size=output_size,
        cache_dir=cache_dir,
        allow_network=allow_network,
    )
    if reference_rgb is None:
        return None, {
            **source_evidence,
            "palette_version": PALETTE_VERSION,
        }
    masks = extract_amap_salience_masks(reference_rgb)
    guide = AmapSalienceGuide(
        masks, bbox_local, tolerance_px=tolerance_px)
    return guide, {
        **source_evidence,
        "status": "ready",
        "palette_version": PALETTE_VERSION,
        "mask_evidence": masks.evidence,
        "constraint": (
            "read-only salience evidence; ranks existing complete OSM "
            "corridors within existing print budgets"
        ),
    }


def _resize_bool(mask: np.ndarray, shape) -> np.ndarray:
    height, width = (int(shape[0]), int(shape[1]))
    if mask.shape == (height, width):
        return mask.astype(bool)
    image = Image.fromarray(mask.astype(np.uint8) * 255)
    return np.asarray(image.resize(
        (width, height), Image.Resampling.NEAREST)) > 0


def _mask_recall(reference, candidate, tolerance_px: int) -> float:
    if not reference.any():
        return 1.0
    support = ndimage.binary_dilation(
        candidate, iterations=max(0, int(tolerance_px)))
    return float((reference & support).sum()) / float(reference.sum())


def _distance_p95(reference, candidate) -> float | None:
    if not reference.any() or not candidate.any():
        return None
    distances = ndimage.distance_transform_edt(~candidate)
    return float(np.percentile(distances[reference], 95))


def _component_coverage(reference, candidate, tolerance_px: int,
                        min_pixels: int = 40) -> dict:
    labels, count = ndimage.label(reference)
    support = ndimage.binary_dilation(
        candidate, iterations=max(0, int(tolerance_px)))
    ratios = []
    for label in range(1, count + 1):
        component = labels == label
        pixels = int(component.sum())
        if pixels < min_pixels:
            continue
        ratios.append(float((component & support).sum()) / pixels)
    return {
        "reference_components": len(ratios),
        "well_supported_components": sum(ratio >= 0.60 for ratio in ratios),
        "component_recall_median": (round(float(np.median(ratios)), 5)
                                    if ratios else None),
        "component_recall_min": (round(float(min(ratios)), 5)
                                 if ratios else None),
    }


def _grid_distribution_similarity(reference, candidate, grid_size: int = 8):
    height, width = reference.shape
    ref_values = []
    candidate_values = []
    for row in range(grid_size):
        y0, y1 = row * height // grid_size, (row + 1) * height // grid_size
        for column in range(grid_size):
            x0 = column * width // grid_size
            x1 = (column + 1) * width // grid_size
            ref_values.append(float(reference[y0:y1, x0:x1].sum()))
            candidate_values.append(float(candidate[y0:y1, x0:x1].sum()))
    ref_values = np.asarray(ref_values)
    candidate_values = np.asarray(candidate_values)
    denominator = np.linalg.norm(ref_values) * np.linalg.norm(candidate_values)
    if denominator <= 0:
        return None
    return round(float(np.dot(ref_values, candidate_values) / denominator), 5)


def compare_salience_masks(
    reference: SalienceMasks,
    candidate_roads: np.ndarray,
    candidate_water: np.ndarray,
    *,
    tolerance_px: int = 5,
) -> dict:
    """Compare a printable candidate with AMap's visual hierarchy."""

    shape = reference.water.shape
    candidate_roads = _resize_bool(candidate_roads, shape)
    candidate_water = _resize_bool(candidate_water, shape)
    road_classes = {
        "major": reference.road_major,
        "arterial": reference.road_arterial,
        "context": reference.road_context,
    }
    road_metrics = {}
    for name, mask in road_classes.items():
        road_metrics[name] = {
            "reference_pixels": int(mask.sum()),
            "recall_with_tolerance": round(
                _mask_recall(mask, candidate_roads, tolerance_px), 5),
            "distance_p95_px": (
                round(value, 3) if (value := _distance_p95(
                    mask, candidate_roads)) is not None else None),
            **_component_coverage(
                mask, candidate_roads, tolerance_px),
        }
    water_metrics = {
        "reference_pixels": int(reference.water.sum()),
        "recall_with_tolerance": round(
            _mask_recall(reference.water, candidate_water, tolerance_px), 5),
        "distance_p95_px": (
            round(value, 3) if (value := _distance_p95(
                reference.water, candidate_water)) is not None else None),
        **_component_coverage(
            reference.water, candidate_water, tolerance_px),
    }
    return {
        "version": COMPARISON_VERSION,
        "status": "evidence_only",
        "shape": list(shape),
        "tolerance_px": int(tolerance_px),
        "roads": road_metrics,
        "water": water_metrics,
        "road_ink_distribution_similarity": _grid_distribution_similarity(
            reference.road_all, candidate_roads),
        "water_distribution_similarity": _grid_distribution_similarity(
            reference.water, candidate_water),
        "warning": (
            "Metrics constrain visible composition only. They must not "
            "create roads, water geometry, mesh, Z values, or booleans."
        ),
    }


def render_salience_comparison(
    reference: SalienceMasks,
    candidate_roads: np.ndarray,
    candidate_water: np.ndarray,
    report: dict,
    output_path,
) -> str:
    """Render reference, candidate, and uncovered salience evidence."""

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    shape = reference.water.shape
    candidate_roads = _resize_bool(candidate_roads, shape)
    candidate_water = _resize_bool(candidate_water, shape)
    tolerance = int(report["tolerance_px"])
    road_support = ndimage.binary_dilation(candidate_roads,
                                           iterations=tolerance)
    water_support = ndimage.binary_dilation(candidate_water,
                                            iterations=tolerance)

    def composite(roads, water):
        image = np.full((*shape, 3), 248, dtype=np.uint8)
        image[roads] = (66, 66, 66)
        image[water] = (36, 92, 145)
        return image

    reference_image = np.full((*shape, 3), 248, dtype=np.uint8)
    reference_image[reference.road_context] = (236, 196, 95)
    reference_image[reference.road_arterial] = (224, 144, 31)
    reference_image[reference.road_major] = (208, 73, 30)
    reference_image[reference.water] = (36, 92, 145)
    miss_image = np.full((*shape, 3), 248, dtype=np.uint8)
    miss_image[reference.road_context & ~road_support] = (236, 196, 95)
    miss_image[reference.road_arterial & ~road_support] = (224, 110, 31)
    miss_image[reference.road_major & ~road_support] = (190, 28, 32)
    miss_image[reference.water & ~water_support] = (93, 44, 148)

    fig, axes = plt.subplots(2, 2, figsize=(14, 13), constrained_layout=True)
    axes[0, 0].imshow(reference_image)
    axes[0, 0].set_title("AMap cartographic salience mask")
    axes[0, 1].imshow(composite(candidate_roads, candidate_water))
    axes[0, 1].set_title("Printable candidate masks")
    axes[1, 0].imshow(miss_image)
    axes[1, 0].set_title("AMap salience not supported by candidate")
    axes[1, 1].axis("off")
    lines = [
        "READ-ONLY COMPOSITION EVIDENCE",
        "",
        f"Road ink distribution: {report['road_ink_distribution_similarity']}",
        f"Water distribution: {report['water_distribution_similarity']}",
        "",
    ]
    for name in ("major", "arterial", "context"):
        metric = report["roads"][name]
        lines.append(
            f"{name:>8} road recall: "
            f"{metric['recall_with_tolerance'] * 100:5.1f}%  "
            f"components {metric['well_supported_components']}/"
            f"{metric['reference_components']}")
    lines.extend((
        "",
        f"water recall: {report['water']['recall_with_tolerance'] * 100:.1f}%",
        f"water components: {report['water']['well_supported_components']}/"
        f"{report['water']['reference_components']}",
        "",
        "Misses are candidates for OSM corridor promotion or continuity",
        "repair. They are not permission to draw invented geometry.",
    ))
    axes[1, 1].text(0.03, 0.97, "\n".join(lines), va="top", ha="left",
                    family="monospace", fontsize=12)
    for axis in axes.flat[:3]:
        axis.axis("off")
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=150)
    plt.close(fig)
    return str(output_path)
