import asyncio
from pathlib import Path
from queue import Empty

from fastapi.testclient import TestClient

from camdash.config import AppConfig, CameraConfig, save_config


def test_mjpeg_viewer_queue_wait_is_bounded():
    calls = []

    class FrameQueue:
        def get(self, *, timeout):
            calls.append(timeout)
            if len(calls) == 1:
                raise Empty
            return b"frame"

    from camdash.main import MJPEG_QUEUE_WAIT_SECONDS, _next_mjpeg_frame

    assert asyncio.run(_next_mjpeg_frame(FrameQueue())) == b"frame"
    assert calls == [MJPEG_QUEUE_WAIT_SECONDS, MJPEG_QUEUE_WAIT_SECONDS]


def test_frontend_initializes_camera_dropdowns():
    source = (Path(__file__).parents[1] / "camdash" / "static" / "app.js").read_text(encoding="utf-8")
    assert "await loadSettings();await loadCameras();" in source
    assert "data-motor-status" in source
    assert "data-motor-restart" not in source


def test_mjpeg_proxy_uses_shared_relay():
    source = (Path(__file__).parents[1] / "camdash" / "main.py").read_text(encoding="utf-8")
    assert "s.mjpeg_relay(camera_id, hd)" in source


def test_api_smoke_and_settings_mask(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    cfg = AppConfig(data_dir=str(tmp_path))
    cfg.mqtt.host = "127.0.0.1"
    cfg.mqtt.port = 9
    cfg.mqtt.password = "secret"
    (tmp_path / "alerts.yaml").write_text("alerts:\n  - name: Cat\n    keywords: [cat]\n", encoding="utf-8")
    save_config(cfg, config_path)
    monkeypatch.setenv("CAMDASH_CONFIG", str(config_path))
    monkeypatch.setenv("CAMDASH_ALERT_EMAIL", "alerts@example.test")
    monkeypatch.setenv("CAMDASH_ALERT_SMTP_PASSWORD", "private")

    from camdash.main import app

    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        status = client.get("/api/status")
        assert status.status_code == 200
        assert status.json()["ok"] is True
        settings = client.get("/api/settings").json()
        assert "password" not in settings["mqtt"]
        assert settings["mqtt"]["has_password"] is True
        assert settings["analysis"]["alert_email"] == "alerts@example.test"
        assert settings["analysis"]["alert_email_configured"] is True
        assert settings["analysis"]["alert_rules"] == ["Cat"]
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


def test_live_info_returns_selected_rtsp_profile(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    cfg = AppConfig(data_dir=str(tmp_path))
    cfg.mqtt.host = "127.0.0.1"
    cfg.mqtt.port = 9
    cfg.cameras = [CameraConfig(
        id="cam", name="Camera", host="192.0.2.20", adapter="thingino",
        username="viewer", password="p@ss word", enabled=True,
    )]
    save_config(cfg, config_path)
    monkeypatch.setenv("CAMDASH_CONFIG", str(config_path))

    from camdash.main import app

    with TestClient(app) as client:
        sub = client.get("/api/cameras/cam/live/info").json()
        main = client.get("/api/cameras/cam/live/info?hd=true").json()
        assert sub["rtsp_url"] == "rtsp://viewer:p%40ss%20word@192.0.2.20/ch1"
        assert main["rtsp_url"] == "rtsp://viewer:p%40ss%20word@192.0.2.20/ch0"


def test_chat_uses_camera_prompt_and_appends_reasoning(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    cfg = AppConfig(data_dir=str(tmp_path), cameras=[CameraConfig(
        id="cam", name="Camera", host="192.0.2.20", prompt_override="Camera-specific prompt",
    )])
    cfg.mqtt.host = "127.0.0.1"
    cfg.mqtt.port = 9
    cfg.analysis.enabled = True
    cfg.analysis.chat_enabled = True
    save_config(cfg, config_path)
    monkeypatch.setenv("CAMDASH_CONFIG", str(config_path))

    from camdash.main import app, state

    image = tmp_path / "capture.jpg"
    image.write_bytes(b"image")
    with TestClient(app) as client:
        database = state().db
        database.create_event({
            "id": "event", "camera_id": "cam", "camera_name": "Camera", "source": "test",
            "triggered_at": "2026-08-03T00:00:00Z", "received_at": "2026-08-03T00:00:00Z",
            "profile": "day", "status": "complete", "created_at": "2026-08-03T00:00:00Z",
        })
        database.add_media({
            "id": "media", "event_id": "event", "kind": "snapshot", "captured_at": "2026-08-03T00:00:00Z",
            "path": str(image), "mime_type": "image/jpeg",
        })
        assert client.get("/api/media/media/chat").json()["prompt"] == "Camera-specific prompt"

        captured = {}
        monkeypatch.setattr("camdash.main.analyzer.analyze_image", lambda path, config, prompt: (
            captured.update(prompt=prompt) or {"description": "Answer", "detections": []}
        ))
        response = client.post("/api/media/media/chat", json={"prompt": "What is visible?"})
        assert response.status_code == 200
        assert captured["prompt"] == "What is visible?\nInclude your reasoning."
