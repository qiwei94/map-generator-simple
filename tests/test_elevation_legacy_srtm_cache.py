def test_srtm_download_reuses_repository_legacy_cache(monkeypatch, tmp_path):
    from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers import elevation

    legacy = tmp_path / "cache" / "srtm" / "N48E002.hgt"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"known-local-tile")
    monkeypatch.setattr(elevation, "_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(elevation, "DEM_CACHE_DIR", str(tmp_path / "dem_cache"))
    monkeypatch.setattr(elevation, "_CACHE_DIR", str(tmp_path / "other_cache"))

    assert elevation._download_tile(48, 2) == str(legacy)
