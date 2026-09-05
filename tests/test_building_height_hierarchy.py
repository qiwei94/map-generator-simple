import numpy as np
import pytest
import trimesh
from shapely.geometry import box

from _TEXTURE_STYLE_OF_DEEPSEEK._layer_preprocess import LayerPolygons
from _TEXTURE_STYLE_OF_DEEPSEEK.print_profile import DEFAULT_PRINTER_PROFILE
from aesthetic.building_height_hierarchy import (
    POLICY_VERSION,
    apply_building_height_hierarchy,
    cap_building_heights_to_terrain,
    route_sub_nozzle_heroes,
)


def _terrain():
    vertices = np.array([
        [-10.0, -10.0, 0.0],
        [10.0, -10.0, 0.0],
        [-10.0, 10.0, 0.0],
        [10.0, 10.0, 2.0],
    ])
    faces = np.array([[0, 1, 2], [1, 3, 2]])
    return trimesh.Trimesh(vertices=vertices, faces=faces, process=False)


def test_terrain_owned_scene_compresses_hero_height_without_geometry_change():
    polygon = box(-1, -1, 1, 1)
    layers = LayerPolygons(
        BL=[(polygon, 4.44)],
        BL_height_roles=["identity_exact"],
    )
    evidence = apply_building_height_hierarchy(
        layers,
        _terrain(),
        scale_mm_per_m=1.0,
        scene_policy={
            "activation": "active",
            "archetype": "water_terrain_garden_city",
        },
        printer_profile=DEFAULT_PRINTER_PROFILE,
    )

    assert evidence["policy_version"] == POLICY_VERSION
    assert evidence["status"] == "active"
    assert layers.BL[0][0].equals(polygon)
    assert layers.BL[0][1] < 4.44
    assert layers.BL[0][1] / 0.12 == pytest.approx(
        round(layers.BL[0][1] / 0.12))
    assert evidence["geometry_changed"] is False


def test_audit_only_policy_does_not_change_height():
    layers = LayerPolygons(BL=[(box(0, 0, 1, 1), 4.0)])
    evidence = apply_building_height_hierarchy(
        layers,
        _terrain(),
        scale_mm_per_m=1.0,
        scene_policy={
            "activation": "audit_only",
            "archetype": "water_terrain_garden_city",
        },
        printer_profile=DEFAULT_PRINTER_PROFILE,
    )

    assert evidence["status"] == "inactive"
    assert layers.BL[0][1] == 4.0


def test_watertight_base_does_not_fake_terrain_relief():
    vertices = np.array([
        [-10.0, -10.0, -1.6], [10.0, -10.0, -1.6],
        [-10.0, 10.0, -1.6], [10.0, 10.0, -1.6],
        [-10.0, -10.0, 0.0], [10.0, -10.0, 0.0],
        [-10.0, 10.0, 0.0], [10.0, 10.0, 0.0],
    ])
    faces = np.array([[0, 1, 2], [1, 3, 2], [4, 5, 6], [5, 7, 6]])
    layers = LayerPolygons(BL=[(box(0, 0, 1, 1), 4.0)])
    evidence = apply_building_height_hierarchy(
        layers,
        trimesh.Trimesh(vertices=vertices, faces=faces, process=False),
        scale_mm_per_m=1.0,
        scene_policy={
            "activation": "active",
            "archetype": "water_terrain_garden_city",
        },
        printer_profile=DEFAULT_PRINTER_PROFILE,
    )

    assert evidence["status"] == "invalid_terrain_evidence"
    assert evidence["terrain_relief_mm"] == 0.0
    assert layers.BL[0][1] == 4.0


def test_exact_frame_sliver_is_demoted_to_city_mass():
    sliver = box(0, 0, 0.3, 2.0)
    printable = box(2, 0, 3, 2)
    layers = LayerPolygons(
        BL=[(sliver, 1.2), (printable, 1.2)],
        BL_height_roles=["identity_exact", "identity_exact"],
        BL_categories=["semantic", "semantic"],
        BO=[],
        BO_heights=[],
    )
    evidence = apply_building_height_hierarchy(
        layers,
        _terrain(),
        scale_mm_per_m=1.0,
        scene_policy={
            "activation": "active",
            "archetype": "water_terrain_garden_city",
        },
        printer_profile=DEFAULT_PRINTER_PROFILE,
    )

    assert evidence["sub_nozzle_heroes_demoted_to_mass"] == 1
    assert [item[0] for item in layers.BL] == [printable]
    assert layers.BL_height_roles == ["identity_exact"]
    assert layers.BL_categories == ["semantic"]
    assert layers.BO == [sliver]
    assert layers.BO_heights == [DEFAULT_PRINTER_PROFILE.min_surface_height_mm]


def test_routing_and_terrain_cap_are_explicitly_separated():
    sliver = box(0, 0, 0.3, 2.0)
    hero = box(2, 0, 3, 2)
    layers = LayerPolygons(
        BL=[(sliver, 4.0), (hero, 4.0)],
        BL_height_roles=["identity_exact", "identity_exact"],
        BO=[],
        BO_heights=[],
    )
    policy = {
        "activation": "active",
        "archetype": "water_terrain_garden_city",
    }

    routing = route_sub_nozzle_heroes(
        layers,
        scale_mm_per_m=1.0,
        scene_policy=policy,
        printer_profile=DEFAULT_PRINTER_PROFILE,
    )

    assert routing["stage_role"] == "pre_building_mass_routing"
    assert routing["sub_nozzle_heroes_demoted_to_mass"] == 1
    assert layers.BL == [(hero, 4.0)]
    assert layers.BO == [sliver]
    # Routing owns XY semantics only; it must not pre-emptively alter height.
    assert layers.BL[0][1] == 4.0

    capping = cap_building_heights_to_terrain(
        layers,
        _terrain(),
        scale_mm_per_m=1.0,
        scene_policy=policy,
        printer_profile=DEFAULT_PRINTER_PROFILE,
    )

    assert capping["stage_role"] == "post_building_mass_terrain_cap"
    assert capping["geometry_changed"] is False
    assert layers.BL[0][0].equals(hero)
    assert layers.BL[0][1] < 4.0


def test_terrain_cap_rejects_implicit_scale_state():
    layers = LayerPolygons(BL=[(box(0, 0, 1, 1), 2.0)])
    with pytest.raises(ValueError, match="scale_mm_per_m"):
        cap_building_heights_to_terrain(
            layers,
            _terrain(),
            scale_mm_per_m=0.0,
            scene_policy={
                "activation": "active",
                "archetype": "water_terrain_garden_city",
            },
            printer_profile=DEFAULT_PRINTER_PROFILE,
        )
