#!/usr/bin/env python3
"""Close S11 using the project validator and hash-bound slicer evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aesthetic.pipeline_acceptance import accept_pipeline_artifact  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--3mf", required=True, dest="artifact", type=Path)
    parser.add_argument("--slicer-report", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    report = accept_pipeline_artifact(
        ledger_path=args.ledger,
        artifact_path=args.artifact,
        slicer_report_path=args.slicer_report,
        output_dir=args.output_dir,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
