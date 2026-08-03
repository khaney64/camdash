from dataclasses import asdict
from pathlib import Path

import yaml
import pytest

from camdash.config import AppConfig, CameraConfig, load_config, merge_public_update, public_config, save_config


def test_config_round_trip_and_masks_secrets(tmp_path: Path):
    path = tmp_path / "config.yaml"
    cfg = AppConfig(data_dir=str(tmp_path), cameras=[CameraConfig(
        id="camera-one", name="Camera One", host="192.0.2.1", adapter="thingino",
        username="thingino", password="private", token="private-token", record_stream="sub",
    )])
    cfg.mqtt.password = "broker-secret"
    save_config(cfg, path)
    loaded, _ = load_config(path)
    assert loaded.cameras[0].password == "private"
    assert loaded.cameras[0].record_stream == "sub"
    public = public_config(loaded)
    assert "password" not in public["cameras"][0]
    assert public["cameras"][0]["has_password"] is True
    assert "password" not in public["mqtt"]


def test_camera_record_stream_validation():
    camera = CameraConfig(id="camera-one", name="Camera One", host="192.0.2.1", record_stream="invalid")
    with pytest.raises(ValueError, match="record stream"):
        camera.validate()


def test_public_update_preserves_blank_secrets(tmp_path: Path):
    cfg = AppConfig(data_dir=str(tmp_path), cameras=[CameraConfig(
        id="camera-one", name="Camera One", host="192.0.2.1", password="keep", token="keep-token",
    )])
    cfg.mqtt.password = "keep-mqtt"
    updated = merge_public_update(cfg, {
        "mqtt": {"host": "broker", "password": ""},
        "cameras": [{**public_config(cfg)["cameras"][0], "name": "Renamed", "password": "", "token": ""}],
    })
    assert updated.cameras[0].name == "Renamed"
    assert updated.cameras[0].password == "keep"
    assert updated.cameras[0].token == "keep-token"
    assert updated.mqtt.password == "keep-mqtt"


def test_alert_settings_round_trip_and_person_modes_are_exclusive(tmp_path: Path):
    path = tmp_path / "config.yaml"
    cfg = AppConfig(data_dir=str(tmp_path))
    cfg.analysis.alert_cooldown_minutes = 12
    cfg.analysis.alert_rules_enabled = {"Person": False, "Cat": True}
    cfg.analysis.remove_person_only_images = True
    save_config(cfg, path)
    loaded, _ = load_config(path)
    assert loaded.analysis.alert_cooldown_minutes == 12
    assert loaded.analysis.alert_rules_enabled == {"Person": False, "Cat": True}
    assert loaded.analysis.remove_person_only_images is True

    loaded.analysis.alert_rules_enabled["Person"] = True
    with pytest.raises(ValueError, match="cannot both"):
        save_config(loaded, path)


def test_analysis_generation_settings_round_trip_and_validate(tmp_path: Path):
    path = tmp_path / "config.yaml"
    cfg = AppConfig(data_dir=str(tmp_path))
    cfg.analysis.max_tokens = 4096
    cfg.analysis.thinking_budget = 8192
    cfg.analysis.temperature = 0.8
    cfg.analysis.chat_enabled = True
    save_config(cfg, path)
    loaded, _ = load_config(path)
    assert loaded.analysis.max_tokens == 4096
    assert loaded.analysis.thinking_budget == 8192
    assert loaded.analysis.temperature == 0.8
    assert loaded.analysis.chat_enabled is True

    loaded.analysis.temperature = 1.1
    with pytest.raises(ValueError, match="temperature"):
        save_config(loaded, path)
