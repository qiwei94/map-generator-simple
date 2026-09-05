from aesthetic.building_data_quality import resolve_local_building_quality


def _cells(grid_size=4, *, building_coverage=0.10, building_count=80,
           road_density=14.0, junction_density=50.0):
    result = []
    for row in range(grid_size):
        for column in range(grid_size):
            result.append({
                "row": row,
                "column": column,
                "bounds": [column * 1000, row * 1000,
                           (column + 1) * 1000, (row + 1) * 1000],
                "building_coverage": building_coverage,
                "building_count": building_count,
                "road_density_km_km2": road_density,
                "major_road_density_km_km2": 3.0,
                "junction_density_km2": junction_density,
                "water_fraction": 0.0,
            })
    return result


def _resolve(cells, **kwargs):
    return resolve_local_building_quality(
        cells,
        grid_size=4,
        building_metrics={
            "regularization_pressure": 0.82,
            "independent_survival_fraction": 0.05,
        },
        terrain_metrics={"status": "unavailable"},
        **kwargs,
    )


def test_complete_sub_nozzle_city_resolves_neighborhood_mass():
    report = _resolve(_cells())

    assert report["version"] == "local-building-quality-v1"
    assert report["city_name_lookup"] is False
    assert report["summary"]["strategy_counts"] == {
        "neighborhood_mass": 16}
    assert report["summary"]["local_data_completeness_score"] >= 0.95
    assert report["summary"]["distribution_profile"] == (
        "continuous_urban_fabric")
    assert report["summary"]["recommended_representation"] == (
        "neighborhood_mass_with_selective_hero_footprints")


def test_road_supported_hole_uses_block_base_only_with_urban_context():
    cells = _cells()
    hole = next(cell for cell in cells
                if cell["row"] == 2 and cell["column"] == 2)
    hole["building_coverage"] = 0.0
    hole["building_count"] = 0

    report = _resolve(cells)
    resolved = next(cell for cell in report["cells"]
                    if cell["row"] == 2 and cell["column"] == 2)

    assert resolved["strategy"] == "block_base_support"
    assert resolved["weights"]["block_base"] > 0.8
    assert resolved["scores"]["road_building_contradiction"] >= 0.42


def test_uniformly_sparse_scene_is_not_filled_from_low_density_alone():
    cells = _cells(
        building_coverage=0.0, building_count=0,
        road_density=1.0, junction_density=0.0)

    report = _resolve(cells)

    assert report["summary"]["strategy_counts"] == {
        "open_space_preserve": 16}
    assert report["summary"]["suspected_building_gap_cells"] == 0


def test_external_urban_map_can_raise_gap_confidence_but_not_geometry_authority():
    cells = _cells(
        building_coverage=0.0, building_count=0,
        road_density=14.0, junction_density=50.0)
    report = _resolve(cells, external_urban={
        "status": "evidence_only",
        "urban_network_support": 0.92,
        "road_presence_cell_fraction": 0.88,
    })

    assert report["summary"]["strategy_counts"] == {
        "block_base_support": 16}
    assert report["external_evidence_role"] == (
        "confidence_only_never_geometry")
    assert report["geometry_authority"] == "source vectors only"


def test_water_cells_never_receive_urban_mass():
    cells = _cells()
    cells[0]["water_fraction"] = 0.9

    report = _resolve(cells)

    assert report["cells"][0]["strategy"] == "water"
    assert report["cells"][0]["weights"] == {
        "block_base": 0.0,
        "neighborhood_mass": 0.0,
        "literal_footprints": 0.0,
    }


def test_complete_city_with_large_open_area_preserves_it_for_crosscheck():
    cells = _cells()
    for cell in cells:
        if cell["column"] == 0:
            cell["building_coverage"] = 0.0
            cell["building_count"] = 0
            cell["road_density_km_km2"] = 0.0
            cell["major_road_density_km_km2"] = 0.0
            cell["junction_density_km2"] = 0.0

    report = _resolve(cells)

    assert report["summary"]["distribution_profile"] == (
        "continuous_urban_with_large_open_space")
    assert "crosscheck_water" in report["summary"][
        "recommended_representation"]
