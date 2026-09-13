import numpy as np
import pytest
import geopandas as gpd
from types import SimpleNamespace
from PIL import Image
from shapely.geometry import box
from aesthetic.scale_aware_topology import POLICY_VERSION as TOPOLOGY_POLICY_VERSION

from tools.evaluate_building_mass_strategy import (
    _build_reference_comparison,
    _legacy_topology_cache_is_compatible,
    _parse_bbox,
    _copy_layers_for_diagnostic,
    _raster_metrics,
    _render_reference_scorecard,
    _resolve_diagnostic_scale,
    _evaluation_verdict,
    _subset_wgs84_frame,
)


def test_run_scale_rejects_stale_cache_before_generation():
    with pytest.raises(ValueError, match="cached layer scale mismatch"):
        _resolve_diagnostic_scale(
            SimpleNamespace(nozzle_real_m=60.0),
            {"width_m": 25000.0, "height_m": 25000.0}, 200.0)


def test_qualitative_scale_override_never_uses_stale_nozzle():
    scale, nozzle, evidence = _resolve_diagnostic_scale(
        SimpleNamespace(nozzle_real_m=60.0),
        {"width_m": 25000.0, "height_m": 25000.0}, 200.0,
        allow_mismatch=True)
    assert scale == 0.008
    assert nozzle == 50.0
    assert evidence["comparability"] == "qualitative_only"
    assert _evaluation_verdict({"status": "composed"}, evidence)[
        "status"] == "not_comparable"


def test_scale_matched_candidate_still_requires_human_review():
    _, _, evidence = _resolve_diagnostic_scale(
        SimpleNamespace(nozzle_real_m=50.0),
        {"width_m": 25000.0, "height_m": 25000.0}, 200.0)
    assert evidence["comparability"] == "controlled"
    assert _evaluation_verdict({"status": "composed"}, evidence)[
        "status"] == "human_review_required"


@pytest.mark.parametrize("status", ["guarded_fallback", "policy_preserved_baseline"])
def test_fallback_is_not_a_candidate_success(status):
    verdict = _evaluation_verdict(
        {"status": status}, {"cached_scale_matches": True})
    assert verdict["status"] == "rerun"
    assert verdict["candidate_applied"] is False
    assert verdict["displayed_geometry"] == "baseline_fallback"


def test_diagnostic_layer_copy_does_not_mutate_formal_baseline_channels():
    source = SimpleNamespace(
        BL=[(box(0, 0, 1, 1), 1.2)],
        BL_categories=[None],
        BL_height_roles=["identity_exact"],
        BO=[box(2, 0, 3, 1)],
        BO_heights=[0.24],
        road_roles={"policy": "test"},
    )

    candidate = _copy_layers_for_diagnostic(source)
    candidate.BL.clear()
    candidate.BO.append(box(4, 0, 5, 1))
    candidate.road_roles["policy"] = "candidate"

    assert len(source.BL) == 1
    assert len(source.BO) == 1
    assert source.road_roles == {"policy": "test"}


def _legacy_cache_payload():
    common = {
        "gdf_cache": "/trusted/cache.pkl",
        "gdf_cache_size": 123,
        "gdf_cache_mtime_ns": 456,
        "bbox": [1.0, 2.0, 3.0, 4.0],
        "model_span_mm": 196.0,
        "road_width_multiplier": 1.0,
        "road_role_policy": "print-road-roles-v14.0",
        "topology_policy": TOPOLOGY_POLICY_VERSION,
        "printer_profile": {"profile_id": "test"},
    }
    return common, {
        "key": {
            "schema": "building-mass-topology-cache-v1",
            **common,
            "building_mass_policy": "building-mass-candidate-v4",
        },
        "blocks": [box(0, 0, 1, 1)],
        "evidence": {
            "policy_version": TOPOLOGY_POLICY_VERSION,
            "target_min_model_mm": 1.3,
            "hard_floor_model_mm": 0.63,
            "boundary_inset_each_side_model_mm": 0.46,
        },
    }


def test_legacy_topology_cache_accepts_only_explicit_matching_controls():
    common, payload = _legacy_cache_payload()

    assert _legacy_topology_cache_is_compatible(
        payload,
        common_key=common,
        target_min_model_mm=1.3,
        hard_floor_model_mm=0.63,
        boundary_inset_model_mm=0.46,
    )
    payload["evidence"]["target_min_model_mm"] = 1.31
    assert not _legacy_topology_cache_is_compatible(
        payload,
        common_key=common,
        target_min_model_mm=1.3,
        hard_floor_model_mm=0.63,
        boundary_inset_model_mm=0.46,
    )


