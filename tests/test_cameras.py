from camdash.cameras import ThinginoAdapter
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
        return FakeResponse({"message": {"xpos": 510, "ypos": 250}})


def test_thingino_ptz_uses_native_motor_endpoint():
    adapter = ThinginoAdapter(CameraConfig(
        id="cam", name="Camera", host="192.0.2.30", adapter="thingino", token="secret",
    ))
    adapter.session = FakeSession()

    adapter.ptz("right")

    assert [call[0] for call in adapter.session.calls] == [
        "http://192.0.2.30/x/json-motor-params.cgi",
        "http://192.0.2.30/x/json-motor.cgi",
    ]
    assert adapter.session.calls[1][1] == {"d": "g", "x": 10.0, "y": 0.0, "token": "secret"}
