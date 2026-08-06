from pathlib import Path


ROOT = Path(__file__).parents[1]


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


def test_deprovision_motion_disables_send2mqtt_and_camera_motion():
    source = (ROOT / "scripts" / "deprovision-motion.sh").read_text(encoding="utf-8")
    assert "jct /etc/send2.json set mqtt.enabled false" in source
    assert "jct /etc/prudynt.json set motion.enabled false" in source


def test_deprovision_motion_backs_up_before_restarting_prudynt():
    source = (ROOT / "scripts" / "deprovision-motion.sh").read_text(encoding="utf-8")
    backup = source.index("cp /etc/send2.json")
    restart = source.index("/etc/init.d/S31prudynt restart")
    assert backup < restart
