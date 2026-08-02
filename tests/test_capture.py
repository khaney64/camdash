from datetime import datetime, timezone
from pathlib import Path

import pytest

from camdash.capture import _parse_timestamp, capture_profile, safe_unlink
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

