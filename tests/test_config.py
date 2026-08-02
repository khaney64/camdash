from dataclasses import asdict
from pathlib import Path

import yaml

from camdash.config import AppConfig, CameraConfig, load_config, merge_public_update, public_config, save_config


def test_config_round_trip_and_masks_secrets(tmp_path: Path):
    path = tmp_path / "config.yaml"
    cfg = AppConfig(data_dir=str(tmp_path), cameras=[CameraConfig(
        id="camera-one", name="Camera One", host="192.0.2.1", adapter="thingino",
        username="thingino", password="private", token="private-token",
    )])
    cfg.mqtt.password = "broker-secret"
    save_config(cfg, path)
    loaded, _ = load_config(path)
    assert loaded.cameras[0].password == "private"
    public = public_config(loaded)
    assert "password" not in public["cameras"][0]
    assert public["cameras"][0]["has_password"] is True
    assert "password" not in public["mqtt"]


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

