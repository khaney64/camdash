from pathlib import Path

from camdash.alerter import AlertEngine


def event(label: str | None = None) -> dict:
    detections = [{"label": label, "confidence": 9}] if label else []
    return {
        "id": "event-1", "camera_name": "Camera", "triggered_at": "2026-08-02T12:00:00Z",
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
