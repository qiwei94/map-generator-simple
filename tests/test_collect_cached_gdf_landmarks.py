import pandas as pd

from tools.collect_cached_gdf_landmarks import records_from_buildings


def test_records_from_buildings_keeps_names_and_print_height_evidence():
    buildings = pd.DataFrame({
        "wikidata": ["Q42", "Q7;Q8", None],
        "building": ["tower", "yes", "house"],
        "height": ["123", None, None],
        "building:levels": [None, "7", "2"],
        "name": ["测试塔", "历史建筑", "普通住宅"],
        "name:en": ["Test Tower", None, None],
    })
    rows = records_from_buildings(buildings)
    assert [row["qid"] for row in rows] == ["Q42", "Q7", "Q8"]
    assert rows[0]["osm_height"] == "123"
    assert rows[0]["osm_name"] == "测试塔"
    assert rows[1]["osm_levels"] == "7"
