import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString, Polygon, box

from aesthetic.scene_character import analyze_scene_character


FRAME = (0.0, 0.0, 4000.0, 4000.0)


def _empty():
    return gpd.GeoDataFrame({"geometry": []}, geometry="geometry",
                            crs="EPSG:3857")


def _regular_grid():
    positions = list(range(250, 4000, 500))
    lines = []
    for value in positions:
        lines.append(LineString([(x, value) for x in positions]))
        lines.append(LineString([(value, y) for y in positions]))
    return gpd.GeoDataFrame(
        {"highway": ["residential"] * len(lines), "geometry": lines},
        geometry="geometry", crs="EPSG:3857")


def _uniform_buildings(skip=None):
    geometries = []
    for row in range(4):
        for column in range(4):
            if skip == (row, column):
                continue
            x, y = column * 1000 + 300, row * 1000 + 300
            geometries.extend((box(x, y, x + 120, y + 120),
                               box(x + 220, y, x + 340, y + 120)))
    return gpd.GeoDataFrame(
        {"building": ["yes"] * len(geometries), "geometry": geometries},
        geometry="geometry", crs="EPSG:3857")


def test_regular_dense_scene_reports_grid_and_dense_core():
    report = analyze_scene_character(
        _regular_grid(), _uniform_buildings(), _empty(), FRAME, grid_size=4)

    assert report["version"] == "scene-character-v1"
    assert "grid_structure" in report["summary"]["traits"]
    assert "dense_urban_core" in report["summary"]["traits"]
    assert report["summary"]["osm_internal_consistency"] == "high"
    assert sum("grid" in cell["roles"] for cell in report["cells"]) >= 8


def test_large_water_mass_creates_water_and_waterfront_roles():
    water = gpd.GeoDataFrame(
        {"natural": ["water"],
         "geometry": [Polygon([(0, 0), (1900, 0), (1900, 4000), (0, 4000)])]},
        geometry="geometry", crs="EPSG:3857")
    report = analyze_scene_character(
        _regular_grid(), _uniform_buildings(), water, FRAME, grid_size=4)

    assert "water_led" in report["summary"]["traits"]
    assert any(cell["dominant_role"] == "water" for cell in report["cells"])
    assert any("waterfront" in cell["roles"] for cell in report["cells"])


def test_local_empty_cell_surrounded_by_urban_evidence_is_only_suspected():
    lines = []
    buildings = []
    grid_size = 5
    cell_width = 4000 / grid_size
    for row in range(grid_size):
        for column in range(grid_size):
            if (row, column) == (2, 2):
                continue
            x, y = column * cell_width, row * cell_width
            center_x, center_y = x + cell_width / 2, y + cell_width / 2
            lines.extend((
                LineString([(x + 80, center_y), (center_x, center_y),
                            (x + cell_width - 80, center_y)]),
                LineString([(center_x, y + 80), (center_x, center_y),
                            (center_x, y + cell_width - 80)]),
            ))
            buildings.append(box(x + 180, y + 180, x + 360, y + 360))
    roads = gpd.GeoDataFrame(
        {"highway": ["residential"] * len(lines), "geometry": lines},
        geometry="geometry", crs="EPSG:3857")
    building_gdf = gpd.GeoDataFrame(
        {"building": ["yes"] * len(buildings), "geometry": buildings},
        geometry="geometry", crs="EPSG:3857")

    report = analyze_scene_character(
        roads, building_gdf, _empty(), FRAME, grid_size=grid_size)
    center = next(cell for cell in report["cells"]
                  if cell["row"] == 2 and cell["column"] == 2)

    assert center["dominant_role"] == "possible_data_gap"
    assert report["summary"]["possible_data_gap_cells"] == 1


def test_semantic_landmark_tag_creates_landmark_focus():
    buildings = _uniform_buildings()
    landmark = gpd.GeoDataFrame(
        {"building": ["museum"], "wikidata": ["Q123"],
         "geometry": [box(1200, 1200, 1500, 1500)]},
        geometry="geometry", crs="EPSG:3857")
    buildings = gpd.GeoDataFrame(
        pd.concat([buildings, landmark], ignore_index=True),
        geometry="geometry", crs="EPSG:3857")

    report = analyze_scene_character(
        _regular_grid(), buildings, _empty(), FRAME, grid_size=4)

    assert "landmark_evidence" in report["summary"]["traits"]
    assert any(cell["dominant_role"] == "landmark_focus"
               for cell in report["cells"])
