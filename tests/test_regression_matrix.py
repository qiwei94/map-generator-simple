import json

from _TEXTURE_STYLE_OF_DEEPSEEK.regression_matrix import (
    evaluate_result_set,
    expected_runs,
    load_regression_matrix,
)


def test_required_matrix_covers_chicago_beijing_shanghai_and_westlake():
    matrix = load_regression_matrix()
    required = {
        item["key"]: item
        for item in matrix["scenarios"] if item["required"]
    }

    assert {"chicago", "beijing", "shanghai", "westlake"} <= set(required)
    assert required["chicago"]["archetype"] == "dense_grid_major_lake"
    assert required["beijing"]["archetype"] == "ring_radial_landlocked"
    assert required["shanghai"]["archetype"] == "dense_grid_major_river"


def test_required_scenarios_match_showcase_source_identity():
    matrix = load_regression_matrix()
    showcase = json.loads(
        (matrix_path().parent / "showcase_cities.json").read_text(
            encoding="utf-8"))
    showcase_by_key = {item["key"]: item for item in showcase["cities"]}

    for scenario in matrix["scenarios"]:
        if not scenario["required"]:
            continue
        source = showcase_by_key[scenario.get("showcase_key", scenario["key"])]
        assert scenario["center"] == source["center"]
        assert scenario["pbf"] == source["pbf"]


def matrix_path():
    from _TEXTURE_STYLE_OF_DEEPSEEK.regression_matrix import DEFAULT_MATRIX_PATH
    return DEFAULT_MATRIX_PATH


def test_matrix_expands_every_required_city_across_both_preview_sizes():
    matrix = load_regression_matrix()
    runs = expected_runs(matrix)
    required_count = sum(item["required"] for item in matrix["scenarios"])

    assert len(runs) == required_count * 3
    for city in ("chicago", "beijing", "shanghai", "westlake"):
        city_runs = [run for run in runs if run["scenario_key"] == city]
        assert {(run["mode"], run["size_km"]) for run in city_runs} == {
            ("topdown", 15.0),
            ("topdown", 25.0),
            ("full_3mf", 25.0),
        }


def test_one_city_cannot_approve_a_cross_city_default_change():
    matrix = load_regression_matrix()
    chicago_runs = [run for run in expected_runs(matrix)
                    if run["scenario_key"] == "chicago"]
    results = [{
        "scenario_key": run["scenario_key"],
        "mode": run["mode"],
        "size_km": run["size_km"],
        "status": "passed",
        "checks": {check: True for check in run["required_checks"]},
    } for run in chicago_runs]

    verdict = evaluate_result_set(matrix, results)

    assert verdict["passed"] is False
    assert verdict["received_required_run_count"] == 3
    assert any("beijing" in problem for problem in verdict["problems"])
    assert any("shanghai" in problem for problem in verdict["problems"])


def test_all_runs_and_checks_are_required_for_approval():
    matrix = load_regression_matrix()
    runs = expected_runs(matrix)
    results = [{
        "scenario_key": run["scenario_key"],
        "mode": run["mode"],
        "size_km": run["size_km"],
        "status": "passed",
        "checks": {check: True for check in run["required_checks"]},
    } for run in runs]
    assert evaluate_result_set(matrix, results)["passed"] is True

    results[0]["checks"][runs[0]["required_checks"][0]] = False
    verdict = evaluate_result_set(matrix, results)
    assert verdict["passed"] is False
    assert "failed or missing" in verdict["problems"][0]
