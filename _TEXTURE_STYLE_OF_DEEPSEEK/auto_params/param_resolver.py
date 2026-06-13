"""Rules engine: CityProfile → ResolvedParams.

Implements style mapping (Spec §1.2) and parameter decision tables (Spec §2.1-2.6).
Each parameter carries a reason string for traceability.
"""

from dataclasses import dataclass, field, asdict
from typing import Optional

from .city_profile import CityProfile


@dataclass
class ParamDecision:
    """A resolved parameter value with its reason."""
    value: object
    reason: str


@dataclass
class ResolvedParams:
    """All pipeline parameters resolved from a CityProfile."""

    # Style
    style: str = "classic"

    # Terrain
    z_gamma: float = 0.45
    terrain_thickness_mm: float = 4.0
    elevation_smoothing_sigma: float = 2.5

    # Buildings
    flat_mode: bool = False
    building_density_threshold: float = 0.005
    building_count_threshold: int = 1
    building_print_limit_m2: float = 2500.0
    building_aggregate_buffer_m: float = 20.0
    building_simplify_tol_m: float = 25.0
    building_v2_road_tier: int = 5
    building_v2_hotspot_relax: float = 10.0
    building_v2_landmark_top_percent: float = 1.0

    # Roads
    road_width_multiplier: float = 5.0
    road_filter_tier: Optional[set] = None

    # Water
    water_high_detail: bool = False
    water_min_area_m2: float = 50000.0

    # Vegetation
    vegetation_min_area_m2: float = 5000.0
    vegetation_enabled: bool = True

    # Brick texture
    brick_perlin_amp: float = 4.0
    brick_corner_r_m: float = 8.0

    # Decisions log (param_name → reason)
    _reasons: dict = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("_reasons", None)
        if d.get("road_filter_tier") is not None:
            d["road_filter_tier"] = sorted(d["road_filter_tier"])
        return d

    @property
    def reasons(self) -> dict:
        return self._reasons


def resolve_params(
    profile: CityProfile,
    user_overrides: Optional[dict] = None,
) -> ResolvedParams:
    """Rules engine: CityProfile → ResolvedParams.

    Priority: user_overrides > auto-resolved > defaults.
    """
    params = ResolvedParams()
    reasons = {}

    # ── Step 1: Style selection (Spec §1.2) ──
    params.style, reasons["style"] = _resolve_style(profile)

    # ── Step 2: Terrain params (Spec §2.5) ──
    _resolve_terrain(profile, params, reasons)

    # ── Step 3: Building params (Spec §2.2) ──
    _resolve_buildings(profile, params, reasons)

    # ── Step 4: Road params (Spec §2.3) ──
    _resolve_roads(profile, params, reasons)

    # ── Step 5: Water params (Spec §2.4) ──
    _resolve_water(profile, params, reasons)

    # ── Step 6: Vegetation ──
    _resolve_vegetation(profile, params, reasons)

    # ── Step 7: Brick texture (Spec §2.6) ──
    _resolve_brick(profile, params, reasons)

    # ── Step 8: Style overrides ──
    _apply_style_overrides(params, reasons)

    # ── Apply user overrides (highest priority) ──
    if user_overrides:
        for key, val in user_overrides.items():
            if hasattr(params, key):
                setattr(params, key, val)
                reasons[key] = f"user override: {val}"

    params._reasons = reasons
    return params


def explain_decisions(
    profile: CityProfile,
    params: ResolvedParams,
) -> dict:
    """Generate param_decision.json content."""
    return {
        "detected_features": profile.to_dict(),
        "style_selected": params.style,
        "params_applied": {
            k: {"value": v, "reason": params.reasons.get(k, "default")}
            for k, v in params.to_dict().items()
        },
    }


# ─── Internal resolvers ──────────────────────────────────────────────