def test_parse_bbox_rejects_wrong_arity():
    with pytest.raises(Exception):
        _parse_bbox("1,2,3")


def test_topology_cache_rejects_pre_validity_policy():
    common, payload = _legacy_cache_payload()
    payload['evidence']['policy_version'] = 'scale-aware-block-topology-v2'
    assert not _legacy_topology_cache_is_compatible(
        payload, common_key=common, target_min_model_mm=1.3,
        hard_floor_model_mm=0.63, boundary_inset_model_mm=0.46)


def test_raster_metrics_separate_fragment_count_from_fragment_ink():
    building = np.zeros((32, 32), dtype=np.float32)
    building[2, 2] = 1.0
    building[16:20, 16:20] = 1.0
    bundle = {
        "building_mask": building,
        "water_mask": np.zeros_like(building),
    }

    result = _raster_metrics(
        bundle, nozzle_real_m=4.0, frame_width_m=32.0)

    assert result["raster_components"] == 2
    assert result["small_component_fraction"] == 0.5
    assert result["small_component_ink_fraction"] == pytest.approx(
        1 / 17, abs=1e-5)
    assert result["land_building_coverage"] == pytest.approx(
        17 / 1024, abs=1e-5)


def test_subset_wgs84_frame_preclips_large_regional_cache():
    source = gpd.GeoDataFrame(
        {"name": ["inside", "crossing", "outside"]},
        geometry=[
            box(120.05, 30.05, 120.06, 30.06),
            box(119.90, 30.02, 120.02, 30.03),
            box(121.00, 31.00, 121.10, 31.10),
        ],
        crs="EPSG:4326",
    )

    scoped = _subset_wgs84_frame(
        source, (30.0, 120.0, 30.1, 120.1))

    assert list(scoped["name"]) == ["inside", "crossing"]
    assert len(source) == 3


def test_reference_comparison_keeps_demo_and_effect_evidence_separate(
        tmp_path):
    candidate_evidence = {
        "component_min_width_p50_model_mm": 1.55,
        "component_width_target": {
            "target_min_model_mm": 1.3,
            "target_max_model_mm": 2.3,
            "status": "within_target",
        },
        "global_representation": {
            "mode": "supported_cluster_infill",
            "reason": "test route",
        },
        "component_metrics_by_role": {
            "urban_mass": {
                "short_axis_model_mm": {"p50": 1.6},
                "solidity": {"p50": 0.996},
                "outline_vertices": {"p50": 6.0},
            },
        },
        "mid_frequency_role_filter": {
            "demoted_to_quiet_texture": 12,
        },
        "silhouette_regularization": {
            "reference_audit": {
                "cohort": "10-city compact white-relief sample",
                "city_median_solidity_range": [0.9904, 1.0],
                "typical_effective_outline_vertices_p75": [6, 11],
                "semantic_boundary": "morphology only",
            },
            "after": {
                "solidity": {"p50": 0.995},
                "outline_vertices": {"p50": 7.0},
            },
        },
        "hard_boundaries": {
            "observed_minimum_two_sided_seam_nozzles": 2.2,
            "required_minimum_two_sided_seam_nozzles": 2.1,
            "boundary_clearance_passed": True,
        },
    }
    raster = {
        "baseline": {
            "land_building_coverage": 0.25,
            "raster_components": 1200,
            "small_component_ink_fraction": 0.01,
            "largest_component_ink_fraction": 0.03,
        },
        "candidate": {
            "land_building_coverage": 0.27,
            "raster_components": 900,
            "small_component_ink_fraction": 0.005,
            "largest_component_ink_fraction": 0.031,
        },
    }

    comparison = _build_reference_comparison(
        candidate_evidence, {"status": "composed"}, raster)
    checks = {check["id"]: check for check in comparison["checks"]}

    assert comparison["scope"]["comparability"] == "morphology_only"
    assert comparison["scope"]["same_bbox_reference_image"] is False
    assert checks["component_short_axis_p50_model_mm"]["status"] == "pass"
    assert checks["component_short_axis_p50_model_mm"]["current"] == 1.6
    assert checks["silhouette_solidity_p50"]["status"] == "pass"
    assert comparison["representation"]["measurement_role"] == "urban_mass"
    assert checks["outline_vertices"]["status"] == "info"
    assert comparison["measured_effect"]["raster_components"]["delta"] == -300

    output = tmp_path / "reference_scorecard.png"
    _render_reference_scorecard(comparison, output)
    with Image.open(output) as rendered:
        assert rendered.size == (720, 720)
        assert rendered.mode == "RGB"
