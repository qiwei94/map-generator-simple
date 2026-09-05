from shapely.geometry import Polygon, box

from tools.analyze_reference_3mf_shapes import silhouette_metrics


def test_silhouette_metrics_distinguish_regular_and_gnawed_components():
    regular = silhouette_metrics(box(0, 0, 4, 3), simplify_mm=0.01)
    gnawed = silhouette_metrics(Polygon([
        (0, 0), (4, 0), (4, 3), (2.4, 3), (2.4, 1.4),
        (1.6, 1.4), (1.6, 3), (0, 3),
    ]), simplify_mm=0.01)

    assert regular["solidity"] == 1.0
    assert regular["perimeter_excess"] == 1.0
    assert regular["reflex_vertex_fraction"] == 0.0
    assert gnawed["solidity"] < regular["solidity"]
    assert gnawed["perimeter_excess"] > regular["perimeter_excess"]
    assert gnawed["concavity_depth_fraction"] > 0.2
    assert gnawed["reflex_vertex_fraction"] > 0.0


def test_silhouette_metrics_report_holes_without_treating_them_as_smooth():
    ring = Polygon(
        [(0, 0), (5, 0), (5, 5), (0, 5)],
        holes=[[(2, 2), (3, 2), (3, 3), (2, 3)]],
    )

    metrics = silhouette_metrics(ring, simplify_mm=0.01)

    assert metrics["hole_count"] == 1
    assert metrics["solidity"] < 1.0
    assert metrics["perimeter_excess"] > 1.0
