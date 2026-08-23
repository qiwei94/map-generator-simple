from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import launch_showcase_batch as launcher  # noqa: E402


def test_build_command_preserves_partition_and_size():
    args = argparse.Namespace(
        only="chicago,new_york", size_km=25.0, min_free_gb=8.0,
        force=False, fail_fast=True,
        pbf_size_manifest="data/pbf.json", wait_seconds=3600,
        poll_seconds=15)

    command = launcher.build_command(args)

    assert command[0] == sys.executable
    assert command[1].endswith("tools/generate_showcase_samples.py")
    assert command[command.index("--size-km") + 1] == "25"
    assert command[command.index("--only") + 1] == "chicago,new_york"
    assert "--force" not in command
    assert "--fail-fast" in command
    assert command[command.index("--pbf-size-manifest") + 1] == "data/pbf.json"
    assert command[command.index("--wait-seconds") + 1] == "3600"
