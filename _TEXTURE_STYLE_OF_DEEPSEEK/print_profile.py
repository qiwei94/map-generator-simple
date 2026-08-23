"""Physical printer constraints and deterministic map-to-model scaling.

This module is deliberately independent from aesthetic decisions.  It turns
declared printer capabilities and a real-world extent into auditable numeric
constraints.  It never changes mesh vertices by itself and contains no LLM
integration.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Literal

from .config import INTERNAL_SPAN_MM


QuantizeMode = Literal["nearest", "ceil", "floor"]


@dataclass(frozen=True)
class PrinterProfile:
    """Capabilities used by deterministic geometry and print checks.

    Values are model millimetres.  ``min_colored_strip_mm`` is intentionally
    wider than one extrusion line: a multi-material feature that only survives
    as a single theoretical line is not a reliable product default.
    """

    profile_id: str = "fdm-0.4-balanced-v1"
    nozzle_diameter_mm: float = 0.4
    extrusion_width_mm: float = 0.42
    layer_height_mm: float = 0.12
    min_colored_strip_mm: float = 0.63
    min_gap_mm: float = 0.55
    min_surface_layers: int = 2

    def __post_init__(self) -> None:
        if not self.profile_id.strip():
            raise ValueError("profile_id must not be empty")
        positive = {
            "nozzle_diameter_mm": self.nozzle_diameter_mm,
            "extrusion_width_mm": self.extrusion_width_mm,
            "layer_height_mm": self.layer_height_mm,
            "min_colored_strip_mm": self.min_colored_strip_mm,
            "min_gap_mm": self.min_gap_mm,
        }
        for name, value in positive.items():
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a finite positive number")
        if isinstance(self.min_surface_layers, bool) or self.min_surface_layers < 1:
            raise ValueError("min_surface_layers must be a positive integer")
        if int(self.min_surface_layers) != self.min_surface_layers:
            raise ValueError("min_surface_layers must be a positive integer")
        if self.extrusion_width_mm < self.nozzle_diameter_mm * 0.8:
            raise ValueError("extrusion_width_mm is implausibly narrow for the nozzle")
        if self.min_colored_strip_mm < self.extrusion_width_mm:
            raise ValueError("min_colored_strip_mm must be at least one extrusion width")
        if self.min_gap_mm < self.extrusion_width_mm:
            raise ValueError("min_gap_mm must be at least one extrusion width")

    @property
    def min_surface_height_mm(self) -> float:
        return self.layer_height_mm * self.min_surface_layers

    def to_dict(self) -> dict:
        return asdict(self)


DEFAULT_PRINTER_PROFILE = PrinterProfile()


@dataclass(frozen=True)
class PrintScale:
    """Deterministic XY scale for one requested output extent."""

    real_width_m: float
    real_height_m: float
    model_span_mm: float = INTERNAL_SPAN_MM

    def __post_init__(self) -> None:
        for name in ("real_width_m", "real_height_m", "model_span_mm"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a finite positive number")

    @property
    def scale_mm_per_m(self) -> float:
        return self.model_span_mm / max(self.real_width_m, self.real_height_m)

    @property
    def real_m_per_model_mm(self) -> float:
        return 1.0 / self.scale_mm_per_m

    @property
    def model_width_mm(self) -> float:
        return self.real_width_m * self.scale_mm_per_m

    @property
    def model_height_mm(self) -> float:
        return self.real_height_m * self.scale_mm_per_m

    def model_mm_to_real_m(self, value_mm: float) -> float:
        return float(value_mm) * self.real_m_per_model_mm

    def real_m_to_model_mm(self, value_m: float) -> float:
        return float(value_m) * self.scale_mm_per_m

    def to_dict(self) -> dict:
        return {
            "real_width_m": self.real_width_m,
            "real_height_m": self.real_height_m,
            "model_span_mm": self.model_span_mm,
            "model_width_mm": self.model_width_mm,
            "model_height_mm": self.model_height_mm,
            "scale_mm_per_m": self.scale_mm_per_m,
            "real_m_per_model_mm": self.real_m_per_model_mm,
        }


def quantize_thickness_mm(
    thickness_mm: float,
    layer_height_mm: float,
    *,
    mode: QuantizeMode = "ceil",
    min_layers: int = 1,
) -> float:
    """Quantize a non-negative semantic thickness to printer layers.

    ``ceil`` is the safe default for printable surfaces: it never makes a
    requested positive thickness thinner.  Absolute Z positions are not
    accepted here because they need a separately declared datum policy.
    """

    if not math.isfinite(thickness_mm) or thickness_mm < 0:
        raise ValueError("thickness_mm must be a finite non-negative number")
    if not math.isfinite(layer_height_mm) or layer_height_mm <= 0:
        raise ValueError("layer_height_mm must be a finite positive number")
    if isinstance(min_layers, bool) or min_layers < 0 or int(min_layers) != min_layers:
        raise ValueError("min_layers must be a non-negative integer")
    if mode not in {"nearest", "ceil", "floor"}:
        raise ValueError("mode must be one of: nearest, ceil, floor")
    if thickness_mm == 0 and min_layers == 0:
        return 0.0

    raw_layers = thickness_mm / layer_height_mm
    if mode == "ceil":
        layers = math.ceil(raw_layers - 1e-12)
    elif mode == "floor":
        layers = math.floor(raw_layers + 1e-12)
    else:
        layers = math.floor(raw_layers + 0.5)
    layers = max(int(min_layers), layers)
    return round(layers * layer_height_mm, 10)


def build_printability_report(
    profile: PrinterProfile,
    scale: PrintScale,
    *,
    current_thresholds: dict | None = None,
    z_thicknesses_mm: dict | None = None,
) -> dict:
    """Return JSON-ready evidence without mutating generation."""

    z_layer_audit = {}
    for name, raw_value in (z_thicknesses_mm or {}).items():
        value = float(raw_value)
        nearest = quantize_thickness_mm(
            value, profile.layer_height_mm,
            mode="nearest", min_layers=0)
        safe = quantize_thickness_mm(
            value, profile.layer_height_mm,
            mode="ceil", min_layers=profile.min_surface_layers)
        z_layer_audit[str(name)] = {
            "requested_mm": value,
            "nearest_layer_grid_mm": nearest,
            "safe_quantized_mm": safe,
            "nearest_layer_count": int(round(nearest / profile.layer_height_mm)),
            "safe_layer_count": int(round(safe / profile.layer_height_mm)),
            "on_layer_grid": math.isclose(value, nearest, abs_tol=1e-9),
            "meets_min_surface_height": value >= profile.min_surface_height_mm,
        }

    return {
        "report_version": "1.0",
        "printer_profile": profile.to_dict(),
        "scale": scale.to_dict(),
        "derived_xy_real_m": {
            "nozzle_diameter": scale.model_mm_to_real_m(
                profile.nozzle_diameter_mm),
            "extrusion_width": scale.model_mm_to_real_m(
                profile.extrusion_width_mm),
            "min_colored_strip": scale.model_mm_to_real_m(
                profile.min_colored_strip_mm),
            "min_gap": scale.model_mm_to_real_m(profile.min_gap_mm),
        },
        "derived_z_model_mm": {
            "layer_height": profile.layer_height_mm,
            "min_surface_height": profile.min_surface_height_mm,
        },
        "current_pipeline_thresholds": dict(current_thresholds or {}),
        "z_layer_audit": z_layer_audit,
        "notes": [
            "XY values are converted through the requested map scale.",
            "Z is semantic/exaggerated and is not treated as real-world XY scale.",
            "DesignSpec records this report but does not drive mesh geometry.",
        ],
    }
