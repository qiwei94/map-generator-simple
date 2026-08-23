import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_env_doctor_json_reports_ready_without_secret_values():
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "env_doctor.py"), "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["ready"] is True
    assert report["summary"]["fail"] == 0
    assert all(
        check["detail"] in ("set", "unset (optional)")
        for check in report["checks"]
        if check["name"].startswith("env:")
    )
