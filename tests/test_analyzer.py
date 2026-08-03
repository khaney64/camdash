from camdash.analyzer import aggregate, local_chat_url, parse_result, with_reasoning


def test_parse_json_and_aggregate_highest_confidence():
    first = parse_result('{"description":"A deer","detections":[{"label":"Deer","name":"Daisy","confidence":7,"reasoning":"Known markings"}]}')
    second = parse_result('{"description":"Deer walking","detections":[{"label":"deer","confidence":9},{"label":"fox","confidence":4}]}')
    result = aggregate([first, second])
    detections = {item["label"]: item["confidence"] for item in result["detections"]}
    assert detections == {"deer": 9.0, "fox": 4.0}
    deer = next(item for item in result["detections"] if item["label"] == "deer")
    assert deer["name"] == "Daisy"
    assert deer["reasoning"] == "Known markings"
    assert result["images_analyzed"] == 2


def test_free_text_is_preserved():
    result = parse_result("Nothing visible")
    assert result["description"] == "Nothing visible"
    assert result["detections"] == []


def test_local_chat_url_accepts_server_v1_or_full_endpoint():
    assert local_chat_url("http://llm:8080") == "http://llm:8080/v1/chat/completions"
    assert local_chat_url("http://llm:8080/v1/") == "http://llm:8080/v1/chat/completions"
    assert local_chat_url("http://llm:8080/v1/chat/completions") == "http://llm:8080/v1/chat/completions"


def test_chat_prompt_requests_reasoning():
    assert with_reasoning("What is here?  ") == "What is here?\nInclude your reasoning."
