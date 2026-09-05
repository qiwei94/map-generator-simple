import numpy as np

from aesthetic.landform_character import analyze_landform_character


FRAME = (0.0, 0.0, 15000.0, 15000.0)


def _coordinates(size=129):
    return np.mgrid[-1:1:complex(size), -1:1:complex(size)]


def test_isolated_peak_has_single_dominant_prominence():
    y, x = _coordinates()
    elevation = 200.0 + 2600.0 * np.exp(
        -((x / 0.18) ** 2 + (y / 0.16) ** 2))

    report = analyze_landform_character(elevation, FRAME)

    assert report["status"] == "ready"
    assert report["peaks"]["significant_count"] == 1
    assert report["scores"]["isolated_prominence"] >= 0.75
    assert report["scores"]["crater_rim"] < 0.20


def test_elongated_highland_is_a_ridge_not_an_isolated_monolith():
    y, x = _coordinates()
    elevation = (300.0 + 1800.0 * np.exp(-(x / 0.12) ** 2)
                 * np.exp(-(y / 0.75) ** 8))

    report = analyze_landform_character(elevation, FRAME)

    assert report["scores"]["ridge_network"] >= 0.75
    assert (report["scores"]["ridge_network"]
            > report["scores"]["isolated_prominence"])


def test_closed_rim_is_distinguished_from_open_linear_valley():
    y, x = _coordinates()
    radius = np.hypot(x, y)
    crater = (1000.0
              + 900.0 * np.exp(-((radius - 0.32) / 0.055) ** 2)
              - 250.0 * np.exp(-(radius / 0.18) ** 2))
    canyon = (1500.0 - 1100.0 * np.exp(-(x / 0.075) ** 2)
              * np.exp(-(y / 0.9) ** 8))

    crater_report = analyze_landform_character(crater, FRAME)
    canyon_report = analyze_landform_character(canyon, FRAME)

    assert crater_report["scores"]["crater_rim"] >= 0.75
    assert crater_report["depressions"]["rim_closure"] >= 0.90
    assert canyon_report["scores"]["canyon_valley"] >= 0.75
    assert canyon_report["scores"]["crater_rim"] < 0.35


def test_repeated_cones_and_open_plain_remain_distinct():
    y, x = _coordinates()
    cones = 100.0 + sum(
        700.0 * np.exp(-(((x - center_x) / 0.10) ** 2
                         + ((y - center_y) / 0.10) ** 2))
        for center_x, center_y in (
            (-0.45, -0.30), (0.0, -0.20), (0.45, -0.25),
            (-0.25, 0.35), (0.30, 0.40))
    )
    plain = 300.0 + 4.0 * np.sin(x * 3.0) * np.cos(y * 2.0)

    cones_report = analyze_landform_character(cones, FRAME)
    plain_report = analyze_landform_character(plain, FRAME)

    assert cones_report["peaks"]["significant_count"] == 5
    assert cones_report["scores"]["repeated_cones"] >= 0.50
    assert plain_report["scores"]["open_plain"] >= 0.80
    assert plain_report["scores"]["crater_rim"] < 0.10


def test_missing_or_tiny_dem_is_explicitly_unavailable():
    assert analyze_landform_character(None, FRAME)["status"] == "unavailable"
    assert analyze_landform_character(np.zeros((4, 4)), FRAME)["status"] == (
        "unavailable")
