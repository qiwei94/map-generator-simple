from types import SimpleNamespace

from tools.batch_generate_gallery import (
    LANDSCAPE_STYLE_VARIANTS,
    STYLE_VARIANTS,
    classify_scene_type,
    variants_for_scene,
)


def _profile(**overrides):
    values = {
        "building_density": 800.0,
        "water_ratio": 0.01,
        "elevation_range_m": 20.0,
        "vegetation_ratio": 0.03,
        "road_density_km_per_km2": 20.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_sparse_large_lake_uses_water_landscape_scene():
    scene = classify_scene_type(_profile(
        building_density=1.7,
        water_ratio=0.42,
        elevation_range_m=331.0,
        vegetation_ratio=0.19,
    ), "landscape")

    assert scene == "water_landscape"
    variants = variants_for_scene(scene)
    assert variants is LANDSCAPE_STYLE_VARIANTS
    assert variants["minimal"]["delta"]["road_width_multiplier"] == (
        "set", 2.0)
    assert variants["minimal"]["label"] == "山水留白"


def test_dense_skyline_keeps_urban_variants():
    scene = classify_scene_type(_profile(
        building_density=2500.0, water_ratio=0.08), "skyline")

    assert scene == "urban"
    assert variants_for_scene(scene) is STYLE_VARIANTS


def test_dense_road_network_overrides_sparse_terrain_hint():
    scene = classify_scene_type(_profile(
        building_density=198.3,
        road_density_km_per_km2=26.1,
        water_ratio=0.009,
        elevation_range_m=484.0,
        vegetation_ratio=0.095,
    ), "terrain")

    assert scene == "urban"


def test_sparse_mountain_without_city_roads_remains_landscape():
    scene = classify_scene_type(_profile(
        building_density=20.0,
        road_density_km_per_km2=2.0,
        water_ratio=0.01,
        elevation_range_m=484.0,
        vegetation_ratio=0.15,
    ), "terrain")

    assert scene == "landscape"
