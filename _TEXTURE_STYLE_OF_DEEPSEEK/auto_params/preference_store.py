"""User preference learning (Layer 3, Spec §10.3).

Records accept/reject judgments on generated PNGs, then extracts
style bias from accumulated preferences to feed into resolve_params.
"""

import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional


@dataclass
class PreferenceRecord:
    """A single user judgment on a generated output."""
    city: str
    params: dict
    png_path: str
    verdict: str         # "accept" or "reject"
    user_note: str = ""
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat(timespec="seconds")


class PreferenceStore:
    """Append-only JSONL preference log with bias extraction."""

    def __init__(self, log_path: str = "preference_log.jsonl"):
        self._path = log_path

    def record(self, rec: PreferenceRecord) -> None:
        """Append a preference record to the log."""
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")

    def load_all(self) -> list[PreferenceRecord]:
        """Load all records from the log file."""
        if not os.path.exists(self._path):
            return []

        records = []
        with open(self._path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    records.append(PreferenceRecord(**data))
                except (json.JSONDecodeError, TypeError):
                    continue
        return records

    def extract_bias(self, min_records: int = 10) -> dict:
        """Extract parameter bias from preference history.

        Returns a dict of param adjustments (can be passed as user_overrides).
        Only activates after min_records judgments.
        """
        records = self.load_all()
        if len(records) < min_records:
            return {}

        accepted = [r for r in records if r.verdict == "accept"]
        rejected = [r for r in records if r.verdict == "reject"]

        if not accepted:
            return {}

        # Compare mean param values between accepted and rejected sets
        bias = {}
        target_params = [
            "z_gamma", "building_density_threshold",
            "road_width_multiplier", "vegetation_min_area_m2",
        ]

        for param in target_params:
            acc_vals = [
                r.params.get(param) for r in accepted
                if param in r.params and r.params[param] is not None
            ]
            rej_vals = [
                r.params.get(param) for r in rejected
                if param in r.params and r.params[param] is not None
            ]

            if not acc_vals:
                continue

            acc_mean = sum(acc_vals) / len(acc_vals)

            if rej_vals:
                rej_mean = sum(rej_vals) / len(rej_vals)
                # Bias toward accepted mean
                if abs(acc_mean - rej_mean) / max(acc_mean, 0.001) > 0.1:
                    bias[param] = acc_mean

        return bias

    @property
    def count(self) -> int:
        """Number of records in the store."""
        if not os.path.exists(self._path):
            return 0
        with open(self._path, "r") as f:
            return sum(1 for line in f if line.strip())
