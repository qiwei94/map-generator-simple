"""Contracts for browser assets that are easy to break without a JS build step."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "webapp" / "static" / "app.js"
INDEX_HTML = ROOT / "webapp" / "static" / "index.html"


def test_quality_check_renderer_has_html_escape_helper():
    source = APP_JS.read_text(encoding="utf-8")

    assert "const esc = (value)" in source
    assert source.index("const esc = (value)") < source.index(
        "function renderQualityChecks"
    )
    assert "${esc(check.label)}" in source
    assert "${esc(check.detail)}" in source


def test_restored_gallery_uses_original_bbox_and_server_identity():
    source = APP_JS.read_text(encoding="utf-8")

    assert "function restoreJobArea(job)" in source
    assert "state.galleryBbox = bbox" in source
    assert "body.gallery_slug = state.gallerySlug" in source
    assert "if (j.mode === \"styles\") restoreJobArea(j);" in source


def test_restored_gallery_also_loads_existing_preview_artifacts():
    source = APP_JS.read_text(encoding="utf-8")

    styles_branch = source[source.index('if (mode === "styles")'):]
    assert "await refreshArtifacts(state.jobSlug);" in styles_branch


def test_app_script_cache_key_is_bumped_for_account_queue_ui():
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert '<script src="app.js?v=44"></script>' in html
    assert 'id="accountDialog"' in html
    assert 'id="myTasksCard"' in html
