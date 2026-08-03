from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_motion_callback_notifies_before_media_work():
    source = (ROOT / "scripts" / "camdash-motion").read_text(encoding="utf-8")
    assert source.index("Notify CAM Dashboard via MQTT") < source.index("Capture snapshot")
    assert source.count("MOTION_PHOTO_FILE= MOTION_VIDEO_FILE= send2mqtt &") == 1


def test_home_assistant_notifications_are_optional():
    source = (ROOT / "scripts" / "camdash-motion").read_text(encoding="utf-8")
    assert source.count("if ha_enabled && [ -x /usr/sbin/ha-event ]; then") == 2


def test_motion_callback_ignores_current_and_legacy_motor_activity_markers():
    source = (ROOT / "scripts" / "camdash-motion").read_text(encoding="utf-8")
    assert "/run/motors-active" in source
    assert "/run/motors.pid" in source
    assert "motors -b" in source


def test_motor_health_cgi_is_authenticated_and_detects_blocked_daemon():
    source = (ROOT / "scripts" / "camdash-motor.cgi").read_text(encoding="utf-8")
    assert "require_auth" in source
    assert 'state" = "D"' in source
    assert "/var/run/motors-daemon" in source
    assert "pidof motors-daemon" in source
    assert '"requires_reboot":true' in source
    assert "/etc/init.d/S59motor restart" not in source
    assert 'center) center_motors' in source
    assert "motors -I y" in source
    assert 'motors -d h -x "$x" -y "$y"' in source


def test_installer_deploys_motor_recovery_helper():
    source = (ROOT / "scripts" / "configure-thingino.sh").read_text(encoding="utf-8")
    assert 'scp -O "$script_dir/camdash-motor.cgi"' in source
    assert "cp /tmp/camdash-motor.cgi /var/www/x/camdash-motor.cgi" in source


def test_installer_restarts_prudynt_after_persisting_motion_config():
    source = (ROOT / "scripts" / "configure-thingino.sh").read_text(encoding="utf-8")
    persist = source.index("jct /etc/prudynt.json set motion.script_path")
    restart = source.index("/etc/init.d/S31prudynt restart")
    assert persist < restart


def test_installer_uses_prudynt_auto_dimensions_for_motion_roi():
    source = (ROOT / "scripts" / "configure-thingino.sh").read_text(encoding="utf-8")
    for field in ("frame_width", "frame_height", "roi_1_x", "roi_1_y"):
        assert f"jct /etc/prudynt.json set motion.{field} 16384" in source
