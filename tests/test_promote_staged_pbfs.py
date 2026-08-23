from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import promote_staged_pbfs as promoter  # noqa: E402


def test_promote_requires_exact_size_and_uses_final_name(tmp_path):
    source = tmp_path / "stage" / "city.osm.pbf"
    destination = tmp_path / "hot" / "city.osm.pbf"
    source.parent.mkdir()
    source.write_bytes(b"partial")

    assert not promoter.promote(source, destination, 8)
    assert not destination.exists()

    source.write_bytes(b"complete")
    assert promoter.promote(source, destination, 8)
    assert destination.read_bytes() == b"complete"
    assert not (destination.parent / ".city.osm.pbf.incoming").exists()


def test_selected_files_rejects_paths_and_unknown_names():
    sizes = {"city.osm.pbf": 8}

    assert promoter.selected_files("city.osm.pbf", sizes) == ["city.osm.pbf"]
    for unsafe in ("../city.osm.pbf", "unknown.osm.pbf"):
        try:
            promoter.selected_files(unsafe, sizes)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted unsafe name: {unsafe}")
