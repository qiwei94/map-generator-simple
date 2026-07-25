"""逐轮留痕：trajectory.jsonl + 最终 summary.json."""

import json
import os
from datetime import datetime


class Trajectory:
    def __init__(self, out_dir: str):
        os.makedirs(out_dir, exist_ok=True)
        self.out_dir = out_dir
        self.path = os.path.join(out_dir, "trajectory.jsonl")

    def log(self, record: dict) -> None:
        rec = {"ts": datetime.now().isoformat(timespec="seconds"), **record}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    def write_summary(self, payload: dict) -> str:
        path = os.path.join(self.out_dir, "summary.json")
        payload = {"ts": datetime.now().isoformat(timespec="seconds"), **payload}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
        return path
