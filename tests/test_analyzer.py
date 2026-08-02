from camdash.analyzer import aggregate, parse_result


def test_parse_json_and_aggregate_highest_confidence():
    first = parse_result('{"description":"A deer","detections":[{"label":"Deer","confidence":7}]}')
    second = parse_result('{"description":"Deer walking","detections":[{"label":"deer","confidence":9},{"label":"fox","confidence":4}]}')
    result = aggregate([first, second])
    detections = {item["label"]: item["confidence"] for item in result["detections"]}
    assert detections == {"deer": 9.0, "fox": 4.0}
    assert result["images_analyzed"] == 2


def test_free_text_is_preserved():
    result = parse_result("Nothing visible")
    assert result["description"] == "Nothing visible"
    assert result["detections"] == []

