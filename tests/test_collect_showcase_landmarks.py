from tools.collect_showcase_landmarks import frame_bbox, parse_opl_landmarks


def test_frame_bbox_is_centered_and_approximately_square():
    south, west, north, east = frame_bbox(31.23, 121.47, 25)
    assert round((south + north) / 2, 5) == 31.23
    assert round((west + east) / 2, 5) == 121.47
    assert 0.22 < north - south < 0.24
    assert 0.25 < east - west < 0.28


def test_parse_opl_keeps_only_wikidata_buildings():
    opl = "\n".join([
        "w10 v1 dV c1 t2020-01-01T00:00:00Z i1 uuser "
        "Tbuilding=skyscraper,height=123,wikidata=Q42 Nn1,n2,n1",
        "n11 v1 dV c1 t2020-01-01T00:00:00Z i1 uuser "
        "Ttourism=attraction,wikidata=Q99 x1 y2",
        "r12 v1 dV c1 t2020-01-01T00:00:00Z i1 uuser "
        "Tbuilding:part=yes,building:levels=7,wikidata=Q7%3b%Q8 M",
    ])
    rows = parse_opl_landmarks(opl)
    assert rows == [
        {"qid": "Q42", "osm_id": "w10", "building": "skyscraper",
         "osm_height": "123", "osm_levels": None},
        {"qid": "Q7", "osm_id": "r12", "building": "yes",
         "osm_height": None, "osm_levels": "7"},
        {"qid": "Q8", "osm_id": "r12", "building": "yes",
         "osm_height": None, "osm_levels": "7"},
    ]
