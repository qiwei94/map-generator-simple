import json
from pathlib import Path
import subprocess
import sys

import trimesh

from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf


ROOT = Path(__file__).resolve().parents[1]


def test_cli_json_reports_strict_zero_warning_acceptance(tmp_path):
    model = tmp_path / "valid.3mf"
    export_deepseek_3mf(
        {"terrain": trimesh.creation.box(extents=[196, 196, 4])},
        str(model),
    )

    completed = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "validate_3mf.py"),
         str(model), "--json"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    report = json.loads(completed.stdout)

    assert completed.returncode == 0
    assert report["strict_passed"] is True
    assert report["errors"] == []
    assert report["warnings"] == []


def test_cli_fails_for_missing_model(tmp_path):
    completed = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "validate_3mf.py"),
         str(tmp_path / "missing.3mf"), "--json"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    report = json.loads(completed.stdout)

    assert completed.returncode == 1
    assert report["strict_passed"] is False
    assert report["errors"]
