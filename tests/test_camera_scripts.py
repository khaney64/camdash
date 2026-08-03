from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_motion_callback_notifies_before_media_work():
    source = (ROOT / "scripts" / "camdash-motion").read_text(encoding="utf-8")
    assert source.index("Notify CAM Dashboard via MQTT") < source.index("Capture snapshot")
    assert source.count("MOTION_PHOTO_FILE= MOTION_VIDEO_FILE= send2mqtt &") == 1


def test_home_assistant_notifications_are_optional():
    source = (ROOT / "scripts" / "camdash-motion").read_text(encoding="utf-8")
    assert source.count("if ha_enabled && [ -x /usr/sbin/ha-event ]; then") == 2


def test_installer_restarts_prudynt_after_persisting_motion_config():
    source = (ROOT / "scripts" / "configure-thingino.sh").read_text(encoding="utf-8")
    persist = source.index("jct /etc/prudynt.json set motion.script_path")
    restart = source.index("/etc/init.d/S31prudynt restart")
    assert persist < restart


def test_installer_uses_prudynt_auto_dimensions_for_motion_roi():
    source = (ROOT / "scripts" / "configure-thingino.sh").read_text(encoding="utf-8")
    for field in ("frame_width", "frame_height", "roi_1_x", "roi_1_y"):
        assert f"jct /etc/prudynt.json set motion.{field} 16384" in source
