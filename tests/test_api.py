import asyncio
import time
from pathlib import Path
from queue import Empty

from fastapi.testclient import TestClient

from camdash.config import AppConfig, CameraConfig, load_config, save_config


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
    assert 'aria-label="Open ${esc(media.kind)}"' in source
    assert "const mediaId=esc(media.id),mediaUrl=encodeURIComponent(String(media.id ?? ''));" in source
    assert 'data-open-media="${mediaId}"' in source
    assert 'data-chat-media="${mediaId}"' in source
    assert 'data-save-media="${mediaId}"' in source
    assert "const mediaUrl=encodeURIComponent(String(mediaId ?? ''))" in source


def test_gallery_uses_in_app_delete_confirmation_and_media_lightbox():
    root = Path(__file__).parents[1] / "camdash" / "static"
    source = (root / "app.js").read_text(encoding="utf-8")
    markup = (root / "index.html").read_text(encoding="utf-8")
    styles = (root / "style.css").read_text(encoding="utf-8")

    assert "confirm(" not in source
    assert 'class="card-delete" data-delete-event=' in source
    assert "askConfirmation('Delete event?'" in source
    assert 'data-open-media="${mediaId}"' in source
    assert 'id="media-dialog"' in markup
    assert 'id="confirm-dialog"' in markup
    assert "width: min(66.667vw, 1200px)" in styles
    assert ".media-dialog-card { width: 100vw; height: 100vh" in styles


def test_mjpeg_proxy_uses_shared_relay():
    source = (Path(__file__).parents[1] / "camdash" / "main.py").read_text(encoding="utf-8")
    assert "s.mjpeg_relay(camera_id, hd)" in source


def test_hls_file_route_touches_last_accessed():
    source = (Path(__file__).parents[1] / "camdash" / "main.py").read_text(encoding="utf-8")
    assert "s.touch_hls(camera_id)" in source


