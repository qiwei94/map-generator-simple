import pytest

from aesthetic.presets import CityPreset
from aesthetic.rerun_harness import CityHarness
import aesthetic.rerun_harness as rerun_harness


def test_harness_binds_requested_pbf_before_spatial_work(monkeypatch, tmp_path):
    requested = tmp_path / "beijing-latest.osm.pbf"
    requested.write_bytes(b"pbf-placeholder")
    preset = CityPreset(
        name="binding-test",
        bbox=(39.8, 116.2, 40.0, 116.5),
        pbf=str(requested),
        prototype="skyline",
        reference_dir="",
    )
    calls = []

    monkeypatch.setattr(
        rerun_harness,
        "set_pbf_file_path",
        lambda path: calls.append(path),
    )

    def stop_after_binding(*_args, **_kwargs):
        raise RuntimeError("stop-after-binding")

    monkeypatch.setattr(rerun_harness, "bbox_to_utm", stop_after_binding)

    with pytest.raises(RuntimeError, match="stop-after-binding"):
        CityHarness(preset, use_cache=False).prepare()

    assert calls == [str(requested)]


def test_harness_rejects_missing_city_pbf_before_fallback_scan(tmp_path):
    preset = CityPreset(
        name="missing-binding-test",
        bbox=(39.8, 116.2, 40.0, 116.5),
        pbf=str(tmp_path / "missing.osm.pbf"),
        prototype="skyline",
        reference_dir="",
    )

    with pytest.raises(FileNotFoundError, match="PBF file not found"):
        CityHarness(preset, use_cache=False).prepare()
