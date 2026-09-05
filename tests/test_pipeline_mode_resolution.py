import pytest

from generate_city_legacy import _pipeline_mode, parse_args


BASE_ARGS = [
    "--bbox", "30.1,120.0,30.2,120.1",
    "--pbf", "fixture.osm.pbf",
    "--city", "fixture-city",
]


def test_vegetation_surface_is_opt_in():
    assert parse_args(BASE_ARGS).no_vegetation is True
    assert parse_args([*BASE_ARGS, "--vegetation"]).no_vegetation is False
    assert parse_args([*BASE_ARGS, "--no-vegetation"]).no_vegetation is True


def test_cli_draft_and_review_modes_are_unambiguous(monkeypatch):
    monkeypatch.delenv("MAP_PIPELINE_MODE", raising=False)
    assert _pipeline_mode(parse_args([*BASE_ARGS, "--draft"])) == "draft"
    assert _pipeline_mode(parse_args([
        *BASE_ARGS, "--draft", "--review-png", "--review-only",
    ])) == "review"


def test_fetch_and_styles_may_be_selected_by_job_envelope(monkeypatch):
    args = parse_args(BASE_ARGS)
    monkeypatch.setenv("MAP_PIPELINE_MODE", "fetch")
    assert _pipeline_mode(args) == "fetch"
    monkeypatch.setenv("MAP_PIPELINE_MODE", "styles")
    assert _pipeline_mode(args) == "styles"


@pytest.mark.parametrize(
    ("environment_mode", "cli_tail"),
    [
        ("full", ["--draft"]),
        ("draft", []),
        ("review", []),
        ("styles", ["--draft"]),
    ],
)
def test_conflicting_environment_and_cli_modes_fail_closed(
        monkeypatch, environment_mode, cli_tail):
    monkeypatch.setenv("MAP_PIPELINE_MODE", environment_mode)
    with pytest.raises(ValueError, match="disagrees"):
        _pipeline_mode(parse_args([*BASE_ARGS, *cli_tail]))
