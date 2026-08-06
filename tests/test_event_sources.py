import pytest

from camdash.config import AppConfig, CameraConfig
from camdash.event_sources import MotionSuppressor, WebhookSource


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
async def test_webhook_records_error_when_trigger_fails():
    async def trigger(*args, **kwargs):
        raise KeyError("camera-one")

    config = AppConfig(cameras=[])
    source = WebhookSource(lambda: config, trigger)

    with pytest.raises(KeyError):
        await source.handle("camera-one", b"", "application/json")

    assert source.last_error
