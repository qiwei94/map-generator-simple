"""CLI contract tests for the official West Lake generator entry point."""

from generate_city import build_parser
from real_back_up_westlake_cli import build_parser as compatibility_parser


def test_png_flags_are_opt_in():
    args = build_parser().parse_args([])

    assert args.png is False
    assert args.review_png is False


def test_png_flags_can_be_enabled_together():
    args = build_parser().parse_args(["--png", "--review-png"])

    assert args.png is True
    assert args.review_png is True


def test_historical_entry_uses_the_official_parser():
    args = compatibility_parser().parse_args(["--png"])

    assert args.png is True
    assert args.review_png is False
