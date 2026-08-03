import pytest

from camdash.cameras import CameraError, ThinginoAdapter
from camdash.config import CameraConfig


class FakeResponse:
    status_code = 200

    def __init__(self, payload=None):
        self.payload = payload or {}

    def json(self):
        return self.payload

    def raise_for_status(self):
        return None


class FakeSession:
    def __init__(self):
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params, timeout))
        if url.endswith("json-motor-params.cgi"):
            return FakeResponse({"steps_pan": 1000, "steps_tilt": 500})
        if url.endswith("camdash-motor.cgi"):
            return FakeResponse({"supported": True, "healthy": True, "motor": {"status": "0"}})
        return FakeResponse({"message": {"xpos": 510, "ypos": 250}})


def test_thingino_ptz_uses_native_motor_endpoint():
    adapter = ThinginoAdapter(CameraConfig(
        id="cam", name="Camera", host="192.0.2.30", adapter="thingino", token="secret",
    ))
    adapter.session = FakeSession()

    result = adapter.ptz("right")

    assert [call[0] for call in adapter.session.calls] == [
        "http://192.0.2.30/x/json-motor-params.cgi",
        "http://192.0.2.30/x/json-motor.cgi",
    ]
    assert adapter.session.calls[1][1] == {"d": "g", "x": 10, "y": 0, "token": "secret"}
    assert result == {
        "driver": "thingino", "requested": {"d": "g", "x": 10, "y": 0},
        "position": {"x": 510, "y": 250}, "status": None,
    }


@pytest.mark.parametrize("command", ["center", "home"])
def test_thingino_center_and_home_use_configured_homing_position(command):
    adapter = ThinginoAdapter(CameraConfig(
        id="cam", name="Camera", host="192.0.2.30", adapter="thingino", token="secret",
    ))
    adapter.session = FakeSession()

    adapter.ptz(command)

    assert adapter.session.calls == [
        ("http://192.0.2.30/x/camdash-motor.cgi", {"token": "secret", "action": "center"}, 50),
    ]


def test_thingino_ptz_rejects_missing_axis_calibration():
    adapter = ThinginoAdapter(CameraConfig(
        id="cam", name="Camera", host="192.0.2.30", adapter="thingino", token="secret",
    ))
    adapter.session = FakeSession()
    adapter.session.get = lambda url, params=None, timeout=None: FakeResponse({"steps_pan": 0, "steps_tilt": 500})

    with pytest.raises(CameraError, match="steps_pan is not configured"):
        adapter.ptz("right")


def test_thingino_motor_health_uses_authenticated_helper():
    adapter = ThinginoAdapter(CameraConfig(
        id="cam", name="Camera", host="192.0.2.30", adapter="thingino", token="secret",
    ))
    adapter.session = FakeSession()
    adapter.session.get = lambda url, params=None, timeout=None: FakeResponse(
        {"supported": True, "healthy": True, "restartable": False, "motor": {"status": "0"}}
    )

    assert adapter.motor_status()["healthy"] is True