def test_reap_idle_hls_stops_camera_after_timeout(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    save_config(AppConfig(data_dir=str(tmp_path)), config_path)
    monkeypatch.setenv("CAMDASH_CONFIG", str(config_path))

    from camdash import main

    class FakeProcess:
        def __init__(self):
            self.returncode = None
            self.terminated = False

        def terminate(self):
            self.terminated = True
            self.returncode = 0

        async def wait(self):
            return self.returncode

    state = main.AppState()
    process = FakeProcess()
    directory = tmp_path / "hls-camera"
    directory.mkdir()
    state.hls["cam"] = (process, directory)
    state.hls_last_accessed["cam"] = time.monotonic() - (main.HLS_IDLE_TIMEOUT_SECONDS + 1)
    try:
        asyncio.run(state._reap_idle_hls())
        assert process.terminated is True
        assert "cam" not in state.hls
        assert "cam" not in state.hls_last_accessed
    finally:
        state.db.close()


def test_reap_idle_hls_leaves_recently_touched_camera_running(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    save_config(AppConfig(data_dir=str(tmp_path)), config_path)
    monkeypatch.setenv("CAMDASH_CONFIG", str(config_path))

    from camdash import main

    class FakeProcess:
        returncode = None

        def terminate(self):
            raise AssertionError("should not stop a recently touched HLS session")

    state = main.AppState()
    directory = tmp_path / "hls-camera"
    directory.mkdir()
    state.hls["cam"] = (FakeProcess(), directory)
    state.touch_hls("cam")
    try:
        asyncio.run(state._reap_idle_hls())
        assert "cam" in state.hls
    finally:
        state.db.close()


def test_close_mjpeg_relays_clears_all(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    save_config(AppConfig(data_dir=str(tmp_path)), config_path)
    monkeypatch.setenv("CAMDASH_CONFIG", str(config_path))

    from camdash import main

    class Relay:
        closed = False

        def close(self):
            self.closed = True

    state = main.AppState()
    relay = Relay()
    state.mjpeg_relays[("removed-camera", False)] = relay
    try:
        state.close_mjpeg_relays()
        assert relay.closed is True
        assert state.mjpeg_relays == {}
    finally:
        state.db.close()


def test_api_smoke_and_settings_mask(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    cfg = AppConfig(data_dir=str(tmp_path))
    cfg.webhook.shared_secret = "secret"
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
        assert "shared_secret" not in settings["webhook"]
        assert settings["webhook"]["has_secret"] is True
        assert settings["analysis"]["alert_email"] == "alerts@example.test"
        assert settings["analysis"]["alert_email_configured"] is True
        assert settings["analysis"]["alert_rules"] == ["Cat"]
        events = client.get("/api/events").json()
        assert events == {"events": []}


def test_surveillance_webhook_requires_correct_secret(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    cfg = AppConfig(data_dir=str(tmp_path), cameras=[CameraConfig(
        id="cam", name="Camera", host="192.0.2.20", adapter="thingino",
    )])
    cfg.webhook.shared_secret = "topsecret"
    save_config(cfg, config_path)
    monkeypatch.setenv("CAMDASH_CONFIG", str(config_path))

    from camdash.main import app

    with TestClient(app) as client:
        assert client.post("/api/webhooks/surveillance/cam").status_code == 401
        assert client.post("/api/webhooks/surveillance/cam?secret=wrong").status_code == 401


def test_surveillance_webhook_unknown_camera_is_conflict(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    cfg = AppConfig(data_dir=str(tmp_path))
    cfg.webhook.shared_secret = "topsecret"
    save_config(cfg, config_path)
    monkeypatch.setenv("CAMDASH_CONFIG", str(config_path))

    from camdash.main import app

    with TestClient(app) as client:
        response = client.post("/api/webhooks/surveillance/unknown-camera?secret=topsecret")
        assert response.status_code == 409


def test_surveillance_webhook_routes_to_capture_trigger(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    cfg = AppConfig(data_dir=str(tmp_path), cameras=[CameraConfig(
        id="cam", name="Camera", host="192.0.2.20", adapter="thingino",
    )])
    cfg.webhook.shared_secret = "topsecret"
    save_config(cfg, config_path)
    monkeypatch.setenv("CAMDASH_CONFIG", str(config_path))

    from camdash import main

    with TestClient(main.app) as client:
        called = []

        async def fake_trigger(camera_id, source, **kwargs):
            called.append((camera_id, source, kwargs))
            return {"id": "event-1"}

        main.STATE.webhook.trigger = fake_trigger

        response = client.post("/api/webhooks/surveillance/cam?secret=topsecret")
        assert response.status_code == 202
        assert response.json() == {"id": "event-1"}
        assert called == [("cam", "webhook", called[0][2])]
        assert called[0][2]["source_key"]

        status = client.get("/api/status").json()
        assert status["webhook"]["last_camera_id"] == "cam"
        assert status["webhook"]["last_received_at"]


def test_hls_path_traversal_rejected(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    cfg = AppConfig(data_dir=str(tmp_path))
    save_config(cfg, config_path)
    monkeypatch.setenv("CAMDASH_CONFIG", str(config_path))

    from camdash.main import app

    with TestClient(app) as client:
        assert client.get("/api/cameras/cam/hls/not-a-playlist.txt").status_code == 400


def test_live_info_returns_selected_rtsp_profile(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    cfg = AppConfig(data_dir=str(tmp_path))
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


def test_ptz_set_center_persists_detected_position(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    cfg = AppConfig(data_dir=str(tmp_path), cameras=[CameraConfig(
        id="cam", name="Camera", host="192.0.2.20", adapter="thingino", ptz=True,
    )])
    save_config(cfg, config_path)
    monkeypatch.setenv("CAMDASH_CONFIG", str(config_path))

    from camdash.cameras import ThinginoAdapter
    monkeypatch.setattr(ThinginoAdapter, "current_position", lambda self: {"x": 42, "y": 17})

    from camdash.main import app

    with TestClient(app) as client:
        response = client.post("/api/cameras/cam/ptz/set-center")
        assert response.status_code == 200
        assert response.json() == {"center_x": 42.0, "center_y": 17.0}

    reloaded, _ = load_config(config_path)
    assert reloaded.camera("cam").center_x == 42.0
    assert reloaded.camera("cam").center_y == 17.0


def test_ptz_set_center_rejects_camera_without_ptz(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    cfg = AppConfig(data_dir=str(tmp_path), cameras=[CameraConfig(
        id="cam", name="Camera", host="192.0.2.20", adapter="thingino", ptz=False,
    )])
    save_config(cfg, config_path)
    monkeypatch.setenv("CAMDASH_CONFIG", str(config_path))

    from camdash.main import app

    with TestClient(app) as client:
        response = client.post("/api/cameras/cam/ptz/set-center")
        assert response.status_code == 409


def test_chat_uses_camera_prompt_and_appends_reasoning(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    cfg = AppConfig(data_dir=str(tmp_path), cameras=[CameraConfig(
        id="cam", name="Camera", host="192.0.2.20", prompt_override="Camera-specific prompt",
    )])
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
