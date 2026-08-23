import numpy as np
import pytest

from aesthetic.metrics import _road_ink_metrics, road_ink_budget_status


def test_road_ink_excludes_water_material_from_denominator_and_numerator():
    road = np.zeros((8, 8), dtype=float)
    water = np.zeros((8, 8), dtype=float)
    water[:, :4] = 1.0
    road[:, :4] = 1.0       # Roads hidden under water must not count as ink.
    road[:2, 4:] = 1.0

    global_ratio, local_p95 = _road_ink_metrics(
        road, water, n_cells=2)

    assert global_ratio == pytest.approx(0.25)
    # Two eligible land cells contain 50% and 0% road ink; NumPy's linear
    # percentile interpolation therefore yields 47.5% at P95.
    assert local_p95 == pytest.approx(0.475)


def test_road_ink_rejects_incompatible_masks():
    with pytest.raises(ValueError, match="equal 2D arrays"):
        _road_ink_metrics(np.zeros((4, 4)), np.zeros((3, 4)))


@pytest.mark.parametrize(
    ("global_ratio", "local_p95", "passed"),
    [
        (0.0440, 0.1397, True),   # Chicago 25 km first accepted evidence
        (0.0340, 0.1181, True),   # Beijing 25 km
        (0.0383, 0.1240, True),   # Shanghai 25 km
        (0.0560, 0.1200, False),  # globally too dark
        (0.0400, 0.1700, False),  # local black knot
    ],
)
def test_road_ink_budget_is_global_and_local(global_ratio, local_p95, passed):
    status = road_ink_budget_status(global_ratio, local_p95)

    assert status["passed"] is passed
    assert status["land_ratio_max"] == 0.055
    assert status["local_p95_max"] == 0.16


def test_road_ink_budget_rejects_negative_ratios():
    with pytest.raises(ValueError, match="non-negative"):
        road_ink_budget_status(-0.01, 0.1)
