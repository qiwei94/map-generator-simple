"""Decision report output: param_decision.json."""

import json
import os
from datetime import datetime

from .city_profile import CityProfile
from .param_resolver import ResolvedParams, explain_decisions


def save_decision_report(
    profile: CityProfile,
    params: ResolvedParams,
    output_dir: str,
    city_name: str,
) -> str:
    """Write param_decision.json to output directory.

    Returns the path to the written file.
    """
    report = explain_decisions(profile, params)
    report["city"] = city_name
    report["timestamp"] = datetime.now().isoformat(timespec="seconds")

    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, "param_decision.json")

    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)

    print(f"[auto_params] Decision report saved: {path}")
    return path
