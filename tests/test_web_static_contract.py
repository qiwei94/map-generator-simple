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


def test_app_script_cache_key_is_bumped_for_hero_sample_carousel():
    html = INDEX_HTML.read_text(encoding="utf-8")
    source = APP_JS.read_text(encoding="utf-8")

    assert '<script src="app.js?v=54"></script>' in html
    assert '<link rel="stylesheet" href="style.css?v=53">' in html
    assert 'id="accountDialog"' in html
    assert 'id="myTasksCard"' in html
    assert 'id="heroShowcase"' in html
    assert 'id="heroShowcasePrev"' in html
    assert 'id="heroShowcaseNext"' in html
    assert 'id="showcaseTrack"' not in html
    assert "15 + 25 KM" in html
    assert 'id="baseThickness"' not in html
    assert "westlake-15km-standard.jpg" not in html
    assert "westlake-15km-block-fill.jpg" not in html
    assert 'src="assets/westlake-real-output.jpg"' in html
    assert "westlake-real-output.jpg" in source
    assert "chicago-15km-dense.jpg" in source


def test_fixed_framing_tiers_and_center_preview_are_explained():
    html = INDEX_HTML.read_text(encoding="utf-8")
    source = APP_JS.read_text(encoding="utf-8")

    assert 'data-km="15"' in html
    assert 'data-km="25"' in html
    assert 'data-km="5"' not in html
    assert 'data-km="10"' not in html
    assert "const TIERS = [15, 25]" in source
    assert "中心 5 km" in html
    assert "完整 15 / 25 km" in html
    assert "framing.recommended_size_km" in source


def test_hero_samples_autoplay_and_keep_manual_controls():
    source = APP_JS.read_text(encoding="utf-8")

    assert "const HERO_AUTOPLAY_MS = 5200" in source
    assert "window.setTimeout" in source
    assert "showHeroSample(heroRequestedIndex + 1)" in source
    assert '$("heroShowcasePrev").onclick' in source
    assert '$("heroShowcaseNext").onclick' in source


def test_hero_sample_image_and_caption_commit_as_one_versioned_state():
    source = APP_JS.read_text(encoding="utf-8")

    assert "const requestId = ++heroRenderRequestId" in source
    assert "window.clearTimeout(heroAutoplayTimer)" in source
    assert "preloadHeroImage(sample.url).then(commit)" in source
    assert "if (requestId !== heroRenderRequestId) return" in source
    assert "heroSampleIndex = targetIndex" in source
    assert "image.dataset.sampleUrl = sample.url" in source
    assert "window.setTimeout(apply, 130)" not in source
