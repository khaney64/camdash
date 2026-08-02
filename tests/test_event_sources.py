import pytest

from camdash.event_sources import _camera_from_topic


def test_camera_id_from_normalized_topic():
    assert _camera_from_topic("camdash/cameras/camera-one/motion") == "camera-one"


@pytest.mark.parametrize("topic", ["camera/motion", "camdash/cameras/x", "camdash/camera/x/motion"])
def test_invalid_topics(topic):
    with pytest.raises(ValueError):
        _camera_from_topic(topic)
