#!/usr/bin/env python3
"""Validate one generated 3MF and fail unless errors and warnings are zero."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _TEXTURE_STYLE_OF_DEEPSEEK.validator import (  # noqa: E402
    print_validation_report,
    validate_3mf,
)


def _json_default(value):
    """Serialize NumPy scalar values returned by geometry checks."""

    item = getattr(value, "item", None)
    if callable(item):
        return item()
    raise TypeError(
        f"Object of type {value.__class__.__name__} is not JSON serializable")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument(
        "--design-spec",
        type=Path,
        help=("validate against this exact DesignSpec instead of the mutable "
              "design_spec.json compatibility alias"),
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        help="also persist the complete validation result as JSON",
    )
    args = parser.parse_args()

    result = validate_3mf(
        str(args.path),
        design_spec_path=(
            str(args.design_spec) if args.design_spec is not None else None),
    )
    result["strict_passed"] = bool(
        result.get("passed")
        and not result.get("errors")
        and not result.get("warnings")
    )
    payload = json.dumps(
        result, ensure_ascii=False, indent=2, default=_json_default)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    if args.json:
        print(payload)
    else:
        print_validation_report(result)
        print(f"  Strict acceptance: {'PASSED' if result['strict_passed'] else 'FAILED'}")
    return 0 if result["strict_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