def _resolve_style(profile: CityProfile) -> tuple[str, str]:
    """Spec §1.2 style mapping."""
    if (profile.relief_ratio == "mountainous" and profile.water_ratio > 0.05):
        return "terrain-first", (
            f"relief={profile.relief_ratio}, water_ratio={profile.water_ratio:.2f} "
            f"→ terrain-first (mountain + water)"
        )

    if profile.water_ratio > 0.15:
        return "water-first", (
            f"water_ratio={profile.water_ratio:.2f} > 0.15 → water-first"
        )

    if (profile.building_density > 2000
            and profile.road_density_km_per_km2 > 10):
        return "classic", (
            f"density={profile.building_density:.0f}/km², "
            f"road={profile.road_density_km_per_km2:.1f}km/km² → classic (dense urban)"
        )

    if (profile.building_density < 200
            and profile.vegetation_ratio > 0.3):
        return "terrain-first", (
            f"density={profile.building_density:.0f}/km² (sparse), "
            f"vegetation={profile.vegetation_ratio:.2f} → terrain-first (nature area)"
        )

    return "classic", "default style"


def _resolve_terrain(
    profile: CityProfile, params: ResolvedParams, reasons: dict
):
    """Spec §2.5 terrain adaptive."""
    elev = profile.elevation_range_m

    if elev < 50:
        params.z_gamma = 0.60
        reasons["z_gamma"] = f"elevation_range={elev:.0f}m < 50 → gamma=0.60 (amplify)"
    elif elev <= 300:
        params.z_gamma = 0.45
        reasons["z_gamma"] = f"elevation_range={elev:.0f}m (normal) → gamma=0.45"
    else:
        params.z_gamma = 0.35
        reasons["z_gamma"] = f"elevation_range={elev:.0f}m > 300 → gamma=0.35 (compress)"

    if elev > 500:
        params.terrain_thickness_mm = 5.0
        reasons["terrain_thickness_mm"] = (
            f"elevation_range={elev:.0f}m > 500 → thickness=5.0mm (prevent punch-through)"
        )

    max_slope = elev / max((profile.area_km2 ** 0.5) * 1000 * 0.1, 1)
    if max_slope > 45:
        params.elevation_smoothing_sigma = 3.0
        reasons["elevation_smoothing_sigma"] = (
            f"estimated max_slope={max_slope:.0f}° > 45 → sigma=3.0 (smooth overhangs)"
        )


def _resolve_buildings(
    profile: CityProfile, params: ResolvedParams, reasons: dict
):
    """Spec §2.2 building density adaptive."""
    density = profile.building_density
    avg_area = profile.avg_building_area_m2
    height_cov = profile.height_tag_coverage

    # Flat mode
    if height_cov < 0.30:
        params.flat_mode = True
        reasons["flat_mode"] = (
            f"height_coverage={height_cov:.2f} < 0.30 → flat mode"
        )
    else:
        reasons["flat_mode"] = (
            f"height_coverage={height_cov:.2f} >= 0.30 → height mode"
        )

    # Density threshold
    if density > 2000:
        params.building_density_threshold = 0.01
        reasons["building_density_threshold"] = (
            f"density={density:.0f}/km² > 2000 → threshold=0.01 (reduce noise)"
        )
    elif density < 200:
        params.building_density_threshold = 0.001
        reasons["building_density_threshold"] = (
            f"density={density:.0f}/km² < 200 → threshold=0.001 (keep info)"
        )
    else:
        reasons["building_density_threshold"] = (
            f"density={density:.0f}/km² (normal) → threshold=0.005"
        )

    # Print limit based on avg building size
    if avg_area > 500:
        params.building_print_limit_m2 = 1500.0
        reasons["building_print_limit_m2"] = (
            f"avg_area={avg_area:.0f}m² > 500 (CBD) → limit=1500 (keep individuals)"
        )
    elif avg_area < 100:
        params.building_print_limit_m2 = 4000.0
        reasons["building_print_limit_m2"] = (
            f"avg_area={avg_area:.0f}m² < 100 (dense old town) → limit=4000 (force aggregate)"
        )
    else:
        reasons["building_print_limit_m2"] = (
            f"avg_area={avg_area:.0f}m² (normal) → limit=2500"
        )


