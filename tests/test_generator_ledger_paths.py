from pathlib import Path

from generate_city_legacy import _absolute_artifact_paths


def test_generator_normalizes_project_relative_artifacts_before_ledger(
        tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    artifact = Path("output/city/scene_character.json")
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{}", encoding="utf-8")

    normalized = _absolute_artifact_paths({"scene": artifact})
    assert normalized == {"scene": str(artifact.resolve())}


def test_generator_preserves_empty_artifact_mapping():
    assert _absolute_artifact_paths(None) is None
    assert _absolute_artifact_paths({}) == {}
