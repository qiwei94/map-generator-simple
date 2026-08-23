import numpy as np
import pytest

from aesthetic.metrics import _road_ink_metrics


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
