from pathlib import Path

from camdash.alerter import AlertEngine


def event(label: str | None = None, camera_id: str = "camera-1") -> dict:
    detections = [{"label": label, "confidence": 9}] if label else []
    return {
        "id": "event-1", "camera_id": camera_id, "camera_name": "Camera", "triggered_at": "2026-08-02T12:00:00Z",
        "analysis": {"description": label or "", "detections": detections},
    }


def test_gardepro_alerts_key_and_catch_all_are_supported(tmp_path: Path):
    rules = tmp_path / "alerts.yaml"
    rules.write_text("""
alerts:
  - name: person
    keywords: [person, human]
    action: log
  - name: other
    catch_all: true
    action: log
""", encoding="utf-8")
    engine = AlertEngine(rules)

    assert engine.evaluate(event("person"), None, 0)["triggered"] == ["person"]
    assert engine.evaluate(event(), None, 0)["triggered"] == ["other"]


def test_disabled_rule_falls_through_to_catch_all(tmp_path: Path):
    rules = tmp_path / "alerts.yaml"
    rules.write_text("""
alerts:
  - name: Person
    keywords: [person]
    action: log
  - name: Other
    catch_all: true
    action: log
""", encoding="utf-8")
    result = AlertEngine(rules).evaluate(event("person"), None, 0, {"Person": False})
    assert result["triggered"] == ["Other"]


def test_matching_email_rules_are_consolidated(tmp_path: Path, monkeypatch):
    rules = tmp_path / "alerts.yaml"
    rules.write_text("""
alerts:
  - name: Cat
    keywords: [cat]
    action: email
  - name: Wildlife
    keywords: [wildlife]
    action: email
""", encoding="utf-8")
    engine = AlertEngine(rules)
    value = event("cat")
    value["analysis"]["description"] = "cat and wildlife"
    calls = []
    monkeypatch.setattr(engine, "_email", lambda alert_event, names, thumb: calls.append(names))

    result = engine.evaluate(value, None, 0)

    assert calls == [["Cat", "Wildlife"]]
    assert result == {"triggered": ["Cat", "Wildlife"], "errors": [], "matched": True, "sent": True}


def test_evaluate_reports_matched_false_when_no_rule_matches(tmp_path: Path):
    rules = tmp_path / "alerts.yaml"
    rules.write_text("""
alerts:
  - name: Cat
    keywords: [cat]
    action: log
""", encoding="utf-8")
    result = AlertEngine(rules).evaluate(event("dog"), None, 0)
    assert result == {"triggered": [], "errors": [], "matched": False, "sent": False}


def test_cooldown_is_scoped_per_camera(tmp_path: Path):
    rules = tmp_path / "alerts.yaml"
    rules.write_text("""
alerts:
  - name: Person
    keywords: [person]
    action: log
""", encoding="utf-8")
    engine = AlertEngine(rules)

    first = engine.evaluate(event("person", camera_id="camera-1"), None, 300)
    second_same_camera = engine.evaluate(event("person", camera_id="camera-1"), None, 300)
    second_other_camera = engine.evaluate(event("person", camera_id="camera-2"), None, 300)

    assert first["triggered"] == ["Person"]
    assert second_same_camera["triggered"] == []
    assert second_other_camera["triggered"] == ["Person"]
