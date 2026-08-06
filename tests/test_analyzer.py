from pathlib import Path

import camdash.analyzer as analyzer
from camdash.analyzer import aggregate, local_chat_url, parse_result, with_reasoning
from camdash.config import AnalysisConfig


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


def test_parse_result_flags_unparseable_text_for_retry():
    assert parse_result("").get("unparseable") is True
    assert parse_result("{not valid json").get("unparseable") is True
    assert parse_result("Nothing visible").get("unparseable") is True
    assert "unparseable" not in parse_result('{"detections":[]}')
    assert "unparseable" not in parse_result('{"description":"A deer","detections":[{"label":"deer","confidence":7}]}')


def test_local_chat_url_accepts_server_v1_or_full_endpoint():
    assert local_chat_url("http://llm:8080") == "http://llm:8080/v1/chat/completions"
    assert local_chat_url("http://llm:8080/v1/") == "http://llm:8080/v1/chat/completions"
    assert local_chat_url("http://llm:8080/v1/chat/completions") == "http://llm:8080/v1/chat/completions"


def test_chat_prompt_requests_reasoning():
    assert with_reasoning("What is here?  ") == "What is here?\nInclude your reasoning."


def test_well_formed_empty_response_does_not_retry(tmp_path: Path, monkeypatch):
    image = tmp_path / "image.jpg"
    image.write_bytes(b"image")
    cfg = AnalysisConfig(enabled=True, thinking_budget=2048)
    calls = []
    monkeypatch.setattr(
        analyzer, "_call",
        lambda encoded, attempt, prompt: (calls.append(attempt.thinking_budget) or {"description": "", "detections": []}),
    )

    result = analyzer.analyze_image(image, cfg)

    assert calls == [2048]
    assert result == {"description": "", "detections": []}


def test_invalid_response_retries_with_double_thinking_budget(tmp_path: Path, monkeypatch):
    image = tmp_path / "image.jpg"
    image.write_bytes(b"image")
    cfg = AnalysisConfig(enabled=True, thinking_budget=2048)
    budgets = []
    results = iter([
        {"description": "", "detections": [], "raw": "", "unparseable": True},
        {"description": "A cat", "detections": [{"label": "cat", "confidence": 8}]},
    ])
    monkeypatch.setattr(analyzer, "_call", lambda encoded, attempt, prompt: (budgets.append(attempt.thinking_budget) or next(results)))

    result = analyzer.analyze_image(image, cfg)

    assert budgets == [2048, 4096]
    assert result["description"] == "A cat"


def test_low_confidence_analysis_retries_with_double_thinking_budget(tmp_path: Path, monkeypatch):
    image = tmp_path / "image.jpg"
    image.write_bytes(b"image")
    cfg = AnalysisConfig(enabled=True, thinking_budget=1024)
    budgets = []
    results = iter([
        {"description": "Maybe a cat", "detections": [{"label": "cat", "confidence": 3}]},
        {"description": "A cat", "detections": [{"label": "cat", "confidence": 7}]},
    ])
    monkeypatch.setattr(analyzer, "_call", lambda encoded, attempt, prompt: (budgets.append(attempt.thinking_budget) or next(results)))

    result = analyzer.analyze_image(image, cfg)

    assert budgets == [1024, 2048]
    assert result["detections"][0]["confidence"] == 7


def test_local_payload_uses_adjustable_generation_values(monkeypatch):
    cfg = AnalysisConfig(llm_url="http://llm", llm_model="vision", max_tokens=800,
                         thinking_budget=2048, temperature=0.8)
    captured = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "Answer"}}]}

    monkeypatch.setattr(analyzer.requests, "post", lambda url, **kwargs: (captured.update(kwargs["json"]) or Response()))
    analyzer._local("encoded", cfg, "prompt")
    assert captured["max_tokens"] == 2848
    assert captured["temperature"] == 0.8
    assert captured["chat_template_kwargs"]["thinking_budget"] == 2048
