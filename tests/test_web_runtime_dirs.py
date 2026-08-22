"""Cold-clone runtime directories are created without manual setup."""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
WEBAPP = ROOT / "webapp"
if str(WEBAPP) not in sys.path:
    sys.path.insert(0, str(WEBAPP))

import server  # noqa: E402


def test_ensure_runtime_dirs_creates_all_paths(monkeypatch, tmp_path):
    output = tmp_path / "nested" / "output"
    gallery = output / "style_gallery"
    logs = tmp_path / "nested" / "logs"
    monkeypatch.setattr(server, "OUTPUT_DIR", output)
    monkeypatch.setattr(server, "GALLERY_DIR", gallery)
    monkeypatch.setattr(server, "JOB_LOG_DIR", logs)

    server._ensure_runtime_dirs()

    assert output.is_dir()
    assert gallery.is_dir()
    assert logs.is_dir()
