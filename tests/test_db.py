from datetime import datetime, timedelta, timezone
from pathlib import Path

from camdash.db import Database


def event(event_id="e1", when=None):
    when = when or datetime.now(timezone.utc).isoformat()
    return {"id": event_id, "camera_id": "cam-1", "camera_name": "Camera", "source": "mqtt",
            "source_key": event_id, "triggered_at": when, "received_at": when, "profile": "day",
            "status": "capturing", "created_at": when}


def test_event_media_and_duplicate_source(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    assert db.create_event(event()) is True
    assert db.create_event({**event("e2"), "source_key": "e1"}) is False
    db.add_media({"id": "m1", "event_id": "e1", "kind": "snapshot", "captured_at": event()["triggered_at"],
                  "path": str(tmp_path / "a.jpg"), "analysis": {"detections": [{"label": "deer"}]}})
    db.update_event("e1", status="complete", analysis_json={"description": "deer"})
    result = db.get_event("e1")
    assert result["status"] == "complete"
    assert result["analysis"]["description"] == "deer"
    assert result["media"][0]["analysis"]["detections"][0]["label"] == "deer"


def test_retention_oldest_and_size_limit(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    now = datetime.now(timezone.utc).isoformat()
    db.create_event(event("old", old)); db.create_event(event("new", now))
    db.add_media({"id": "mo", "event_id": "old", "kind": "video", "captured_at": old, "size_bytes": 10})
    db.add_media({"id": "mn", "event_id": "new", "kind": "video", "captured_at": now, "size_bytes": 10})
    candidates = db.retention_candidates(datetime.now(timezone.utc)-timedelta(days=30), 100)
    assert [item["id"] for item in candidates] == ["old"]

