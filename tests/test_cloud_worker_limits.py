"""Cloud worker defaults leave enough time for dense 15 km galleries."""
from pathlib import Path


def test_worker_default_timeout_and_bounded_mode_are_exposed():
    source = (Path(__file__).resolve().parents[1] / "tools" /
              "cloud_worker.py").read_text(encoding="utf-8")

    assert "timeout_s: int = 7200" in source
    assert 'ap.add_argument("--task-timeout"' in source
    assert 'ap.add_argument("--max-tasks"' in source
    assert "completed_tasks >= args.max_tasks" in source
