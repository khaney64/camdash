from datetime import datetime, timezone
from pathlib import Path

import pytest

from camdash.capture import CaptureManager, _parse_timestamp, capture_profile, safe_unlink
from camdash.config import AppConfig, CameraConfig


def test_day_night_and_camera_override(tmp_path: Path):
    cfg = AppConfig(data_dir=str(tmp_path), cameras=[])
    camera = CameraConfig(id="cam-1", name="Cam", host="192.0.2.1", capture={"snapshots": 5})
    profile, effective = capture_profile(cfg, camera, datetime(2026, 1, 1, 15, tzinfo=timezone.utc))
    assert profile == "day"
    assert effective.snapshots == 5
    profile, _ = capture_profile(cfg, camera, datetime(2026, 1, 1, 2, tzinfo=timezone.utc))
    assert profile == "night"


def test_timestamp_rejects_large_clock_skew():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert _parse_timestamp("0", now) == now


def test_safe_unlink_rejects_outside_root(tmp_path: Path):
    outside = tmp_path.parent / "outside.jpg"
    with pytest.raises(ValueError):
        safe_unlink(str(outside), tmp_path)


@pytest.mark.asyncio
async def test_person_only_analysis_requests_event_removal(tmp_path: Path, monkeypatch):
    cfg = AppConfig(data_dir=str(tmp_path))
    cfg.analysis.enabled = True
    cfg.analysis.remove_person_only_images = True
    cfg.analysis.alert_rules_enabled = {"Person": False}
    camera = CameraConfig(id="cam-1", name="Cam", host="192.0.2.1")

    class FakeDatabase:
        analysis = None

        def update_media(self, *args, **kwargs):
            pass

        def update_event(self, event_id, **values):
            self.analysis = values.get("analysis_json", self.analysis)

        def get_event(self, event_id):
            return {"id": event_id, "camera_name": "Cam", "triggered_at": "now", "analysis": self.analysis}

    async def broadcast(_message):
        pass

    monkeypatch.setattr("camdash.capture.analyzer.analyze_image", lambda *args: {
        "description": "A person is visible", "detections": [{"label": "person", "confidence": 9}]
    })
    manager = CaptureManager(FakeDatabase(), lambda: cfg, broadcast)
    remove = await manager._analyze(
        {"id": "event-1", "camera_name": "Cam", "triggered_at": "now"},
        camera,
        [{"id": "media-1", "path": str(tmp_path / "capture.jpg"), "thumb_path": None}],
    )
    assert remove is True
