from shapely.geometry import Polygon, box

from aesthetic.city_surface_plan import _clean_overlay_roundoff


def test_overlay_residue_removed_without_filtering_real_small_blocks():
    residue = Polygon([(94.0071147281746, -44.861051031919985),
        (94.00711472817473, -44.861051031919864),
        (94.0071147281746, -44.86105103191999)])
    small = box(0., 0., .001, .001)
    polys, owners, evidence = _clean_overlay_roundoff([residue, small], [10, 20], 1.)
    assert owners == [20]
    assert len(polys) == 1 and polys[0].equals(small)
    assert evidence['vanished_polygons'] == 1
    assert evidence['vanished_area_mm2'] < 1e-20


def test_cleanup_scale_and_owner_height_correspondence():
    poly = box(2., 3., 6., 7.)
    polys, owners, evidence = _clean_overlay_roundoff([poly], [42], .008)
    assert owners == [42] and polys[0].symmetric_difference(poly).area < 1e-8
    assert evidence['vanished_polygons'] == 0
