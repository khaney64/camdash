from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_motion_callback_notifies_before_media_work():
    source = (ROOT / "scripts" / "camdash-motion").read_text(encoding="utf-8")
    assert source.index("Notify CAM Dashboard via MQTT") < source.index("Capture snapshot")
    assert source.count("MOTION_PHOTO_FILE= MOTION_VIDEO_FILE= send2mqtt &") == 1


def test_home_assistant_notifications_are_optional():
    source = (ROOT / "scripts" / "camdash-motion").read_text(encoding="utf-8")
    assert source.count("if ha_enabled && [ -x /usr/sbin/ha-event ]; then") == 2
