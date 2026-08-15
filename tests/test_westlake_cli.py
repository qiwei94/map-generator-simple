"""CLI contract tests for the dedicated West Lake generator."""

from real_back_up_westlake_cli import build_parser


def test_png_flags_are_opt_in():
    args = build_parser().parse_args([])

    assert args.png is False
    assert args.review_png is False


def test_png_flags_can_be_enabled_together():
    args = build_parser().parse_args(["--png", "--review-png"])

    assert args.png is True
    assert args.review_png is True
