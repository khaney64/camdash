from pathlib import Path

from fastapi.testclient import TestClient

from camdash.config import AppConfig, save_config


def test_api_smoke_and_settings_mask(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    cfg = AppConfig(data_dir=str(tmp_path))
    cfg.mqtt.host = "127.0.0.1"
    cfg.mqtt.port = 9
    cfg.mqtt.password = "secret"
    save_config(cfg, config_path)
    monkeypatch.setenv("CAMDASH_CONFIG", str(config_path))

    from camdash.main import app

    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        status = client.get("/api/status")
        assert status.status_code == 200
        assert status.json()["ok"] is True
        settings = client.get("/api/settings").json()
        assert "password" not in settings["mqtt"]
        assert settings["mqtt"]["has_password"] is True
        events = client.get("/api/events").json()
        assert events == {"events": []}


def test_hls_path_traversal_rejected(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    cfg = AppConfig(data_dir=str(tmp_path))
    cfg.mqtt.host = "127.0.0.1"
    cfg.mqtt.port = 9
    save_config(cfg, config_path)
    monkeypatch.setenv("CAMDASH_CONFIG", str(config_path))

    from camdash.main import app

    with TestClient(app) as client:
        assert client.get("/api/cameras/cam/hls/not-a-playlist.txt").status_code == 400
