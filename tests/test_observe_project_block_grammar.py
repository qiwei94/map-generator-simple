import json

import geopandas as gpd
from shapely.geometry import box

from tools.observe_project_block_grammar import (
    adapt_candidate_evidence,
    compare_source_to_reference,
    measure_source_buildings,
)


def _reference():
    return {
        "population": {"eligible_component_count": 2},
        "block_grammar": {
            "spatial_fullness": {
                "occupied_cell_fraction": 0.5,
            },
            "component_shape": {
                "metrics": {
                    "short_axis_mm": {"p50": 2.0},
                    "solidity": {"p50": 1.0},
                    "rectangularity": {"p50": 1.0},
                    "perimeter_excess": {"p50": 1.0},
                },
                "orientation": {"orthogonal_coherence": 1.0},
            },
        },
    }


def test_measure_source_buildings_reports_model_scale_and_comparable_sample():
    buildings = gpd.GeoDataFrame(
        geometry=[
            box(120.0002, 30.0002, 120.0004, 30.0004),
            box(120.0030, 30.0030, 120.0032, 30.0032),
        ],
        crs="EPSG:4326",
    )
    result = measure_source_buildings(
        buildings, [30.0, 120.0, 30.01, 120.01],
        model_span_mm=196.0, sample_limit=10,
    )
    assert result["source_footprint_count"] == 2
    assert result["scale_mm_per_m"] > 0
    assert result["all_source"]["shape_sample"]["sample_count"] == 2
    assert result["reference_comparable_sample"]["shape"]["sample_count"] == 2
    assert result["all_source"]["spatial_fullness"][
        "occupied_cell_fraction"] > 0


def test_adapt_candidate_evidence_preserves_partial_comparability(tmp_path):
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps({
        "candidate": {
            "source_footprints": 20,
            "output_components": {"total": 5},
            "component_metrics_by_role": {"quiet_texture": {"components": 5}},
        },
        "raster_comparison": {"candidate": {"land_building_coverage": 0.2}},
        "provenance": {"scale_aware_topology": {"status": "met"}},
    }), encoding="utf-8")
    adapted = adapt_candidate_evidence(path)
    assert adapted["output_components"]["total"] == 5
    assert adapted["raster_spatial_metrics"]["land_building_coverage"] == 0.2
    assert adapted["comparability"] == "partial_existing_pipeline_evidence"


def test_compare_source_to_reference_separates_observation_and_inference():
    source = {
        "reference_comparable_sample": {
            "estimated_population_count": 10,
            "shape": {
                "metrics": {
                    "short_axis_mm": {"p50": 1.0},
                    "solidity": {"p50": 0.8},
                    "rectangularity": {"p50": 0.7},
                    "perimeter_excess": {"p50": 1.1},
                },
                "orientation": {"orthogonal_coherence": 0.5},
            },
        },
        "all_source": {
            "spatial_fullness": {"occupied_cell_fraction": 0.5},
        },
    }
    result = compare_source_to_reference(source, _reference())
    assert result["observations"][
        "reference_to_estimated_comparable_count_ratio"] == 0.2
    hypotheses = {item["hypothesis"] for item in result["inferences"]}
    assert "reference_demo_uses_component_aggregation_or_selection" in hypotheses
    assert "reference_demo_uses_outline_regularization" in hypotheses
    assert "reference_demo_coarsens_the_print_scale_granularity" in hypotheses
