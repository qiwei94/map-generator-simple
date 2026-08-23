"""Blocking cross-city regression contract for visual/print parameter changes.

The matrix deliberately separates declaring representative scenes from running
expensive generation.  A result set only passes when every required city,
output mode, physical size and evidence check is present and true.  This keeps
one attractive city from being used as proof that a new default generalizes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Mapping


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MATRIX_PATH = ROOT / "data" / "print_regression_scenarios.json"
VALID_MODES = {"topdown", "full_3mf"}


def load_regression_matrix(path: Path | str = DEFAULT_MATRIX_PATH) -> dict:
    matrix_path = Path(path)
    payload = json.loads(matrix_path.read_text(encoding="utf-8"))
    validate_regression_matrix(payload)
    return payload


def validate_regression_matrix(matrix: Mapping) -> None:
    """Reject incomplete or ambiguous scenario declarations."""

    if int(matrix.get("version", 0)) < 1:
        raise ValueError("regression matrix version must be >= 1")
    policy = matrix.get("policy")
    scenarios = matrix.get("scenarios")
    if not isinstance(policy, Mapping) or not isinstance(scenarios, list):
        raise ValueError("matrix requires policy and scenarios")
    if policy.get("default_change_requires") != "all_required_runs_pass":
        raise ValueError("default changes must require all required runs")

    for name in ("topdown_sizes_km", "full_3mf_sizes_km"):
        sizes = policy.get(name)
        if not isinstance(sizes, list) or not sizes:
            raise ValueError(f"{name} must be a non-empty list")
        if any(float(size) <= 0 for size in sizes):
            raise ValueError(f"{name} values must be positive")
    for name in ("topdown_checks", "full_3mf_checks"):
        checks = policy.get(name)
        if not isinstance(checks, list) or not checks:
            raise ValueError(f"{name} must be a non-empty list")

    keys = []
    required_archetypes = set()
    for scenario in scenarios:
        if not isinstance(scenario, Mapping):
            raise ValueError("each scenario must be an object")
        key = str(scenario.get("key") or "")
        if not key:
            raise ValueError("scenario key must not be empty")
        keys.append(key)
        if scenario.get("required"):
            required_archetypes.add(str(scenario.get("archetype") or ""))
        center = scenario.get("center")
        if (not isinstance(center, list) or len(center) != 2
                or not -90 <= float(center[0]) <= 90
                or not -180 <= float(center[1]) <= 180):
            raise ValueError(f"{key}: invalid center")
        if not str(scenario.get("pbf") or "").endswith(".osm.pbf"):
            raise ValueError(f"{key}: invalid PBF filename")
        if not scenario.get("expected_traits"):
            raise ValueError(f"{key}: expected_traits must not be empty")
        if not isinstance(scenario.get("extra_checks", []), list):
            raise ValueError(f"{key}: extra_checks must be a list")
    if len(keys) != len(set(keys)):
        raise ValueError("scenario keys must be unique")
    if len([item for item in scenarios if item.get("required")]) < 3:
        raise ValueError("at least three required scenarios are needed")
    if len(required_archetypes - {""}) < 3:
        raise ValueError("required matrix must cover multiple archetypes")


def expected_runs(matrix: Mapping, *, include_advisory: bool = False) -> list[dict]:
    """Expand the matrix into deterministic run/check records."""

    validate_regression_matrix(matrix)
    policy = matrix["policy"]
    runs = []
    for scenario in matrix["scenarios"]:
        if not scenario.get("required") and not include_advisory:
            continue
        common = {
            "scenario_key": scenario["key"],
            "archetype": scenario["archetype"],
            "extra_checks": list(scenario.get("extra_checks", [])),
        }
        for mode, sizes_key, checks_key in (
            ("topdown", "topdown_sizes_km", "topdown_checks"),
            ("full_3mf", "full_3mf_sizes_km", "full_3mf_checks"),
        ):
            checks = list(policy[checks_key]) + common["extra_checks"]
            for size in policy[sizes_key]:
                runs.append({
                    **common,
                    "mode": mode,
                    "size_km": float(size),
                    "required_checks": sorted(set(checks)),
                })
    return runs


def evaluate_result_set(matrix: Mapping, results: Iterable[Mapping]) -> dict:
    """Return a blocking verdict for generated cross-city evidence."""

    expected = {
        (run["scenario_key"], run["mode"], run["size_km"]): run
        for run in expected_runs(matrix)
    }
    actual = {}
    problems = []
    for result in results:
        try:
            key = (
                str(result["scenario_key"]),
                str(result["mode"]),
                float(result["size_km"]),
            )
        except (KeyError, TypeError, ValueError):
            problems.append("result is missing scenario_key/mode/size_km")
            continue
        if key[1] not in VALID_MODES:
            problems.append(f"{key}: unsupported mode")
            continue
        if key in actual:
            problems.append(f"{key}: duplicate result")
            continue
        actual[key] = result

    for key, run in expected.items():
        result = actual.get(key)
        if result is None:
            problems.append(f"{key}: missing required run")
            continue
        if result.get("status") != "passed":
            problems.append(f"{key}: status is not passed")
        checks = result.get("checks")
        if not isinstance(checks, Mapping):
            problems.append(f"{key}: checks object is missing")
            continue
        for check in run["required_checks"]:
            if checks.get(check) is not True:
                problems.append(f"{key}: required check failed or missing: {check}")

    return {
        "passed": not problems,
        "required_run_count": len(expected),
        "received_required_run_count": len(set(expected) & set(actual)),
        "problems": problems,
    }