def _resolve_roads(
    profile: CityProfile, params: ResolvedParams, reasons: dict
):
    """Spec §2.3 road adaptive."""
    rd = profile.road_density_km_per_km2

    if rd > 15:
        params.building_v2_road_tier = 4
        reasons["building_v2_road_tier"] = (
            f"road_density={rd:.1f}km/km² > 15 → tier=4 (reduce fragmentation)"
        )
    elif rd < 5:
        params.building_v2_road_tier = 5
        reasons["building_v2_road_tier"] = (
            f"road_density={rd:.1f}km/km² < 5 → tier=5 (keep all roads for blocks)"
        )
    else:
        reasons["building_v2_road_tier"] = (
            f"road_density={rd:.1f}km/km² (normal) → tier=5"
        )

    # Large area road filter
    if profile.area_km2 > 50:
        params.road_filter_tier = {
            "motorway", "motorway_link", "trunk", "trunk_link",
            "primary", "primary_link", "secondary", "secondary_link",
        }
        reasons["road_filter_tier"] = (
            f"area={profile.area_km2:.0f}km² > 50 → filter to major roads"
        )
    else:
        reasons["road_filter_tier"] = "area < 50km² → no road filter"

    # Width multiplier
    if rd > 15:
        params.road_width_multiplier = 4.0
        reasons["road_width_multiplier"] = (
            f"road_density={rd:.1f} > 15 → multiplier=4.0 (prevent overlap)"
        )


def _resolve_water(
    profile: CityProfile, params: ResolvedParams, reasons: dict
):
    """Spec §2.4 water adaptive."""
    wr = profile.water_ratio

    if wr > 0.3:
        params.water_min_area_m2 = 100000.0
        reasons["water_min_area_m2"] = (
            f"water_ratio={wr:.2f} > 0.3 → min_area=100000 (reduce small fragments)"
        )

    if wr > 0.05:
        params.water_high_detail = True
        reasons["water_high_detail"] = (
            f"water_ratio={wr:.2f} > 0.05 → high detail (preserve islands)"
        )
    else:
        reasons["water_high_detail"] = f"water_ratio={wr:.2f} ≤ 0.05 → normal detail"


def _resolve_vegetation(
    profile: CityProfile, params: ResolvedParams, reasons: dict
):
    """Vegetation settings based on style."""
    if profile.vegetation_ratio < 0.02:
        params.vegetation_min_area_m2 = 2000.0
        reasons["vegetation_min_area_m2"] = (
            f"vegetation_ratio={profile.vegetation_ratio:.2f} < 0.02 "
            f"→ lower threshold to preserve what exists"
        )


def _resolve_brick(
    profile: CityProfile, params: ResolvedParams, reasons: dict
):
    """Spec §2.6 brick texture adaptive."""
    # This would normally adapt to scale, but scale_engine is deferred.
    # Keep defaults for now; mark as default.
    reasons["brick_perlin_amp"] = "default (scale_engine deferred)"
    reasons["brick_corner_r_m"] = "default (scale_engine deferred)"


def _apply_style_overrides(params: ResolvedParams, reasons: dict):
    """Apply style-level overrides (Spec §1.3)."""
    if params.style == "terrain-first":
        if "building_v2_road_tier" not in reasons or "user override" not in reasons.get("building_v2_road_tier", ""):
            params.building_v2_road_tier = min(params.building_v2_road_tier, 3)
            reasons["building_v2_road_tier"] = (
                f"style=terrain-first → road_tier capped at 3"
            )
        params.vegetation_enabled = False
        reasons["vegetation_enabled"] = "style=terrain-first → vegetation disabled"

    elif params.style == "water-first":
        params.water_high_detail = True
        reasons["water_high_detail"] = "style=water-first → force high detail"
