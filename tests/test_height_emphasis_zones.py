from types import SimpleNamespace

import geopandas as gpd
from shapely.geometry import box

from _TEXTURE_STYLE_OF_DEEPSEEK.print_profile import PrinterProfile
from aesthetic.height_emphasis_zones import (
    POLICY_VERSION,
    apply_height_emphasis_zones,
    prepare_region_first_height_roles,
)


def _cell(row, column, *, score, coverage, count=100, water=0.0):
    x0 = column * 1000.0
    y0 = row * 1000.0
    return {
        "row": row,
        "column": column,
        "bounds": [x0, y0, x0 + 1000.0, y0 + 1000.0],
        "building_count": count,
        "building_coverage": coverage,
        "urban_signal": score,
        "neighbor_urban_signal": score,
        "landmark_focus_score": 0.2 * score,
        "water_fraction": water,
    }


def _scene():
    cells = []
    for row in range(3):
        for column in range(3):
            signal = 0.50
            coverage = 0.04
            if (row, column) == (0, 0):
                signal, coverage = 0.95, 0.22
            if (row, column) == (2, 2):
                signal, coverage = 0.90, 0.20
            cells.append(_cell(row, column, score=signal, coverage=coverage))
    return {
        "grid_size": 3,
        "cells": cells,
        "summary": {"landmark_focus_cell_limit": 2},
    }


def _layers():
    mass = [
        box(300, 300, 450, 420),
        box(550, 500, 690, 620),
        box(2300, 2300, 2450, 2420),
        box(2500, 2500, 2650, 2620),
        box(1300, 1300, 1450, 1420),
    ]
    quiet = box(100, 700, 150, 750)
    return SimpleNamespace(
        BL=[],
        BL_categories=[],
        BL_height_roles=[],
        BO=[quiet] + mass,
        BO_heights=[0.24] + [0.84] * len(mass),
        building_height_evidence={},
    )


def _buildings():
    return gpd.GeoDataFrame({
        "height_source": ["wikidata", "osm_levels", "default"],
        "est_height": [120.0, 60.0, 10.0],
        "geometry": [
            box(320, 320, 350, 350),
            box(2320, 2320, 2350, 2350),
            box(1400, 1400, 1430, 1430),
        ],
    }, crs="EPSG:3857")


def _apply(layers):
    return apply_height_emphasis_zones(
        layers,
        _buildings(),
        _scene(),
        {"activation": "active"},
        printer_profile=PrinterProfile(),
        scale_mm_per_m=0.01,
        building_mass_evidence={"status": "active"},
    )


def test_prepare_demotes_source_heroes_without_reauthoring_geometry():
    hero = box(0, 0, 100, 100)
    quiet = box(200, 0, 300, 100)
    layers = SimpleNamespace(
        BL=[(hero, 2.4)],
        BL_categories=["landmark"],
        BL_height_roles=["identity_exact"],
        BO=[quiet],
        BO_heights=[0.24],
    )

    result = prepare_region_first_height_roles(layers)

    assert result["policy_version"] == POLICY_VERSION
    assert result["demoted_source_heroes"] == 1
    assert result["geometry_changed"] is False
    assert layers.BL == []
    assert [polygon.wkb for polygon in layers.BO] == [quiet.wkb, hero.wkb]
    assert layers.BO_heights == [0.24, 0.24]


def test_region_first_promotes_only_unchanged_printable_mass_components():
    layers = _layers()
    original_wkb = [polygon.wkb for polygon in layers.BO]

    result = _apply(layers)

    assert result["status"] == "active"
    assert result["selected_zone_count"] == 2
    assert result["promoted_component_count"] == 4
    assert result["new_footprints_created"] == 0
    assert result["cross_topology_merges"] == 0
    assert result["road_water_topology_preserved"] is True
    assert layers.BL_height_roles == ["zone_height_mass"] * 4
    assert all(height / 0.12 == round(height / 0.12)
               for _polygon, height in layers.BL)
    assert all(height > 0.84 for _polygon, height in layers.BL)
    # Every promoted polygon is byte-for-byte one of the pre-existing mass
    # components.  The region policy may select, but may not author geometry.
    assert all(polygon.wkb in original_wkb for polygon, _height in layers.BL)
    assert layers.BO_heights == [0.24, 0.84]


def test_region_first_is_deterministic_and_ignores_quiet_texture():
    first_layers = _layers()
    second_layers = _layers()

    first = _apply(first_layers)
    second = _apply(second_layers)

    assert first["selected_zones"] == second["selected_zones"]
    assert first["promoted_components"] == second["promoted_components"]
    assert [polygon.wkb for polygon, _height in first_layers.BL] == [
        polygon.wkb for polygon, _height in second_layers.BL]
    assert first_layers.BO[0].equals(box(100, 700, 150, 750))


def test_region_first_refuses_to_run_without_active_mass():
    layers = _layers()
    result = apply_height_emphasis_zones(
        layers,
        _buildings(),
        _scene(),
        {"activation": "active"},
        printer_profile=PrinterProfile(),
        scale_mm_per_m=0.01,
        building_mass_evidence={"status": "guarded_fallback"},
    )

    assert result["status"] == "inactive"
    assert "building mass" in result["reason"]
    assert layers.BL == []
