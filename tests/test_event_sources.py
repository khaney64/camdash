import asyncio
import json
from types import SimpleNamespace

import pytest

from camdash.config import AppConfig, CameraConfig
from camdash.event_sources import MotionSuppressor, MqttSource, _camera_from_topic


def test_camera_id_from_normalized_topic():
    assert _camera_from_topic("camdash/cameras/camera-one/motion") == "camera-one"


@pytest.mark.parametrize("topic", ["camera/motion", "camdash/cameras/x", "camdash/camera/x/motion"])
def test_invalid_topics(topic):
    with pytest.raises(ValueError):
        _camera_from_topic(topic)


def test_motion_suppressor_expires_per_camera():
    now = [10.0]
    suppressor = MotionSuppressor(lambda: now[0])
    suppressor.suppress("camera-one", 5)

    assert suppressor.remaining("camera-one") == 5
    assert suppressor.remaining("camera-two") == 0
    now[0] = 15.0
    assert suppressor.remaining("camera-one") == 0


def test_mqtt_motion_is_ignored_during_ptz_suppression():
    called = []

    async def trigger(*args, **kwargs):
        called.append((args, kwargs))
        return {}

    config = AppConfig(cameras=[CameraConfig(
        id="camera-one", name="Camera", host="192.0.2.10", adapter="thingino",
        mqtt_topic="camdash/cameras/camera-one/motion",
    )])
    suppressor = MotionSuppressor()
    suppressor.suppress("camera-one", 5)
    source = MqttSource(lambda: config, trigger, suppressor)
    source.loop = asyncio.new_event_loop()
    message = SimpleNamespace(
        topic="camdash/cameras/camera-one/motion",
        payload=json.dumps({"camera_id": "camera-one", "event": "motion"}).encode(),
    )

    try:
        source._on_message(None, None, message)
    finally:
        source.loop.close()

    assert called == []
