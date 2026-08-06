import json

import pytest

from camdash.cameras import CameraDisabledError
from camdash.config import AppConfig, CameraConfig
from camdash.event_sources import MotionSuppressor, WebhookSource, _describe_payload


def test_describe_payload_extracts_reported_camera_and_thumbnail():
    body = json.dumps({
        "time": "2023-02-01T15:05:39", "camera": "Camera01",
        "event": "Unknown license plate detected",
        "thumbnail": "https://nas.example.lan:5001/webapi/SurveillanceStation/Webhook/GetThumbnail/v1/example-0/x.jpg?v=1",
    }).encode()

    description = _describe_payload(body)

    assert "reported_camera='Camera01'" in description
    assert "GetThumbnail" in description


def test_describe_payload_falls_back_for_non_json_body():
    assert "body=" in _describe_payload(b"not json")


def test_motion_suppressor_expires_per_camera():
    now = [10.0]
    suppressor = MotionSuppressor(lambda: now[0])
    suppressor.suppress("camera-one", 5)

    assert suppressor.remaining("camera-one") == 5
    assert suppressor.remaining("camera-two") == 0
    now[0] = 15.0
    assert suppressor.remaining("camera-one") == 0


@pytest.mark.asyncio
async def test_webhook_motion_is_ignored_during_ptz_suppression():
    called = []

    async def trigger(*args, **kwargs):
        called.append((args, kwargs))
        return {}

    config = AppConfig(cameras=[CameraConfig(id="camera-one", name="Camera", host="192.0.2.10", adapter="thingino")])
    suppressor = MotionSuppressor()
    suppressor.suppress("camera-one", 5)
    source = WebhookSource(lambda: config, trigger, suppressor)

    result = await source.handle("camera-one", b"", "application/json")

    assert called == []
    assert result == {"duplicate": True, "suppressed": True}


@pytest.mark.asyncio
async def test_webhook_routes_to_trigger():
    called = []

    async def trigger(camera_id, source, **kwargs):
        called.append((camera_id, source, kwargs))
        return {"id": "event-1"}

    config = AppConfig(cameras=[CameraConfig(id="camera-one", name="Camera", host="192.0.2.10", adapter="thingino")])
    source = WebhookSource(lambda: config, trigger)

    result = await source.handle("camera-one", b"{}", "application/json")

    assert result == {"id": "event-1"}
    assert len(called) == 1
    camera_id, kind, kwargs = called[0]
    assert camera_id == "camera-one"
    assert kind == "webhook"
    assert kwargs["source_key"]
    assert source.last_camera_id == "camera-one"
    assert source.last_received_at
    assert source.last_error == ""


@pytest.mark.asyncio
async def test_webhook_silently_ignores_disabled_camera():
    async def trigger(*args, **kwargs):
        raise CameraDisabledError("camera is disabled")

    config = AppConfig(cameras=[])
    source = WebhookSource(lambda: config, trigger)
    source.last_error = "stale error from a previous trigger"

    result = await source.handle("camera-one", b"", "application/json")

    assert result == {"ignored": True, "reason": "disabled", "camera_id": "camera-one"}
    assert source.last_error == ""


@pytest.mark.asyncio
async def test_webhook_records_error_when_trigger_fails():
    async def trigger(*args, **kwargs):
        raise KeyError("camera-one")

    config = AppConfig(cameras=[])
    source = WebhookSource(lambda: config, trigger)

    with pytest.raises(KeyError):
        await source.handle("camera-one", b"", "application/json")

    assert source.last_error
