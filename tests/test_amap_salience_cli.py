from generate_city_legacy import (
    _amap_salience_cache_fingerprint,
    _load_amap_salience_guide,
    parse_args,
)


def test_amap_salience_cli_is_opt_in():
    base = [
        "--bbox", "39.8,116.2,40.0,116.5",
        "--pbf", "beijing.osm.pbf",
        "--city", "beijing",
    ]

    assert parse_args(base).amap_salience == "off"
    assert parse_args([*base, "--amap-salience", "cache"]).amap_salience == (
        "cache")


def test_disabled_salience_never_loads_or_fetches_reference():
    guide, evidence = _load_amap_salience_guide(
        "off",
        (39.8, 116.2, 40.0, 116.5),
        (0, 0, 10000, 10000),
    )

    assert guide is None
    assert evidence["status"] == "disabled"
    assert _amap_salience_cache_fingerprint("off", evidence)["mode"] == "off"
