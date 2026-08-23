from pathlib import Path

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers import height_enrichment


def _make_executable(path: Path) -> None:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)


def test_overture_cli_resolves_next_to_active_python(monkeypatch, tmp_path):
    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    cli = python.with_name("overturemaps")
    _make_executable(cli)

    monkeypatch.delenv("OVERTUREMAPS_BIN", raising=False)
    monkeypatch.setattr(height_enrichment.sys, "executable", str(python))
    monkeypatch.setattr(height_enrichment.shutil, "which", lambda _: None)

    assert height_enrichment._resolve_overture_cli() == str(cli.resolve())


def test_overture_cli_override_takes_priority(monkeypatch, tmp_path):
    cli = tmp_path / "custom-overturemaps"
    _make_executable(cli)
    monkeypatch.setenv("OVERTUREMAPS_BIN", str(cli))

    assert height_enrichment._resolve_overture_cli() == str(cli.resolve())


def test_download_skips_cleanly_when_cli_is_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(height_enrichment, "_resolve_overture_cli", lambda: None)

    result = height_enrichment._download_overture(
        (41.88, -87.63, 41.89, -87.62),
        cache_dir=str(tmp_path),
    )

    assert result is None
    assert list(tmp_path.iterdir()) == []
